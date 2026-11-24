from __future__ import annotations

import json

from cima_ve.api._compat import whitelist
from cima_ve.services.storefront_service import (
    DEFAULT_PRODUCT_IMAGE,
    build_order_notification_payload,
    build_payment_methods_catalog,
    build_whatsapp_link,
    calculate_storefront_totals,
    normalize_rif_or_cedula,
    parse_delivery_providers,
    resolve_delivery_provider,
    validate_registration_payload,
)


def _get_storefront_config() -> dict:
    import frappe

    settings = frappe.get_single("Cima VE Settings")
    warehouses = frappe.get_all(
        "Warehouse",
        filters={"is_group": 0, "disabled": 0},
        pluck="name",
        order_by="creation asc",
    )
    default_wh = settings.get("storefront_warehouse") or ""
    if not default_wh:
        preferred = next(
            (w for w in warehouses if "white point" in w.lower() or "principal" in w.lower()),
            warehouses[0] if warehouses else "",
        )
        default_wh = preferred

    webshop_pl = ""
    if frappe.db.exists("DocType", "Webshop Settings"):
        webshop_pl = frappe.db.get_single_value("Webshop Settings", "price_list") or ""

    price_list = webshop_pl or "Detal USD"
    if not frappe.db.exists("Price List", price_list):
        pl_list = frappe.get_all("Price List", filters={"selling": 1, "enabled": 1}, pluck="name", limit=1)
        price_list = pl_list[0] if pl_list else "Standard Selling"

    companies = frappe.get_all("Company", pluck="name", order_by="creation asc", limit=1)
    company = companies[0] if companies else ""

    all_selling_pls = frappe.get_all(
        "Price List",
        filters={"selling": 1, "enabled": 1},
        pluck="name",
        order_by="name asc",
        limit=50,
    )
    mayorista_pl = next(
        (pl for pl in all_selling_pls if "mayor" in pl.lower() and "usd" in pl.lower()),
        next((pl for pl in all_selling_pls if "mayor" in pl.lower()), price_list),
    )

    return {
        "company": company,
        "warehouse": default_wh,
        "price_list": price_list,
        "mayorista_price_list": mayorista_pl,
        "simulation_mode": bool(settings.simulation_mode),
        "cashea_enabled": bool(settings.cashea_enabled),
        "iva_rate": float(settings.iva_rate or 16.0),
        "igtf_rate": float(settings.igtf_rate or 3.0),
        "google_pay_merchant_id": settings.get("google_pay_merchant_id") or "",
        "google_pay_merchant_name": settings.get("google_pay_merchant_name") or "CIMA ELECTRONIC VE",
        "pago_movil_info": settings.get("pago_movil_info") or "",
        "zelle_info": settings.get("zelle_info") or "",
        "binance_pay_info": settings.get("binance_pay_info") or "",
        "bank_transfer_info": settings.get("bank_transfer_info") or "",
        "delivery_providers": parse_delivery_providers(settings.get("delivery_providers")),
        "notify_order_email": (settings.get("notify_order_email") or "cimaelectronic@gmail.com, alexandermigueld@gmail.com").strip(),
        "notify_whatsapp_number": (settings.get("notify_whatsapp_number") or "+584243472198").strip(),
        "whatsapp_webhook_url": (settings.get("whatsapp_webhook_url") or "").strip(),
    }


def _get_google_auth_info() -> dict:
    import frappe

    if not frappe.db.exists("DocType", "Social Login Key"):
        return {"google_enabled": False, "google_auth_url": ""}

    keys = frappe.get_all(
        "Social Login Key",
        filters={"enable_social_login": 1},
        fields=["name", "provider_name", "client_id"],
    )
    google_key = next(
        (k for k in keys if "google" in (k.get("provider_name") or k.get("name") or "").lower() and k.get("client_id")),
        None,
    )
    if not google_key:
        return {"google_enabled": False, "google_auth_url": ""}

    try:
        from frappe.utils.oauth import get_oauth2_authorize_url

        auth_url = get_oauth2_authorize_url(google_key["name"], "/tienda")
        return {"google_enabled": bool(auth_url), "google_auth_url": auth_url or ""}
    except Exception:
        return {"google_enabled": False, "google_auth_url": ""}


def _find_customer_for_user(user_email: str) -> str | None:
    import frappe

    if not user_email or user_email == "Guest":
        return None

    contacts = frappe.get_all("Contact", filters={"email_id": user_email}, pluck="name", limit=5)
    for contact_name in contacts:
        links = frappe.get_all(
            "Dynamic Link",
            filters={"parent": contact_name, "parenttype": "Contact", "link_doctype": "Customer"},
            pluck="link_name",
            limit=1,
        )
        if links:
            return links[0]

    customer_by_email = frappe.db.get_value("Customer", {"email_id": user_email}, "name")
    if customer_by_email:
        return customer_by_email

    return None


