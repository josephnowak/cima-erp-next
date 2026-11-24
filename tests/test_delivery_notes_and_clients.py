import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cima_ve.services.client_consumption_service import compute_client_consumption
from cima_ve.services.delivery_note_service import (
    DEFAULT_COMMISSION_RULES,
    DEFAULT_NOTIFICATION_EMAILS,
    DEFAULT_NOTIFICATION_WHATSAPP,
    append_payment_history,
    build_delivery_note_notification_payload,
    build_overdue_delivery_notes_email_html,
    compute_delivery_note_discounts,
    compute_delivery_note_settlement_totals,
    compute_historical_bs_payment,
    compute_margin_commission,
    compute_partial_payment_state,
    filter_overdue_delivery_notes,
    is_cash_payment_method,
    is_non_commission_channel,
    parse_auto_discount_tiers,
    parse_commission_rules,
    parse_notification_emails,
    resolve_commission_rule,
    summarize_delivery_notes,
    summarize_seller_commissions,
    validate_due_date,
)


class DeliveryNotesAndClientConsumptionTests(unittest.TestCase):
    def test_creation_notifications_are_deferred_until_commit(self) -> None:
        from cima_ve.api import delivery_notes as api

        doc = SimpleNamespace(name="DN-1", docstatus=1)
        after_commit = Mock()
        frappe = SimpleNamespace(
            enqueue=Mock(), get_doc=Mock(return_value=doc), logger=Mock(),
            db=SimpleNamespace(after_commit=after_commit),
        )
        extra = {"origin": "Tienda Web", "customer_email": "customer@example.com"}
        with patch.dict(sys.modules, {"frappe": frappe}), \
             patch.object(api, "send_delivery_note_created_notifications") as send:
            api.enqueue_delivery_note_created_notifications(doc.name, extra)
            frappe.enqueue.assert_not_called()
            frappe.get_doc.assert_not_called()
            send.assert_not_called()

            after_commit.add.assert_called_once()
            enqueue = after_commit.add.call_args.args[0]
            enqueue()
            frappe.enqueue.assert_called_once_with(
                "cima_ve.api.delivery_notes._run_delivery_note_created_notifications",
                queue="default", delivery_note="DN-1", extra_info=extra,
            )
            send.assert_not_called()

            api._run_delivery_note_created_notifications(doc.name, extra)
            frappe.get_doc.assert_called_once_with("Delivery Note", doc.name)
            send.assert_called_once_with(doc, extra_info=extra)
            send.reset_mock()
            doc.docstatus = 2
            api._run_delivery_note_created_notifications(doc.name, extra)
            send.assert_not_called()

            frappe.enqueue.side_effect = ConnectionError("Queue unavailable")
            enqueue()
            frappe.logger.return_value.exception.assert_called_once()
            send.assert_not_called()

    def test_paginated_notes_keep_totals_and_load_visible_items_only(self) -> None:
        from cima_ve.api import delivery_notes as api

        rows = [
            {"name": "DN-3", "docstatus": 1, "ve_ne_estado": "Pagada", "grand_total": 40},
            {"name": "DN-2", "docstatus": 1, "ve_ne_estado": "Pago Parcial", "grand_total": 100,
             "ve_ne_monto_pagado": 40, "ve_ne_saldo_pendiente": 60, "ve_ne_comision_pct": 8,
             "ve_ne_comision_monto": 8, "ve_ne_costo_total": 50},
            {"name": "DN-1", "docstatus": 1, "grand_total": 50, "canal_venta": "Detal"},
        ]
        sql = Mock(side_effect=lambda *args, **kwargs: [
            {**row, "customer": "CUST-1", "seller_user": "sales@example.com",
             "posting_date": f"2026-01-0{row['name'][-1]}"} for row in rows
        ])
        frappe = SimpleNamespace(
            db=SimpleNamespace(sql=sql), session=SimpleNamespace(user="sales@example.com"),
            get_roles=lambda user: ["Sales User"], get_all=Mock(return_value=[]),
            get_meta=lambda doctype: SimpleNamespace(has_field=lambda field: True),
            utils=SimpleNamespace(get_fullname=lambda user: user),
        )

        def load_items(names, *, cost_only=False):
            return {name: [{"qty": 1, "item_code": "X", "incoming_rate": 10,
                            "has_discount": not cost_only}] for name in names}

        with patch.dict(sys.modules, {"frappe": frappe}), \
             patch.object(api, "_get_tax_rates", return_value=(16, 3)), \
             patch.object(api, "_get_exchange_rate_snapshot", return_value={}), \
             patch.object(api, "_get_commission_config", return_value=(True, DEFAULT_COMMISSION_RULES)), \
             patch.object(api, "_get_delivery_note_items", side_effect=load_items) as items:
            first = api.get_delivery_notes(status="Pendiente", page=1, page_size=1)
            second = api.get_delivery_notes(status="Pendiente", page=9, page_size=1)
            self.assertEqual([n["name"] for n in first["notes"]], ["DN-1"])
            self.assertEqual([n["name"] for n in second["notes"]], ["DN-2"])
            self.assertEqual(second["pagination"], {"page": 2, "page_size": 1, "total": 2, "pages": 2})
            self.assertEqual(first["summary"], second["summary"])
            self.assertEqual(second["summary"]["paid_total"], 80)
            self.assertEqual(second["summary"]["filtered_commission"], 12)
            self.assertEqual(second["summary"]["filtered_cost"], 60)
            self.assertEqual(second["clients"][0]["pending_amount"], 110)
            self.assertTrue(second["notes"][0]["has_discount"])
            self.assertEqual(items.call_args_list[-2].args, (["DN-1"],))
            self.assertEqual(items.call_args_list[-2].kwargs, {"cost_only": True})
            items.assert_called_with(["DN-2"])

            pending = api.get_client_payment_context("CUST-1", seller="ignored@example.com")
            self.assertEqual(pending["pending_amount"], 110)
            self.assertEqual([n["name"] for n in pending["notes"]], ["DN-1", "DN-2"])
            query, params = sql.call_args.args
            self.assertIn("dn.customer = %(customer)s", query)
            self.assertIn("LOWER(%(current_user)s)", query)
            self.assertEqual(params, {"customer": "CUST-1", "current_user": "sales@example.com"})

    def test_delivery_note_settlement_totals_iva_and_igtf(self) -> None:
        no_tax = compute_delivery_note_settlement_totals(100.0, apply_iva=False, apply_igtf=False)
        self.assertEqual(no_tax["iva_amount"], 0.0)
        self.assertEqual(no_tax["igtf_amount"], 0.0)
        self.assertEqual(no_tax["grand_total"], 100.0)

        iva_only = compute_delivery_note_settlement_totals(100.0, apply_iva=True, apply_igtf=False)
        self.assertEqual(iva_only["iva_amount"], 16.0)
        self.assertEqual(iva_only["grand_total"], 116.0)

        iva_and_igtf = compute_delivery_note_settlement_totals(100.0, apply_iva=True, apply_igtf=True)
        self.assertEqual(iva_and_igtf["iva_amount"], 16.0)
        self.assertEqual(iva_and_igtf["igtf_amount"], 3.48)
        self.assertEqual(iva_and_igtf["grand_total"], 119.48)

    def test_summarize_delivery_notes(self) -> None:
        notes = [
            {"name": "DN-1", "docstatus": 1, "ve_ne_estado": "Pendiente", "grand_total": 50.0},
            {"name": "DN-2", "docstatus": 1, "ve_ne_estado": "Pagada", "ve_ne_factura": "SINV-1", "grand_total": 116.0},
            {"name": "DN-3", "docstatus": 2, "ve_ne_estado": "Cancelada", "grand_total": 30.0},
        ]
        res = summarize_delivery_notes(notes)
        self.assertEqual(res["summary"]["total_count"], 3)
        self.assertEqual(res["summary"]["pending_count"], 1)
        self.assertEqual(res["summary"]["pending_total"], 50.0)
        self.assertEqual(res["summary"]["paid_count"], 1)
        self.assertEqual(res["summary"]["paid_total"], 116.0)
        self.assertEqual(res["summary"]["cancelled_count"], 1)
        self.assertEqual(res["summary"]["cancelled_total"], 30.0)

    def test_partial_payment_state_and_history(self) -> None:
        first = compute_partial_payment_state(116.0, 0.0, 50.0)
        self.assertEqual(first["estado"], "Pago Parcial")
        self.assertEqual(first["payment_amount"], 50.0)
        self.assertEqual(first["new_paid_total"], 50.0)
        self.assertEqual(first["new_remaining"], 66.0)
        self.assertFalse(first["is_fully_paid"])

        second = compute_partial_payment_state(116.0, 50.0, 66.0)
        self.assertEqual(second["estado"], "Pagada")
        self.assertEqual(second["new_paid_total"], 116.0)
        self.assertEqual(second["new_remaining"], 0.0)
        self.assertTrue(second["is_fully_paid"])

        with self.assertRaises(ValueError):
            compute_partial_payment_state(116.0, 50.0, 0.0)
        with self.assertRaises(ValueError):
            compute_partial_payment_state(116.0, 50.0, 70.0)

        hist_list, hist_json = append_payment_history("", {"amount": 50.0, "method": "Zelle"})
        hist_list2, _ = append_payment_history(hist_json, {"amount": 66.0, "method": "Contado USD"})
        self.assertEqual(len(hist_list), 1)
        self.assertEqual(len(hist_list2), 2)

    def test_summarize_delivery_notes_with_partial_payments(self) -> None:
        notes = [
            {"name": "DN-1", "docstatus": 1, "ve_ne_estado": "Pendiente", "grand_total": 50.0},
            {
                "name": "DN-2",
                "docstatus": 1,
                "ve_ne_estado": "Pago Parcial",
                "ve_ne_factura": "SINV-2",
                "ve_ne_monto_total": 100.0,
                "ve_ne_monto_pagado": 40.0,
                "ve_ne_saldo_pendiente": 60.0,
                "grand_total": 100.0,
            },
        ]
        res = summarize_delivery_notes(notes)
        self.assertEqual(res["summary"]["pending_count"], 2)
        self.assertEqual(res["summary"]["partial_count"], 1)
        self.assertEqual(res["summary"]["pending_total"], 110.0)
        self.assertEqual(res["summary"]["paid_total"], 40.0)
        self.assertEqual(res["notes"][1]["remaining_amount"], 60.0)

    def test_compute_client_consumption_with_per_seller_split(self) -> None:
        invoices = [
            {
                "name": "SINV-1",
                "customer": "CUST-1",
                "customer_name": "Cliente Alfa",
                "posting_date": "2026-11-10",
                "grand_total": 100.0,
                "paid_amount": 100.0,
                "outstanding_amount": 0.0,
                "total_qty": 2,
                "seller_user": "vendedor1@cima.ve",
                "seller_name": "Juan Pérez",
            },
            {
                "name": "SINV-2",
                "customer": "CUST-1",
                "customer_name": "Cliente Alfa",
                "posting_date": "2026-11-12",
                "grand_total": 300.0,
                "paid_amount": 300.0,
                "outstanding_amount": 0.0,
                "total_qty": 5,
                "seller_user": "vendedor2@cima.ve",
                "seller_name": "María Gómez",
            },
        ]
        dns = [
            {
                "name": "DN-10",
                "customer": "CUST-1",
                "customer_name": "Cliente Alfa",
                "posting_date": "2026-11-15",
                "grand_total": 75.0,
                "seller_user": "vendedor1@cima.ve",
                "seller_name": "Juan Pérez",
            }
        ]
        meta = {"CUST-1": {"ve_rif_cedula": "J-12345678-9"}}
        res = compute_client_consumption(invoices, delivery_notes=dns, customer_meta=meta)
        self.assertEqual(res["summary"]["total_customers"], 1)
        self.assertEqual(res["summary"]["total_consumed"], 400.0)
        self.assertEqual(res["summary"]["total_pending_dn"], 75.0)

        client = res["clients"][0]
        self.assertEqual(client["rif_cedula"], "J-12345678-9")
        self.assertEqual(len(client["by_seller"]), 2)
        self.assertEqual(client["by_seller"][0]["seller_name"], "María Gómez")
        self.assertEqual(client["by_seller"][0]["total_amount"], 300.0)
        self.assertEqual(client["by_seller"][0]["share_pct"], 75.0)
        self.assertEqual(client["by_seller"][1]["seller_name"], "Juan Pérez")
        self.assertEqual(client["by_seller"][1]["total_amount"], 100.0)
        self.assertEqual(client["by_seller"][1]["pending_dn_amount"], 75.0)

    def test_pages_and_fixtures_markers_present(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        cc_js = (root / "cima_ve" / "cima_ve" / "page" / "clientes_consumo" / "clientes_consumo.js").read_text(encoding="utf-8")
        vr_js = (root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.js").read_text(encoding="utf-8")
        ws = json.loads((root / "cima_ve" / "fixtures" / "workspace.json").read_text(encoding="utf-8"))

        self.assertIn("register_client_payment", ne_js)
        self.assertIn("paid_amount", ne_js)
        self.assertIn("Pago Parcial", ne_js)
        self.assertIn("ne-payment-screenshot", ne_js)
        self.assertIn("screenshot_url", ne_js)
        self.assertIn("revert_delivery_note", ne_js)
        self.assertIn("get_client_consumption", cc_js)
        self.assertIn("create_quick_delivery_note", vr_js)
        self.assertIn("cima-enable-discount", vr_js)
        self.assertIn("cima-discount-pct", vr_js)
        self.assertIn("EMITIR NOTA DE ENTREGA", vr_js)

        seller_links = {s["link_to"] for s in ws[0]["shortcuts"]}
        admin_links = {s["link_to"] for s in ws[1]["shortcuts"]}
        self.assertEqual(seller_links, {"venta-rapida", "notas-entrega"})
        self.assertIn("notas-entrega", admin_links)
        self.assertIn("clientes-consumo", admin_links)
        self.assertIn("metricas-ventas", admin_links)

    def test_auto_and_manual_delivery_note_discounts(self) -> None:
        tiers = parse_auto_discount_tiers(None)
        self.assertEqual(len(tiers), 3)

        # Under $100 -> 0% auto, optional 10% manual on $40 -> $36.00
        d40 = compute_delivery_note_discounts(40.0, auto_discount_enabled=True, tiers=tiers)
        self.assertEqual(d40["auto_discount_pct"], 0.0)
        self.assertEqual(d40["final_total"], 40.0)
        self.assertFalse(d40["has_global_discount"])

        d40_manual = compute_delivery_note_discounts(
            40.0,
            auto_discount_enabled=True,
            tiers=tiers,
            manual_discount_enabled=True,
            manual_discount_percentage=10.0,
        )
        self.assertEqual(d40_manual["manual_discount_pct"], 10.0)
        self.assertEqual(d40_manual["manual_discount_amount"], 4.0)
        self.assertEqual(d40_manual["final_total"], 36.0)
        self.assertTrue(d40_manual["has_global_discount"])

        # Between $500 and $1,000 -> 4.76%
        d600 = compute_delivery_note_discounts(600.0, auto_discount_enabled=True, tiers=tiers)
        self.assertEqual(d600["auto_discount_pct"], 4.76)
        self.assertEqual(d600["auto_discount_amount"], 28.56)
        self.assertEqual(d600["final_total"], 571.44)

        # Over $1,000 -> 9.09%
        d1200 = compute_delivery_note_discounts(1200.0, auto_discount_enabled=True, tiers=tiers)
        self.assertEqual(d1200["auto_discount_pct"], 9.09)
        self.assertEqual(d1200["auto_discount_amount"], 109.08)
        self.assertEqual(d1200["final_total"], 1090.92)

        # Custom configurable tier added in settings
        custom_tiers = parse_auto_discount_tiers(
            json.dumps([{"label": "Mayorista > 2000", "min_amount": 2000, "max_amount": None, "discount_percentage": 15}])
        )
        d2500 = compute_delivery_note_discounts(2500.0, auto_discount_enabled=True, tiers=custom_tiers)
        self.assertEqual(d2500["auto_discount_pct"], 15.0)
        self.assertEqual(d2500["final_total"], 2125.0)

    def test_cash_payment_method_detection(self) -> None:
        self.assertTrue(is_cash_payment_method("Efectivo USD"))
        self.assertTrue(is_cash_payment_method("Efectivo Bs"))
        self.assertTrue(is_cash_payment_method("Cash"))
        self.assertFalse(is_cash_payment_method("Pago Móvil / Bs"))
        self.assertFalse(is_cash_payment_method("Zelle"))
        self.assertFalse(is_cash_payment_method("Cashea"))

    def test_non_admin_delivery_notes_access_and_scoping(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        dn_api = (root / "cima_ve" / "api" / "delivery_notes.py").read_text(encoding="utf-8")
        ne_json = json.loads((root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.json").read_text(encoding="utf-8"))
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        vr_js = (root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.js").read_text(encoding="utf-8")

        # create_quick_delivery_note bypasses Custom DocPerm restriction on Delivery Note for Sales User
        self.assertIn("doc.flags.ignore_permissions = True", dn_api)
        self.assertIn("doc.insert(ignore_permissions=True)", dn_api)

        # get_delivery_notes scopes non-admin users to their own records only
        self.assertIn('is_admin = "System Manager" in frappe.get_roles(current_user)', dn_api)
        self.assertIn("if not is_admin:", dn_api)
        self.assertIn('params["current_user"] = current_user', dn_api)

        # notas-entrega page allows Sales User and Sales Manager
        roles = {r["role"] for r in ne_json.get("roles", [])}
        self.assertTrue({"Sales User", "Sales Manager", "System Manager"}.issubset(roles))

        # notas_entrega.js hides admin-only controls for non-admins while allowing own-note payment
        self.assertIn("applyRoleUi()", ne_js)
        self.assertIn("Mis Notas de Entrega", ne_js)
        self.assertIn("Al día", ne_js)

        # venta_rapida.js exposes Notas de Entrega button to all sellers, keeping metrics/clients admin-only
        self.assertIn('data-action="open-delivery-notes">\n\t\t\t\t\t\t\tNotas de Entrega', vr_js)
        self.assertIn('data-action="open-client-consumption" data-admin-only="1"', vr_js)
        self.assertIn('data-action="open-metrics" data-admin-only="1"', vr_js)

    def test_delivery_note_notifications_and_storefront_integration(self) -> None:
        self.assertEqual(
            parse_notification_emails(None),
            ["cimaelectronic@gmail.com", "alexandermigueld@gmail.com"],
        )
        self.assertEqual(
            parse_notification_emails("cimaelectronic@gmail.com, alexandermigueld@gmail.com; cimaelectronic@gmail.com"),
            ["cimaelectronic@gmail.com", "alexandermigueld@gmail.com"],
        )

        notif = build_delivery_note_notification_payload(
            delivery_note="DN-00099",
            customer_name="María Pérez",
            origin="Tienda Web",
            items=[{"item_name": "Cable HDMI", "qty": 2, "rate": 10.0, "amount": 20.0}],
            grand_total=25.0,
            bs_rate=50.0,
            delivery_provider="MRW",
            delivery_cost=5.0,
            payment_method="Pago Móvil",
            payment_reference="998877",
            screenshot_url="/files/cap.png",
        )
        self.assertIn("DN-00099", notif["subject"])
        self.assertIn("$25.00", notif["subject"])
        self.assertIn("Bs. 1,250.00", notif["text"])
        self.assertEqual(notif["email_recipients"], DEFAULT_NOTIFICATION_EMAILS)
        self.assertEqual(notif["whatsapp_number"], DEFAULT_NOTIFICATION_WHATSAPP)
        self.assertIn("https://wa.me/584243472198?text=", notif["whatsapp_url"])

        root = pathlib.Path(__file__).resolve().parent.parent
        sf_py = (root / "cima_ve" / "api" / "storefront.py").read_text(encoding="utf-8")
        tienda_js = (root / "cima_ve" / "public" / "js" / "tienda.js").read_text(encoding="utf-8")
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")

        self.assertIn('"doctype": "Delivery Note"', sf_py)
        self.assertIn("upload_storefront_payment_proof", sf_py)
        self.assertIn("enqueue_delivery_note_created_notifications(", sf_py)
        self.assertIn("pay-screenshot", tienda_js)
        self.assertIn("upload_storefront_payment_proof", tienda_js)
        self.assertIn("ve_ne_comprobante_url", ne_js) if "ve_ne_comprobante_url" in ne_js else self.assertIn("screenshot_url", ne_js)
        self.assertIn("Tienda Web", ne_js)

    def test_margin_commissions_and_seller_summary(self) -> None:
        # Default configurable rules: Meta Ads 7%, Detal 8%, Mayor 10%
        rules = parse_commission_rules(None)
        self.assertEqual(len(rules), 3)
        self.assertEqual(resolve_commission_rule("Meta Ads", rules)["percentage"], 7.0)
        self.assertEqual(resolve_commission_rule("Detal 8%", rules)["percentage"], 8.0)
        mayor = resolve_commission_rule("Mayor", rules)
        self.assertEqual(mayor["percentage"], 10.0)

        # Custom configurable rule added in settings
        custom = parse_commission_rules(
            json.dumps([
                {"label": "Mayor 10%", "channel": "Mayor", "percentage": 10},
                {"label": "VIP 12%", "channel": "Detal", "percentage": 12},
            ])
        )
        self.assertEqual(resolve_commission_rule("VIP 12%", custom)["percentage"], 12.0)

        # Commission is calculated on selling price (net_sale_amount), not margin:
        # sale $60, cost $50, Mayor 10% -> margin $10 (informational), seller commission $6.00
        calc = compute_margin_commission(net_sale_amount=60.0, total_cost=50.0, commission_pct=10.0)
        self.assertEqual(calc["margin_amount"], 10.0)
        self.assertEqual(calc["commission_amount"], 6.0)

        # Detal 8% on $25.00 sale (cost $9.31, margin $15.69) -> $2.00 commission
        detal_calc = compute_margin_commission(net_sale_amount=25.0, total_cost=9.31, commission_pct=8.0)
        self.assertEqual(detal_calc["margin_amount"], 15.69)
        self.assertEqual(detal_calc["commission_amount"], 2.0)

        # Even if margin is negative, commission still applies on net_sale_amount
        loss_calc = compute_margin_commission(net_sale_amount=45.0, total_cost=50.0, commission_pct=10.0)
        self.assertEqual(loss_calc["margin_amount"], -5.0)
        self.assertEqual(loss_calc["commission_amount"], 4.5)

        notes = [
            {
                "name": "DN-1",
                "docstatus": 1,
                "ve_ne_estado": "Pagada",
                "grand_total": 60.0,
                "net_total": 60.0,
                "seller_user": "isabella@cima.ve",
                "seller_name": "Isabella",
                "ve_ne_comision_opcion": "Mayor 10%",
                "ve_ne_comision_pct": 10.0,
                "ve_ne_costo_total": 50.0,
                "ve_ne_margen_ganancia": 10.0,
                "ve_ne_comision_monto": 1.0,
            },
            {
                "name": "DN-2",
                "docstatus": 1,
                "ve_ne_estado": "Pendiente",
                "grand_total": 100.0,
                "net_total": 100.0,
                "seller_user": "isabella@cima.ve",
                "seller_name": "Isabella",
                "ve_ne_comision_opcion": "Detal 8%",
                "ve_ne_comision_pct": 8.0,
                "ve_ne_costo_total": 75.0,
                "ve_ne_margen_ganancia": 25.0,
                "ve_ne_comision_monto": 2.0,
            },
            {
                "name": "DN-3",
                "docstatus": 2,
                "ve_ne_estado": "Cancelada",
                "grand_total": 50.0,
                "net_total": 50.0,
                "seller_user": "isabella@cima.ve",
                "seller_name": "Isabella",
                "ve_ne_comision_opcion": "Meta Ads 7%",
                "ve_ne_comision_pct": 7.0,
                "ve_ne_costo_total": 30.0,
                "ve_ne_margen_ganancia": 20.0,
                "ve_ne_comision_monto": 1.4,
            },
        ]
        summarized = summarize_delivery_notes(notes)
        self.assertEqual(summarized["summary"]["total_commission"], 3.0)
        self.assertEqual(summarized["summary"]["paid_commission"], 1.0)
        self.assertEqual(summarized["summary"]["pending_commission"], 2.0)

        by_seller = summarize_seller_commissions(summarized["notes"])
        self.assertEqual(len(by_seller), 1)
        self.assertEqual(by_seller[0]["notes_count"], 2)
        self.assertEqual(by_seller[0]["commission_total"], 3.0)
        self.assertEqual(by_seller[0]["paid_commission"], 1.0)
        self.assertEqual(by_seller[0]["pending_commission"], 2.0)

        root = pathlib.Path(__file__).resolve().parent.parent
        settings_json = (root / "cima_ve" / "cima_ve" / "doctype" / "cima_ve_settings" / "cima_ve_settings.json").read_text(encoding="utf-8")
        cf_json = (root / "cima_ve" / "fixtures" / "custom_field.json").read_text(encoding="utf-8")
        vr_js = (root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.js").read_text(encoding="utf-8")
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")

        self.assertIn("commission_rules", settings_json)
        self.assertIn("ve_ne_comision_monto", cf_json)
        self.assertIn("commission_option", vr_js)
        self.assertIn("calculate_seller_commissions", ne_js)
        self.assertIn("Calcular Comisiones", ne_js)

    def test_historical_bs_payment_and_pos_bs_display(self) -> None:
        # First partial payment at rate 1000 Bs/USD
        step1 = compute_historical_bs_payment(
            total_to_pay_usd=100.0,
            paid_now_usd=40.0,
            current_bs_rate=1000.0,
            existing_history=[],
            is_fully_paid=False,
        )
        self.assertEqual(step1["exchange_rate"], 1000.0)
        self.assertEqual(step1["paid_now_bs"], 40000.0)
        self.assertEqual(step1["paid_total_bs"], 40000.0)
        self.assertEqual(step1["total_bs"], 0.0)

        # Second payment completing the order at rate 1100 Bs/USD -> freezes total_bs
        step2 = compute_historical_bs_payment(
            total_to_pay_usd=100.0,
            paid_now_usd=60.0,
            current_bs_rate=1100.0,
            existing_history=[{"amount": 40.0, "amount_bs": 40000.0, "exchange_rate": 1000.0}],
            is_fully_paid=True,
        )
        self.assertEqual(step2["paid_now_bs"], 66000.0)
        self.assertEqual(step2["paid_total_bs"], 106000.0)
        self.assertEqual(step2["total_bs"], 106000.0)

        # summarize_delivery_notes preserves frozen historical Bs on Pagada notes
        summarized = summarize_delivery_notes(
            [
                {
                    "name": "DN-PAID",
                    "docstatus": 1,
                    "ve_ne_estado": "Pagada",
                    "grand_total": 100.0,
                    "ve_ne_monto_total": 100.0,
                    "ve_ne_monto_pagado": 100.0,
                    "ve_ne_tasa_cambio": 1069.55,
                    "ve_ne_monto_bs": 106955.0,
                    "ve_ne_monto_pagado_bs": 106955.0,
                }
            ]
        )
        note = summarized["notes"][0]
        self.assertTrue(note["is_bs_frozen"])
        self.assertEqual(note["total_bs"], 106955.0)
        self.assertEqual(note["paid_bs"], 106955.0)
        self.assertEqual(note["exchange_rate_used"], 1069.55)

        root = pathlib.Path(__file__).resolve().parent.parent
        cf_json = (root / "cima_ve" / "fixtures" / "custom_field.json").read_text(encoding="utf-8")
        vr_js = (root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.js").read_text(encoding="utf-8")
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        sp_py = (root / "cima_ve" / "api" / "seller_pos.py").read_text(encoding="utf-8")

        for fname in ("ve_ne_tasa_cambio", "ve_ne_monto_bs", "ve_ne_monto_pagado_bs"):
            self.assertIn(fname, cf_json)
        self.assertIn('"exchange_rate": exchange_rate', sp_py)
        self.assertIn("TOTAL (Bs.)", vr_js)
        self.assertIn("getBsRate", vr_js)
        self.assertIn("(hist.)", ne_js)
        self.assertIn("Referencia histórica", ne_js)

    def test_non_admin_can_open_notas_entrega_from_venta_rapida(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        vr_js = (root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.js").read_text(encoding="utf-8")
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        self.assertIn('root.on("click", "[data-action=\'open-delivery-notes\']", () => {\n\t\t\tfrappe.set_route("notas-entrega");', vr_js)
        self.assertIn("Ver en Mis Notas de Entrega", vr_js)
        self.assertIn("upload_delivery_note_payment_proof", ne_js)
        self.assertIn("const canRevert = isAdmin &&", ne_js)
        self.assertIn("data-revert-dn", ne_js)
        self.assertIn("revert_delivery_note", ne_js)
        self.assertIn("Registrar Pago Cliente", ne_js)
        self.assertIn("Pagos / Factura", ne_js)
        self.assertIn("<strong>Cancelada</strong>", ne_js)
        self.assertNotIn("data-cancel-dn", ne_js)

    def test_cash_payment_allows_screenshot_proof(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        dn_py = (root / "cima_ve" / "api" / "delivery_notes.py").read_text(encoding="utf-8")

        self.assertIn('d.$wrapper.find("#ne-screenshot-group").show();', ne_js)
        self.assertNotIn('d.$wrapper.find("#ne-screenshot-group").toggle(!cash);', ne_js)
        self.assertIn('clean_screenshot = (screenshot_url or "").strip()', dn_py)
        self.assertNotIn('clean_screenshot = "" if is_cash else', dn_py)

    def test_tienda_web_and_mercadolibre_excluded_from_commissions(self) -> None:
        self.assertTrue(is_non_commission_channel(origin="Tienda Web"))
        self.assertTrue(is_non_commission_channel(origin="MercadoLibre"))
        self.assertTrue(is_non_commission_channel(sales_channel="MercadoLibre"))
        self.assertTrue(is_non_commission_channel(commission_option="MercadoLibre (Sin comisión)"))
        self.assertTrue(is_non_commission_channel(sales_channel="Mercado Libre"))
        self.assertFalse(is_non_commission_channel(origin="Facturación Rápida", sales_channel="Detal"))

        notes = [
            {
                "name": "DN-DETAL",
                "docstatus": 1,
                "ve_ne_estado": "Pagada",
                "ve_ne_origen": "Facturación Rápida",
                "canal_venta": "Detal",
                "ve_vendedor_usuario": "ana@cima.com",
                "ve_vendedor_nombre": "Ana",
                "grand_total": 100.0,
                "net_total": 100.0,
                "ve_ne_comision_opcion": "Detal 8%",
                "ve_ne_comision_pct": 8.0,
                "ve_ne_costo_total": 60.0,
                "ve_ne_margen_ganancia": 40.0,
                "ve_ne_comision_monto": 3.2,
            },
            {
                "name": "DN-WEB",
                "docstatus": 1,
                "ve_ne_estado": "Pagada",
                "ve_ne_origen": "Tienda Web",
                "canal_venta": "Detal",
                "ve_vendedor_usuario": "ana@cima.com",
                "ve_vendedor_nombre": "Ana",
                "grand_total": 200.0,
                "net_total": 200.0,
                "ve_ne_comision_pct": 8.0,
                "ve_ne_costo_total": 100.0,
                "ve_ne_margen_ganancia": 100.0,
                "ve_ne_comision_monto": 8.0,
            },
            {
                "name": "DN-ML",
                "docstatus": 1,
                "ve_ne_estado": "Pendiente",
                "ve_ne_origen": "MercadoLibre",
                "canal_venta": "MercadoLibre",
                "ve_vendedor_usuario": "ana@cima.com",
                "ve_vendedor_nombre": "Ana",
                "grand_total": 150.0,
                "net_total": 150.0,
                "ve_ne_comision_pct": 10.0,
                "ve_ne_costo_total": 90.0,
                "ve_ne_margen_ganancia": 60.0,
                "ve_ne_comision_monto": 6.0,
            },
        ]
        summarized = summarize_delivery_notes(notes)
        self.assertEqual(summarized["summary"]["total_commission"], 3.2)
        self.assertEqual(summarized["summary"]["paid_commission"], 3.2)
        self.assertEqual(summarized["summary"]["pending_commission"], 0.0)
        self.assertFalse(summarized["notes"][0]["commission_excluded"])
        self.assertTrue(summarized["notes"][1]["commission_excluded"])
        self.assertEqual(summarized["notes"][1]["commission_amount"], 0.0)
        self.assertTrue(summarized["notes"][2]["commission_excluded"])
        self.assertEqual(summarized["notes"][2]["commission_amount"], 0.0)

        by_seller = summarize_seller_commissions(summarized["notes"])
        self.assertEqual(len(by_seller), 1)
        self.assertEqual(by_seller[0]["notes_count"], 1)
        self.assertEqual(by_seller[0]["sales_total"], 100.0)
        self.assertEqual(by_seller[0]["commission_total"], 3.2)

        root = pathlib.Path(__file__).resolve().parent.parent
        dn_py = (root / "cima_ve" / "api" / "delivery_notes.py").read_text(encoding="utf-8")
        vr_js = (root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.js").read_text(encoding="utf-8")
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        cf_json = (root / "cima_ve" / "fixtures" / "custom_field.json").read_text(encoding="utf-8")
        self.assertIn("is_non_commission_channel", dn_py)
        self.assertIn("MercadoLibre (Sin comisión)", vr_js)
        self.assertIn("MercadoLibre", ne_js)
        self.assertIn("MercadoLibre", cf_json)

    def test_due_date_validation_overdue_filtering_and_monday_email(self) -> None:
        self.assertEqual(validate_due_date("2026-11-30"), "2026-11-30")
        with self.assertRaises(ValueError):
            validate_due_date("")
        with self.assertRaises(ValueError):
            validate_due_date("   ")
        with self.assertRaises(ValueError):
            validate_due_date("2026-99-99")

        notes = [
            {
                "name": "DN-OVER-1",
                "docstatus": 1,
                "posting_date": "2026-11-01",
                "customer_name": "Cliente Moroso",
                "seller_name": "Carlos",
                "ve_ne_estado": "Pendiente",
                "ve_ne_fecha_vencimiento": "2026-11-10",
                "grand_total": 120.0,
            },
            {
                "name": "DN-OVER-PARTIAL",
                "docstatus": 1,
                "posting_date": "2026-11-05",
                "customer_name": "Cliente Abono",
                "seller_name": "Ana",
                "ve_ne_estado": "Pago Parcial",
                "ve_ne_fecha_vencimiento": "2026-11-18",
                "ve_ne_factura": "SINV-10",
                "ve_ne_monto_total": 200.0,
                "ve_ne_monto_pagado": 50.0,
                "ve_ne_saldo_pendiente": 150.0,
                "grand_total": 200.0,
            },
            {
                "name": "DN-DUE-TODAY",
                "docstatus": 1,
                "posting_date": "2026-11-15",
                "customer_name": "Cliente Hoy",
                "ve_ne_estado": "Pendiente",
                "ve_ne_fecha_vencimiento": "2026-11-23",
                "grand_total": 80.0,
            },
            {
                "name": "DN-PAID",
                "docstatus": 1,
                "posting_date": "2026-11-01",
                "customer_name": "Cliente Pagado",
                "ve_ne_estado": "Pagada",
                "ve_ne_fecha_vencimiento": "2026-11-10",
                "ve_ne_factura": "SINV-11",
                "ve_ne_monto_total": 90.0,
                "ve_ne_monto_pagado": 90.0,
                "grand_total": 90.0,
            },
            {
                "name": "DN-CANCELLED",
                "docstatus": 2,
                "posting_date": "2026-11-01",
                "customer_name": "Cliente Anulado",
                "ve_ne_estado": "Cancelada",
                "ve_ne_fecha_vencimiento": "2026-11-10",
                "grand_total": 60.0,
            },
        ]

        ref_monday = "2026-11-23"
        summarized = summarize_delivery_notes(notes, reference_date=ref_monday)
        self.assertEqual(summarized["summary"]["overdue_count"], 2)
        self.assertEqual(summarized["summary"]["overdue_total"], 270.0)

        overdue = filter_overdue_delivery_notes(notes, reference_date=ref_monday)
        self.assertEqual([n["name"] for n in overdue], ["DN-OVER-1", "DN-OVER-PARTIAL"])
        self.assertEqual(overdue[0]["days_overdue"], 13)
        self.assertEqual(overdue[1]["days_overdue"], 5)
        self.assertEqual(overdue[1]["remaining_amount"], 150.0)

        email_html = build_overdue_delivery_notes_email_html(overdue, reference_date=ref_monday)
        self.assertIn("DN-OVER-1", email_html)
        self.assertIn("DN-OVER-PARTIAL", email_html)
        self.assertIn("Cliente Moroso", email_html)
        self.assertIn("$270.00", email_html)
        self.assertIn("13 días", email_html)
        self.assertNotIn("DN-PAID", email_html)
        self.assertNotIn("DN-DUE-TODAY", email_html)
        self.assertNotIn("DN-CANCELLED", email_html)

    def test_due_date_and_monday_cron_wiring(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        cf_list = json.loads((root / "cima_ve" / "fixtures" / "custom_field.json").read_text(encoding="utf-8"))
        due_cf = next((f for f in cf_list if f.get("name") == "Delivery Note-ve_ne_fecha_vencimiento"), None)
        self.assertIsNotNone(due_cf)
        self.assertEqual(due_cf["fieldtype"], "Date")
        self.assertEqual(due_cf["reqd"], 1)

        hooks_py = (root / "cima_ve" / "hooks.py").read_text(encoding="utf-8")
        self.assertIn("0 8 * * 1", hooks_py)
        self.assertIn("cima_ve.tasks.send_monday_overdue_delivery_notes_alert", hooks_py)

        tasks_py = (root / "cima_ve" / "tasks.py").read_text(encoding="utf-8")
        self.assertIn("def send_monday_overdue_delivery_notes_alert", tasks_py)

        vr_js = (root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.js").read_text(encoding="utf-8")
        self.assertIn("cima-due-date", vr_js)
        self.assertIn("fecha_vencimiento", vr_js)

        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        self.assertIn("ve_ne_fecha_vencimiento", ne_js)
        self.assertIn("VENCIDA", ne_js)

        sf_py = (root / "cima_ve" / "api" / "storefront.py").read_text(encoding="utf-8")
        self.assertIn("ve_ne_fecha_vencimiento", sf_py)


class DeliveryNotePermissionTests(unittest.TestCase):
    def test_hooks_and_module_restrict_delivery_notes(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        hooks = (root / "cima_ve" / "hooks.py").read_text(encoding="utf-8")
        self.assertIn("permission_query_conditions", hooks)
        self.assertIn("has_permission", hooks)
        from cima_ve.overrides import delivery_note_permissions as p

        self.assertTrue(p._is_admin("x@y.com", ["System Manager"]))
        self.assertFalse(p._is_admin("x@y.com", ["Sales User"]))

        class Doc:
            owner = "a@x.com"
            ve_vendedor_usuario = "b@x.com"

        class FakeFrappe:
            class session:
                user = "a@x.com"

            @staticmethod
            def get_roles(user):
                if user == "admin@x.com":
                    return ["System Manager"]
                return ["Sales User"]

        import sys

        sys.modules["frappe"] = FakeFrappe
        try:
            self.assertFalse(p.has_permission(Doc(), user="a@x.com"))
            self.assertIs(p.has_permission(Doc(), user="b@x.com"), True)
            self.assertIs(p.has_permission(Doc(), user="B@X.COM"), True)
            self.assertIs(p.has_permission(Doc(), user="admin@x.com"), True)
        finally:
            del sys.modules["frappe"]

    def test_seller_case_insensitive_settle_and_proof_upload(self) -> None:
        import sys
        from cima_ve.api import delivery_notes as dn_api

        class DocMixedCaseOwner:
            owner = "Marcanocamila2009@gmail.com"
            ve_vendedor_usuario = ""

        class DocDifferentSeller:
            owner = "Marcanocamila2009@gmail.com"
            ve_vendedor_usuario = "other@x.com"

        class FakeFrappe:
            @staticmethod
            def get_roles(user):
                return ["Sales User"]

        sys.modules["frappe"] = FakeFrappe
        try:
            self.assertTrue(dn_api._can_user_settle_delivery_note(DocMixedCaseOwner(), "marcanocamila2009@gmail.com"))
            self.assertFalse(dn_api._can_user_settle_delivery_note(DocDifferentSeller(), "marcanocamila2009@gmail.com"))
        finally:
            del sys.modules["frappe"]

        root = pathlib.Path(__file__).resolve().parent.parent
        dn_py = (root / "cima_ve" / "api" / "delivery_notes.py").read_text(encoding="utf-8")
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        self.assertIn("LOWER(COALESCE(NULLIF(dn.ve_vendedor_usuario, ''), dn.owner)) = LOWER(%(current_user)s)", dn_py)
        self.assertIn('frappe.set_user("Administrator")', dn_py)
        self.assertNotIn("openUploadProofDialog", ne_js)
        self.assertNotIn("data-proof-dn", ne_js)
        self.assertIn("openClientPaymentDialog", ne_js)

    def test_client_grouping_and_fifo_payment_allocation(self) -> None:
        from cima_ve.services.delivery_note_service import (
            allocate_client_payment_fifo,
            summarize_client_delivery_notes,
        )

        notes = [
            {
                "name": "DN-NEW",
                "docstatus": 1,
                "posting_date": "2026-11-10",
                "creation": "2026-11-10 10:00:00",
                "customer": "CUST-1",
                "customer_name": "Cliente Uno",
                "ve_ne_cliente_email": "uno@example.com",
                "ve_ne_estado": "Pendiente",
                "ve_ne_fecha_vencimiento": "2026-11-20",
                "grand_total": 100.0,
            },
            {
                "name": "DN-OLD",
                "docstatus": 1,
                "posting_date": "2026-11-01",
                "creation": "2026-11-01 09:00:00",
                "customer": "CUST-1",
                "customer_name": "Cliente Uno",
                "ve_ne_estado": "Pendiente",
                "ve_ne_fecha_vencimiento": "2026-11-05",
                "grand_total": 60.0,
            },
            {
                "name": "DN-MID",
                "docstatus": 1,
                "posting_date": "2026-11-05",
                "creation": "2026-11-05 09:00:00",
                "customer": "CUST-1",
                "customer_name": "Cliente Uno",
                "ve_ne_estado": "Pendiente",
                "ve_ne_fecha_vencimiento": "2026-11-15",
                "grand_total": 80.0,
            },
        ]
        summarized = summarize_delivery_notes(notes, reference_date="2026-11-12")
        clients = summarize_client_delivery_notes(summarized["notes"], reference_date="2026-11-12")
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["pending_amount"], 240.0)
        self.assertEqual([n["name"] for n in clients[0]["notes"]], ["DN-OLD", "DN-MID", "DN-NEW"])

        # Client pays $100 -> pays DN-OLD ($60) in full + $40 partial on DN-MID
        plan = allocate_client_payment_fifo(clients[0]["notes"], 100.0)
        self.assertEqual(plan["total_pending_before"], 240.0)
        self.assertEqual(plan["total_pending_after"], 140.0)
        self.assertEqual(len(plan["allocations"]), 2)
        self.assertEqual(plan["allocations"][0]["delivery_note"], "DN-OLD")
        self.assertEqual(plan["allocations"][0]["new_estado"], "Pagada")
        self.assertEqual(plan["allocations"][1]["delivery_note"], "DN-MID")
        self.assertEqual(plan["allocations"][1]["allocated_amount"], 40.0)
        self.assertEqual(plan["allocations"][1]["new_estado"], "Pago Parcial")

        with self.assertRaises(ValueError):
            allocate_client_payment_fifo(clients[0]["notes"], 300.0)

    def test_commission_currency_split_usd_and_bs(self) -> None:
        from cima_ve.services.delivery_note_service import (
            compute_note_commission_currency_split,
            get_payment_method_currency,
        )

        self.assertEqual(get_payment_method_currency("Pago Móvil / Bs"), "Bs")
        self.assertEqual(get_payment_method_currency("Efectivo Bs"), "Bs")
        self.assertEqual(get_payment_method_currency("Zelle"), "USD")
        self.assertEqual(get_payment_method_currency("Efectivo USD"), "USD")

        # Seller wins $500 commission on a note paid 60% in USD and 40% in Pago Móvil (Bs @ 50 Bs/USD)
        note = {
            "name": "DN-COMM",
            "docstatus": 1,
            "ve_ne_estado": "Pagada",
            "grand_total": 1000.0,
            "net_total": 1000.0,
            "ve_ne_monto_total": 1000.0,
            "ve_ne_monto_pagado": 1000.0,
            "seller_user": "vendedor@cima.ve",
            "seller_name": "Carlos",
            "ve_ne_comision_opcion": "Mayor 10%",
            "ve_ne_comision_pct": 10.0,
            "ve_ne_comision_monto": 500.0,
            "ve_ne_historial_pagos": json.dumps([
                {"amount": 600.0, "method": "Zelle", "exchange_rate": 50.0},
                {"amount": 400.0, "method": "Pago Móvil / Bs", "exchange_rate": 50.0},
            ]),
        }
        summarized = summarize_delivery_notes([note])
        split = compute_note_commission_currency_split(summarized["notes"][0], fallback_bs_rate=50.0)
        self.assertEqual(split["commission_usd"], 300.0)
        self.assertEqual(split["commission_bs_usd_equiv"], 200.0)
        self.assertEqual(split["commission_bs"], 10000.0)

        by_seller = summarize_seller_commissions(summarized["notes"], current_bs_rate=50.0)
        self.assertEqual(by_seller[0]["commission_usd"], 300.0)
        self.assertEqual(by_seller[0]["commission_bs_usd_equiv"], 200.0)
        self.assertEqual(by_seller[0]["commission_bs"], 10000.0)

    def test_client_email_and_printable_document_generation(self) -> None:
        from cima_ve.services.delivery_note_service import (
            build_client_delivery_note_email_html,
            build_delivery_note_document_html,
        )

        mail = build_client_delivery_note_email_html(
            delivery_note="DN-100",
            customer_name="Ana Gómez",
            items=[{"item_name": "Router", "qty": 1, "rate": 45.0, "amount": 45.0}],
            grand_total=45.0,
            bs_rate=50.0,
            due_date="2026-12-01",
        )
        self.assertIn("DN-100", mail["subject"])
        self.assertIn("Ana Gómez", mail["html"])
        self.assertIn("2026-12-01", mail["html"])

        doc_html = build_delivery_note_document_html(
            customer_info={"customer_name": "Ana Gómez", "rif_cedula": "V-12345678", "email": "ana@example.com"},
            notes=[
                {
                    "name": "DN-100",
                    "posting_date": "2026-11-20",
                    "ve_ne_fecha_vencimiento": "2026-12-01",
                    "ve_ne_estado": "Pendiente",
                    "total_to_pay": 45.0,
                    "paid_amount": 0.0,
                    "remaining_amount": 45.0,
                    "items": [{"item_code": "RTR-1", "item_name": "Router", "qty": 1, "rate": 45.0, "amount": 45.0}],
                }
            ],
            bs_rate=50.0,
        )
        self.assertIn("cima-logo-color.png", doc_html)
        self.assertIn("Ana Gómez", doc_html)
        self.assertIn("V-12345678", doc_html)
        self.assertIn("DN-100", doc_html)
        self.assertIn("$45.00", doc_html)

    def test_mayorista_and_contact_storefront_wiring(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        cf = (root / "cima_ve" / "fixtures" / "custom_field.json").read_text(encoding="utf-8")
        sf_py = (root / "cima_ve" / "api" / "storefront.py").read_text(encoding="utf-8")
        tienda_html = (root / "cima_ve" / "www" / "tienda.html").read_text(encoding="utf-8")
        tienda_js = (root / "cima_ve" / "public" / "js" / "tienda.js").read_text(encoding="utf-8")

        self.assertIn("Customer-ve_es_mayorista", cf)
        self.assertIn("User-ve_es_mayorista", cf)
        self.assertIn("price_mode", sf_py)
        self.assertIn("Solo los clientes registrados como Mayorista", sf_py)
        self.assertNotIn("Envíos desde", tienda_html)
        self.assertIn("Contáctanos", tienda_html)
        self.assertIn("filter-mayorista", tienda_html)
        self.assertIn("openContactModal", tienda_js)
        self.assertIn("price_mode", tienda_js)

    def test_client_and_cima_pdf_notifications_and_mobile_pdf_wiring(self) -> None:
        import sys
        import types
        from unittest.mock import patch
        from cima_ve.services.delivery_note_service import (
            build_client_delivery_note_email_html,
            build_delivery_note_document_html,
            build_delivery_note_notification_payload,
        )

        # 1. Pure-Python client message & WhatsApp payload with date and Spanish thank-you
        client_msg = build_client_delivery_note_email_html(
            delivery_note="DN-200",
            customer_name="Luis Rojas",
            items=[{"item_name": "Inversor 2KW", "qty": 1, "rate": 320.0, "amount": 320.0}],
            grand_total=320.0,
            bs_rate=55.0,
            due_date="2026-12-05",
            seller_name="María",
            posting_date="2026-11-24",
            customer_phone="0414-5551234",
        )
        self.assertIn("Gracias por tu compra", client_msg["subject"])
        self.assertIn("2026-11-24", client_msg["html"])
        self.assertIn("PDF", client_msg["html"])
        self.assertIn("Gracias por comprar con *CIMA Electronic VE*", client_msg["whatsapp_text"])
        self.assertIn("2026-11-24", client_msg["whatsapp_text"])
        self.assertIn("https://wa.me/584145551234", client_msg["whatsapp_url"])

        # 2. CIMA tracking payload includes posting_date
        cima_payload = build_delivery_note_notification_payload(
            delivery_note="DN-200",
            customer_name="Luis Rojas",
            grand_total=320.0,
            posting_date="2026-11-24",
        )
        self.assertIn("2026-11-24", cima_payload["text"])
        self.assertIn("2026-11-24", cima_payload["html"])

        # 3. Mobile-friendly document HTML includes viewport, table-wrap, and Descargar PDF button
        doc_html = build_delivery_note_document_html(
            customer_info={"customer_name": "Luis Rojas"},
            notes=[{"name": "DN-200", "posting_date": "2026-11-24", "items": []}],
            pdf_download_url="/api/method/cima_ve.api.delivery_notes.download_delivery_note_pdf?delivery_note=DN-200",
        )
        self.assertIn('name="viewport"', doc_html)
        self.assertIn("table-wrap", doc_html)
        self.assertIn("@media (max-width: 768px)", doc_html)
        self.assertIn("Descargar PDF", doc_html)

        # 4. send_delivery_note_created_notifications sends 1 email + 1 WA to CIMA and 1 email + 1 WA to client, all with PDF
        sent_emails = []
        sent_wa = []

        class _FakeMeta:
            def has_field(self, _name):
                return True

        class _FakeSettings:
            meta = _FakeMeta()

            def get(self, key):
                return {
                    "notify_order_email": "cima@example.com",
                    "notify_whatsapp_number": "+584243472198",
                    "whatsapp_webhook_url": "https://wa.example.com/send",
                }.get(key, "")

            def get_password(self, _key):
                return "secret-tok"

        fake_frappe = types.SimpleNamespace(
            get_single=lambda _dt: _FakeSettings(),
            sendmail=lambda **kwargs: sent_emails.append(kwargs),
            log_error=lambda *a, **kw: None,
            utils=types.SimpleNamespace(today=lambda: "2026-11-24"),
            db=types.SimpleNamespace(get_value=lambda *a, **kw: ""),
            get_all=lambda *a, **kw: [],
        )
        doc = types.SimpleNamespace(
            name="DN-200",
            customer="CUST-1",
            customer_name="Luis Rojas",
            posting_date="2026-11-24",
            ve_ne_fecha_vencimiento="2026-12-05",
            ve_vendedor_nombre="María",
            ve_ne_origen="Facturación Rápida",
            ve_ne_cliente_email="luis@example.com",
            ve_ne_cliente_telefono="+584145551234",
            grand_total=320.0,
            discount_amount=0.0,
            items=[types.SimpleNamespace(item_code="INV-2K", item_name="Inversor 2KW", qty=1, rate=320.0, amount=320.0)],
        )

        from cima_ve.api import delivery_notes as dn_api

        with patch.dict(sys.modules, {"frappe": fake_frappe}), \
             patch.object(dn_api, "_get_exchange_rate_snapshot", return_value={"bcv_rate": 55.0, "adjusted_rate": 55.0}), \
             patch.object(dn_api, "_generate_delivery_note_pdf_bytes_for_doc", return_value=b"%PDF-1.4 fake"), \
             patch.object(dn_api, "_post_whatsapp_webhook", side_effect=lambda _u, _t, p: sent_wa.append(p) or True):
            res = dn_api.send_delivery_note_created_notifications(doc)

        self.assertTrue(res["email_sent"])
        self.assertTrue(res["client_email_sent"])
        self.assertTrue(res["whatsapp_sent"])
        self.assertTrue(res["client_whatsapp_sent"])
        self.assertTrue(res["pdf_attached"])
        self.assertEqual(len(sent_emails), 2)
        self.assertEqual(sent_emails[0]["recipients"], ["cima@example.com"])
        self.assertEqual(sent_emails[1]["recipients"], ["luis@example.com"])
        self.assertEqual(sent_emails[0]["attachments"][0]["fname"], "Nota_de_Entrega_DN-200.pdf")
        self.assertEqual(sent_emails[1]["attachments"][0]["fname"], "Nota_de_Entrega_DN-200.pdf")

        self.assertEqual(len(sent_wa), 2)
        self.assertEqual(sent_wa[0]["recipient_type"], "company")
        self.assertEqual(sent_wa[0]["to"], "+584243472198")
        self.assertIn("pdf_base64", sent_wa[0])
        self.assertEqual(sent_wa[1]["recipient_type"], "client")
        self.assertEqual(sent_wa[1]["to"], "+584145551234")
        self.assertIn("pdf_base64", sent_wa[1])
        self.assertIn("2026-11-24", sent_wa[1]["message"])

        # 5. Frontend mobile PDF wiring check
        root = pathlib.Path(__file__).resolve().parent.parent
        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        vr_js = (root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.js").read_text(encoding="utf-8")
        dn_py = (root / "cima_ve" / "api" / "delivery_notes.py").read_text(encoding="utf-8")

        self.assertIn("download_delivery_note_pdf", dn_py)
        self.assertIn("download_delivery_note_pdf", ne_js)
        self.assertIn("download_delivery_note_pdf", vr_js)
        self.assertLess(ne_js.index('window.open("", "_blank")'), ne_js.index("cima_ve.api.delivery_notes.get_delivery_note_document"))

    def test_whatsapp_group_routing_for_orders_and_registered_payments(self) -> None:
        import sys
        import types
        from unittest.mock import patch
        from cima_ve.api import delivery_notes as dn_api
        from cima_ve.services.delivery_note_service import (
            DEFAULT_WHATSAPP_ORDERS_CHAT_ID,
            DEFAULT_WHATSAPP_PAYMENTS_CHAT_ID,
            build_payment_registered_whatsapp_payload,
        )

        # 1. Pure payment receipt builder
        pay_payload = build_payment_registered_whatsapp_payload(
            customer_name="Luis Rojas",
            payment_amount=120.0,
            payment_method="Pago Móvil / Bs",
            reference="998877",
            registered_by="Daniela",
            allocations=[
                {"delivery_note": "MAT-DN-2026-00031", "allocated_amount": 100.0, "new_estado": "Pagada"},
                {"delivery_note": "MAT-DN-2026-00032", "allocated_amount": 20.0, "new_estado": "Pago Parcial"},
            ],
            total_pending_after=30.0,
            bs_rate=60.0,
            screenshot_url="https://cimaelectronicve.v.frappe.cloud/private/files/capture.jpg",
        )
        self.assertEqual(pay_payload["event"], "payment_registered")
        self.assertEqual(pay_payload["chat_id"], DEFAULT_WHATSAPP_PAYMENTS_CHAT_ID)
        self.assertIn("Luis Rojas", pay_payload["message"])
        self.assertIn("$120.00", pay_payload["message"])
        self.assertIn("Bs. 7,200.00", pay_payload["message"])
        self.assertIn("998877", pay_payload["message"])
        self.assertIn("Daniela", pay_payload["message"])
        self.assertIn("MAT-DN-2026-00031", pay_payload["message"])
        self.assertIn("capture.jpg", pay_payload["message"])

        # 2. Live helper routes payments to Capture de pagos group & resolves absolute screenshot URL
        sent_wa = []

        class _FakeMeta:
            def has_field(self, _name):
                return True

        class _FakeSettings:
            meta = _FakeMeta()

            def get(self, key):
                return {
                    "notify_whatsapp_number": "+584243472198",
                    "whatsapp_webhook_url": "https://hook.us2.make.com/demo",
                    "whatsapp_orders_chat_id": DEFAULT_WHATSAPP_ORDERS_CHAT_ID,
                    "whatsapp_payments_chat_id": DEFAULT_WHATSAPP_PAYMENTS_CHAT_ID,
                }.get(key, "")

            def get_password(self, _key):
                return ""

        fake_frappe = types.SimpleNamespace(
            get_single=lambda _dt: _FakeSettings(),
            log_error=lambda *a, **kw: None,
            utils=types.SimpleNamespace(
                get_url=lambda path: f"https://cimaelectronicve.v.frappe.cloud{path}",
                get_fullname=lambda u: "Daniela" if u == "valeria0205sz@gmail.com" else u,
            ),
        )

        with patch.dict(sys.modules, {"frappe": fake_frappe}), \
             patch.object(dn_api, "_get_exchange_rate_snapshot", return_value={"bcv_rate": 60.0, "adjusted_rate": 65.0}), \
             patch.object(dn_api, "_post_whatsapp_webhook", side_effect=lambda _u, _t, p: sent_wa.append(p) or True):
            res = dn_api.send_client_payment_whatsapp_notification(
                customer="CUST-1",
                customer_name="Luis Rojas",
                payment_amount=50.0,
                payment_method="Zelle",
                reference="Z-1234",
                registered_by="valeria0205sz@gmail.com",
                allocations=[{"delivery_note": "MAT-DN-2026-00031", "allocated_amount": 50.0}],
                results=[{"delivery_note": "MAT-DN-2026-00031", "ve_ne_estado": "Pagada", "paid_now_bs": 3250.0}],
                total_pending_after=0.0,
                screenshot_url="/private/files/pago.png",
            )

        self.assertTrue(res["whatsapp_sent"])
        self.assertEqual(res["chat_id"], DEFAULT_WHATSAPP_PAYMENTS_CHAT_ID)
        self.assertEqual(len(sent_wa), 1)
        self.assertEqual(sent_wa[0]["event"], "payment_registered")
        self.assertEqual(sent_wa[0]["chat_id"], DEFAULT_WHATSAPP_PAYMENTS_CHAT_ID)
        self.assertEqual(sent_wa[0]["screenshot_url"], "https://cimaelectronicve.v.frappe.cloud/private/files/pago.png")
        self.assertIn("Daniela", sent_wa[0]["message"])
        self.assertIn("Pagada", sent_wa[0]["message"])


if __name__ == "__main__":
    unittest.main()
