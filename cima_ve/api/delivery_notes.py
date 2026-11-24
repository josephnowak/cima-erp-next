from __future__ import annotations

import json

from cima_ve.api._compat import whitelist
from cima_ve.services.delivery_note_service import (
    DEFAULT_AUTO_DISCOUNT_TIERS,
    DEFAULT_COMMISSION_RULES,
    DEFAULT_NOTIFICATION_EMAILS,
    DEFAULT_NOTIFICATION_WHATSAPP,
    DEFAULT_WHATSAPP_ORDERS_CHAT_ID,
    DEFAULT_WHATSAPP_PAYMENTS_CHAT_ID,
    allocate_client_payment_fifo,
    append_payment_history,
    build_client_delivery_note_email_html,
    build_delivery_note_document_html,
    build_delivery_note_notification_payload,
    build_payment_registered_whatsapp_payload,
    compute_delivery_note_discounts,
    compute_delivery_note_settlement_totals,
    compute_historical_bs_payment,
    compute_margin_commission,
    compute_note_commission_currency_split,
    compute_partial_payment_state,
    get_payment_method_currency,
    is_cash_payment_method,
    is_non_commission_channel,
    parse_auto_discount_tiers,
    parse_commission_rules,
    resolve_commission_rule,
    summarize_client_delivery_notes,
    summarize_delivery_notes,
    summarize_seller_commissions,
    validate_due_date,
)
from cima_ve.services.seller_pos_service import (
    compute_custom_price_breakdown,
    validate_quick_invoice_input,
)


def _get_tax_rates() -> tuple[float, float]:
    import frappe

    settings = frappe.get_single("Cima VE Settings")
    return float(settings.iva_rate or 16.0), float(settings.igtf_rate or 3.0)


def _get_auto_discount_config() -> tuple[bool, list[dict]]:
    import frappe

    try:
        settings = frappe.get_single("Cima VE Settings")
        enabled = (
            bool(int(settings.auto_discount_enabled or 0))
            if settings.meta.has_field("auto_discount_enabled")
            else True
        )
        raw_tiers = settings.auto_discount_tiers if settings.meta.has_field("auto_discount_tiers") else None
        return enabled, parse_auto_discount_tiers(raw_tiers)
    except Exception:
        return True, [dict(t) for t in DEFAULT_AUTO_DISCOUNT_TIERS]


def _get_commission_config() -> tuple[bool, list[dict]]:
    import frappe

    try:
        settings = frappe.get_single("Cima VE Settings")
        enabled = (
            bool(int(settings.commission_enabled or 0))
            if settings.meta.has_field("commission_enabled")
            else True
        )
        raw_rules = settings.commission_rules if settings.meta.has_field("commission_rules") else None
        return enabled, parse_commission_rules(raw_rules)
    except Exception:
        return True, [dict(r) for r in DEFAULT_COMMISSION_RULES]


def _resolve_items_unit_costs(item_codes: list[str], warehouses: list[str] | None = None) -> tuple[dict[tuple[str, str], float], dict[str, float]]:
    """Devuelve mapas de costo unitario desde Bin.valuation_rate e Item.valuation_rate / last_purchase_rate."""
    import frappe

    bin_costs: dict[tuple[str, str], float] = {}
    item_costs: dict[str, float] = {}
    clean_codes = sorted({c for c in item_codes if c})
    if not clean_codes:
        return bin_costs, item_costs

    clean_wh = sorted({w for w in (warehouses or []) if w})
    if clean_wh:
        for b in frappe.get_all(
            "Bin",
            filters={"item_code": ["in", clean_codes], "warehouse": ["in", clean_wh]},
            fields=["item_code", "warehouse", "valuation_rate"],
            limit_page_length=2000,
        ):
            vr = float(b.get("valuation_rate") or 0.0)
            if vr > 0:
                bin_costs[(b["item_code"], b["warehouse"])] = vr

    for it in frappe.get_all(
        "Item",
        filters={"name": ["in", clean_codes]},
        fields=["name", "valuation_rate", "last_purchase_rate"],
        limit_page_length=2000,
    ):
        vr = float(it.get("valuation_rate") or 0.0) or float(it.get("last_purchase_rate") or 0.0)
        if vr > 0:
            item_costs[it["name"]] = vr

    return bin_costs, item_costs


def _compute_dn_items_total_cost(
    dn_items: list,
    default_warehouse: str = "",
    bin_costs: dict[tuple[str, str], float] | None = None,
    item_costs: dict[str, float] | None = None,
) -> float:
    """Calcula el costo total de los productos de una Nota de Entrega."""
    if bin_costs is None or item_costs is None:
        codes = [getattr(r, "item_code", None) or r.get("item_code") for r in dn_items]
        whs = [getattr(r, "warehouse", None) or r.get("warehouse") or default_warehouse for r in dn_items]
        bin_costs, item_costs = _resolve_items_unit_costs(codes, whs)

    total_cost = 0.0
    for row in dn_items:
        code = getattr(row, "item_code", None) if not isinstance(row, dict) else row.get("item_code")
        qty = float((getattr(row, "qty", None) if not isinstance(row, dict) else row.get("qty")) or 0.0)
        wh = (getattr(row, "warehouse", None) if not isinstance(row, dict) else row.get("warehouse")) or default_warehouse
        inc = float((getattr(row, "incoming_rate", None) if not isinstance(row, dict) else row.get("incoming_rate")) or 0.0)
        unit_cost = inc if inc > 0 else (bin_costs.get((code, wh), 0.0) or item_costs.get(code, 0.0))
        total_cost += qty * max(unit_cost, 0.0)
    return round(total_cost, 2)


def _get_exchange_rate_snapshot() -> dict:
    try:
        from cima_ve.api.exchange_rates import get_current_exchange_rates

        return get_current_exchange_rates()
    except Exception:
        return {"bcv_rate": 0.0, "adjusted_rate": 0.0, "applied_brecha_pct": 0.0}


def _generate_delivery_note_pdf_bytes_for_doc(
    doc,
    *,
    bs_rate: float = 0.0,
    client_email: str = "",
    client_phone: str = "",
) -> bytes | None:
    """Genera los bytes del PDF de una Nota de Entrega individual para adjuntar en correo y WhatsApp."""
    try:
        import frappe
        from frappe.utils.pdf import get_pdf

        cust_name = str(getattr(doc, "customer", "") or "")
        cust_doc = {}
        if cust_name:
            cust_doc = frappe.db.get_value(
                "Customer",
                cust_name,
                ["customer_name", "tax_id", "ve_rif_cedula", "email_id", "mobile_no", "primary_address"],
                as_dict=True,
            ) or {}

        customer_info = {
            "customer": cust_name,
            "customer_name": cust_doc.get("customer_name") or getattr(doc, "customer_name", None) or cust_name,
            "rif_cedula": cust_doc.get("ve_rif_cedula") or cust_doc.get("tax_id") or "",
            "email": client_email or cust_doc.get("email_id") or getattr(doc, "ve_ne_cliente_email", "") or "",
            "phone": client_phone or cust_doc.get("mobile_no") or getattr(doc, "ve_ne_cliente_telefono", "") or "",
            "address": cust_doc.get("primary_address") or getattr(doc, "ve_ne_envio_direccion", "") or "",
        }
        items_list = [
            {
                "item_code": r.item_code,
                "item_name": getattr(r, "item_name", None) or r.item_code,
                "qty": float(getattr(r, "qty", 0) or 0),
                "rate": float(getattr(r, "rate", 0) or 0),
                "amount": float(getattr(r, "amount", 0) or 0),
            }
            for r in (getattr(doc, "items", None) or [])
        ]
        grand_total = float(getattr(doc, "grand_total", 0.0) or 0.0)
        note_row = {
            "name": doc.name,
            "posting_date": str(getattr(doc, "posting_date", "") or frappe.utils.today()),
            "ve_ne_fecha_vencimiento": str(getattr(doc, "ve_ne_fecha_vencimiento", "") or ""),
            "ve_ne_estado": getattr(doc, "ve_ne_estado", None) or "Pendiente",
            "total_to_pay": grand_total,
            "grand_total": grand_total,
            "amount": grand_total,
            "paid_amount": float(getattr(doc, "ve_ne_monto_pagado", 0.0) or 0.0),
            "remaining_amount": float(getattr(doc, "ve_ne_saldo_pendiente", None) if getattr(doc, "ve_ne_saldo_pendiente", None) is not None else grand_total),
            "discount_amount": float(getattr(doc, "discount_amount", 0.0) or 0.0),
            "items": items_list,
        }
        logo_abs = "/assets/cima_ve/images/cima-logo-color.png"
        try:
            logo_abs = frappe.utils.get_url(logo_abs)
        except Exception:
            pass

        doc_html = build_delivery_note_document_html(
            customer_info=customer_info,
            notes=[note_row],
            bs_rate=bs_rate,
            logo_url=logo_abs,
            generated_at=str(getattr(doc, "posting_date", "") or frappe.utils.today()),
        )
        return get_pdf(doc_html)
    except Exception:
        try:
            import frappe
            frappe.log_error(frappe.get_traceback(), "Cima VE Delivery Note PDF Generation Error")
        except Exception:
            pass
        return None


def _post_whatsapp_webhook(webhook_url: str, token: str, payload_dict: dict) -> bool:
    import urllib.request

    if not webhook_url or not (payload_dict.get("to") or "").strip():
        return False
    body = json.dumps(payload_dict).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(webhook_url, data=body, headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=12)
    return True