def _ensure_customer_for_user(
    user_email: str,
    full_name: str = "",
    ve_rif_cedula: str = "",
    ve_taxpayer_type: str = "No Contribuyente",
    phone: str = "",
    address: str = "",
) -> str:
    import frappe

    existing = _find_customer_for_user(user_email)
    user_full_name = full_name or frappe.db.get_value("User", user_email, "full_name") or user_email.split("@")[0]

    if existing:
        if ve_rif_cedula:
            norm_rif = normalize_rif_or_cedula(ve_rif_cedula)
            frappe.db.set_value(
                "Customer",
                existing,
                {
                    "ve_rif_cedula": norm_rif,
                    "tax_id": norm_rif,
                    "ve_taxpayer_type": ve_taxpayer_type or "No Contribuyente",
                },
            )
        if phone:
            frappe.db.set_value("Customer", existing, "mobile_no", phone)
        return existing

    norm_rif = normalize_rif_or_cedula(ve_rif_cedula)
    customer_group = "Individual"
    if frappe.db.exists("DocType", "Webshop Settings"):
        customer_group = frappe.db.get_single_value("Webshop Settings", "default_customer_group") or "Individual"
    if not frappe.db.exists("Customer Group", customer_group):
        groups = frappe.get_all("Customer Group", filters={"is_group": 0}, pluck="name", limit=1)
        customer_group = groups[0] if groups else "All Customer Groups"

    territories = frappe.get_all("Territory", filters={"is_group": 0}, pluck="name", limit=1)
    territory = territories[0] if territories else "All Territories"

    cust_doc = frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": user_full_name,
            "customer_type": "Company" if norm_rif.startswith(("J-", "G-")) else "Individual",
            "customer_group": customer_group,
            "territory": territory,
            "email_id": user_email,
            "mobile_no": phone or "",
            "tax_id": norm_rif,
            "ve_rif_cedula": norm_rif,
            "ve_taxpayer_type": ve_taxpayer_type or "No Contribuyente",
        }
    )
    cust_doc.insert(ignore_permissions=True)

    contact = frappe.get_doc(
        {
            "doctype": "Contact",
            "first_name": user_full_name,
            "email_id": user_email,
            "mobile_no": phone or "",
            "user": user_email if frappe.db.exists("User", user_email) else None,
            "email_ids": [{"email_id": user_email, "is_primary": 1}],
            "links": [{"link_doctype": "Customer", "link_name": cust_doc.name}],
        }
    )
    contact.insert(ignore_permissions=True)

    if address:
        _upsert_customer_address(cust_doc.name, user_full_name, address, phone, user_email)

    frappe.db.commit()
    return cust_doc.name


def _upsert_customer_address(
    customer_name: str,
    title: str,
    address_line: str,
    phone: str = "",
    email: str = "",
) -> str | None:
    import frappe

    if not (address_line or "").strip():
        return None

    links = frappe.get_all(
        "Dynamic Link",
        filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Address"},
        pluck="parent",
        limit=1,
    )
    if links:
        addr = frappe.get_doc("Address", links[0])
        addr.address_line1 = address_line.strip()
        if phone:
            addr.phone = phone.strip()
        addr.save(ignore_permissions=True)
        return addr.name

    country = "Venezuela" if frappe.db.exists("Country", "Venezuela") else frappe.db.get_default("country") or "Venezuela"
    addr = frappe.get_doc(
        {
            "doctype": "Address",
            "address_title": title or customer_name,
            "address_type": "Billing",
            "address_line1": address_line.strip(),
            "city": "Venezuela",
            "country": country,
            "phone": phone or "",
            "email_id": email or "",
            "is_primary_address": 1,
            "is_shipping_address": 1,
            "links": [{"link_doctype": "Customer", "link_name": customer_name}],
        }
    )
    addr.insert(ignore_permissions=True)
    return addr.name


def _get_customer_profile_dict(user_email: str) -> dict:
    import frappe

    if not user_email or user_email == "Guest":
        return {
            "is_guest": True,
            "email": "",
            "full_name": "Invitado",
            "customer_name": "",
            "ve_rif_cedula": "",
            "ve_taxpayer_type": "No Contribuyente",
            "phone": "",
            "address": "",
            "is_mayorista": False,
        }

    user_fields = ["full_name", "user_image"]
    if frappe.get_meta("User").has_field("ve_es_mayorista"):
        user_fields.append("ve_es_mayorista")
    user_doc = frappe.db.get_value("User", user_email, user_fields, as_dict=True) or {}
    customer_name = _ensure_customer_for_user(user_email, full_name=user_doc.get("full_name") or "")
    cust_fields = ["customer_name", "ve_rif_cedula", "ve_taxpayer_type", "mobile_no"]
    if frappe.get_meta("Customer").has_field("ve_es_mayorista"):
        cust_fields.append("ve_es_mayorista")
    cust = frappe.db.get_value(
        "Customer",
        customer_name,
        cust_fields,
        as_dict=True,
    ) or {}
    is_mayorista = bool(int(cust.get("ve_es_mayorista") or 0) or int(user_doc.get("ve_es_mayorista") or 0))

    address_text = ""
    addr_links = frappe.get_all(
        "Dynamic Link",
        filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Address"},
        pluck="parent",
        limit=1,
    )
    if addr_links:
        address_text = frappe.db.get_value("Address", addr_links[0], "address_line1") or ""

    return {
        "is_guest": False,
        "email": user_email,
        "full_name": cust.get("customer_name") or user_doc.get("full_name") or user_email,
        "user_image": user_doc.get("user_image") or "",
        "customer_name": customer_name,
        "ve_rif_cedula": cust.get("ve_rif_cedula") or "",
        "ve_taxpayer_type": cust.get("ve_taxpayer_type") or "No Contribuyente",
        "phone": cust.get("mobile_no") or "",
        "address": address_text,
        "is_mayorista": is_mayorista,
    }


