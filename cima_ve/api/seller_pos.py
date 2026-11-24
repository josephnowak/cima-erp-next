from __future__ import annotations

import json

from cima_ve.api._compat import whitelist
from cima_ve.services.seller_pos_service import (
    build_seller_invoice_summary,
    compute_custom_price_breakdown,
    validate_quick_invoice_input,
)


def _is_admin_user(user: str) -> bool:
    import frappe

    clean = (user or "").strip()
    if not clean or clean == "Guest":
        return False
    if clean == "Administrator":
        return True
    return "System Manager" in (frappe.get_roles(clean) or [])


def _fetch_enabled_sales_persons() -> list[dict]:
    import frappe

    sp_fields = ["name", "employee"]
    try:
        sp_meta = frappe.get_meta("Sales Person")
        if sp_meta and sp_meta.has_field("usuario"):
            sp_fields.append("usuario")
    except Exception:
        pass
    return (
        frappe.get_all(
            "Sales Person",
            filters={"enabled": 1, "is_group": 0},
            fields=sp_fields,
            order_by="name asc",
            limit_page_length=100,
        )
        or []
    )


def _match_sales_person_name(fullname: str, sales_persons: list[dict]) -> str:
    clean_full = (fullname or "").strip().lower()
    if not clean_full:
        return ""
    exact = next(
        (sp["name"] for sp in sales_persons if (sp.get("name") or "").strip().lower() == clean_full),
        "",
    )
    if exact:
        return exact
    first_token = clean_full.split()[0]
    if len(first_token) >= 3:
        return next(
            (sp["name"] for sp in sales_persons if (sp.get("name") or "").strip().lower() == first_token),
            "",
        )
    return ""


def _resolve_sales_person_for_user(
    target_user: str,
    sales_person: str = "",
    sales_persons: list[dict] | None = None,
) -> str:
    import frappe

    chosen_sp = (sales_person or "").strip()
    if chosen_sp and frappe.db.exists("Sales Person", chosen_sp):
        return chosen_sp

    sp_rows = sales_persons if sales_persons is not None else _fetch_enabled_sales_persons()
    clean_user = (target_user or "").strip()
    if clean_user:
        chosen_sp = next(
            (
                sp["name"]
                for sp in sp_rows
                if (sp.get("usuario") or "").strip().lower() == clean_user.lower()
            ),
            "",
        )
        if not chosen_sp:
            matched_emp = frappe.db.get_value("Employee", {"user_id": clean_user}, "name")
            if matched_emp:
                chosen_sp = next(
                    (sp["name"] for sp in sp_rows if sp.get("employee") == matched_emp),
                    "",
                )
        if not chosen_sp:
            full_name = (frappe.utils.get_fullname(clean_user) or "").strip()
            chosen_sp = _match_sales_person_name(full_name, sp_rows)

    sp_names = [sp["name"] for sp in sp_rows]
    if not chosen_sp:
        chosen_sp = next(
            (n for n in sp_names if "tienda" in n.lower() or "mostrador" in n.lower()),
            sp_names[0] if sp_names else "",
        )
    if not chosen_sp:
        any_sp = frappe.get_all("Sales Person", fields=["name"], limit_page_length=1)
        chosen_sp = any_sp[0]["name"] if any_sp else ""
    return chosen_sp