def send_delivery_note_created_notifications(doc, extra_info: dict | None = None) -> dict:
    """Envía correo y WhatsApp con el PDF de la Nota de Entrega al cliente y a CIMA."""
    import base64
    import frappe

    extra = extra_info or {}
    settings = frappe.get_single("Cima VE Settings")
    raw_emails = (settings.get("notify_order_email") or "").strip() if settings.meta.has_field("notify_order_email") else ""
    target_wa = (settings.get("notify_whatsapp_number") or "").strip() if settings.meta.has_field("notify_whatsapp_number") else ""
    webhook_url = (settings.get("whatsapp_webhook_url") or "").strip() if settings.meta.has_field("whatsapp_webhook_url") else ""
    orders_chat_id = (
        (settings.get("whatsapp_orders_chat_id") or "").strip()
        if settings.meta.has_field("whatsapp_orders_chat_id")
        else ""
    ) or DEFAULT_WHATSAPP_ORDERS_CHAT_ID

    fx = _get_exchange_rate_snapshot()
    bs_rate = float(fx.get("adjusted_rate") or fx.get("bcv_rate") or 0.0)
    posting_date = str(getattr(doc, "posting_date", "") or extra.get("posting_date") or frappe.utils.today())
    due_date = str(getattr(doc, "ve_ne_fecha_vencimiento", "") or extra.get("due_date") or "")
    seller_name = getattr(doc, "ve_vendedor_nombre", None) or getattr(doc, "owner", "") or ""
    customer_name = getattr(doc, "customer_name", None) or doc.customer

    client_email = (
        getattr(doc, "ve_ne_cliente_email", None)
        or extra.get("customer_email")
        or _resolve_customer_email(doc.customer)
        or ""
    ).strip()
    client_phone = (
        getattr(doc, "ve_ne_cliente_telefono", None)
        or extra.get("customer_phone")
        or _resolve_customer_phone(doc.customer)
        or ""
    ).strip()

    items_list = [
        {
            "item_code": r.item_code,
            "item_name": r.item_name or r.item_code,
            "qty": float(r.qty or 0),
            "rate": float(r.rate or 0),
            "amount": float(r.amount or 0),
        }
        for r in (doc.items or [])
    ]

    payload = build_delivery_note_notification_payload(
        delivery_note=doc.name,
        customer_name=customer_name,
        seller_name=seller_name,
        origin=getattr(doc, "ve_ne_origen", None) or extra.get("origin") or "Facturación Rápida",
        items=items_list,
        grand_total=float(doc.grand_total or 0.0),
        bs_rate=bs_rate,
        discount_total=float(getattr(doc, "discount_amount", 0.0) or 0.0),
        delivery_provider=getattr(doc, "ve_ne_envio_proveedor", None) or extra.get("delivery_provider") or "",
        delivery_cost=float(getattr(doc, "ve_ne_envio_costo", None) or extra.get("delivery_cost") or 0.0),
        delivery_address=getattr(doc, "ve_ne_envio_direccion", None) or extra.get("delivery_address") or "",
        customer_email=client_email,
        customer_phone=client_phone,
        payment_method=getattr(doc, "ve_ne_metodo_pago", None) or extra.get("payment_method") or "",
        payment_reference=getattr(doc, "ve_ne_referencia_pago", None) or extra.get("payment_reference") or "",
        payment_bank=getattr(doc, "ve_ne_banco_pago", None) or extra.get("payment_bank") or "",
        screenshot_url=getattr(doc, "ve_ne_comprobante_url", None) or extra.get("screenshot_url") or "",
        whatsapp_number=target_wa or DEFAULT_NOTIFICATION_WHATSAPP,
        email_recipients_raw=raw_emails or ", ".join(DEFAULT_NOTIFICATION_EMAILS),
        due_date=due_date,
        posting_date=posting_date,
    )

    pdf_bytes = _generate_delivery_note_pdf_bytes_for_doc(
        doc,
        bs_rate=bs_rate,
        client_email=client_email,
        client_phone=client_phone,
    )
    pdf_filename = f"Nota_de_Entrega_{doc.name}.pdf"
    pdf_attachments = [{"fname": pdf_filename, "fcontent": pdf_bytes}] if pdf_bytes else None
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii") if pdf_bytes else ""

    # 1. Correo a CIMA (único correo para control de pedidos, con PDF adjunto)
    email_sent = False
    try:
        frappe.sendmail(
            recipients=payload["email_recipients"],
            subject=payload["subject"],
            message=payload["html"],
            attachments=pdf_attachments,
            now=True,
        )
        email_sent = True
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Cima VE Delivery Note Email Notification Error")

    # 2. Correo + mensaje WhatsApp para el cliente (con PDF adjunto)
    client_mail = build_client_delivery_note_email_html(
        delivery_note=doc.name,
        customer_name=customer_name,
        items=items_list,
        grand_total=float(doc.grand_total or 0.0),
        bs_rate=bs_rate,
        due_date=due_date,
        seller_name=seller_name,
        discount_total=float(getattr(doc, "discount_amount", 0.0) or 0.0),
        posting_date=posting_date,
        customer_phone=client_phone,
    )

    client_email_sent = False
    if client_email and "@" in client_email:
        try:
            frappe.sendmail(
                recipients=[client_email],
                subject=client_mail["subject"],
                message=client_mail["html"],
                attachments=pdf_attachments,
                now=True,
            )
            client_email_sent = True
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Cima VE Delivery Note Client Email Error")

    # 3. WhatsApp a CIMA y al cliente (ambos con PDF en base64 si está disponible)
    whatsapp_sent = False
    client_whatsapp_sent = False
    if webhook_url:
        token = ""
        try:
            token = settings.get_password("whatsapp_api_token") or ""
        except Exception:
            token = ""

        common_wa_meta = {
            "delivery_note": doc.name,
            "posting_date": posting_date,
            "customer_name": customer_name,
            "grand_total": float(doc.grand_total or 0.0),
        }
        if pdf_b64:
            common_wa_meta["pdf_filename"] = pdf_filename
            common_wa_meta["pdf_base64"] = pdf_b64
            common_wa_meta["media_type"] = "application/pdf"

        try:
            whatsapp_sent = _post_whatsapp_webhook(
                webhook_url,
                token,
                {
                    **common_wa_meta,
                    "event": "delivery_note_created",
                    "recipient_type": "company",
                    "chat_id": orders_chat_id,
                    "to": payload["whatsapp_number"],
                    "message": payload["text"],
                },
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Cima VE Delivery Note WhatsApp Notification Error")

        if client_phone:
            try:
                client_whatsapp_sent = _post_whatsapp_webhook(
                    webhook_url,
                    token,
                    {
                        **common_wa_meta,
                        "event": "delivery_note_client",
                        "recipient_type": "client",
                        "chat_id": "",
                        "to": client_phone,
                        "message": client_mail["whatsapp_text"],
                    },
                )
            except Exception:
                frappe.log_error(frappe.get_traceback(), "Cima VE Delivery Note Client WhatsApp Notification Error")

    return {
        "email_sent": email_sent,
        "client_email_sent": client_email_sent,
        "client_email": client_email,
        "email_recipients": payload["email_recipients"],
        "whatsapp_sent": whatsapp_sent,
        "whatsapp_number": payload["whatsapp_number"],
        "whatsapp_chat_id": orders_chat_id,
        "whatsapp_url": payload["whatsapp_url"],
        "client_whatsapp_sent": client_whatsapp_sent,
        "client_whatsapp_number": client_phone,
        "client_whatsapp_url": client_mail.get("whatsapp_url") or "",
        "pdf_attached": bool(pdf_bytes),
    }


def send_client_payment_whatsapp_notification(
    *,
    customer: str,
    customer_name: str,
    payment_amount: float,
    payment_method: str,
    reference: str = "",
    registered_by: str = "",
    allocations: list[dict] | None = None,
    results: list[dict] | None = None,
    total_pending_after: float = 0.0,
    screenshot_url: str = "",
) -> dict:
    """Envía el comprobante del pago registrado al grupo de WhatsApp Capture de pagos vía webhook."""
    import frappe

    settings = frappe.get_single("Cima VE Settings")
    target_wa = (
        (settings.get("notify_whatsapp_number") or "").strip()
        if settings.meta.has_field("notify_whatsapp_number")
        else ""
    )
    webhook_url = (
        (settings.get("whatsapp_webhook_url") or "").strip()
        if settings.meta.has_field("whatsapp_webhook_url")
        else ""
    )
    payments_chat_id = (
        (settings.get("whatsapp_payments_chat_id") or "").strip()
        if settings.meta.has_field("whatsapp_payments_chat_id")
        else ""
    ) or DEFAULT_WHATSAPP_PAYMENTS_CHAT_ID

    fx = _get_exchange_rate_snapshot()
    bs_rate = float(fx.get("adjusted_rate") or fx.get("bcv_rate") or 0.0)

    res_by_dn = {
        str(r.get("delivery_note") or ""): r
        for r in (results or [])
        if isinstance(r, dict) and r.get("delivery_note")
    }
    enriched_allocs: list[dict] = []
    total_paid_bs = 0.0
    for alloc in allocations or []:
        dn_name = str(alloc.get("delivery_note") or "")
        matched = res_by_dn.get(dn_name) or {}
        total_paid_bs += float(matched.get("paid_now_bs") or 0.0)
        enriched_allocs.append(
            {
                **alloc,
                "new_estado": matched.get("ve_ne_estado") or alloc.get("new_estado") or "",
            }
        )

    abs_screenshot = (screenshot_url or "").strip()
    if abs_screenshot and abs_screenshot.startswith("/"):
        try:
            abs_screenshot = frappe.utils.get_url(abs_screenshot)
        except Exception:
            pass

    registered_by_label = registered_by
    if registered_by:
        try:
            registered_by_label = frappe.utils.get_fullname(registered_by) or registered_by
        except Exception:
            registered_by_label = registered_by

    payload = build_payment_registered_whatsapp_payload(
        customer_name=customer_name or customer,
        payment_amount=payment_amount,
        payment_method=payment_method,
        reference=reference,
        registered_by=registered_by_label,
        allocations=enriched_allocs,
        total_pending_after=total_pending_after,
        bs_rate=bs_rate,
        paid_now_bs=total_paid_bs,
        screenshot_url=abs_screenshot,
        whatsapp_number=target_wa or DEFAULT_NOTIFICATION_WHATSAPP,
        chat_id=payments_chat_id,
    )

    whatsapp_sent = False
    if webhook_url:
        token = ""
        try:
            token = settings.get_password("whatsapp_api_token") or ""
        except Exception:
            token = ""
        try:
            whatsapp_sent = _post_whatsapp_webhook(webhook_url, token, payload)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Cima VE Payment WhatsApp Notification Error")

    return {
        "whatsapp_sent": whatsapp_sent,
        "chat_id": payments_chat_id,
        "whatsapp_number": payload["to"],
    }


def _resolve_tax_account(company: str, keyword: str = "") -> str:
    import frappe

    filters = {"company": company, "is_group": 0, "account_type": "Tax"}
    accounts = frappe.get_all("Account", filters=filters, pluck="name", limit=20)
    if keyword and accounts:
        match = next((a for a in accounts if keyword.lower() in a.lower()), "")
        if match:
            return match
    if accounts:
        return accounts[0]

    liab = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0, "root_type": "Liability"},
        pluck="name",
        limit=1,
    )
    if liab:
        return liab[0]
    any_acc = frappe.get_all("Account", filters={"company": company, "is_group": 0}, pluck="name", limit=1)
    return any_acc[0] if any_acc else ""


def _resolve_customer_phone(customer: str, override_phone: str = "") -> str:
    import frappe

    candidate = (override_phone or "").strip()
    if not candidate and customer:
        candidate = (frappe.db.get_value("Customer", customer, "mobile_no") or "").strip()
    if not candidate and customer:
        links = frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
            pluck="parent",
            limit=5,
        )
        for contact_name in links:
            c_phone = (
                frappe.db.get_value("Contact", contact_name, "mobile_no")
                or frappe.db.get_value("Contact", contact_name, "phone")
                or ""
            ).strip()
            if c_phone:
                candidate = c_phone
                break
    return candidate


