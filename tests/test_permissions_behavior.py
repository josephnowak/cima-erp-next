from __future__ import annotations

import sys
import types
import unittest
from contextlib import contextmanager


class FakePermissionError(Exception):
    pass


def make_fake_frappe(roles: list[str], user: str = "seller@x.com", allow_doc: bool = True):
    fk = types.ModuleType("frappe")
    fk.PermissionError = FakePermissionError
    fk.session = types.SimpleNamespace(user=user)
    fk.calls = []

    def only_for(role):
        allowed = [role] if isinstance(role, str) else list(role)
        if not set(allowed) & set(roles):
            raise FakePermissionError("not permitted")

    def has_permission(doctype, ptype=None, doc=None, throw=False, **kw):
        fk.calls.append(("has_permission", doctype, ptype))
        if not allow_doc:
            if throw:
                raise FakePermissionError("no doc permission")
            return False
        return True

    def throw(msg, exc=None):
        raise (exc or Exception)(msg)

    class FakeDoc:
        customer = "C"
        customer_name = "C"
        items: list = []
        grand_total = 1

        def db_set(self, *a, **k):
            fk.calls.append(("db_set", a))

    fk.only_for = only_for
    fk.has_permission = has_permission
    fk.throw = throw
    fk.get_roles = lambda user=None: list(roles)
    fk.set_user = lambda u: setattr(fk.session, "user", u)
    fk.get_doc = lambda *a, **k: FakeDoc()
    fk.whitelist = lambda *a, **k: (lambda fn: fn)
    fk.__path__ = []
    utils = types.ModuleType("frappe.utils")
    utils.now_datetime = lambda: "now"
    utils.flt = float
    utils.cint = int
    utils.nowdate = lambda: "today"
    utils.today = lambda: "today"
    utils.getdate = lambda *a, **k: None
    fk.utils = utils
    return fk