def _get_selectable_sellers(current_user: str, sales_persons: list[dict]) -> list[dict]:
    """Devuelve lista de usuarios vendedores seleccionables por administradores/caja."""
    import frappe

    excluded_users = {"Guest", "Administrator", "integration@api.com"}
    sp_by_user: dict[str, str] = {}
    sp_by_emp: dict[str, str] = {}
    for sp in sales_persons or []:
        sp_name = (sp.get("name") or "").strip()
        if not sp_name:
            continue
        sp_user = (sp.get("usuario") or "").strip().lower()
        if sp_user:
            sp_by_user[sp_user] = sp_name
        emp = (sp.get("employee") or "").strip()
        if emp:
            sp_by_emp[emp] = sp_name

    if sp_by_emp:
        for emp_row in (
            frappe.get_all(
                "Employee",
                filters={"name": ["in", list(sp_by_emp.keys())]},
                fields=["name", "user_id"],
                limit_page_length=200,
            )
            or []
        ):
            uid = (emp_row.get("user_id") or "").strip().lower()
            if uid and uid not in sp_by_user:
                sp_by_user[uid] = sp_by_emp[emp_row["name"]]

    users = (
        frappe.get_all(
            "User",
            filters={"enabled": 1, "user_type": "System User"},
            fields=["name", "full_name"],
            order_by="full_name asc",
            limit_page_length=100,
        )
        or []
    )

    def _build_entry(u_name: str, u_full: str) -> dict:
        key = u_name.strip().lower()
        full = (u_full or "").strip() or u_name
        sp_name = sp_by_user.get(key, "") or _match_sales_person_name(full, sales_persons)
        return {
            "user": u_name,
            "fullname": full,
            "sales_person": sp_name,
        }

    options: list[dict] = []
    seen: set[str] = set()
    if current_user and current_user != "Guest":
        cur_full = frappe.utils.get_fullname(current_user) or current_user
        options.append(_build_entry(current_user, cur_full))
        seen.add(current_user.strip().lower())

    others: list[dict] = []
    for row in users:
        u_name = (row.get("name") or "").strip()
        if not u_name or u_name in excluded_users:
            continue
        key = u_name.lower()
        if key in seen:
            continue
        seen.add(key)
        others.append(_build_entry(u_name, row.get("full_name") or u_name))

    others.sort(key=lambda item: (0 if item.get("sales_person") else 1, item["fullname"].lower()))
    options.extend(others)
    return options


def _resolve_effective_seller(
    requested_seller_user: str = "",
    requested_sales_person: str = "",
) -> tuple[str, str, str]:
    """Resuelve (seller_user, seller_fullname, sales_person).

    Solo Administrator / System Manager puede atribuir documentos a otro vendedor.
    """
    import frappe

    current_user = (getattr(frappe.session, "user", None) or "").strip()
    if not current_user or current_user == "Guest":
        frappe.throw("Debes iniciar sesión para emitir una Nota de Entrega.", frappe.PermissionError)

    req_user = (requested_seller_user or "").strip()
    req_sp = (requested_sales_person or "").strip()
    is_admin = _is_admin_user(current_user)

    target_user = current_user
    if req_user and req_user.lower() != current_user.lower():
        if not is_admin:
            frappe.throw(
                "Solo un administrador puede emitir documentos a nombre de otro vendedor.",
                frappe.PermissionError,
            )
        user_row = frappe.db.get_value(
            "User",
            req_user,
            ["name", "enabled", "full_name"],
            as_dict=True,
        )
        if not user_row or not int(user_row.get("enabled") or 0):
            frappe.throw(f"El vendedor seleccionado ({req_user}) no existe o está deshabilitado.")
        target_user = user_row["name"]
        seller_name = (user_row.get("full_name") or "").strip() or frappe.utils.get_fullname(target_user) or target_user
    else:
        seller_name = frappe.utils.get_fullname(target_user) or target_user

    if req_sp and not is_admin:
        req_sp = ""

    resolved_sp = _resolve_sales_person_for_user(target_user, sales_person=req_sp)
    return target_user, seller_name, resolved_sp