def _resolve_customer_email(customer: str, override_email: str = "") -> str:
    import frappe

    candidate = (override_email or "").strip().lower()
    if not candidate:
        candidate = (frappe.db.get_value("Customer", customer, "email_id") or "").strip().lower()
    if not candidate:
        links = frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
            pluck="parent",
            limit=5,
        )
        for contact_name in links:
            c_email = (frappe.db.get_value("Contact", contact_name, "email_id") or "").strip().lower()
            if c_email:
                candidate = c_email
                break
    return candidate


@whitelist()
def create_quick_delivery_note(
    customer: str,
    items: str | list[dict],
    price_list: str = "",
    warehouse: str = "",
    sales_channel: str = "Detal",
    sales_person: str = "",
    notes: str = "",
    manual_discount_enabled: int | bool = 0,
    manual_discount_percentage: float | str = 0.0,
    commission_option: str = "",
    fecha_vencimiento: str = "",
    customer_email: str = "",
    seller_user: str = "",
) -> dict:
    """Crea y confirma una Nota de Entrega (Delivery Note): descuenta inventario sin cobrar dinero aún."""
    import frappe
    from cima_ve.api.seller_pos import (
        _apply_required_sales_metadata,
        _resolve_company_and_currency,
        _resolve_effective_seller,
    )

    session_user = (getattr(frappe.session, "user", None) or "").strip()
    effective_seller_user, seller_name, resolved_sales_person = _resolve_effective_seller(
        requested_seller_user=seller_user,
        requested_sales_person=sales_person,
    )

    try:
        validated_due_date = validate_due_date(fecha_vencimiento)
    except ValueError as exc:
        frappe.throw(str(exc))

    resolved_email = _resolve_customer_email(customer, customer_email)
    resolved_phone = _resolve_customer_phone(customer)
    if not resolved_email or "@" not in resolved_email or "." not in resolved_email.split("@")[-1]:
        frappe.throw("El correo electrónico del cliente es obligatorio para emitir la Nota de Entrega.")

    if customer_email:
        existing_cust_email = (frappe.db.get_value("Customer", customer, "email_id") or "").strip()
        if not existing_cust_email:
            try:
                frappe.db.set_value("Customer", customer, "email_id", resolved_email, update_modified=False)
            except Exception:
                pass

    parsed_items = json.loads(items) if isinstance(items, str) else items
    validated = validate_quick_invoice_input(customer, parsed_items)
    custom_price_map: dict[str, float] = {}
    for raw_row in parsed_items:
        code = str(raw_row.get("item_code") or "").strip()
        if code and raw_row.get("is_custom_price") and raw_row.get("rate") is not None:
            custom_price_map[code] = max(float(raw_row.get("rate") or 0.0), 0.0)

    company, currency = _resolve_company_and_currency(price_list)

    dn_items = []
    for row in validated:
        item_dict: dict = {
            "item_code": row.item_code,
            "qty": row.qty,
        }
        if warehouse:
            item_dict["warehouse"] = warehouse
        if row.item_code in custom_price_map:
            item_dict["rate"] = custom_price_map[row.item_code]
            item_dict["ignore_pricing_rule"] = 1
        dn_items.append(item_dict)

    default_remark = (
        f"Nota de Entrega ({seller_name}, emitida por {session_user}) — Pendiente por cobrar"
        if effective_seller_user.lower() != session_user.lower()
        else f"Nota de Entrega ({seller_name}) — Pendiente por cobrar"
    )

    doc = frappe.get_doc(
        {
            "doctype": "Delivery Note",
            "company": company,
            "customer": customer,
            "currency": currency,
            "selling_price_list": price_list or None,
            "set_warehouse": warehouse or None,
            "ignore_pricing_rule": 0,
            "ve_vendedor_usuario": effective_seller_user,
            "ve_vendedor_nombre": seller_name,
            "ve_ne_estado": "Pendiente",
            "ve_ne_fecha_vencimiento": validated_due_date,
            "ve_ne_cliente_email": resolved_email,
            "ve_ne_cliente_telefono": resolved_phone,
            "remarks": (notes or default_remark).strip(),
            "items": dn_items,
        }
    )
    comm_enabled, comm_rules = _get_commission_config()
    selected_comm_input = (commission_option or sales_channel or "").strip()
    matched_rule = resolve_commission_rule(selected_comm_input, comm_rules)
    effective_channel = (matched_rule.get("channel") if matched_rule else sales_channel) or "Detal"
    comm_label = (matched_rule.get("label") if matched_rule else selected_comm_input) or effective_channel
    comm_pct = float(matched_rule.get("percentage") or 0.0) if (comm_enabled and matched_rule) else 0.0

    if is_non_commission_channel(sales_channel=effective_channel, commission_option=selected_comm_input):
        comm_pct = 0.0
        lowered = f"{selected_comm_input} {effective_channel}".lower()
        if "mercado" in lowered:
            effective_channel = "MercadoLibre"
            comm_label = "MercadoLibre (Sin comisión)"
            if doc.meta.has_field("ve_ne_origen"):
                doc.ve_ne_origen = "MercadoLibre"
        elif "tienda" in lowered:
            effective_channel = "Tienda Web"
            comm_label = "Tienda Web (Sin comisión)"
            if doc.meta.has_field("ve_ne_origen"):
                doc.ve_ne_origen = "Tienda Web"

    _apply_required_sales_metadata(
        doc,
        sales_channel=effective_channel,
        sales_person=resolved_sales_person,
        seller_user=effective_seller_user,
    )
    doc.run_method("set_missing_values")

    if custom_price_map:
        for item_doc in doc.items:
            if item_doc.item_code in custom_price_map:
                custom_rate = custom_price_map[item_doc.item_code]
                orig_rate = float(getattr(item_doc, "price_list_rate", 0) or 0)
                bd = compute_custom_price_breakdown(orig_rate, custom_rate)
                item_doc.rate = bd["rate"]
                item_doc.discount_amount = bd["discount_amount"]
                item_doc.discount_percentage = bd["discount_percentage"]
                if item_doc.meta.has_field("ignore_pricing_rule"):
                    item_doc.ignore_pricing_rule = 1

    doc.run_method("calculate_taxes_and_totals")

    subtotal_before_global = round(float(doc.net_total or doc.total or doc.grand_total or 0.0), 2)
    auto_disc_enabled, auto_disc_tiers = _get_auto_discount_config()
    manual_disc_flag = (
        bool(int(manual_discount_enabled))
        if isinstance(manual_discount_enabled, (int, str)) and str(manual_discount_enabled).strip() != ""
        else bool(manual_discount_enabled)
    )
    try:
        manual_disc_pct = float(manual_discount_percentage or 0.0)
    except (TypeError, ValueError):
        manual_disc_pct = 0.0

    disc_info = compute_delivery_note_discounts(
        subtotal=subtotal_before_global,
        auto_discount_enabled=auto_disc_enabled,
        tiers=auto_disc_tiers,
        manual_discount_enabled=manual_disc_flag,
        manual_discount_percentage=manual_disc_pct,
    )

    if disc_info["total_discount_amount"] > 0:
        doc.apply_discount_on = "Net Total"
        doc.discount_amount = disc_info["total_discount_amount"]
        doc.additional_discount_percentage = disc_info["effective_discount_pct"]
        doc.run_method("calculate_taxes_and_totals")

    if doc.meta.has_field("ve_ne_subtotal_sin_descuento"):
        doc.ve_ne_subtotal_sin_descuento = disc_info["subtotal"]
    if doc.meta.has_field("ve_ne_descuento_auto_pct"):
        doc.ve_ne_descuento_auto_pct = disc_info["auto_discount_pct"]
    if doc.meta.has_field("ve_ne_descuento_auto_monto"):
        doc.ve_ne_descuento_auto_monto = disc_info["auto_discount_amount"]
    if doc.meta.has_field("ve_ne_descuento_auto_etiqueta"):
        doc.ve_ne_descuento_auto_etiqueta = disc_info["auto_discount_label"]
    if doc.meta.has_field("ve_ne_descuento_manual_pct"):
        doc.ve_ne_descuento_manual_pct = disc_info["manual_discount_pct"]
    if doc.meta.has_field("ve_ne_descuento_manual_monto"):
        doc.ve_ne_descuento_manual_monto = disc_info["manual_discount_amount"]

    discounted_items: list[dict] = []
    for item_doc in doc.items:
        orig_rate = float(getattr(item_doc, "price_list_rate", 0) or 0)
        final_rate = float(getattr(item_doc, "rate", 0) or 0)
        disc_pct = float(getattr(item_doc, "discount_percentage", 0) or 0)
        disc_amt = float(getattr(item_doc, "discount_amount", 0) or 0)
        if disc_pct > 0 or disc_amt > 0 or (orig_rate > 0 and final_rate < orig_rate - 0.0001):
            discounted_items.append(
                {
                    "item_code": item_doc.item_code,
                    "item_name": item_doc.item_name or item_doc.item_code,
                    "price_list_rate": orig_rate,
                    "rate": final_rate,
                    "discount_percentage": disc_pct,
                    "discount_amount": disc_amt,
                }
            )

    if discounted_items:
        disc_summary = "; ".join(
            f"{d['item_code']} (${d['price_list_rate']:.2f} -> ${d['rate']:.2f})"
            for d in discounted_items
        )
        doc.remarks = f"{doc.remarks} | Descuento en: {disc_summary}".strip(" |")
    if disc_info["auto_discount_amount"] > 0:
        doc.remarks = (
            f"{doc.remarks} | Descuento automático {disc_info['auto_discount_pct']:.2f}% "
            f"({disc_info['auto_discount_label']}): -${disc_info['auto_discount_amount']:.2f}"
        ).strip(" |")
    if disc_info["manual_discount_amount"] > 0:
        doc.remarks = (
            f"{doc.remarks} | Descuento manual {disc_info['manual_discount_pct']:.2f}%: "
            f"-${disc_info['manual_discount_amount']:.2f}"
        ).strip(" |")

    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    doc.submit()
    doc.reload()

    total_cost = _compute_dn_items_total_cost(doc.items, default_warehouse=doc.set_warehouse or warehouse)
    net_sale = float(doc.net_total or doc.grand_total or 0.0)
    comm_calc = compute_margin_commission(net_sale, total_cost, comm_pct)
    if doc.meta.has_field("ve_ne_comision_opcion"):
        doc.db_set("ve_ne_comision_opcion", comm_label, update_modified=False)
    if doc.meta.has_field("ve_ne_comision_pct"):
        doc.db_set("ve_ne_comision_pct", comm_calc["commission_pct"], update_modified=False)
    if doc.meta.has_field("ve_ne_costo_total"):
        doc.db_set("ve_ne_costo_total", comm_calc["total_cost"], update_modified=False)
    if doc.meta.has_field("ve_ne_margen_ganancia"):
        doc.db_set("ve_ne_margen_ganancia", comm_calc["margin_amount"], update_modified=False)
    if doc.meta.has_field("ve_ne_comision_monto"):
        doc.db_set("ve_ne_comision_monto", comm_calc["commission_amount"], update_modified=False)

    notif_result = send_delivery_note_created_notifications(doc)

    return {
        "delivery_note": doc.name,
        "status": doc.status,
        "ve_ne_estado": getattr(doc, "ve_ne_estado", None) or "Pendiente",
        "fecha_vencimiento": getattr(doc, "ve_ne_fecha_vencimiento", None) or validated_due_date,
        "currency": doc.currency,
        "net_total": float(doc.net_total or 0),
        "grand_total": float(doc.grand_total or 0),
        "has_discount": bool(discounted_items or disc_info["has_global_discount"]),
        "discounted_items": discounted_items,
        "discount_info": disc_info,
        "commission": {
            "option": comm_label,
            **comm_calc,
        },
        "notifications": notif_result,
        "seller": {
            "user": getattr(doc, "ve_vendedor_usuario", None) or effective_seller_user,
            "fullname": getattr(doc, "ve_vendedor_nombre", None) or seller_name,
            "sales_person": resolved_sales_person,
        },
    }