@contextmanager
def patched_frappe(fake):
    previous = {k: sys.modules.get(k) for k in ("frappe", "frappe.utils")}
    sys.modules["frappe"] = fake
    sys.modules["frappe.utils"] = fake.utils
    try:
        yield
    finally:
        for k, v in previous.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class AdminOnlyEndpointTests(unittest.TestCase):
    def test_seller_cannot_cancel_or_calculate_commissions(self) -> None:
        from cima_ve.api import delivery_notes as dn

        with patched_frappe(make_fake_frappe(["Sales User"])):
            with self.assertRaises(FakePermissionError):
                dn.cancel_delivery_note("MAT-DN-1")
            with self.assertRaises(FakePermissionError):
                dn.revert_delivery_note("MAT-DN-1")
            with self.assertRaises(FakePermissionError):
                dn.purge_cancelled_delivery_notes()
            with self.assertRaises(FakePermissionError):
                dn.calculate_seller_commissions()

    def test_admin_revert_delivery_note_cancels_payments_invoice_and_note(self) -> None:
        import json
        from cima_ve.api import delivery_notes as dn

        fk = make_fake_frappe(["System Manager"], user="admin@x.com")
        cancelled: list[str] = []
        db_sets: dict[str, object] = {}
        comments: list[str] = []

        class FakeMeta:
            @staticmethod
            def has_field(name: str) -> bool:
                return True

        class FakePE:
            def __init__(self, name: str):
                self.name = name
                self.docstatus = 1
                self.flags = types.SimpleNamespace(ignore_permissions=False)

            def cancel(self):
                self.docstatus = 2
                cancelled.append(self.name)

        class FakeSI:
            name = "SINV-9"
            docstatus = 1
            ve_numero_control = ""
            flags = types.SimpleNamespace(ignore_permissions=False)

            def cancel(self):
                self.docstatus = 2
                cancelled.append(self.name)

        class FakeDN:
            name = "MAT-DN-9"
            docstatus = 1
            ve_ne_estado = "Pagada"
            ve_ne_factura = "SINV-9"
            ve_ne_historial_pagos = json.dumps([{"payment_entry": "PE-2"}])
            meta = FakeMeta()
            flags = types.SimpleNamespace(ignore_permissions=False)

            def cancel(self):
                self.docstatus = 2
                cancelled.append(self.name)

            def db_set(self, k, v):
                db_sets[k] = v

            def add_comment(self, kind, msg):
                comments.append(msg)

        docs = {
            ("Delivery Note", "MAT-DN-9"): FakeDN(),
            ("Sales Invoice", "SINV-9"): FakeSI(),
            ("Payment Entry", "PE-1"): FakePE("PE-1"),
            ("Payment Entry", "PE-2"): FakePE("PE-2"),
        }
        fk.get_doc = lambda dt, name: docs[(dt, name)]
        def get_all(dt, filters=None, pluck=None, **kwargs):
            self.assertEqual(dt, "Payment Entry Reference")
            self.assertEqual(filters, {"reference_doctype": "Sales Invoice", "reference_name": "SINV-9"})
            self.assertEqual(pluck, "parent")
            self.assertEqual(kwargs.get("limit_page_length"), 0)
            return ["PE-1"]

        fk.get_all = get_all

        with patched_frappe(fk):
            res = dn.revert_delivery_note("MAT-DN-9", reason="Cliente devolvió")

        self.assertTrue(res["ok"])
        self.assertEqual(res["ve_ne_estado"], "Cancelada")
        self.assertEqual(res["reverted_invoice"], "SINV-9")
        self.assertEqual(res["reverted_payment_entries"], ["PE-1", "PE-2"])
        self.assertEqual(cancelled, ["PE-1", "PE-2", "SINV-9", "MAT-DN-9"])
        self.assertEqual(db_sets["ve_ne_estado"], "Cancelada")
        self.assertEqual(db_sets["ve_ne_monto_pagado"], 0.0)
        self.assertEqual(db_sets["ve_ne_saldo_pendiente"], 0.0)
        self.assertEqual(db_sets["ve_ne_monto_pagado_bs"], 0.0)
        self.assertEqual(db_sets["ve_ne_motivo_cancelacion"], "Cliente devolvió")
        self.assertTrue(any("REVERTIDA" in c for c in comments))

    def test_admin_revert_reloads_sales_invoice_after_cancelling_payment_entry(self) -> None:
        from cima_ve.api import delivery_notes as dn

        fk = make_fake_frappe(["System Manager"], user="admin@x.com")
        si_db_version = {"v": 1}
        cancelled: list[str] = []

        class FakeMeta:
            @staticmethod
            def has_field(name: str) -> bool:
                return True

        class FakePE:
            name = "PE-11"
            docstatus = 1
            flags = types.SimpleNamespace(ignore_permissions=False)

            def cancel(self):
                self.docstatus = 2
                si_db_version["v"] += 1
                cancelled.append(self.name)

        class FakeSI:
            def __init__(self, loaded_v: int):
                self.name = "SINV-11"
                self.docstatus = 1
                self.ve_numero_control = ""
                self.loaded_v = loaded_v
                self.flags = types.SimpleNamespace(ignore_permissions=False)

            def cancel(self):
                if self.loaded_v != si_db_version["v"]:
                    raise RuntimeError("TimestampMismatchError: Sales Invoice modified after load")
                self.docstatus = 2
                cancelled.append(self.name)

        class FakeDN:
            name = "MAT-DN-11"
            docstatus = 1
            ve_ne_estado = "Pagada"
            ve_ne_factura = "SINV-11"
            ve_ne_historial_pagos = ""
            meta = FakeMeta()
            flags = types.SimpleNamespace(ignore_permissions=False)

            def cancel(self):
                self.docstatus = 2
                cancelled.append(self.name)

            def db_set(self, k, v):
                pass

            def add_comment(self, kind, msg):
                pass

        dn_doc = FakeDN()
        pe_doc = FakePE()

        def fake_get_doc(dt, name):
            if dt == "Delivery Note":
                return dn_doc
            if dt == "Payment Entry":
                return pe_doc
            if dt == "Sales Invoice":
                return FakeSI(si_db_version["v"])
            raise KeyError((dt, name))

        fk.get_doc = fake_get_doc
        fk.get_all = lambda *a, **k: ["PE-11"]

        with patched_frappe(fk):
            res = dn.revert_delivery_note("MAT-DN-11", reason="Error de emisión")

        self.assertTrue(res["ok"])
        self.assertEqual(cancelled, ["PE-11", "SINV-11", "MAT-DN-11"])

    def test_admin_revert_unpaid_note_restores_stock_and_retains_cancelled_record(self) -> None:
        from cima_ve.api import delivery_notes as dn

        fk = make_fake_frappe(["System Manager"], user="admin@x.com")
        cancelled: list[str] = []
        db_sets: dict[str, object] = {}
        comments: list[str] = []

        class FakeMeta:
            @staticmethod
            def has_field(name: str) -> bool:
                return True

        class FakeDN:
            name = "MAT-DN-10"
            docstatus = 1
            ve_ne_estado = "Pendiente"
            ve_ne_factura = ""
            ve_ne_historial_pagos = ""
            meta = FakeMeta()
            flags = types.SimpleNamespace(ignore_permissions=False)

            def cancel(self):
                self.docstatus = 2
                cancelled.append(self.name)

            def db_set(self, field, value):
                db_sets[field] = value

            def add_comment(self, kind, message):
                comments.append(message)

        note = FakeDN()
        fk.get_doc = lambda doctype, name: note

        with patched_frappe(fk):
            result = dn.revert_delivery_note(note.name)

        self.assertEqual(cancelled, [note.name])
        self.assertEqual(note.docstatus, 2)
        self.assertEqual(result["ve_ne_estado"], "Cancelada")
        self.assertEqual(db_sets["ve_ne_estado"], "Cancelada")
        self.assertTrue(any("Inventario restaurado" in comment for comment in comments))

    def test_seller_cannot_settle_or_upload_proof_for_other_sellers_note(self) -> None:
        from cima_ve.api import delivery_notes as dn

        fk = make_fake_frappe(["Sales User"], user="seller@x.com")

        class OtherDoc:
            name = "MAT-DN-2"
            docstatus = 1
            ve_vendedor_usuario = "other@x.com"
            owner = "other@x.com"

        fk.get_doc = lambda *a, **k: OtherDoc()
        with patched_frappe(fk):
            # Per-note payment is blocked globally
            with self.assertRaises(Exception) as ctx:
                dn.mark_delivery_note_paid("MAT-DN-2", "Efectivo")
            self.assertIn("deshabilitado", str(ctx.exception))
            with self.assertRaises(FakePermissionError):
                dn.upload_delivery_note_payment_proof("MAT-DN-2", "proof.png", "AAAA")

    def test_seller_can_upload_proof_and_pass_ownership_for_own_note(self) -> None:
        from cima_ve.api import delivery_notes as dn

        fk = make_fake_frappe(["Sales User"], user="seller@x.com")
        inserted = []

        class OwnDoc:
            name = "MAT-DN-1"
            docstatus = 1
            ve_vendedor_usuario = "seller@x.com"
            owner = "seller@x.com"
            ve_ne_estado = "Pagada"
            ve_ne_factura = "SINV-1"

        class FakeFileDoc:
            def __init__(self, payload):
                self.name = "FILE-1"
                self.file_url = "/private/files/proof.png"
                self.file_name = payload["file_name"]
                self.flags = types.SimpleNamespace(ignore_permissions=False)
                self._payload = payload

            def insert(self, ignore_permissions=False):
                inserted.append((self._payload, ignore_permissions, self.flags.ignore_permissions))

        def fake_get_doc(arg1, arg2=None):
            if isinstance(arg1, dict) and arg1.get("doctype") == "File":
                return FakeFileDoc(arg1)
            return OwnDoc()

        fk.get_doc = fake_get_doc
        with patched_frappe(fk):
            res = dn.upload_delivery_note_payment_proof("MAT-DN-1", "proof.png", "aGVsbG8=")
            self.assertEqual(res["file_url"], "/private/files/proof.png")
            self.assertEqual(len(inserted), 1)
            self.assertTrue(inserted[0][1])
            self.assertEqual(inserted[0][0]["attached_to_name"], "MAT-DN-1")
            self.assertEqual(inserted[0][0]["is_private"], 1)

            # mark_delivery_note_paid is blocked globally in favor of client-level FIFO payment
            with self.assertRaises(Exception) as ctx:
                dn.mark_delivery_note_paid("MAT-DN-1", "Efectivo")
            self.assertIn("deshabilitado", str(ctx.exception))
            self.assertEqual(fk.session.user, "seller@x.com")

    def test_seller_cannot_read_admin_reports(self) -> None:
        from cima_ve.api import client_consumption, sales_metrics

        with patched_frappe(make_fake_frappe(["Sales User"])):
            with self.assertRaises(FakePermissionError):
                sales_metrics.get_sales_metrics()
            with self.assertRaises(FakePermissionError):
                client_consumption.get_client_consumption()

    def test_admin_can_assign_delivery_note_to_selected_seller(self) -> None:
        from cima_ve.api import seller_pos

        fk = make_fake_frappe(["System Manager"], user="cimaelectronic@gmail.com")
        fk.utils.get_fullname = lambda u: {
            "cimaelectronic@gmail.com": "Cima Electronic",
            "marcanocamila2009@gmail.com": "Camila Marcano",
        }.get(u, u)

        class FakeMeta:
            @staticmethod
            def has_field(name: str) -> bool:
                return name in {"usuario", "sales_team"}

        fk.get_meta = lambda dt: FakeMeta()
        fk.db = types.SimpleNamespace(
            get_value=lambda dt, name, fields=None, as_dict=False: (
                {"name": "marcanocamila2009@gmail.com", "enabled": 1, "full_name": "Camila Marcano"}
                if dt == "User" and name == "marcanocamila2009@gmail.com"
                else None
            ),
            exists=lambda dt, name: dt == "Sales Person" and name in {"Camila", "Daniela", "Tienda (Mostrador)"},
        )
        fk.get_all = lambda dt, **kw: (
            [
                {"name": "Camila", "employee": None, "usuario": "marcanocamila2009@gmail.com"},
                {"name": "Daniela", "employee": None, "usuario": "valeria0205sz@gmail.com"},
                {"name": "Tienda (Mostrador)", "employee": None, "usuario": None},
            ]
            if dt == "Sales Person"
            else []
        )

        with patched_frappe(fk):
            seller_user, seller_name, sales_person = seller_pos._resolve_effective_seller(
                requested_seller_user="marcanocamila2009@gmail.com"
            )

        self.assertEqual(seller_user, "marcanocamila2009@gmail.com")
        self.assertEqual(seller_name, "Camila Marcano")
        self.assertEqual(sales_person, "Camila")

    def test_non_admin_cannot_impersonate_another_seller(self) -> None:
        from cima_ve.api import delivery_notes as dn

        fk = make_fake_frappe(["Sales User"], user="valeria0205sz@gmail.com")
        fk.utils.get_fullname = lambda u: "Daniela"

        with patched_frappe(fk):
            with self.assertRaises(FakePermissionError):
                dn.create_quick_delivery_note(
                    customer="Cliente Demo",
                    items=[{"item_code": "TV-1", "qty": 1}],
                    fecha_vencimiento="2026-12-01",
                    customer_email="cliente@example.com",
                    seller_user="marcanocamila2009@gmail.com",
                )