@whitelist()
def get_seller_bootstrap() -> dict:
    """Devuelve configuración inicial para la interfaz simplificada de vendedores."""
    import frappe

    companies = frappe.get_all(
        "Company",
        fields=["name", "default_currency"],
        order_by="creation asc",
        limit_page_length=5,
    )
    default_company = companies[0]["name"] if companies else ""

    price_lists = frappe.get_all(
        "Price List",
        filters={"selling": 1, "enabled": 1},
        fields=["name", "currency"],
        order_by="name asc",
        limit_page_length=50,
    )
    warehouses = frappe.get_all(
        "Warehouse",
        filters={"is_group": 0, "disabled": 0},
        fields=["name", "warehouse_name", "company"],
        order_by="name asc",
        limit_page_length=50,
    )

    preferred_pl = next(
        (pl["name"] for pl in price_lists if "detal usd" in pl["name"].lower()),
        price_lists[0]["name"] if price_lists else "Standard Selling",
    )

    current_user = frappe.session.user
    current_user_fullname = frappe.utils.get_fullname(current_user) or current_user
    is_admin = _is_admin_user(current_user)

    sales_persons = _fetch_enabled_sales_persons()
    sales_person_names = [sp["name"] for sp in sales_persons]
    default_sales_person = _resolve_sales_person_for_user(current_user, sales_persons=sales_persons)
    seller_options = _get_selectable_sellers(current_user, sales_persons) if is_admin else []

    sales_channels: list[str] = []
    si_meta = frappe.get_meta("Sales Invoice")
    if si_meta.has_field("canal_venta"):
        raw_opts = si_meta.get_field("canal_venta").options or ""
        sales_channels = [opt.strip() for opt in raw_opts.splitlines() if opt.strip()]
    default_sales_channel = next(
        (ch for ch in sales_channels if ch.lower() == "detal"),
        sales_channels[0] if sales_channels else "",
    )

    from cima_ve.services.delivery_note_service import (
        DEFAULT_AUTO_DISCOUNT_TIERS,
        DEFAULT_COMMISSION_RULES,
        parse_auto_discount_tiers,
        parse_commission_rules,
    )

    auto_discount_enabled = True
    auto_discount_tiers = [dict(t) for t in DEFAULT_AUTO_DISCOUNT_TIERS]
    commission_enabled = True
    commission_rules = [dict(r) for r in DEFAULT_COMMISSION_RULES]
    try:
        settings = frappe.get_single("Cima VE Settings")
        if settings.meta.has_field("auto_discount_enabled"):
            auto_discount_enabled = bool(int(settings.auto_discount_enabled or 0))
        if settings.meta.has_field("auto_discount_tiers"):
            auto_discount_tiers = parse_auto_discount_tiers(settings.auto_discount_tiers)
        if settings.meta.has_field("commission_enabled"):
            commission_enabled = bool(int(settings.commission_enabled or 0))
        if settings.meta.has_field("commission_rules"):
            commission_rules = parse_commission_rules(settings.commission_rules)
    except Exception:
        pass

    exchange_rate = {"bcv_rate": 0.0, "adjusted_rate": 0.0, "applied_brecha_pct": 0.0}
    try:
        from cima_ve.api.exchange_rates import get_current_exchange_rates

        exchange_rate = get_current_exchange_rates()
    except Exception:
        pass

    return {
        "company": default_company,
        "current_user": current_user,
        "current_user_fullname": current_user_fullname,
        "is_admin": is_admin,
        "seller_options": seller_options,
        "default_price_list": preferred_pl,
        "price_lists": price_lists,
        "warehouses": warehouses,
        "default_warehouse": warehouses[0]["name"] if warehouses else "",
        "sales_persons": sales_person_names,
        "default_sales_person": default_sales_person,
        "sales_channels": sales_channels,
        "default_sales_channel": default_sales_channel,
        "auto_discount_enabled": auto_discount_enabled,
        "auto_discount_tiers": auto_discount_tiers,
        "commission_enabled": commission_enabled,
        "commission_rules": commission_rules,
        "exchange_rate": exchange_rate,
    }