@whitelist()
def get_delivery_notes(
    status: str = "",
    from_date: str = "",
    to_date: str = "",
    seller: str = "",
    search: str = "",
) -> dict:
    """Lista Notas de Entrega. Admin ve todas; otros usuarios solo ven las propias."""
    import frappe

    current_user = frappe.session.user
    if not current_user or current_user == "Guest":
        frappe.throw("Debes iniciar sesión para ver Notas de Entrega.", frappe.PermissionError)

    is_admin = "System Manager" in frappe.get_roles(current_user)

    dn_meta = frappe.get_meta("Delivery Note")
    has_estado = dn_meta.has_field("ve_ne_estado")
    has_fecha_venc = dn_meta.has_field("ve_ne_fecha_vencimiento")
    has_vendedor_user = dn_meta.has_field("ve_vendedor_usuario")
    has_vendedor_name = dn_meta.has_field("ve_vendedor_nombre")
    has_fecha_res = dn_meta.has_field("ve_ne_fecha_resolucion")
    has_user_res = dn_meta.has_field("ve_ne_usuario_resolucion")
    has_metodo = dn_meta.has_field("ve_ne_metodo_pago")
    has_iva = dn_meta.has_field("ve_ne_aplica_iva")
    has_igtf = dn_meta.has_field("ve_ne_aplica_igtf")
    has_factura = dn_meta.has_field("ve_ne_factura")
    has_motivo = dn_meta.has_field("ve_ne_motivo_cancelacion")
    has_monto_total = dn_meta.has_field("ve_ne_monto_total")
    has_monto_pagado = dn_meta.has_field("ve_ne_monto_pagado")
    has_saldo_pendiente = dn_meta.has_field("ve_ne_saldo_pendiente")
    has_historial_pagos = dn_meta.has_field("ve_ne_historial_pagos")
    has_sub_sin_desc = dn_meta.has_field("ve_ne_subtotal_sin_descuento")
    has_desc_auto_pct = dn_meta.has_field("ve_ne_descuento_auto_pct")
    has_desc_auto_monto = dn_meta.has_field("ve_ne_descuento_auto_monto")
    has_desc_auto_etiq = dn_meta.has_field("ve_ne_descuento_auto_etiqueta")
    has_desc_manual_pct = dn_meta.has_field("ve_ne_descuento_manual_pct")
    has_desc_manual_monto = dn_meta.has_field("ve_ne_descuento_manual_monto")
    has_origen = dn_meta.has_field("ve_ne_origen")
    has_ref_pago = dn_meta.has_field("ve_ne_referencia_pago")
    has_banco_pago = dn_meta.has_field("ve_ne_banco_pago")
    has_comprobante = dn_meta.has_field("ve_ne_comprobante_url")
    has_envio_prov = dn_meta.has_field("ve_ne_envio_proveedor")
    has_envio_costo = dn_meta.has_field("ve_ne_envio_costo")
    has_envio_dir = dn_meta.has_field("ve_ne_envio_direccion")
    has_cli_email = dn_meta.has_field("ve_ne_cliente_email")
    has_cli_tel = dn_meta.has_field("ve_ne_cliente_telefono")
    has_canal_venta = dn_meta.has_field("canal_venta")
    has_com_opcion = dn_meta.has_field("ve_ne_comision_opcion")
    has_com_pct = dn_meta.has_field("ve_ne_comision_pct")
    has_costo_total = dn_meta.has_field("ve_ne_costo_total")
    has_margen = dn_meta.has_field("ve_ne_margen_ganancia")
    has_com_monto = dn_meta.has_field("ve_ne_comision_monto")
    has_tasa_cambio = dn_meta.has_field("ve_ne_tasa_cambio")
    has_monto_bs = dn_meta.has_field("ve_ne_monto_bs")
    has_monto_pagado_bs = dn_meta.has_field("ve_ne_monto_pagado_bs")

    conditions = ["dn.docstatus IN (1, 2)"]
    params: dict = {}

    if from_date:
        conditions.append("dn.posting_date >= %(from_date)s")
        params["from_date"] = from_date
    if to_date:
        conditions.append("dn.posting_date <= %(to_date)s")
        params["to_date"] = to_date
    if not is_admin:
        if has_vendedor_user:
            conditions.append("LOWER(COALESCE(NULLIF(dn.ve_vendedor_usuario, ''), dn.owner)) = LOWER(%(current_user)s)")
        else:
            conditions.append("LOWER(dn.owner) = LOWER(%(current_user)s)")
        params["current_user"] = current_user
    elif seller:
        if has_vendedor_user:
            conditions.append("COALESCE(NULLIF(dn.ve_vendedor_usuario, ''), dn.owner) = %(seller)s")
        else:
            conditions.append("dn.owner = %(seller)s")
        params["seller"] = seller
    if search:
        conditions.append("(dn.name LIKE %(q)s OR dn.customer LIKE %(q)s OR dn.customer_name LIKE %(q)s)")
        params["q"] = f"%{search.strip()}%"

    select_cols = [
        "dn.name",
        "dn.docstatus",
        "dn.posting_date",
        "dn.posting_time",
        "dn.creation",
        "dn.owner",
        "dn.customer",
        "dn.customer_name",
        "dn.currency",
        "dn.total",
        "dn.net_total",
        "dn.grand_total",
        "dn.discount_amount",
        "dn.additional_discount_percentage",
        "dn.total_qty",
        "dn.status",
        "COALESCE(NULLIF(dn.ve_vendedor_usuario, ''), dn.owner) AS seller_user" if has_vendedor_user else "dn.owner AS seller_user",
        "dn.ve_vendedor_nombre AS seller_name" if has_vendedor_name else "'' AS seller_name",
        "dn.ve_ne_estado AS ve_ne_estado" if has_estado else "'' AS ve_ne_estado",
        "dn.ve_ne_fecha_vencimiento AS ve_ne_fecha_vencimiento" if has_fecha_venc else "NULL AS ve_ne_fecha_vencimiento",
        "dn.ve_ne_fecha_resolucion AS ve_ne_fecha_resolucion" if has_fecha_res else "NULL AS ve_ne_fecha_resolucion",
        "dn.ve_ne_usuario_resolucion AS ve_ne_usuario_resolucion" if has_user_res else "'' AS ve_ne_usuario_resolucion",
        "dn.ve_ne_metodo_pago AS ve_ne_metodo_pago" if has_metodo else "'' AS ve_ne_metodo_pago",
        "dn.ve_ne_aplica_iva AS ve_ne_aplica_iva" if has_iva else "0 AS ve_ne_aplica_iva",
        "dn.ve_ne_aplica_igtf AS ve_ne_aplica_igtf" if has_igtf else "0 AS ve_ne_aplica_igtf",
        "dn.ve_ne_factura AS ve_ne_factura" if has_factura else "'' AS ve_ne_factura",
        "dn.ve_ne_motivo_cancelacion AS ve_ne_motivo_cancelacion" if has_motivo else "'' AS ve_ne_motivo_cancelacion",
        "dn.ve_ne_monto_total AS ve_ne_monto_total" if has_monto_total else "0 AS ve_ne_monto_total",
        "dn.ve_ne_monto_pagado AS ve_ne_monto_pagado" if has_monto_pagado else "0 AS ve_ne_monto_pagado",
        "dn.ve_ne_saldo_pendiente AS ve_ne_saldo_pendiente" if has_saldo_pendiente else "0 AS ve_ne_saldo_pendiente",
        "dn.ve_ne_historial_pagos AS ve_ne_historial_pagos" if has_historial_pagos else "'' AS ve_ne_historial_pagos",
        "dn.ve_ne_subtotal_sin_descuento AS ve_ne_subtotal_sin_descuento" if has_sub_sin_desc else "0 AS ve_ne_subtotal_sin_descuento",
        "dn.ve_ne_descuento_auto_pct AS ve_ne_descuento_auto_pct" if has_desc_auto_pct else "0 AS ve_ne_descuento_auto_pct",
        "dn.ve_ne_descuento_auto_monto AS ve_ne_descuento_auto_monto" if has_desc_auto_monto else "0 AS ve_ne_descuento_auto_monto",
        "dn.ve_ne_descuento_auto_etiqueta AS ve_ne_descuento_auto_etiqueta" if has_desc_auto_etiq else "'' AS ve_ne_descuento_auto_etiqueta",
        "dn.ve_ne_descuento_manual_pct AS ve_ne_descuento_manual_pct" if has_desc_manual_pct else "0 AS ve_ne_descuento_manual_pct",
        "dn.ve_ne_descuento_manual_monto AS ve_ne_descuento_manual_monto" if has_desc_manual_monto else "0 AS ve_ne_descuento_manual_monto",
        "dn.ve_ne_origen AS ve_ne_origen" if has_origen else "'Facturación Rápida' AS ve_ne_origen",
        "dn.ve_ne_referencia_pago AS ve_ne_referencia_pago" if has_ref_pago else "'' AS ve_ne_referencia_pago",
        "dn.ve_ne_banco_pago AS ve_ne_banco_pago" if has_banco_pago else "'' AS ve_ne_banco_pago",
        "dn.ve_ne_comprobante_url AS ve_ne_comprobante_url" if has_comprobante else "'' AS ve_ne_comprobante_url",
        "dn.ve_ne_envio_proveedor AS ve_ne_envio_proveedor" if has_envio_prov else "'' AS ve_ne_envio_proveedor",
        "dn.ve_ne_envio_costo AS ve_ne_envio_costo" if has_envio_costo else "0 AS ve_ne_envio_costo",
        "dn.ve_ne_envio_direccion AS ve_ne_envio_direccion" if has_envio_dir else "'' AS ve_ne_envio_direccion",
        "dn.ve_ne_cliente_email AS ve_ne_cliente_email" if has_cli_email else "'' AS ve_ne_cliente_email",
        "dn.ve_ne_cliente_telefono AS ve_ne_cliente_telefono" if has_cli_tel else "'' AS ve_ne_cliente_telefono",
        "dn.canal_venta AS canal_venta" if has_canal_venta else "'' AS canal_venta",
        "dn.ve_ne_comision_opcion AS ve_ne_comision_opcion" if has_com_opcion else "'' AS ve_ne_comision_opcion",
        "dn.ve_ne_comision_pct AS ve_ne_comision_pct" if has_com_pct else "0 AS ve_ne_comision_pct",
        "dn.ve_ne_costo_total AS ve_ne_costo_total" if has_costo_total else "0 AS ve_ne_costo_total",
        "dn.ve_ne_margen_ganancia AS ve_ne_margen_ganancia" if has_margen else "0 AS ve_ne_margen_ganancia",
        "dn.ve_ne_comision_monto AS ve_ne_comision_monto" if has_com_monto else "0 AS ve_ne_comision_monto",
        "dn.ve_ne_tasa_cambio AS ve_ne_tasa_cambio" if has_tasa_cambio else "0 AS ve_ne_tasa_cambio",
        "dn.ve_ne_monto_bs AS ve_ne_monto_bs" if has_monto_bs else "0 AS ve_ne_monto_bs",
        "dn.ve_ne_monto_pagado_bs AS ve_ne_monto_pagado_bs" if has_monto_pagado_bs else "0 AS ve_ne_monto_pagado_bs",
    ]

    rows = frappe.db.sql(
        f"""
        SELECT {", ".join(select_cols)}
        FROM `tabDelivery Note` dn
        WHERE {" AND ".join(conditions)}
        ORDER BY dn.creation DESC
        LIMIT 300
        """,
        params,
        as_dict=True,
    )

    dn_names = [r["name"] for r in rows]
    items_by_dn: dict[str, list[dict]] = {}
    all_item_codes: list[str] = []
    all_warehouses: list[str] = []
    if dn_names:
        dn_items = frappe.get_all(
            "Delivery Note Item",
            filters={"parent": ["in", dn_names]},
            fields=[
                "parent",
                "item_code",
                "item_name",
                "qty",
                "warehouse",
                "incoming_rate",
                "price_list_rate",
                "rate",
                "discount_percentage",
                "discount_amount",
                "amount",
            ],
            limit_page_length=3000,
        )
        for it in dn_items:
            orig_rate = float(it.get("price_list_rate") or 0)
            final_rate = float(it.get("rate") or 0)
            disc_pct = float(it.get("discount_percentage") or 0)
            disc_amt = float(it.get("discount_amount") or 0)
            it["has_discount"] = bool(
                disc_pct > 0 or disc_amt > 0 or (orig_rate > 0 and final_rate < orig_rate - 0.0001)
            )
            items_by_dn.setdefault(it["parent"], []).append(it)
            if it.get("item_code"):
                all_item_codes.append(it["item_code"])
            if it.get("warehouse"):
                all_warehouses.append(it["warehouse"])

    comm_enabled, comm_rules = _get_commission_config()
    bin_costs, item_costs = _resolve_items_unit_costs(all_item_codes, all_warehouses)

    for r in rows:
        r["items"] = items_by_dn.get(r["name"], [])
        r["has_discount"] = any(it.get("has_discount") for it in r["items"])
        r["discounted_items_count"] = sum(1 for it in r["items"] if it.get("has_discount"))
        if not r.get("seller_name") and r.get("seller_user"):
            r["seller_name"] = frappe.utils.get_fullname(r["seller_user"]) or r["seller_user"]

        is_cancelled = int(r.get("docstatus") or 0) == 2 or (r.get("ve_ne_estado") or "") == "Cancelada"
        is_excluded_channel = is_non_commission_channel(
            r.get("ve_ne_origen"),
            r.get("canal_venta"),
            r.get("ve_ne_comision_opcion"),
        )
        if is_excluded_channel:
            r["ve_ne_comision_pct"] = 0.0
            r["ve_ne_comision_monto"] = 0.0
        elif comm_enabled and not is_cancelled:
            stored_pct = float(r.get("ve_ne_comision_pct") or 0.0)
            net_sale = float(r.get("net_total") or r.get("grand_total") or 0.0)
            if stored_pct <= 0.0:
                rule = resolve_commission_rule(
                    r.get("ve_ne_comision_opcion") or r.get("canal_venta") or "",
                    comm_rules,
                )
                if rule:
                    calc_cost = _compute_dn_items_total_cost(
                        r["items"],
                        bin_costs=bin_costs,
                        item_costs=item_costs,
                    )
                    calc = compute_margin_commission(net_sale, calc_cost, float(rule.get("percentage") or 0.0))
                    r["ve_ne_comision_opcion"] = rule.get("label") or ""
                    r["ve_ne_comision_pct"] = calc["commission_pct"]
                    r["ve_ne_costo_total"] = calc["total_cost"]
                    r["ve_ne_margen_ganancia"] = calc["margin_amount"]
                    r["ve_ne_comision_monto"] = calc["commission_amount"]
                    r["_commission_computed_fallback"] = True
            else:
                stored_cost = float(r.get("ve_ne_costo_total") or 0.0)
                calc = compute_margin_commission(net_sale, stored_cost, stored_pct)
                stored_comm = round(float(r.get("ve_ne_comision_monto") or 0.0), 2)
                if abs(stored_comm - calc["commission_amount"]) > 0.01:
                    r["ve_ne_comision_monto"] = calc["commission_amount"]
                    r["_commission_computed_fallback"] = True

    summarized = summarize_delivery_notes(rows)
    if status == "Pendiente":
        summarized["notes"] = [
            n for n in summarized["notes"] if n["ve_ne_estado"] in ("Pendiente", "Pago Parcial")
        ]
    elif status:
        summarized["notes"] = [n for n in summarized["notes"] if n["ve_ne_estado"] == status]

    fx_snapshot = _get_exchange_rate_snapshot()
    current_bs_rate = float(fx_snapshot.get("adjusted_rate") or fx_snapshot.get("bcv_rate") or 0.0)

    filtered_active = [n for n in summarized["notes"] if n.get("ve_ne_estado") != "Cancelada"]
    for n in summarized["notes"]:
        split = compute_note_commission_currency_split(n, fallback_bs_rate=current_bs_rate)
        n["commission_usd"] = split["commission_usd"]
        n["commission_bs_usd_equiv"] = split["commission_bs_usd_equiv"]
        n["commission_bs"] = split["commission_bs"]

    summarized["summary"]["filtered_commission"] = round(
        sum(float(n.get("commission_amount") or 0.0) for n in filtered_active), 2
    )
    summarized["summary"]["filtered_commission_usd"] = round(
        sum(float(n.get("commission_usd") or 0.0) for n in filtered_active), 2
    )
    summarized["summary"]["filtered_commission_bs_usd_equiv"] = round(
        sum(float(n.get("commission_bs_usd_equiv") or 0.0) for n in filtered_active), 2
    )
    summarized["summary"]["filtered_commission_bs"] = round(
        sum(float(n.get("commission_bs") or 0.0) for n in filtered_active), 2
    )
    summarized["summary"]["filtered_margin"] = round(
        sum(float(n.get("margin_amount") or 0.0) for n in filtered_active), 2
    )
    summarized["summary"]["filtered_cost"] = round(
        sum(float(n.get("cost_total") or 0.0) for n in filtered_active), 2
    )
    summarized["summary"]["filtered_paid_commission"] = round(
        sum(float(n.get("commission_amount") or 0.0) for n in filtered_active if n.get("ve_ne_estado") == "Pagada"),
        2,
    )
    summarized["summary"]["filtered_pending_commission"] = round(
        sum(float(n.get("commission_amount") or 0.0) for n in filtered_active if n.get("ve_ne_estado") != "Pagada"),
        2,
    )
    summarized["commission_by_seller"] = summarize_seller_commissions(summarized["notes"], current_bs_rate=current_bs_rate)
    summarized["clients"] = summarize_client_delivery_notes(summarized["notes"])

    iva_rate, igtf_rate = _get_tax_rates()
    modes = frappe.get_all("Mode of Payment", filters={"enabled": 1}, pluck="name", order_by="name asc")
    payment_options = modes or ["Efectivo USD", "Efectivo Bs", "Contado USD", "Pago Móvil / Bs", "Cashea", "Punto de Venta", "Zelle", "Binance Pay", "Transferencia Bancaria"]
    for default_m in ["Efectivo USD", "Efectivo Bs", "Contado USD", "Pago Móvil / Bs", "Cashea", "Punto de Venta", "Zelle", "Binance Pay", "Transferencia Bancaria"]:
        if default_m not in payment_options:
            payment_options.append(default_m)

    if is_admin:
        sellers = sorted({n.get("seller_user") for n in rows if n.get("seller_user")})
        seller_options = [
            {"user": u, "fullname": frappe.utils.get_fullname(u) or u}
            for u in sellers
        ]
    else:
        seller_options = [
            {"user": current_user, "fullname": frappe.utils.get_fullname(current_user) or current_user}
        ]

    summarized["config"] = {
        "is_admin": is_admin,
        "current_user": current_user,
        "iva_rate": iva_rate,
        "igtf_rate": igtf_rate,
        "payment_methods": payment_options,
        "payment_method_currencies": {m: get_payment_method_currency(m) for m in payment_options},
        "sellers": seller_options,
        "exchange_rate": fx_snapshot,
        "commission_enabled": comm_enabled,
        "commission_rules": comm_rules,
    }
    return summarized