@whitelist(allow_guest=True)
def get_storefront_bootstrap() -> dict:
    """Devuelve configuración inicial, categorías, métodos de pago y usuario actual."""
    import frappe

    cfg = _get_storefront_config()
    auth = _get_google_auth_info()
    user_profile = _get_customer_profile_dict(frappe.session.user)

    categories = frappe.db.sql(
        """
        SELECT i.item_group AS name, COUNT(*) AS item_count
        FROM `tabItem` i
        WHERE i.disabled = 0 AND i.is_sales_item = 1 AND COALESCE(i.item_group, '') != ''
        GROUP BY i.item_group
        ORDER BY item_count DESC, i.item_group ASC
        """,
        as_dict=True,
    )

    try:
        from cima_ve.api.exchange_rates import get_current_exchange_rates

        exchange_rate = get_current_exchange_rates()
    except Exception:
        exchange_rate = {"bcv_rate": 0.0, "monitor_rate": 0.0, "binance_usdt_rate": 0.0, "applied_brecha_pct": 0.0, "adjusted_rate": 0.0}

    primary_contact_email = (cfg["notify_order_email"].split(",")[0] or "cimaelectronic@gmail.com").strip()
    contact_wa = cfg["notify_whatsapp_number"]

    return {
        "user": user_profile,
        "auth": auth,
        "categories": categories,
        "payment_methods": build_payment_methods_catalog(cfg),
        "delivery_providers": cfg["delivery_providers"],
        "exchange_rate": exchange_rate,
        "contact": {
            "email": primary_contact_email,
            "whatsapp": contact_wa,
            "whatsapp_url": build_whatsapp_link(contact_wa, "Hola CIMA Electronic VE, necesito información sobre productos o acceso mayorista."),
        },
        "settings": {
            "company": cfg["company"],
            "warehouse": cfg["warehouse"],
            "price_list": cfg["price_list"],
            "mayorista_price_list": cfg["mayorista_price_list"],
            "currency": "USD",
            "iva_rate": cfg["iva_rate"],
            "igtf_rate": cfg["igtf_rate"],
            "simulation_mode": cfg["simulation_mode"],
            "default_image": DEFAULT_PRODUCT_IMAGE,
            "notify_whatsapp_number": contact_wa,
        },
    }