@whitelist()
def search_inventory_and_prices(
    query: str = "",
    price_list: str = "",
    warehouse: str = "",
    limit: int = 60,
) -> list[dict]:
    """Busca productos con su stock disponible y precio según la lista seleccionada."""
    import frappe

    limit = min(max(int(limit or 60), 1), 200)
    search_like = f"%{(query or '').strip()}%"

    conditions = ["i.disabled = 0", "i.is_sales_item = 1"]
    params: dict = {
        "search": search_like,
        "price_list": price_list or "",
        "warehouse": warehouse or "",
        "limit": limit,
    }

    if (query or "").strip():
        conditions.append(
            "(i.item_code LIKE %(search)s OR i.item_name LIKE %(search)s OR i.item_group LIKE %(search)s)"
        )

    warehouse_filter = "AND b.warehouse = %(warehouse)s" if warehouse else ""
    where_clause = " AND ".join(conditions)

    rows = frappe.db.sql(
        f"""
        SELECT
            i.item_code,
            i.item_name,
            i.item_group,
            COALESCE(SUM(b.actual_qty), 0) AS stock_qty,
            COALESCE(MAX(ip.price_list_rate), i.standard_rate, 0) AS price,
            COALESCE(MAX(ip.currency), 'USD') AS currency,
            COALESCE(%(warehouse)s, '') AS warehouse,
            %(price_list)s AS price_list
        FROM `tabItem` i
        LEFT JOIN `tabBin` b
            ON b.item_code = i.item_code {warehouse_filter}
        LEFT JOIN `tabItem Price` ip
            ON ip.item_code = i.item_code
            AND ip.selling = 1
            AND (%(price_list)s = '' OR ip.price_list = %(price_list)s)
        WHERE {where_clause}
        GROUP BY i.item_code, i.item_name, i.item_group, i.standard_rate
        ORDER BY stock_qty DESC, i.item_name ASC
        LIMIT %(limit)s
        """,
        params,
        as_dict=True,
    )
    return rows


@whitelist()
def search_customers(query: str = "", limit: int = 20) -> list[dict]:
    """Busca clientes por nombre, código, correo o RIF/Cédula."""
    import frappe

    q = (query or "").strip()
    limit = min(max(int(limit or 20), 1), 50)
    if not q:
        return frappe.get_all(
            "Customer",
            filters={"disabled": 0},
            fields=["name", "customer_name", "tax_id", "ve_rif_cedula", "email_id"],
            order_by="modified desc",
            limit_page_length=limit,
        )

    like = f"%{q}%"
    return frappe.db.sql(
        """
        SELECT name, customer_name, tax_id, ve_rif_cedula, email_id
        FROM `tabCustomer`
        WHERE disabled = 0
          AND (
              name LIKE %(q)s
              OR customer_name LIKE %(q)s
              OR COALESCE(tax_id, '') LIKE %(q)s
              OR COALESCE(ve_rif_cedula, '') LIKE %(q)s
              OR COALESCE(email_id, '') LIKE %(q)s
          )
        ORDER BY customer_name ASC
        LIMIT %(limit)s
        """,
        {"q": like, "limit": limit},
        as_dict=True,
    )


@whitelist()
def quick_create_customer(
    customer_name: str,
    rif_cedula: str,
    taxpayer_type: str = "No Contribuyente",
    email: str = "",
) -> dict:
    """Crea un cliente rápido desde la pantalla de facturación (con correo obligatorio)."""
    import frappe

    name_clean = (customer_name or "").strip()
    rif_clean = (rif_cedula or "").strip().upper()
    email_clean = (email or "").strip().lower()
    if not name_clean:
        frappe.throw("El nombre o razón social del cliente es obligatorio.")
    if not rif_clean:
        frappe.throw("La Cédula o RIF del cliente es obligatoria.")
    if not email_clean or "@" not in email_clean or "." not in email_clean.split("@")[-1]:
        frappe.throw("El correo electrónico del cliente es obligatorio y debe ser válido.")

    doc = frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": name_clean,
            "customer_type": "Individual" if rif_clean.startswith(("V", "E")) else "Company",
            "tax_id": rif_clean,
            "ve_rif_cedula": rif_clean,
            "ve_taxpayer_type": taxpayer_type or "No Contribuyente",
            "email_id": email_clean,
        }
    )
    doc.insert()
    try:
        contact = frappe.get_doc(
            {
                "doctype": "Contact",
                "first_name": name_clean[:140],
                "email_ids": [{"email_id": email_clean, "is_primary": 1}],
                "links": [{"link_doctype": "Customer", "link_name": doc.name}],
            }
        )
        contact.insert(ignore_permissions=True)
        doc.db_set("customer_primary_contact", contact.name, update_modified=False)
    except Exception:
        pass

    return {
        "name": doc.name,
        "customer_name": doc.customer_name,
        "ve_rif_cedula": doc.ve_rif_cedula,
        "email_id": email_clean,
    }