@whitelist()
def send_overdue_delivery_notes_report() -> dict:
    """Dispara manualmente el reporte por correo de Notas de Entrega vencidas sin pago completo (solo System Manager)."""
    import frappe
    from cima_ve.tasks import send_monday_overdue_delivery_notes_alert

    frappe.only_for("System Manager")
    return send_monday_overdue_delivery_notes_alert(force=True)


@whitelist()
def calculate_seller_commissions(
    status: str = "",
    from_date: str = "",
    to_date: str = "",
    seller: str = "",
    search: str = "",
    persist_missing: int | bool = 1,
) -> dict:
    """Calcula (y opcionalmente guarda en Notas de Entrega históricas) el resumen de comisiones por vendedor."""
    import frappe

    frappe.only_for("System Manager")

    data = get_delivery_notes(
        status=status,
        from_date=from_date,
        to_date=to_date,
        seller=seller,
        search=search,
    )
    notes = data.get("notes") or []
    should_persist = bool(int(persist_missing)) if isinstance(persist_missing, (int, str)) else bool(persist_missing)
    persisted_count = 0

    if should_persist:
        dn_meta = frappe.get_meta("Delivery Note")
        can_write = all(
            dn_meta.has_field(f)
            for f in (
                "ve_ne_comision_opcion",
                "ve_ne_comision_pct",
                "ve_ne_costo_total",
                "ve_ne_margen_ganancia",
                "ve_ne_comision_monto",
            )
        )
        if can_write:
            for n in notes:
                if n.get("_commission_computed_fallback") and n.get("ve_ne_estado") != "Cancelada":
                    frappe.db.set_value(
                        "Delivery Note",
                        n["name"],
                        {
                            "ve_ne_comision_opcion": n.get("commission_option") or "",
                            "ve_ne_comision_pct": float(n.get("commission_pct") or 0.0),
                            "ve_ne_costo_total": float(n.get("cost_total") or 0.0),
                            "ve_ne_margen_ganancia": float(n.get("margin_amount") or 0.0),
                            "ve_ne_comision_monto": float(n.get("commission_amount") or 0.0),
                        },
                        update_modified=False,
                    )
                    persisted_count += 1
            if persisted_count:
                frappe.db.commit()

    by_seller = data.get("commission_by_seller") or []
    summary = data.get("summary") or {}
    return {
        "ok": True,
        "persisted_count": persisted_count,
        "notes_count": len([n for n in notes if n.get("ve_ne_estado") != "Cancelada"]),
        "total_sales": round(sum(b["sales_total"] for b in by_seller), 2),
        "total_cost": round(sum(b["cost_total"] for b in by_seller), 2),
        "total_margin": round(sum(b["margin_total"] for b in by_seller), 2),
        "total_commission": round(sum(b["commission_total"] for b in by_seller), 2),
        "paid_commission": round(sum(b["paid_commission"] for b in by_seller), 2),
        "pending_commission": round(sum(b["pending_commission"] for b in by_seller), 2),
        "by_seller": by_seller,
        "summary": summary,
    }