@whitelist(allow_guest=True)
def get_catalog(
    query: str = "",
    item_group: str = "",
    only_in_stock: int = 0,
    sort: str = "stock_desc",
    page: int = 1,
    page_size: int = 24,
    price_mode: str = "detal",
) -> dict:
    """Lista productos con stock real en almacén, precios Detal/Mayorista USD e imagen fallback."""
    import frappe

    cfg = _get_storefront_config()
    clean_mode = (price_mode or "detal").strip().lower()
    active_price_list = cfg["mayorista_price_list"] if clean_mode == "mayorista" else cfg["price_list"]
    page = max(int(page or 1), 1)
    page_size = min(max(int(page_size or 24), 1), 60)
    offset = (page - 1) * page_size

    conditions = ["i.disabled = 0", "i.is_sales_item = 1"]
    params: dict = {
        "warehouse": cfg["warehouse"],
        "price_list": active_price_list,
        "limit": page_size,
        "offset": offset,
    }

    q = (query or "").strip()
    if q:
        conditions.append("(i.item_code LIKE %(q)s OR i.item_name LIKE %(q)s OR i.item_group LIKE %(q)s)")
        params["q"] = f"%{q}%"

    grp = (item_group or "").strip()
    if grp:
        conditions.append("i.item_group = %(item_group)s")
        params["item_group"] = grp

    having_clause = "HAVING stock_qty > 0" if int(only_in_stock or 0) == 1 else ""
    order_map = {
        "stock_desc": "stock_qty DESC, i.item_name ASC",
        "price_asc": "price ASC, stock_qty DESC",
        "price_desc": "price DESC, stock_qty DESC",
        "name_asc": "i.item_name ASC",
    }
    order_by = order_map.get(sort, "stock_qty DESC, i.item_name ASC")
    where_sql = " AND ".join(conditions)
    wh_filter = "AND b.warehouse = %(warehouse)s" if cfg["warehouse"] else ""

    rows = frappe.db.sql(
        f"""
        SELECT
            i.item_code,
            i.item_name,
            i.item_group,
            COALESCE(wi.web_long_description, i.description, '') AS description,
            COALESCE(NULLIF(wi.website_image, ''), NULLIF(i.image, ''), '') AS raw_image,
            COALESCE(SUM(b.actual_qty), 0) AS stock_qty,
            COALESCE(MAX(ip.price_list_rate), i.standard_rate, 0) AS price,
            COALESCE(MAX(ip.currency), 'USD') AS currency
        FROM `tabItem` i
        LEFT JOIN `tabWebsite Item` wi
            ON wi.item_code = i.item_code AND wi.published = 1
        LEFT JOIN `tabBin` b
            ON b.item_code = i.item_code {wh_filter}
        LEFT JOIN `tabItem Price` ip
            ON ip.item_code = i.item_code
            AND ip.selling = 1
            AND ip.price_list = %(price_list)s
        WHERE {where_sql}
        GROUP BY i.item_code, i.item_name, i.item_group, wi.web_long_description, i.description, wi.website_image, i.image, i.standard_rate
        {having_clause}
        ORDER BY {order_by}
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
        as_dict=True,
    )

    count_rows = frappe.db.sql(
        f"""
        SELECT COUNT(*) AS total FROM (
            SELECT i.item_code, COALESCE(SUM(b.actual_qty), 0) AS stock_qty
            FROM `tabItem` i
            LEFT JOIN `tabBin` b ON b.item_code = i.item_code {wh_filter}
            WHERE {where_sql}
            GROUP BY i.item_code
            {having_clause}
        ) sub
        """,
        params,
        as_dict=True,
    )
    total = int(count_rows[0]["total"]) if count_rows else 0

    pricing_rules = frappe.get_all(
        "Pricing Rule",
        filters={"selling": 1, "disable": 0},
        fields=["name", "title", "apply_on", "min_qty", "discount_percentage", "rate"],
        order_by="min_qty asc",
        limit=50,
    )

    for row in rows:
        row["image"] = row.pop("raw_image") or DEFAULT_PRODUCT_IMAGE
        row["stock_qty"] = float(row.get("stock_qty") or 0)
        row["price"] = float(row.get("price") or 0)
        row["has_volume_discount"] = bool(pricing_rules)

    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "default_image": DEFAULT_PRODUCT_IMAGE,
    }


@whitelist(allow_guest=True)
def preview_storefront_cart(
    items: str | list[dict],
    payment_method: str = "cashea",
    delivery_provider: str = "",
    price_mode: str = "detal",
) -> dict:
    """Calcula precios reales (con Pricing Rules), IVA, IGTF y costo de envío para el carrito de tienda."""
    import frappe

    parsed_items = json.loads(items) if isinstance(items, str) else items
    cfg = _get_storefront_config()
    clean_mode = (price_mode or "detal").strip().lower()
    active_price_list = cfg["mayorista_price_list"] if clean_mode == "mayorista" else cfg["price_list"]
    provider = resolve_delivery_provider(delivery_provider, cfg["delivery_providers"])
    shipping_cost = float(provider.get("cost") or 0.0) if parsed_items else 0.0

    if not parsed_items:
        return {
            "items": [],
            "delivery_provider": provider,
            "totals": calculate_storefront_totals([], payment_method=payment_method, shipping_cost=0.0),
        }

    priced_items: list[dict] = []

    try:
        from erpnext.stock.get_item_details import get_item_details

        for row in parsed_items:
            item_code = row.get("item_code")
            qty = float(row.get("qty") or 1)
            if not item_code or qty <= 0:
                continue
            details = get_item_details(
                {
                    "item_code": item_code,
                    "company": cfg["company"],
                    "selling_price_list": active_price_list,
                    "price_list": active_price_list,
                    "currency": "USD",
                    "qty": qty,
                    "warehouse": cfg["warehouse"],
                    "doctype": "Sales Order",
                    "conversion_rate": 1.0,
                    "plc_conversion_rate": 1.0,
                }
            )
            base_rate = float(details.get("price_list_rate") or row.get("price") or 0)
            net_rate = float(details.get("rate") or base_rate)
            discount_pct = float(details.get("discount_percentage") or 0)
            priced_items.append(
                {
                    "item_code": item_code,
                    "item_name": row.get("item_name") or details.get("item_name") or item_code,
                    "qty": qty,
                    "price_list_rate": base_rate,
                    "rate": net_rate,
                    "discount_percentage": discount_pct,
                    "amount": round(net_rate * qty, 2),
                }
            )
    except Exception:
        for row in parsed_items:
            qty = float(row.get("qty") or 1)
            rate = float(row.get("price") or row.get("rate") or 0)
            priced_items.append(
                {
                    "item_code": row.get("item_code"),
                    "item_name": row.get("item_name") or row.get("item_code"),
                    "qty": qty,
                    "price_list_rate": rate,
                    "rate": rate,
                    "discount_percentage": 0.0,
                    "amount": round(rate * qty, 2),
                }
            )

    totals = calculate_storefront_totals(
        priced_items,
        payment_method=payment_method,
        iva_rate=cfg["iva_rate"],
        igtf_rate=cfg["igtf_rate"],
        shipping_cost=shipping_cost,
    )
    return {"items": priced_items, "delivery_provider": provider, "totals": totals}


@whitelist(allow_guest=True)
def register_customer(
    email: str,
    full_name: str,
    password: str,
    ve_rif_cedula: str = "",
    ve_taxpayer_type: str = "No Contribuyente",
    phone: str = "",
    address: str = "",
) -> dict:
    """Registra un nuevo usuario web + Customer (con RIF/datos fiscales opcionales) e inicia sesión."""
    import frappe

    data = validate_registration_payload(
        email=email,
        full_name=full_name,
        password=password,
        ve_rif_cedula=ve_rif_cedula,
        ve_taxpayer_type=ve_taxpayer_type,
    )

    if frappe.db.exists("User", data["email"]):
        frappe.throw("Ya existe una cuenta registrada con este correo. Inicia sesión o usa Google.")

    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": data["email"],
            "first_name": data["full_name"],
            "enabled": 1,
            "user_type": "Website User",
            "send_welcome_email": 0,
            "new_password": data["password"],
        }
    )
    user.append("roles", {"role": "Customer"})
    user.insert(ignore_permissions=True)

    _ensure_customer_for_user(
        user_email=data["email"],
        full_name=data["full_name"],
        ve_rif_cedula=data["ve_rif_cedula"],
        ve_taxpayer_type=data["ve_taxpayer_type"],
        phone=phone,
        address=address,
    )

    frappe.local.login_manager.login_as(data["email"])
    return {"ok": True, "user": _get_customer_profile_dict(data["email"])}


@whitelist()
def get_my_profile_and_orders() -> dict:
    """Devuelve perfil fiscal y pedidos/facturas del cliente autenticado."""
    import frappe

    profile = _get_customer_profile_dict(frappe.session.user)
    customer_name = profile.get("customer_name")
    if not customer_name:
        return {"profile": profile, "orders": [], "invoices": []}

    orders = frappe.get_all(
        "Sales Order",
        filters={"customer": customer_name, "docstatus": ["<", 2]},
        fields=["name", "transaction_date", "grand_total", "currency", "status", "per_billed", "po_no"],
        order_by="creation desc",
        limit=25,
    )
    invoices = frappe.get_all(
        "Sales Invoice",
        filters={"customer": customer_name, "docstatus": 1},
        fields=[
            "name",
            "posting_date",
            "grand_total",
            "status",
            "ve_numero_control",
            "ve_numero_factura_fiscal",
            "ve_fiscal_status",
            "ve_fiscal_pdf_url",
            "cashea_status",
        ],
        order_by="creation desc",
        limit=25,
    )
    delivery_notes = frappe.get_all(
        "Delivery Note",
        filters={"customer": customer_name, "docstatus": ["<", 2]},
        fields=[
            "name",
            "posting_date",
            "grand_total",
            "currency",
            "status",
            "ve_ne_estado",
            "ve_ne_metodo_pago",
            "ve_ne_referencia_pago",
            "ve_ne_envio_proveedor",
        ],
        order_by="creation desc",
        limit=25,
    )
    return {"profile": profile, "delivery_notes": delivery_notes, "orders": orders, "invoices": invoices}


@whitelist()
def update_my_fiscal_profile(
    full_name: str = "",
    ve_rif_cedula: str = "",
    ve_taxpayer_type: str = "No Contribuyente",
    phone: str = "",
    address: str = "",
) -> dict:
    """Actualiza datos fiscales (RIF/Cédula opcional, contribuyente, teléfono, dirección)."""
    import frappe

    user_email = frappe.session.user
    if not user_email or user_email == "Guest":
        frappe.throw("Debes iniciar sesión para actualizar tu perfil.")

    customer_name = _ensure_customer_for_user(user_email, full_name=full_name)
    norm_rif = normalize_rif_or_cedula(ve_rif_cedula)

    updates: dict = {
        "ve_rif_cedula": norm_rif,
        "tax_id": norm_rif,
        "ve_taxpayer_type": ve_taxpayer_type or "No Contribuyente",
    }
    if full_name.strip():
        updates["customer_name"] = full_name.strip()
        frappe.db.set_value("User", user_email, "first_name", full_name.strip())
    if phone.strip():
        updates["mobile_no"] = phone.strip()

    frappe.db.set_value("Customer", customer_name, updates)
    if address.strip():
        _upsert_customer_address(customer_name, full_name or customer_name, address, phone, user_email)

    frappe.db.commit()
    return {"ok": True, "profile": _get_customer_profile_dict(user_email)}


def _resolve_shipping_account(company: str) -> str:
    import frappe

    for field in ("default_income_account", "freight_and_forwarding_account"):
        acc = frappe.db.get_value("Company", company, field)
        if acc:
            return acc
    accounts = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0, "root_type": "Income"},
        pluck="name",
        limit=1,
    )
    if accounts:
        return accounts[0]
    fallback = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0},
        pluck="name",
        limit=1,
    )
    return fallback[0] if fallback else ""


def _send_storefront_order_notifications(cfg: dict, order_payload: dict) -> dict:
    """Envía notificación de nuevo pedido por correo electrónico y WhatsApp (webhook + enlace directo)."""
    import urllib.request
    import frappe

    notif = build_order_notification_payload(**order_payload)
    email_sent = False
    whatsapp_sent = False

    target_email = (cfg.get("notify_order_email") or "").strip()
    if target_email:
        try:
            frappe.sendmail(
                recipients=[target_email],
                subject=notif["subject"],
                message=notif["html"],
                now=True,
            )
            email_sent = True
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Cima VE Storefront Email Notification Error")

    webhook_url = (cfg.get("whatsapp_webhook_url") or "").strip()
    if webhook_url:
        try:
            settings = frappe.get_single("Cima VE Settings")
            token = ""
            try:
                token = settings.get_password("whatsapp_api_token") or ""
            except Exception:
                token = ""
            body = json.dumps(
                {
                    "to": cfg.get("notify_whatsapp_number") or "",
                    "message": notif["text"],
                    "order_id": order_payload.get("order_id"),
                    "customer_name": order_payload.get("customer_name"),
                    "customer_email": order_payload.get("customer_email"),
                    "customer_phone": order_payload.get("customer_phone"),
                    "delivery_provider": order_payload.get("delivery_label"),
                    "grand_total": order_payload.get("grand_total"),
                }
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(webhook_url, data=body, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=8)
            whatsapp_sent = True
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Cima VE Storefront WhatsApp Notification Error")

    store_wa_link = build_whatsapp_link(cfg.get("notify_whatsapp_number"), notif["text"])
    return {
        "email_sent": email_sent,
        "whatsapp_sent": whatsapp_sent,
        "store_whatsapp_url": store_wa_link,
        "customer_whatsapp_url": notif["customer_whatsapp_url"],
    }


@whitelist(allow_guest=True)
def upload_storefront_payment_proof(file_name: str, file_data_base64: str) -> dict:
    """Sube una captura o comprobante de pago desde /tienda para adjuntarlo a la Nota de Entrega."""
    import base64
    import os
    import frappe

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

    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": clean_name,
            "is_private": 0,
            "content": content,
        }
    )
    file_doc.flags.ignore_permissions = True
    file_doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "file_id": file_doc.name,
        "file_url": file_doc.file_url,
        "file_name": file_doc.file_name,
    }


@whitelist(allow_guest=True)
def place_storefront_order(
    items: str | list[dict],
    payment_method: str = "cashea",
    payment_details: str | dict | None = None,
    customer_info: str | dict | None = None,
    delivery_provider: str = "",
    price_mode: str = "detal",
) -> dict:
    """Crea una Nota de Entrega (Delivery Note) desde /tienda con comprobante, envío y notificación Email/WhatsApp."""
    import frappe

    original_user = frappe.session.user
    if original_user != "Guest":
        return _place_storefront_order_impl(items, payment_method, payment_details, customer_info, delivery_provider, price_mode=price_mode)

    frappe.set_user("Administrator")
    try:
        return _place_storefront_order_impl(
            items,
            payment_method,
            payment_details,
            customer_info,
            delivery_provider,
            original_user=original_user,
            price_mode=price_mode,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Cima VE Storefront Order Error")
        raise
    finally:
        frappe.set_user(original_user)


def _place_storefront_order_impl(
    items: str | list[dict],
    payment_method: str = "cashea",
    payment_details: str | dict | None = None,
    customer_info: str | dict | None = None,
    delivery_provider: str = "",
    original_user: str = "",
    price_mode: str = "detal",
) -> dict:
    import frappe
    from cima_ve.api.delivery_notes import _get_auto_discount_config, enqueue_delivery_note_created_notifications
    from cima_ve.api.seller_pos import _apply_required_sales_metadata
    from cima_ve.services.delivery_note_service import DEFAULT_NOTIFICATION_WHATSAPP, compute_delivery_note_discounts

    parsed_items = json.loads(items) if isinstance(items, str) else items
    if not parsed_items:
        frappe.throw("Tu carrito está vacío.")

    pay_details = json.loads(payment_details) if isinstance(payment_details, str) else (payment_details or {})
    cust_info = json.loads(customer_info) if isinstance(customer_info, str) else (customer_info or {})

    cfg = _get_storefront_config()
    provider = resolve_delivery_provider(delivery_provider, cfg["delivery_providers"])
    shipping_cost = float(provider.get("cost") or 0.0)
    address_text = (cust_info.get("address") or "").strip()
    if provider.get("requires_address") and not address_text:
        frappe.throw(f"Ingresa la dirección o agencia de entrega para {provider['label']}.")

    user_email = original_user or frappe.session.user
    effective_email = ""
    effective_name = (cust_info.get("full_name") or "").strip()
    effective_phone = (cust_info.get("phone") or "").strip()

    if user_email == "Guest":
        guest_email = (cust_info.get("email") or "").strip().lower()
        if not guest_email or "@" not in guest_email or "." not in guest_email.split("@")[-1]:
            frappe.throw("Ingresa un correo electrónico válido para completar el pedido.")
        if not effective_name:
            frappe.throw("Ingresa tu nombre completo para completar el pedido.")
        effective_email = guest_email
        if cust_info.get("password"):
            register_customer(
                email=guest_email,
                full_name=effective_name,
                password=cust_info.get("password"),
                ve_rif_cedula=cust_info.get("ve_rif_cedula", ""),
                ve_taxpayer_type=cust_info.get("ve_taxpayer_type", "No Contribuyente"),
                phone=effective_phone,
                address=address_text,
            )
            customer_name = _ensure_customer_for_user(guest_email)
        else:
            customer_name = _ensure_customer_for_user(
                user_email=guest_email,
                full_name=effective_name,
                ve_rif_cedula=cust_info.get("ve_rif_cedula", ""),
                ve_taxpayer_type=cust_info.get("ve_taxpayer_type", "No Contribuyente"),
                phone=effective_phone,
                address=address_text,
            )
    else:
        if "@" not in user_email:
            fallback_email = (cust_info.get("email") or "").strip().lower()
            if not fallback_email or "@" not in fallback_email:
                frappe.throw("Tu cuenta debe tener un correo electrónico válido.")
            effective_email = fallback_email
        else:
            effective_email = user_email
        customer_name = _ensure_customer_for_user(
            user_email=user_email,
            full_name=effective_name,
            ve_rif_cedula=cust_info.get("ve_rif_cedula", ""),
            ve_taxpayer_type=cust_info.get("ve_taxpayer_type", "No Contribuyente"),
            phone=effective_phone,
            address=address_text,
        )
        if not effective_name:
            effective_name = frappe.db.get_value("Customer", customer_name, "customer_name") or effective_email

    method_catalog = {m["id"]: m["label"] for m in build_payment_methods_catalog(cfg)}
    method_label = method_catalog.get(payment_method, payment_method)

    clean_price_mode = (price_mode or "detal").strip().lower()
    if clean_price_mode == "mayorista":
        cust_is_may = bool(int(frappe.db.get_value("Customer", customer_name, "ve_es_mayorista") or 0)) if frappe.get_meta("Customer").has_field("ve_es_mayorista") else False
        user_is_may = (
            bool(int(frappe.db.get_value("User", user_email, "ve_es_mayorista") or 0))
            if user_email != "Guest" and frappe.get_meta("User").has_field("ve_es_mayorista")
            else False
        )
        if user_email == "Guest" or not (cust_is_may or user_is_may):
            frappe.throw(
                "Solo los clientes registrados como Mayorista pueden realizar pedidos con precios de mayorista. "
                "Por favor contáctanos para solicitar tu activación como mayorista."
            )
    active_price_list = cfg["mayorista_price_list"] if clean_price_mode == "mayorista" else cfg["price_list"]
    ref_code = (pay_details.get("reference") or "").strip()
    bank_info = (pay_details.get("bank") or "").strip()
    screenshot_url = (pay_details.get("screenshot_url") or "").strip()
    screenshot_file_id = (pay_details.get("screenshot_file_id") or "").strip()
    payment_notes = (pay_details.get("notes") or "").strip()

    dn_items = []
    for row in parsed_items:
        qty = float(row.get("qty") or 0)
        if qty <= 0:
            continue
        item_entry = {
            "item_code": row.get("item_code"),
            "qty": qty,
        }
        if cfg["warehouse"]:
            item_entry["warehouse"] = cfg["warehouse"]
        dn_items.append(item_entry)

    if not dn_items:
        frappe.throw("No hay productos válidos en el pedido.")

    note_parts = [
        f"Pedido Tienda Web (/tienda)",
        f"Método reportado: {method_label}",
        f"Envío: {provider['label']} (${shipping_cost:.2f})",
    ]
    if address_text:
        note_parts.append(f"Dirección: {address_text}")
    if ref_code:
        note_parts.append(f"Ref: {ref_code}")
    if bank_info:
        note_parts.append(f"Banco/Emisor: {bank_info}")
    if effective_phone:
        note_parts.append(f"Teléfono: {effective_phone}")
    if payment_notes:
        note_parts.append(f"Nota pago: {payment_notes}")
    if pay_details.get("token_summary"):
        note_parts.append(f"Token navegador: {pay_details.get('token_summary')}")

    dn = frappe.get_doc(
        {
            "doctype": "Delivery Note",
            "company": cfg["company"],
            "customer": customer_name,
            "currency": "USD",
            "selling_price_list": active_price_list or None,
            "set_warehouse": cfg["warehouse"] or None,
            "ignore_pricing_rule": 0,
            "ve_vendedor_usuario": None,
            "ve_vendedor_nombre": "Tienda Web (/tienda)",
            "ve_ne_estado": "Pendiente",
            "ve_ne_fecha_vencimiento": frappe.utils.today(),
            "remarks": " | ".join(note_parts),
            "items": dn_items,
        }
    )

    dn.flags.ignore_permissions = True
    dn.flags.ignore_links = False

    if dn.meta.has_field("ve_ne_origen"):
        dn.ve_ne_origen = "Tienda Web"
    if dn.meta.has_field("ve_ne_metodo_pago"):
        dn.ve_ne_metodo_pago = method_label
    if dn.meta.has_field("ve_ne_referencia_pago"):
        dn.ve_ne_referencia_pago = ref_code
    if dn.meta.has_field("ve_ne_banco_pago"):
        dn.ve_ne_banco_pago = bank_info
    if dn.meta.has_field("ve_ne_comprobante_url"):
        dn.ve_ne_comprobante_url = screenshot_url
    if dn.meta.has_field("ve_ne_envio_proveedor"):
        dn.ve_ne_envio_proveedor = provider["label"]
    if dn.meta.has_field("ve_ne_envio_costo"):
        dn.ve_ne_envio_costo = shipping_cost
    if dn.meta.has_field("ve_ne_envio_direccion"):
        dn.ve_ne_envio_direccion = address_text
    if dn.meta.has_field("ve_ne_cliente_email"):
        dn.ve_ne_cliente_email = effective_email
    if dn.meta.has_field("ve_ne_cliente_telefono"):
        dn.ve_ne_cliente_telefono = effective_phone

    _apply_required_sales_metadata(dn, sales_channel="Mayor" if clean_price_mode == "mayorista" else "Detal")
    dn.run_method("set_missing_values")
    dn.run_method("calculate_taxes_and_totals")

    subtotal_before_global = round(float(dn.net_total or dn.total or dn.grand_total or 0.0), 2)
    auto_disc_enabled, auto_disc_tiers = _get_auto_discount_config()
    disc_info = compute_delivery_note_discounts(
        subtotal=subtotal_before_global,
        auto_discount_enabled=auto_disc_enabled,
        tiers=auto_disc_tiers,
        manual_discount_enabled=False,
        manual_discount_percentage=0.0,
    )
    if disc_info["total_discount_amount"] > 0:
        dn.apply_discount_on = "Net Total"
        dn.discount_amount = disc_info["total_discount_amount"]
        dn.additional_discount_percentage = disc_info["effective_discount_pct"]
        dn.run_method("calculate_taxes_and_totals")

    if shipping_cost > 0:
        ship_account = _resolve_shipping_account(cfg["company"])
        if ship_account:
            dn.append(
                "taxes",
                {
                    "charge_type": "Actual",
                    "account_head": ship_account,
                    "description": f"Envío ({provider['label']})",
                    "tax_amount": shipping_cost,
                },
            )
            dn.run_method("calculate_taxes_and_totals")

    if dn.meta.has_field("ve_ne_subtotal_sin_descuento"):
        dn.ve_ne_subtotal_sin_descuento = disc_info["subtotal"]
    if dn.meta.has_field("ve_ne_descuento_auto_pct"):
        dn.ve_ne_descuento_auto_pct = disc_info["auto_discount_pct"]
    if dn.meta.has_field("ve_ne_descuento_auto_monto"):
        dn.ve_ne_descuento_auto_monto = disc_info["auto_discount_amount"]
    if dn.meta.has_field("ve_ne_descuento_auto_etiqueta"):
        dn.ve_ne_descuento_auto_etiqueta = disc_info["auto_discount_label"]
    if dn.meta.has_field("ve_ne_monto_total"):
        dn.ve_ne_monto_total = round(float(dn.grand_total or 0.0), 2)
    if dn.meta.has_field("ve_ne_monto_pagado"):
        dn.ve_ne_monto_pagado = 0.0
    if dn.meta.has_field("ve_ne_saldo_pendiente"):
        dn.ve_ne_saldo_pendiente = round(float(dn.grand_total or 0.0), 2)

    dn.flags.ignore_permissions = True
    dn.insert(ignore_permissions=True)
    dn.submit()
    dn.reload()

    if screenshot_file_id and frappe.db.exists("File", screenshot_file_id):
        frappe.db.set_value(
            "File",
            screenshot_file_id,
            {"attached_to_doctype": "Delivery Note", "attached_to_name": dn.name},
        )

    dn.add_comment("Comment", " | ".join(note_parts) + (f" | Comprobante: {screenshot_url}" if screenshot_url else ""))

    whatsapp_url = build_whatsapp_link(
        cfg["notify_whatsapp_number"] or DEFAULT_NOTIFICATION_WHATSAPP,
        f"Hola CIMA Electronic VE, quisiera coordinar la entrega de mi pedido {dn.name}.",
    )
    enqueue_delivery_note_created_notifications(
        dn.name,
        extra_info={
            "origin": "Tienda Web",
            "delivery_provider": provider["label"],
            "delivery_cost": shipping_cost,
            "delivery_address": address_text,
            "customer_email": effective_email,
            "customer_phone": effective_phone,
            "payment_method": method_label,
            "payment_reference": ref_code,
            "payment_bank": bank_info,
            "screenshot_url": screenshot_url,
        },
    )

    frappe.db.commit()
    return {
        "ok": True,
        "delivery_note": dn.name,
        "sales_order": dn.name,
        "grand_total": float(dn.grand_total or 0),
        "payment_method": payment_method,
        "payment_method_label": method_label,
        "delivery_provider": provider,
        "status": "Nota de Entrega registrada (Pendiente de verificación)",
        "notifications": {"scheduled": True, "whatsapp_url": whatsapp_url},
    }