def _resolve_company_and_currency(price_list: str = "") -> tuple[str, str]:
    import frappe

    companies = frappe.get_all(
        "Company",
        fields=["name", "default_currency"],
        order_by="creation asc",
        limit_page_length=1,
    )
    default_company = companies[0]["name"] if companies else ""
    currency = companies[0]["default_currency"] if companies and companies[0].get("default_currency") else "USD"
    if price_list:
        pl_currency = frappe.db.get_value("Price List", price_list, "currency")
        if pl_currency:
            currency = pl_currency
    return default_company, currency


def _get_fiscal_rates() -> tuple[float, float]:
    import frappe

    try:
        settings = frappe.get_single("Cima VE Settings")
        return float(settings.iva_rate or 16.0), float(settings.igtf_rate or 3.0)
    except Exception:
        return 16.0, 3.0


def _apply_required_sales_metadata(
    doc,
    sales_channel: str = "",
    sales_person: str = "",
    seller_user: str = "",
) -> None:
    """Completa campos personalizados/obligatorios (canal_venta, tipo_pedido, sales_team) si existen."""
    import frappe

    meta = frappe.get_meta(doc.doctype)

    if meta.has_field("canal_venta") and not doc.get("canal_venta"):
        raw_opts = meta.get_field("canal_venta").options or ""
        options = [opt.strip() for opt in raw_opts.splitlines() if opt.strip()]
        chosen = (sales_channel or "").strip()
        if options and chosen not in options:
            chosen = next((o for o in options if o.lower() == "detal"), options[0])
        if chosen:
            doc.set("canal_venta", chosen)

    if meta.has_field("tipo_pedido") and not doc.get("tipo_pedido"):
        raw_opts = meta.get_field("tipo_pedido").options or ""
        options = [opt.strip() for opt in raw_opts.splitlines() if opt.strip()]
        chosen_type = next((o for o in options if o.lower() == "venta"), options[0] if options else "Venta")
        doc.set("tipo_pedido", chosen_type)

    if meta.has_field("sales_team") and not doc.get("sales_team"):
        effective_user = (
            (seller_user or "").strip()
            or (getattr(doc, "ve_vendedor_usuario", None) or "").strip()
            or (getattr(frappe.session, "user", None) or "").strip()
        )
        chosen_sp = _resolve_sales_person_for_user(effective_user, sales_person=sales_person)
        if chosen_sp:
            doc.append("sales_team", {"sales_person": chosen_sp, "allocated_percentage": 100})