@whitelist()
def cancel_delivery_note(delivery_note: str, reason: str = "") -> dict:
    """Cancela una Nota de Entrega pendiente, devolviendo el inventario automáticamente y registrando fecha/usuario."""
    import frappe
    from frappe.utils import now_datetime

    frappe.only_for("System Manager")

    dn = frappe.get_doc("Delivery Note", delivery_note)
    if dn.docstatus == 2 or getattr(dn, "ve_ne_estado", "") == "Cancelada":
        frappe.throw("Esta Nota de Entrega ya está cancelada.")
    if dn.docstatus != 1:
        frappe.throw("Solo se pueden cancelar Notas de Entrega confirmadas.")
    if (
        getattr(dn, "ve_ne_estado", "") in ("Pagada", "Pago Parcial")
        or getattr(dn, "ve_ne_factura", "")
        or float(getattr(dn, "ve_ne_monto_pagado", 0) or 0) > 0
    ):
        frappe.throw("Esta Nota de Entrega ya tiene pagos registrados o factura asociada.")

    resolved_at = str(now_datetime())
    resolved_by = frappe.session.user
    clean_reason = (reason or "").strip() or "Cancelada desde Notas de Entrega"

    dn.flags.ignore_permissions = True
    dn.cancel()
    dn.db_set("ve_ne_estado", "Cancelada")
    dn.db_set("ve_ne_fecha_resolucion", resolved_at)
    dn.db_set("ve_ne_usuario_resolucion", resolved_by)
    dn.db_set("ve_ne_motivo_cancelacion", clean_reason)
    dn.add_comment("Comment", f"Nota de Entrega CANCELADA por {resolved_by} el {resolved_at}. Motivo: {clean_reason}. Inventario restaurado.")

    return {
        "ok": True,
        "delivery_note": dn.name,
        "ve_ne_estado": "Cancelada",
        "fecha_resolucion": resolved_at,
        "usuario_resolucion": resolved_by,
    }


@whitelist()
def revert_delivery_note(delivery_note: str, reason: str = "") -> dict:
    """Revierte una Nota de Entrega (pendiente, pago parcial o pagada), cancelando pagos/factura y devolviendo inventario."""
    import frappe
    from frappe.utils import now_datetime
    from cima_ve.services.delivery_note_service import parse_payment_history

    frappe.only_for("System Manager")

    dn = frappe.get_doc("Delivery Note", delivery_note)
    if dn.docstatus == 2 or getattr(dn, "ve_ne_estado", "") == "Cancelada":
        frappe.throw("Esta Nota de Entrega ya está cancelada.")
    if dn.docstatus != 1:
        frappe.throw("Solo se pueden revertir Notas de Entrega confirmadas.")

    resolved_at = str(now_datetime())
    resolved_by = frappe.session.user
    clean_reason = (reason or "").strip() or "Revertida desde Notas de Entrega"

    reverted_payment_entries: list[str] = []
    reverted_invoice = ""
    si_name = (getattr(dn, "ve_ne_factura", "") or "").strip()
    si = None
    pe_names: list[str] = []

    if si_name:
        si = frappe.get_doc("Sales Invoice", si_name)
        fiscal_ctrl = (getattr(si, "ve_numero_control", "") or "").strip()
        if fiscal_ctrl:
            frappe.throw(
                f"La factura {si.name} tiene número de control fiscal ({fiscal_ctrl}). Emite una Nota de Crédito en lugar de revertir."
            )

        linked_pes = frappe.get_all(
            "Payment Entry Reference",
            filters={
                "reference_doctype": "Sales Invoice",
                "reference_name": si.name,
            },
            pluck="parent",
            limit_page_length=0,
        )
        pe_names.extend([p for p in (linked_pes or []) if p])

    hist_raw = getattr(dn, "ve_ne_historial_pagos", "") if dn.meta.has_field("ve_ne_historial_pagos") else ""
    for entry in parse_payment_history(hist_raw):
        pe_id = (entry.get("payment_entry") or "").strip()
        if pe_id:
            pe_names.append(pe_id)

    seen_pes: set[str] = set()
    for pe_name in pe_names:
        if pe_name in seen_pes:
            continue
        seen_pes.add(pe_name)
        pe = frappe.get_doc("Payment Entry", pe_name)
        if pe.docstatus == 1:
            pe.flags.ignore_permissions = True
            pe.cancel()
            reverted_payment_entries.append(pe.name)

    if si_name:
        # Recargar la factura tras cancelar los Payment Entries para evitar TimestampMismatchError
        si = frappe.get_doc("Sales Invoice", si_name)
    if si and si.docstatus == 1:
        si.flags.ignore_permissions = True
        si.cancel()
        reverted_invoice = si.name

    dn = frappe.get_doc("Delivery Note", delivery_note)
    dn.flags.ignore_permissions = True
    dn.cancel()
    dn.db_set("ve_ne_estado", "Cancelada")
    dn.db_set("ve_ne_fecha_resolucion", resolved_at)
    dn.db_set("ve_ne_usuario_resolucion", resolved_by)
    dn.db_set("ve_ne_motivo_cancelacion", clean_reason)
    if dn.meta.has_field("ve_ne_monto_pagado"):
        dn.db_set("ve_ne_monto_pagado", 0.0)
    if dn.meta.has_field("ve_ne_saldo_pendiente"):
        dn.db_set("ve_ne_saldo_pendiente", 0.0)
    if dn.meta.has_field("ve_ne_monto_pagado_bs"):
        dn.db_set("ve_ne_monto_pagado_bs", 0.0)

    extra_parts = []
    if reverted_invoice:
        extra_parts.append(f"Factura revertida: {reverted_invoice}")
    if reverted_payment_entries:
        extra_parts.append(f"Pagos revertidos: {', '.join(reverted_payment_entries)}")
    extra_txt = f" ({'; '.join(extra_parts)})" if extra_parts else ""

    dn.add_comment(
        "Comment",
        f"Nota de Entrega REVERTIDA por {resolved_by} el {resolved_at}. Motivo: {clean_reason}. Inventario restaurado.{extra_txt}",
    )

    return {
        "ok": True,
        "delivery_note": dn.name,
        "ve_ne_estado": "Cancelada",
        "reverted_invoice": reverted_invoice,
        "reverted_payment_entries": reverted_payment_entries,
        "fecha_resolucion": resolved_at,
        "usuario_resolucion": resolved_by,
    }


@whitelist()
def purge_cancelled_delivery_notes(include_orphan_sales_docs: bool = False) -> dict:
    """Elimina permanentemente Notas de Entrega ya canceladas (docstatus=2) y sus facturas/pagos cancelados vinculados."""
    import frappe
    from cima_ve.services.delivery_note_service import parse_payment_history

    frappe.only_for("System Manager")

    cancelled_dns = frappe.get_all(
        "Delivery Note",
        filters={"docstatus": 2},
        fields=["name", "ve_ne_factura", "ve_ne_historial_pagos"],
        limit_page_length=500,
    )

    deleted_pes: list[str] = []
    deleted_invoices: list[str] = []
    deleted_orders: list[str] = []
    deleted_dns: list[str] = []
    errors: list[dict] = []

    seen_pes: set[str] = set()
    seen_invoices: set[str] = set()
    seen_orders: set[str] = set()

    for row in cancelled_dns or []:
        hist_raw = row.get("ve_ne_historial_pagos") or ""
        for entry in parse_payment_history(hist_raw):
            pe_name = (entry.get("payment_entry") or "").strip()
            if pe_name and pe_name not in seen_pes:
                seen_pes.add(pe_name)
                try:
                    pe = frappe.get_doc("Payment Entry", pe_name)
                    if pe.docstatus == 2:
                        frappe.delete_doc("Payment Entry", pe_name, force=True, ignore_permissions=True)
                        deleted_pes.append(pe_name)
                except Exception as exc:
                    errors.append({"doctype": "Payment Entry", "name": pe_name, "error": str(exc)})

        si_name = (row.get("ve_ne_factura") or "").strip()
        if si_name and si_name not in seen_invoices:
            seen_invoices.add(si_name)
            try:
                si = frappe.get_doc("Sales Invoice", si_name)
                if si.docstatus == 2:
                    frappe.delete_doc("Sales Invoice", si_name, force=True, ignore_permissions=True)
                    deleted_invoices.append(si_name)
            except Exception as exc:
                errors.append({"doctype": "Sales Invoice", "name": si_name, "error": str(exc)})

    for row in cancelled_dns or []:
        dn_name = row["name"]
        try:
            frappe.delete_doc("Delivery Note", dn_name, force=True, ignore_permissions=True)
            deleted_dns.append(dn_name)
        except Exception as exc:
            errors.append({"doctype": "Delivery Note", "name": dn_name, "error": str(exc)})

    if include_orphan_sales_docs and str(include_orphan_sales_docs).lower() not in {"0", "false", "no"}:
        for dt, seen, bucket in [
            ("Payment Entry", seen_pes, deleted_pes),
            ("Sales Invoice", seen_invoices, deleted_invoices),
            ("Sales Order", seen_orders, deleted_orders),
        ]:
            cancelled_rows = frappe.get_all(
                dt,
                filters={"docstatus": 2},
                pluck="name",
                limit_page_length=500,
            )
            for doc_name in cancelled_rows or []:
                if doc_name in seen:
                    continue
                seen.add(doc_name)
                try:
                    frappe.delete_doc(dt, doc_name, force=True, ignore_permissions=True)
                    bucket.append(doc_name)
                except Exception as exc:
                    errors.append({"doctype": dt, "name": doc_name, "error": str(exc)})

    return {
        "ok": len(errors) == 0,
        "deleted_delivery_notes": deleted_dns,
        "deleted_invoices": deleted_invoices,
        "deleted_sales_orders": deleted_orders,
        "deleted_payment_entries": deleted_pes,
        "errors": errors,
    }


