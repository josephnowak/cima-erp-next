import json
import pathlib
import unittest

from cima_ve.services.seller_pos_service import (
    build_seller_invoice_summary,
    compute_custom_price_breakdown,
    filter_inventory_items,
    validate_quick_invoice_input,
)


class SellerPosServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_items = [
            {
                "item_code": "TV-LG-55",
                "item_name": "Televisor LG 55 UHD",
                "item_group": "Televisores",
                "stock_qty": 8,
                "price": 420.0,
            },
            {
                "item_code": "AIR-WP-12",
                "item_name": "Aire Acondicionado White Point 12000 BTU",
                "item_group": "Climatización",
                "stock_qty": 0,
                "price": 310.0,
            },
            {
                "item_code": "LIC-OS-01",
                "item_name": "Licuadora Oster Clásica",
                "item_group": "Electrodomésticos",
                "stock_qty": 15,
                "price": 55.0,
            },
        ]

    def test_filter_inventory_by_query_and_stock(self) -> None:
        found = filter_inventory_items(self.sample_items, query="white point")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["item_code"], "AIR-WP-12")

        in_stock = filter_inventory_items(self.sample_items, only_in_stock=True)
        self.assertEqual([r["item_code"] for r in in_stock], ["TV-LG-55", "LIC-OS-01"])

    def test_validate_quick_invoice_input_rejects_invalid(self) -> None:
        with self.assertRaises(ValueError):
            validate_quick_invoice_input("", [{"item_code": "TV-LG-55", "qty": 1, "rate": 100}])

        with self.assertRaises(ValueError):
            validate_quick_invoice_input("Cliente Prueba", [])

        with self.assertRaises(ValueError):
            validate_quick_invoice_input("Cliente Prueba", [{"item_code": "TV-LG-55", "qty": 0, "rate": 100}])

    def test_validate_quick_invoice_input_allows_omitted_rate_for_pricing_rules(self) -> None:
        parsed = validate_quick_invoice_input(
            "Cliente Prueba",
            [{"item_code": "TV-LG-55", "qty": 100}],
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].qty, 100.0)
        self.assertEqual(parsed[0].unit_price, 0.0)

    def test_compute_custom_price_breakdown(self) -> None:
        discounted = compute_custom_price_breakdown(3.0, 2.8)
        self.assertTrue(discounted["has_discount"])
        self.assertTrue(discounted["price_changed"])
        self.assertEqual(discounted["price_list_rate"], 3.0)
        self.assertEqual(discounted["rate"], 2.8)
        self.assertAlmostEqual(discounted["discount_amount"], 0.2, places=4)
        self.assertAlmostEqual(discounted["discount_percentage"], 6.67, places=2)

        increased = compute_custom_price_breakdown(3.0, 3.5)
        self.assertFalse(increased["has_discount"])
        self.assertTrue(increased["price_changed"])
        self.assertEqual(increased["discount_amount"], 0.0)
        self.assertEqual(increased["discount_percentage"], 0.0)

        same = compute_custom_price_breakdown(3.0, 3.0)
        self.assertFalse(same["has_discount"])
        self.assertFalse(same["price_changed"])

    def test_build_seller_invoice_summary_with_igtf(self) -> None:
        summary = build_seller_invoice_summary(
            [
                {"item_code": "TV-LG-55", "item_name": "TV LG 55", "qty": 1, "rate": 100.0},
                {"item_code": "LIC-OS-01", "item_name": "Licuadora", "qty": 2, "rate": 25.0},
            ],
            apply_igtf=True,
        )
        self.assertEqual(summary.taxable_base, 150.0)
        self.assertEqual(summary.iva_amount, 24.0)
        self.assertEqual(summary.igtf_amount, 5.22)
        self.assertEqual(summary.grand_total, 179.22)

    def test_frappe_page_and_workspace_json_valid(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent
        page_json = root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.json"
        page_js = root / "cima_ve" / "cima_ve" / "page" / "venta_rapida" / "venta_rapida.js"
        workspace_json = root / "cima_ve" / "fixtures" / "workspace.json"

        page_data = json.loads(page_json.read_text(encoding="utf-8"))
        self.assertEqual(page_data["page_name"], "venta-rapida")
        self.assertEqual(page_data["module"], "Cima VE")

        js_text = page_js.read_text(encoding="utf-8")
        self.assertIn("data-qty-input", js_text)
        self.assertIn("preview_cart_pricing", js_text)
        self.assertIn("cima-sales-channel", js_text)
        self.assertNotIn("cima-sales-person", js_text)
        self.assertNotIn("cima-payment-method", js_text)
        self.assertNotIn("cima-apply-igtf", js_text)
        self.assertIn("e.target.select()", js_text)
        self.assertEqual(page_data["title"], "Facturación Rápida")

        seller_pos_py = (root / "cima_ve" / "api" / "seller_pos.py").read_text(encoding="utf-8")
        storefront_py = (root / "cima_ve" / "api" / "storefront.py").read_text(encoding="utf-8")
        self.assertIn("_apply_required_sales_metadata", seller_pos_py)
        self.assertIn("_apply_required_sales_metadata", storefront_py)

        ws_data = json.loads(workspace_json.read_text(encoding="utf-8"))
        self.assertEqual(len(ws_data), 2)
        self.assertEqual(ws_data[0]["label"], "Facturación Rápida")
        self.assertEqual(len(ws_data[0]["shortcuts"]), 2)
        self.assertEqual(ws_data[0]["shortcuts"][0]["link_to"], "venta-rapida")
        self.assertEqual(ws_data[0]["shortcuts"][1]["link_to"], "notas-entrega")
        self.assertEqual(ws_data[1]["label"], "Administración Cima")
        self.assertEqual([r["role"] for r in ws_data[1]["roles"]], ["System Manager"])
        self.assertEqual(len(ws_data[1]["shortcuts"]), 9)
        admin_links = [s["link_to"] for s in ws_data[1]["shortcuts"]]
        self.assertIn("carga-productos", admin_links)
        self.assertIn("asistente-cima", admin_links)
        self.assertIn("gastos-cima", admin_links)
        self.assertIn("reportes-cima", admin_links)
        self.assertIn("bi-cima", admin_links)
        self.assertNotIn("tienda-cima", admin_links)
        self.assertIn("ve_vendedor_usuario", seller_pos_py)
        self.assertIn("is_admin", seller_pos_py)
        self.assertIn("seller_options", seller_pos_py)
        self.assertIn("cima-logged-seller-badge", js_text)
        self.assertIn("cima-seller-select", js_text)
        self.assertIn("data-admin-only=\"1\"", js_text)
        self.assertIn("isAdmin()", js_text)
        self.assertIn("data-add-custom-price", js_text)
        self.assertIn("data-edit-price", js_text)
        self.assertIn("openCustomPriceDialog", js_text)

        ne_js = (root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.js").read_text(encoding="utf-8")
        self.assertIn("has_discount", ne_js)
        self.assertIn("Con descuento", ne_js)

        dn_py = (root / "cima_ve" / "api" / "delivery_notes.py").read_text(encoding="utf-8")
        self.assertIn("discounted_items", dn_py)
        self.assertIn("price_list_rate", dn_py)

        for admin_page in ("metricas_ventas", "clientes_consumo"):
            p_json = root / "cima_ve" / "cima_ve" / "page" / admin_page / f"{admin_page}.json"
            p_data = json.loads(p_json.read_text(encoding="utf-8"))
            self.assertEqual([r["role"] for r in p_data["roles"]], ["System Manager"], admin_page)

        ne_json = json.loads((root / "cima_ve" / "cima_ve" / "page" / "notas_entrega" / "notas_entrega.json").read_text(encoding="utf-8"))
        self.assertEqual({r["role"] for r in ne_json["roles"]}, {"Sales User", "Sales Manager", "System Manager"})

        for api_file in ("sales_metrics.py", "client_consumption.py", "delivery_notes.py"):
            api_text = (root / "cima_ve" / "api" / api_file).read_text(encoding="utf-8")
            self.assertIn('frappe.only_for("System Manager")', api_text, api_file)


if __name__ == "__main__":
    unittest.main()