@whitelist()
def preview_cart_pricing(
    items: str | list[dict],
    customer: str = "",
    price_list: str = "",
    warehouse: str = "",
    apply_igtf: int | bool = False,
) -> dict:
    """Calcula precios, descuentos por cantidad (Pricing Rules) e impuestos para el carrito."""
    import frappe
    from erpnext.stock.get_item_details import get_item_details

    parsed_items = json.loads(items) if isinstance(items, str) else items
    if not parsed_items:
        return {
            "currency": "USD",
            "items": [],
            "tax_summary": build_seller_invoice_summary([], apply_igtf=False).to_dict(),
        }

    validated = validate_quick_invoice_input(customer or "PREVIEW", parsed_items)
    custom_price_map: dict[str, float] = {}
    for raw_row in parsed_items:
        code = str(raw_row.get("item_code") or "").strip()
        if code and raw_row.get("is_custom_price") and raw_row.get("rate") is not None:
            custom_price_map[code] = max(float(raw_row.get("rate") or 0.0), 0.0)
    apply_igtf_flag = bool(int(apply_igtf)) if isinstance(apply_igtf, (int, str)) else bool(apply_igtf)
    default_company, currency = _resolve_company_and_currency(price_list)
    iva_rate, igtf_rate = _get_fiscal_rates()

    priced_items: list[dict] = []
    if customer and frappe.db.exists("Customer", customer):
        doc_items = []
        for row in validated:
            item_dict = {"item_code": row.item_code, "qty": row.qty}
            if warehouse:
                item_dict["warehouse"] = warehouse
            doc_items.append(item_dict)

        doc = frappe.get_doc(
            {
                "doctype": "Sales Invoice",
                "customer": customer,
                "company": default_company or None,
                "currency": currency,
                "selling_price_list": price_list or None,
                "conversion_rate": 1,
                "plc_conversion_rate": 1,
                "ignore_pricing_rule": 0,
                "items": doc_items,
            }
        )
        doc.run_method("set_missing_values")
        doc.run_method("calculate_taxes_and_totals")

        for row in doc.items:
            qty = float(row.qty or 0)
            rate = float(row.rate or 0)
            price_list_rate = float(row.price_list_rate or rate)
            disc_pct = float(row.discount_percentage or 0)
            disc_amt = float(row.discount_amount or 0)
            is_custom = row.item_code in custom_price_map
            if is_custom:
                bd = compute_custom_price_breakdown(price_list_rate, custom_price_map[row.item_code])
                rate = bd["rate"]
                price_list_rate = bd["price_list_rate"]
                disc_pct = bd["discount_percentage"]
                disc_amt = bd["discount_amount"]
            has_discount = bool(disc_pct > 0 or disc_amt > 0 or (price_list_rate > 0 and rate < price_list_rate - 0.0001))
            priced_items.append(
                {
                    "item_code": row.item_code,
                    "item_name": row.item_name or row.item_code,
                    "qty": qty,
                    "price_list_rate": price_list_rate,
                    "discount_percentage": disc_pct,
                    "discount_amount": disc_amt,
                    "rate": rate,
                    "amount": round(qty * rate, 2),
                    "pricing_rules": "" if is_custom else (getattr(row, "pricing_rules", None) or ""),
                    "is_custom_price": is_custom,
                    "has_discount": has_discount,
                }
            )
    else:
        today = frappe.utils.today()
        for row in validated:
            details = get_item_details(
                {
                    "item_code": row.item_code,
                    "qty": row.qty,
                    "price_list": price_list or None,
                    "selling_price_list": price_list or None,
                    "warehouse": warehouse or None,
                    "company": default_company or None,
                    "doctype": "Sales Invoice",
                    "currency": currency,
                    "conversion_rate": 1,
                    "plc_conversion_rate": 1,
                    "transaction_date": today,
                    "ignore_pricing_rule": 0,
                }
            )
            rate = float(details.get("rate") if details.get("rate") is not None else row.unit_price)
            price_list_rate = float(details.get("price_list_rate") or rate)
            disc_pct = float(details.get("discount_percentage") or 0)
            disc_amt = float(details.get("discount_amount") or 0)
            is_custom = row.item_code in custom_price_map
            if is_custom:
                bd = compute_custom_price_breakdown(price_list_rate, custom_price_map[row.item_code])
                rate = bd["rate"]
                price_list_rate = bd["price_list_rate"]
                disc_pct = bd["discount_percentage"]
                disc_amt = bd["discount_amount"]
            has_discount = bool(disc_pct > 0 or disc_amt > 0 or (price_list_rate > 0 and rate < price_list_rate - 0.0001))
            priced_items.append(
                {
                    "item_code": row.item_code,
                    "item_name": details.get("item_name") or row.description,
                    "qty": row.qty,
                    "price_list_rate": price_list_rate,
                    "discount_percentage": disc_pct,
                    "discount_amount": disc_amt,
                    "rate": rate,
                    "amount": round(row.qty * rate, 2),
                    "pricing_rules": "" if is_custom else (details.get("pricing_rules") or ""),
                    "is_custom_price": is_custom,
                    "has_discount": has_discount,
                }
            )

    summary = build_seller_invoice_summary(
        priced_items,
        apply_igtf=apply_igtf_flag,
        iva_rate=iva_rate,
        igtf_rate=igtf_rate,
    )
    return {
        "currency": currency,
        "items": priced_items,
        "tax_summary": summary.to_dict(),
    }


