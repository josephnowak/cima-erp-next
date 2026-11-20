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
            with self.assertRaises(FakePermissionError):
                dn.mark_delivery_note_paid("MAT-DN-2", "Efectivo")
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

            # mark_delivery_note_paid passes ownership check and reaches state validation ("Pagada")
            with self.assertRaises(Exception) as ctx:
                dn.mark_delivery_note_paid("MAT-DN-1", "Efectivo")
            self.assertNotIsInstance(ctx.exception, FakePermissionError)
            self.assertIn("ya fue pagada", str(ctx.exception))
            self.assertEqual(fk.session.user, "seller@x.com")

    def test_seller_settle_and_upload_case_insensitive(self) -> None:
        from cima_ve.api import delivery_notes as dn

        # Testing Camila's exact scenario: owner capitalized, seller lowercase, session either
        fk = make_fake_frappe(["Sales User"], user="Marcanocamila2009@gmail.com")
        inserted = []

        class CamilaDoc:
            name = "MAT-DN-2026-00001"
            docstatus = 1
            ve_vendedor_usuario = "marcanocamila2009@gmail.com"
            owner = "Marcanocamila2009@gmail.com"
            ve_vendedor_nombre = "Camila Marcano"
            ve_ne_estado = "Pagada"
            ve_ne_factura = "SINV-1"

        class FakeFileDoc:
            def __init__(self, payload):
                self.name = "FILE-1"
                self.file_url = "/private/files/comprobante.png"
                self.file_name = payload["file_name"]
                self.flags = types.SimpleNamespace(ignore_permissions=False)
                self._payload = payload

            def insert(self, ignore_permissions=False):
                inserted.append(self._payload)

        fk.get_doc = lambda arg1, arg2=None: FakeFileDoc(arg1) if isinstance(arg1, dict) and arg1.get("doctype") == "File" else CamilaDoc()

        with patched_frappe(fk):
            self.assertTrue(dn._can_user_settle_delivery_note(CamilaDoc(), "Marcanocamila2009@gmail.com"))
            self.assertTrue(dn._can_user_settle_delivery_note(CamilaDoc(), "marcanocamila2009@gmail.com"))
            res = dn.upload_delivery_note_payment_proof("MAT-DN-2026-00001", "comprobante.png", "aGVsbG8=")
            self.assertEqual(res["file_url"], "/private/files/comprobante.png")
            self.assertEqual(len(inserted), 1)

    def test_cashier_caja_cima_can_settle_any_delivery_note(self) -> None:
        from cima_ve.api import delivery_notes as dn

        fk = make_fake_frappe(["Sales User", "Caja CIMA"], user="marcanocamila2009@gmail.com")
        inserted = []

        class OtherSellerDoc:
            name = "MAT-DN-2026-00002"
            docstatus = 1
            ve_vendedor_usuario = "rey@cima.com"
            owner = "rey@cima.com"
            ve_vendedor_nombre = "Rey"
            ve_ne_estado = "Pagada"
            ve_ne_factura = "SINV-2"

        class FakeFileDoc:
            def __init__(self, payload):
                self.name = "FILE-2"
                self.file_url = "/private/files/caja_proof.png"
                self.file_name = payload["file_name"]
                self.flags = types.SimpleNamespace(ignore_permissions=False)
                self._payload = payload

            def insert(self, ignore_permissions=False):
                inserted.append(self._payload)

        fk.get_doc = lambda arg1, arg2=None: FakeFileDoc(arg1) if isinstance(arg1, dict) and arg1.get("doctype") == "File" else OtherSellerDoc()

        with patched_frappe(fk):
            self.assertTrue(dn._can_user_settle_delivery_note(OtherSellerDoc(), "marcanocamila2009@gmail.com"))
            res = dn.upload_delivery_note_payment_proof("MAT-DN-2026-00002", "caja_proof.png", "aGVsbG8=")
            self.assertEqual(res["file_url"], "/private/files/caja_proof.png")
            self.assertEqual(len(inserted), 1)

    def test_seller_can_settle_matching_by_fullname_or_sales_team(self) -> None:
        from cima_ve.api import delivery_notes as dn

        fk = make_fake_frappe(["Sales User"], user="marcanocamila2009@gmail.com")
        fk.utils.get_fullname = lambda u: "Camila Marcano"

        class DocWithNameOnly:
            name = "MAT-DN-3"
            docstatus = 1
            ve_vendedor_usuario = ""
            owner = "admin@x.com"
            ve_vendedor_nombre = "Camila Marcano"
            sales_team: list = []

        class DocWithTeamOnly:
            name = "MAT-DN-4"
            docstatus = 1
            ve_vendedor_usuario = ""
            owner = "admin@x.com"
            ve_vendedor_nombre = ""
            sales_team = [types.SimpleNamespace(sales_person="Camila")]

        with patched_frappe(fk):
            self.assertTrue(dn._can_user_settle_delivery_note(DocWithNameOnly(), "marcanocamila2009@gmail.com"))
            self.assertTrue(dn._can_user_settle_delivery_note(DocWithTeamOnly(), "marcanocamila2009@gmail.com"))

    def test_seller_cannot_read_admin_reports(self) -> None:
        from cima_ve.api import client_consumption, sales_metrics

        with patched_frappe(make_fake_frappe(["Sales User"])):
            with self.assertRaises(FakePermissionError):
                sales_metrics.get_sales_metrics()
            with self.assertRaises(FakePermissionError):
                client_consumption.get_client_consumption()


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