class CasheaAndFiscalPermissionTests(unittest.TestCase):
    def test_create_cashea_checkout_requires_doc_permission(self) -> None:
        from cima_ve.api import cashea

        with patched_frappe(make_fake_frappe(["Sales User"], allow_doc=False)):
            with self.assertRaises(FakePermissionError):
                cashea.create_cashea_checkout("SINV-1")

    def test_stamp_sales_invoice_requires_doc_permission(self) -> None:
        from cima_ve.api import fiscal

        with patched_frappe(make_fake_frappe(["Sales User"], allow_doc=False)):
            with self.assertRaises(FakePermissionError):
                fiscal.stamp_sales_invoice("SINV-1")

    def test_webhook_rejects_when_secret_missing_and_not_simulation(self) -> None:
        from cima_ve.api import cashea

        fk = make_fake_frappe([])

        class Settings:
            simulation_mode = 0
            cashea_api_url = ""
            cashea_api_key = ""
            cashea_webhook_secret = ""

        fk.get_single = lambda name: Settings()
        with patched_frappe(fk):
            with self.assertRaises(FakePermissionError):
                cashea.webhook()

    def test_client_without_secret_never_verifies_signature(self) -> None:
        from cima_ve.services.cashea_client import CasheaClient

        client = CasheaClient(webhook_secret="")
        self.assertFalse(client.verify_webhook_signature(b"{}", "sha256=abc"))
        sim = CasheaClient()
        body, sig = b"{}", None
        sig = "sha256=" + sim.sign_payload(body)
        self.assertTrue(sim.verify_webhook_signature(body, sig))


if __name__ == "__main__":
    unittest.main()