@whitelist()
def create_quick_invoice(
    customer: str,
    items: str | list[dict],
    price_list: str = "",
    warehouse: str = "",
    payment_method: str = "Contado USD",
    apply_igtf: int | bool = False,
    submit: int | bool = True,
    sales_channel: str = "Detal",
    sales_person: str = "",
    seller_user: str = "",
) -> dict:
    """Crea y opcionalmente confirma una Sales Invoice desde la interfaz de vendedor."""
    import frappe
    from cima_ve.api.cashea import create_cashea_checkout

    parsed_items = json.loads(items) if isinstance(items, str) else items
    validated = validate_quick_invoice_input(customer, parsed_items)
    apply_igtf_flag = bool(int(apply_igtf)) if isinstance(apply_igtf, (int, str)) else bool(apply_igtf)
    submit_flag = bool(int(submit)) if isinstance(submit, (int, str)) else bool(submit)

    _, currency = _resolve_company_and_currency(price_list)
    iva_rate, igtf_rate = _get_fiscal_rates()

    invoice_items = []
    for row in validated:
        item_dict = {
            "item_code": row.item_code,
            "qty": row.qty,
        }
        if warehouse:
            item_dict["warehouse"] = warehouse
        invoice_items.append(item_dict)

    effective_seller_user, seller_name, resolved_sales_person = _resolve_effective_seller(
        requested_seller_user=seller_user,
        requested_sales_person=sales_person,
    )

    doc = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "customer": customer,
            "currency": currency,
            "selling_price_list": price_list or None,
            "update_stock": 1 if warehouse else 0,
            "ignore_pricing_rule": 0,
            "ve_igtf_aplicado": 1 if apply_igtf_flag else 0,
            "ve_vendedor_usuario": effective_seller_user,
            "ve_vendedor_nombre": seller_name,
            "remarks": f"Factura rápida vendedor ({seller_name}) | Método: {payment_method}",
            "items": invoice_items,
        }
    )
    _apply_required_sales_metadata(
        doc,
        sales_channel=sales_channel,
        sales_person=resolved_sales_person,
        seller_user=effective_seller_user,
    )
    doc.insert()
    if submit_flag:
        doc.submit()
        doc.reload()

    cashea_data = None
    if (payment_method or "").strip().lower() == "cashea":
        cashea_data = create_cashea_checkout(doc.name)
        doc.reload()

    actual_items = [
        {
            "item_code": row.item_code,
            "item_name": row.item_name or row.item_code,
            "qty": float(row.qty or 0),
            "rate": float(row.rate or 0),
        }
        for row in doc.items
    ]
    summary = build_seller_invoice_summary(
        actual_items or parsed_items,
        apply_igtf=apply_igtf_flag,
        iva_rate=iva_rate,
        igtf_rate=igtf_rate,
    )
    return {
        "invoice_name": doc.name,
        "status": doc.status,
        "currency": doc.currency,
        "grand_total": float(doc.grand_total or summary.grand_total),
        "seller": {
            "user": getattr(doc, "ve_vendedor_usuario", None) or effective_seller_user,
            "fullname": getattr(doc, "ve_vendedor_nombre", None) or seller_name,
        },
        "tax_summary": summary.to_dict(),
        "fiscal": {
            "control_number": getattr(doc, "ve_numero_control", None),
            "fiscal_invoice_number": getattr(doc, "ve_numero_factura_fiscal", None),
            "status": getattr(doc, "ve_fiscal_status", None),
            "pdf_url": getattr(doc, "ve_fiscal_pdf_url", None),
        },
        "cashea": cashea_data,
    }