def _can_user_settle_delivery_note(dn, user: str) -> bool:
    """Admins pueden cobrar cualquier Nota de Entrega; los vendedores solo las suyas."""
    import frappe

    clean_user = (user or "").strip()
    if not clean_user or clean_user == "Guest":
        return False
    if clean_user == "Administrator" or "System Manager" in frappe.get_roles(clean_user):
        return True
    seller_user = (getattr(dn, "ve_vendedor_usuario", None) or "").strip() or (getattr(dn, "owner", None) or "").strip()
    return bool(seller_user and seller_user.lower() == clean_user.lower())


@whitelist()
def upload_delivery_note_payment_proof(delivery_note: str, file_name: str, file_data_base64: str) -> dict:
    """Sube una captura o comprobante de pago para una Nota de Entrega propia (o cualquiera si es admin)."""
    import base64
    import os
    import frappe

    current_user = frappe.session.user
    if not current_user or current_user == "Guest":
        frappe.throw("Debes iniciar sesión para subir comprobantes.", frappe.PermissionError)

    dn = frappe.get_doc("Delivery Note", delivery_note)
    if not _can_user_settle_delivery_note(dn, current_user):
        frappe.throw("Solo puedes subir comprobantes para tus propias Notas de Entrega.", frappe.PermissionError)

    clean_name = os.path.basename((file_name or "comprobante.png").strip()) or "comprobante.png"
    ext = os.path.splitext(clean_name)[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"}:
        frappe.throw("Formato no soportado. Sube una imagen (PNG, JPG, WEBP) o PDF.")

    raw_b64 = (file_data_base64 or "").strip()
    if "," in raw_b64 and raw_b64.startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[1]
    if not raw_b64:
        frappe.throw("El archivo está vacío.")

    try:
        content = base64.b64decode(raw_b64)
    except Exception:
        frappe.throw("No se pudo decodificar el archivo adjunto.")

    if len(content) > 5 * 1024 * 1024:
        frappe.throw("El comprobante supera el tamaño máximo permitido de 5 MB.")

    is_admin_or_sysmgr = current_user == "Administrator" or "System Manager" in frappe.get_roles(current_user)
    if not is_admin_or_sysmgr:
        frappe.set_user("Administrator")
    try:
        file_doc = frappe.get_doc(
            {
                "doctype": "File",
                "file_name": clean_name,
                "attached_to_doctype": "Delivery Note",
                "attached_to_name": dn.name,
                "is_private": 1,
                "content": content,
            }
        )
        file_doc.flags.ignore_permissions = True
        file_doc.insert(ignore_permissions=True)
        dn_meta = getattr(dn, "meta", None)
        if dn_meta and dn_meta.has_field("ve_ne_comprobante_url"):
            frappe.db.set_value("Delivery Note", dn.name, "ve_ne_comprobante_url", file_doc.file_url)
    finally:
        if not is_admin_or_sysmgr:
            frappe.set_user(current_user)

    return {
        "file_id": file_doc.name,
        "file_url": file_doc.file_url,
        "file_name": file_doc.file_name,
    }


@whitelist()
def mark_delivery_note_paid(
    delivery_note: str,
    payment_method: str,
    apply_iva: int | bool = 0,
    apply_igtf: int | bool = 0,
    reference: str = "",
    paid_amount: float | str | None = None,
    screenshot_url: str = "",
) -> dict:
    """Bloqueado: los pagos ahora se registran directamente por cliente (FIFO sobre las notas más antiguas)."""
    import frappe

    frappe.throw(
        "El pago individual por Nota de Entrega está deshabilitado. "
        "Registra el pago directamente por cliente para aplicarlo a sus notas más antiguas."
    )


@whitelist()
def register_client_payment(
    customer: str,
    payment_method: str,
    paid_amount: float | str,
    apply_iva: int | bool = 0,
    apply_igtf: int | bool = 0,
    reference: str = "",
    screenshot_url: str = "",
    seller: str = "",
) -> dict:
    """Registra un pago de cliente y lo distribuye FIFO en sus Notas de Entrega pendientes más antiguas."""
    import frappe

    current_user = frappe.session.user
    if not current_user or current_user == "Guest":
        frappe.throw("Debes iniciar sesión para registrar pagos.", frappe.PermissionError)

    clean_customer = (customer or "").strip()
    if not clean_customer:
        frappe.throw("Debes seleccionar un cliente.")

    is_admin = current_user == "Administrator" or "System Manager" in frappe.get_roles(current_user)
    notes_data = get_delivery_notes(
        status="Pendiente",
        seller=seller if is_admin else "",
    )
    client_notes = [
        n for n in (notes_data.get("notes") or [])
        if str(n.get("customer") or "").strip() == clean_customer
    ]

    try:
        req_amt = float(paid_amount)
    except (TypeError, ValueError):
        frappe.throw("El monto a pagar debe ser un número válido.")

    try:
        plan = allocate_client_payment_fifo(client_notes, req_amt)
    except ValueError as exc:
        frappe.throw(str(exc))

    results: list[dict] = []
    if not is_admin:
        frappe.set_user("Administrator")
    try:
        for alloc in plan["allocations"]:
            dn_doc = frappe.get_doc("Delivery Note", alloc["delivery_note"])
            res = _mark_delivery_note_paid_impl(
                dn=dn_doc,
                resolved_by=current_user,
                payment_method=payment_method,
                apply_iva=apply_iva,
                apply_igtf=apply_igtf,
                reference=reference,
                paid_amount=alloc["allocated_amount"],
                screenshot_url=screenshot_url,
            )
            results.append(res)
    finally:
        if not is_admin:
            frappe.set_user(current_user)

    customer_label = clean_customer
    if client_notes:
        customer_label = str(client_notes[0].get("customer_name") or clean_customer).strip() or clean_customer

    whatsapp_notif = {"whatsapp_sent": False, "chat_id": DEFAULT_WHATSAPP_PAYMENTS_CHAT_ID}
    try:
        whatsapp_notif = send_client_payment_whatsapp_notification(
            customer=clean_customer,
            customer_name=customer_label,
            payment_amount=plan["payment_amount"],
            payment_method=payment_method,
            reference=reference,
            registered_by=current_user,
            allocations=plan["allocations"],
            results=results,
            total_pending_after=plan["total_pending_after"],
            screenshot_url=screenshot_url,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Cima VE Payment WhatsApp Notification Error")

    return {
        "ok": True,
        "customer": clean_customer,
        "payment_amount": plan["payment_amount"],
        "total_pending_before": plan["total_pending_before"],
        "total_pending_after": plan["total_pending_after"],
        "allocations": plan["allocations"],
        "results": results,
        "whatsapp_notification": whatsapp_notif,
    }


@whitelist()
def get_delivery_note_document(
    delivery_note: str = "",
    customer: str = "",
    status_filter: str = "active",
) -> dict:
    """Genera el documento HTML imprimible (tipo factura) para una Nota de Entrega o todas las de un cliente."""
    import frappe
    from frappe.utils import today

    current_user = frappe.session.user
    if not current_user or current_user == "Guest":
        frappe.throw("Debes iniciar sesión para generar documentos.", frappe.PermissionError)

    dn_name = (delivery_note or "").strip()
    cust_name = (customer or "").strip()
    if not dn_name and not cust_name:
        frappe.throw("Debes indicar una Nota de Entrega o un Cliente.")

    data = get_delivery_notes()
    all_notes = data.get("notes") or []

    if dn_name:
        selected = [n for n in all_notes if n.get("name") == dn_name]
        if not selected:
            frappe.throw("No tienes acceso o no existe la Nota de Entrega solicitada.", frappe.PermissionError)
        cust_name = str(selected[0].get("customer") or cust_name)
    else:
        selected = [n for n in all_notes if str(n.get("customer") or "").strip() == cust_name]
        if status_filter == "pending":
            selected = [n for n in selected if n.get("ve_ne_estado") in ("Pendiente", "Pago Parcial")]
        elif status_filter == "active":
            selected = [n for n in selected if n.get("ve_ne_estado") != "Cancelada"]
        if not selected:
            frappe.throw("No hay Notas de Entrega disponibles para este cliente.")

    selected.sort(
        key=lambda row: (
            str(row.get("posting_date") or ""),
            str(row.get("creation") or ""),
            str(row.get("name") or ""),
        )
    )

    cust_doc = frappe.db.get_value(
        "Customer",
        cust_name,
        ["customer_name", "tax_id", "ve_rif_cedula", "email_id", "mobile_no", "primary_address"],
        as_dict=True,
    ) or {}

    customer_info = {
        "customer": cust_name,
        "customer_name": cust_doc.get("customer_name") or selected[0].get("customer_name") or cust_name,
        "rif_cedula": cust_doc.get("ve_rif_cedula") or cust_doc.get("tax_id") or "",
        "email": cust_doc.get("email_id") or selected[0].get("ve_ne_cliente_email") or "",
        "phone": cust_doc.get("mobile_no") or selected[0].get("ve_ne_cliente_telefono") or "",
        "address": cust_doc.get("primary_address") or selected[0].get("ve_ne_envio_direccion") or "",
    }
    fx = (data.get("config") or {}).get("exchange_rate") or {}
    bs_rate = float(fx.get("adjusted_rate") or fx.get("bcv_rate") or 0.0)
    from urllib.parse import urlencode

    pdf_query = urlencode(
        {
            k: v
            for k, v in {
                "delivery_note": dn_name,
                "customer": "" if dn_name else cust_name,
                "status_filter": status_filter,
            }.items()
            if v
        }
    )
    pdf_download_url = f"/api/method/cima_ve.api.delivery_notes.download_delivery_note_pdf?{pdf_query}"
    doc_html = build_delivery_note_document_html(
        customer_info=customer_info,
        notes=selected,
        bs_rate=bs_rate,
        generated_at=str(today()),
        pdf_download_url=pdf_download_url,
    )
    return {
        "ok": True,
        "customer": cust_name,
        "notes_count": len(selected),
        "html": doc_html,
        "pdf_url": pdf_download_url,
    }


@whitelist()
def download_delivery_note_pdf(
    delivery_note: str = "",
    customer: str = "",
    status_filter: str = "active",
) -> None:
    """Genera y descarga el PDF de una Nota de Entrega o estado de cuenta de un cliente."""
    import frappe
    from frappe.utils.pdf import get_pdf

    res = get_delivery_note_document(
        delivery_note=delivery_note,
        customer=customer,
        status_filter=status_filter,
    )
    doc_html = res.get("html") or ""
    logo_rel = "/assets/cima_ve/images/cima-logo-color.png"
    try:
        logo_abs = frappe.utils.get_url(logo_rel)
        if logo_abs and logo_rel in doc_html:
            doc_html = doc_html.replace(logo_rel, logo_abs)
    except Exception:
        pass

    pdf_bytes = get_pdf(doc_html)
    dn_clean = (delivery_note or "").strip()
    cust_clean = (res.get("customer") or customer or "Cliente").strip().replace(" ", "_")
    filename = f"Nota_de_Entrega_{dn_clean}.pdf" if dn_clean else f"Notas_de_Entrega_{cust_clean}.pdf"

    frappe.local.response.filename = filename
    frappe.local.response.filecontent = pdf_bytes
    frappe.local.response.type = "pdf"


def _mark_delivery_note_paid_impl(
    dn,
    resolved_by: str,
    payment_method: str,
    apply_iva: int | bool = 0,
    apply_igtf: int | bool = 0,
    reference: str = "",
    paid_amount: float | str | None = None,
    screenshot_url: str = "",
) -> dict:
    import frappe
    from frappe.utils import now_datetime, today
    from cima_ve.api.seller_pos import _apply_required_sales_metadata
    if dn.docstatus != 1:
        frappe.throw("La Nota de Entrega no está activa (puede estar cancelada o en borrador).")
    if getattr(dn, "ve_ne_estado", "") == "Pagada":
        frappe.throw(f"Esta Nota de Entrega ya fue pagada en su totalidad (Factura {getattr(dn, 've_ne_factura', '')}).")

    clean_method = (payment_method or "").strip()
    if not clean_method:
        frappe.throw("Selecciona un método de pago.")

    is_cash = is_cash_payment_method(clean_method)
    clean_reference = "" if is_cash else (reference or "").strip()
    clean_screenshot = (screenshot_url or "").strip()

    requested_amount: float | None = None
    if paid_amount not in (None, ""):
        try:
            requested_amount = float(paid_amount)
        except (TypeError, ValueError):
            frappe.throw("El monto a pagar debe ser un número válido.")

    existing_invoice = (getattr(dn, "ve_ne_factura", "") or "").strip()
    iva_rate, igtf_rate = _get_tax_rates()

    if not existing_invoice:
        iva_flag = bool(int(apply_iva)) if isinstance(apply_iva, (int, str)) else bool(apply_iva)
        igtf_flag = bool(int(apply_igtf)) if isinstance(apply_igtf, (int, str)) else bool(apply_igtf)

        totals = compute_delivery_note_settlement_totals(
            net_total=float(dn.net_total or dn.grand_total or 0.0),
            apply_iva=iva_flag,
            apply_igtf=igtf_flag,
            iva_rate=iva_rate,
            igtf_rate=igtf_rate,
        )
        try:
            payment_state = compute_partial_payment_state(totals["grand_total"], 0.0, requested_amount)
        except ValueError as exc:
            frappe.throw(str(exc))

        from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

        si = make_sales_invoice(dn.name)
        si.update_stock = 0
        si.set("taxes", [])
        si.ve_igtf_aplicado = 1 if igtf_flag else 0

        seller_user = getattr(dn, "ve_vendedor_usuario", None) or dn.owner or frappe.session.user
        seller_name = getattr(dn, "ve_vendedor_nombre", None) or frappe.utils.get_fullname(seller_user) or seller_user
        si.ve_vendedor_usuario = seller_user
        si.ve_vendedor_nombre = seller_name

        if iva_flag and totals["iva_amount"] > 0:
            iva_acc = _resolve_tax_account(dn.company, "iva")
            if iva_acc:
                si.append(
                    "taxes",
                    {
                        "charge_type": "Actual",
                        "account_head": iva_acc,
                        "description": f"IVA ({iva_rate:g}%)",
                        "tax_amount": totals["iva_amount"],
                    },
                )

        if igtf_flag and totals["igtf_amount"] > 0:
            igtf_acc = _resolve_tax_account(dn.company, "igtf")
            if igtf_acc:
                si.append(
                    "taxes",
                    {
                        "charge_type": "Actual",
                        "account_head": igtf_acc,
                        "description": f"IGTF Divisas ({igtf_rate:g}%)",
                        "tax_amount": totals["igtf_amount"],
                    },
                )

        si.remarks = f"Liquidación Nota de Entrega {dn.name} | Método: {clean_method}" + (f" | Ref: {clean_reference}" if clean_reference else "")
        _apply_required_sales_metadata(
            si,
            sales_channel=getattr(dn, "canal_venta", "Detal") or "Detal",
            seller_user=seller_user,
        )
        si.flags.ignore_fiscal_stamp = True
        si.insert(ignore_permissions=True)
        si.submit()
    else:
        si = frappe.get_doc("Sales Invoice", existing_invoice)
        iva_flag = bool(getattr(dn, "ve_ne_aplica_iva", 0))
        igtf_flag = bool(getattr(dn, "ve_ne_aplica_igtf", 0))
        total_to_pay = round(float(si.grand_total or getattr(dn, "ve_ne_monto_total", 0) or 0.0), 2)
        already_paid_si = round(max(total_to_pay - float(si.outstanding_amount or 0.0), 0.0), 2)
        already_paid_dn = round(float(getattr(dn, "ve_ne_monto_pagado", 0) or 0.0), 2)
        already_paid = max(already_paid_si, already_paid_dn)
        try:
            payment_state = compute_partial_payment_state(total_to_pay, already_paid, requested_amount)
        except ValueError as exc:
            frappe.throw(str(exc))

    pay_amt = payment_state["payment_amount"]
    payment_entry_name = ""
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    pe = get_payment_entry("Sales Invoice", si.name)
    if frappe.db.exists("Mode of Payment", clean_method):
        pe.mode_of_payment = clean_method
    pe.paid_amount = pay_amt
    pe.received_amount = pay_amt
    if getattr(pe, "references", None):
        pe.references[0].allocated_amount = pay_amt
    pe.reference_no = (clean_reference or dn.name)[:140]
    pe.reference_date = today()
    pe.remarks = f"Pago Nota de Entrega {dn.name} ({clean_method}) — ${pay_amt:.2f}"
    pe.insert(ignore_permissions=True)
    pe.submit()
    payment_entry_name = pe.name

    resolved_at = str(now_datetime())
    new_estado = payment_state["estado"]
    total_to_pay = payment_state["total_to_pay"]
    new_paid_total = payment_state["new_paid_total"]
    new_remaining = payment_state["new_remaining"]

    fx = _get_exchange_rate_snapshot()
    current_bs_rate = float(fx.get("adjusted_rate") or fx.get("bcv_rate") or 0.0)
    existing_hist = getattr(dn, "ve_ne_historial_pagos", "") if dn.meta.has_field("ve_ne_historial_pagos") else ""
    from cima_ve.services.delivery_note_service import parse_payment_history

    prior_history = parse_payment_history(existing_hist)
    bs_calc = compute_historical_bs_payment(
        total_to_pay_usd=total_to_pay,
        paid_now_usd=pay_amt,
        current_bs_rate=current_bs_rate,
        existing_history=prior_history,
        is_fully_paid=payment_state["is_fully_paid"],
    )

    history_list, history_json = append_payment_history(
        existing_hist,
        {
            "amount": pay_amt,
            "amount_bs": bs_calc["paid_now_bs"],
            "exchange_rate": bs_calc["exchange_rate"],
            "method": clean_method,
            "reference": clean_reference,
            "screenshot_url": clean_screenshot,
            "paid_at": resolved_at,
            "user": resolved_by,
            "payment_entry": payment_entry_name,
            "paid_total_after": new_paid_total,
            "remaining_after": new_remaining,
        },
    )

    dn.db_set("ve_ne_estado", new_estado)
    dn.db_set("ve_ne_fecha_resolucion", resolved_at)
    dn.db_set("ve_ne_usuario_resolucion", resolved_by)
    dn.db_set("ve_ne_metodo_pago", clean_method)
    dn.db_set("ve_ne_aplica_iva", 1 if iva_flag else 0)
    dn.db_set("ve_ne_aplica_igtf", 1 if igtf_flag else 0)
    dn.db_set("ve_ne_factura", si.name)
    if clean_screenshot and dn.meta.has_field("ve_ne_comprobante_url"):
        dn.db_set("ve_ne_comprobante_url", clean_screenshot)
    if dn.meta.has_field("ve_ne_monto_total"):
        dn.db_set("ve_ne_monto_total", total_to_pay)
    if dn.meta.has_field("ve_ne_monto_pagado"):
        dn.db_set("ve_ne_monto_pagado", new_paid_total)
    if dn.meta.has_field("ve_ne_saldo_pendiente"):
        dn.db_set("ve_ne_saldo_pendiente", new_remaining)
    if dn.meta.has_field("ve_ne_historial_pagos"):
        dn.db_set("ve_ne_historial_pagos", history_json)
    if dn.meta.has_field("ve_ne_tasa_cambio") and bs_calc["exchange_rate"] > 0:
        dn.db_set("ve_ne_tasa_cambio", bs_calc["exchange_rate"])
    if dn.meta.has_field("ve_ne_monto_pagado_bs"):
        dn.db_set("ve_ne_monto_pagado_bs", bs_calc["paid_total_bs"])
    if dn.meta.has_field("ve_ne_monto_bs") and bs_calc["total_bs"] > 0:
        dn.db_set("ve_ne_monto_bs", bs_calc["total_bs"])

    bs_comment = (
        f" (Bs. {bs_calc['paid_now_bs']:,.2f} @ {bs_calc['exchange_rate']:.2f})"
        if bs_calc["paid_now_bs"] > 0
        else ""
    )
    comment_msg = (
        f"Pago registrado por {resolved_by} el {resolved_at}: "
        f"Abono ${pay_amt:.2f}{bs_comment} ({clean_method}) | "
        f"Acumulado: ${new_paid_total:.2f} / ${total_to_pay:.2f} | "
        f"Saldo pendiente: ${new_remaining:.2f} | Estado: {new_estado} | Factura: {si.name}"
        + (f" | Ref: {clean_reference}" if clean_reference else "")
        + (f" | Captura: {clean_screenshot}" if clean_screenshot else "")
        + (f" | Comprobante: {payment_entry_name}" if payment_entry_name else "")
    )
    dn.flags.ignore_permissions = True
    dn.add_comment("Comment", comment_msg)
    if si:
        si.flags.ignore_permissions = True
        si.add_comment("Comment", comment_msg)

    return {
        "ok": True,
        "delivery_note": dn.name,
        "sales_invoice": si.name,
        "payment_entry": payment_entry_name,
        "grand_total": total_to_pay,
        "paid_now": pay_amt,
        "paid_total": new_paid_total,
        "remaining_amount": new_remaining,
        "exchange_rate": bs_calc["exchange_rate"],
        "paid_now_bs": bs_calc["paid_now_bs"],
        "paid_total_bs": bs_calc["paid_total_bs"],
        "total_bs": bs_calc["total_bs"],
        "ve_ne_estado": new_estado,
        "fecha_resolucion": resolved_at,
        "usuario_resolucion": resolved_by,
        "metodo_pago": clean_method,
        "apply_iva": iva_flag,
        "apply_igtf": igtf_flag,
        "payment_history": history_list,
    }
