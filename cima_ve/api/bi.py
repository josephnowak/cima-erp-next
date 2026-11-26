from __future__ import annotations

from datetime import date, timedelta

from cima_ve.api._compat import whitelist
from cima_ve.services.bi_service import (
    analyze_inventory_intelligence,
    build_product_analytics,
    build_weekly_stockout_email_html,
)


def compute_bi_snapshot() -> dict:
    import frappe

    settings = frappe.get_single("Cima VE Settings")
    lead_days = int(getattr(settings, "china_lead_time_days", 45) or 45)
    safety_days = int(getattr(settings, "bi_safety_stock_days", 15) or 15)
    window_days = int(getattr(settings, "bi_analysis_window_days", 90) or 90)

    since_date = (date.today() - timedelta(days=window_days)).isoformat()

    items = frappe.db.sql(
        """
        SELECT
            i.item_code,
            i.item_name,
            i.item_group,
            i.valuation_rate,
            i.standard_rate AS price,
            COALESCE(SUM(b.actual_qty), 0) AS stock_qty,
            COALESCE(SUM(b.ordered_qty), 0) AS incoming_qty
        FROM `tabItem` i
        LEFT JOIN `tabBin` b ON b.item_code = i.item_code
        WHERE i.disabled = 0
          AND i.is_stock_item = 1
        GROUP BY i.item_code, i.item_name, i.item_group, i.valuation_rate, i.standard_rate
        """,
        as_dict=True,
    )

    sales_rows = frappe.db.sql(
        """
        SELECT
            sii.item_code,
            SUM(sii.qty) AS qty,
            SUM(sii.amount) AS amount
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1
          AND si.posting_date >= %(since_date)s
        GROUP BY sii.item_code
        """,
        {"since_date": since_date},
        as_dict=True,
    )

    return analyze_inventory_intelligence(
        items=items,
        sales_rows=sales_rows,
        china_lead_time_days=lead_days,
        safety_stock_days=safety_days,
        analysis_window_days=window_days,
    )


@whitelist()
def get_bi_dashboard() -> dict:
    """Devuelve el análisis completo de Business Intelligence e importación desde China."""
    return compute_bi_snapshot()


@whitelist()
def send_bi_stockout_alert_now() -> dict:
    """Envía manualmente el correo de alerta de agotamiento vs tiempo de llegada desde China."""
    import frappe

    settings = frappe.get_single("Cima VE Settings")
    raw_emails = str(getattr(settings, "bi_alert_emails", "") or "").strip()
    recipients = [e.strip() for e in raw_emails.split(",") if e.strip()]
    if not recipients:
        frappe.throw("Configure al menos un correo en 'Cima VE Settings -> Correos para Alertas Semanales BI'.")

    analysis = compute_bi_snapshot()
    critical = analysis["critical_items"]
    if not critical:
        return {"sent": False, "critical_count": 0, "message": "No hay productos en riesgo de agotamiento."}

    html = build_weekly_stockout_email_html(analysis)
    frappe.sendmail(
        recipients=recipients,
        subject=f"[CIMA BI] Alerta Semanal: {len(critical)} productos se agotarán antes de llegar de China",
        message=html,
    )
    return {"sent": True, "critical_count": len(critical), "recipients": recipients}


@whitelist()
def get_product_bi_analytics(item_code: str) -> dict:
    """Devuelve la analítica completa, historial de ventas y predicciones para un producto específico."""
    import frappe

    frappe.only_for(["System Manager", "Sales Manager", "Stock Manager"])
    code = str(item_code or "").strip()
    if not code:
        frappe.throw("Debe indicar el código del producto.")

    item_row = frappe.db.get_value(
        "Item",
        code,
        ["item_code", "item_name", "item_group", "valuation_rate", "standard_rate"],
        as_dict=True,
    )
    if not item_row:
        frappe.throw(f"Producto no encontrado: {code}")

    bins = frappe.db.sql(
        """
        SELECT
            warehouse,
            COALESCE(actual_qty, 0) AS actual_qty,
            COALESCE(ordered_qty, 0) AS ordered_qty
        FROM `tabBin`
        WHERE item_code = %(item_code)s
        ORDER BY actual_qty DESC
        """,
        {"item_code": code},
        as_dict=True,
    )

    total_stock = sum(float(b.get("actual_qty") or 0.0) for b in bins)
    total_incoming = sum(float(b.get("ordered_qty") or 0.0) for b in bins)
    item_payload = {
        "item_code": item_row.get("item_code"),
        "item_name": item_row.get("item_name"),
        "item_group": item_row.get("item_group"),
        "valuation_rate": float(item_row.get("valuation_rate") or 0.0),
        "price": float(item_row.get("standard_rate") or 0.0),
        "stock_qty": total_stock,
        "incoming_qty": total_incoming,
    }

    sales_entries = frappe.db.sql(
        """
        SELECT
            si.posting_date,
            si.name AS invoice_name,
            si.customer,
            si.customer_name,
            sii.qty,
            sii.rate,
            sii.amount
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1
          AND sii.item_code = %(item_code)s
        ORDER BY si.posting_date DESC, si.creation DESC
        """,
        {"item_code": code},
        as_dict=True,
    )

    pending_dn_row = frappe.db.sql(
        """
        SELECT COALESCE(SUM(dni.qty), 0) AS pending_qty
        FROM `tabDelivery Note Item` dni
        INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
        WHERE dn.docstatus = 1
          AND dni.item_code = %(item_code)s
          AND COALESCE(dn.ve_ne_estado, 'Pendiente') IN ('Pendiente', 'Pago Parcial')
        """,
        {"item_code": code},
        as_dict=True,
    )
    pending_dn_qty = float((pending_dn_row[0].get("pending_qty") if pending_dn_row else 0.0) or 0.0)

    settings = frappe.get_single("Cima VE Settings")
    lead_days = int(getattr(settings, "china_lead_time_days", 45) or 45)
    safety_days = int(getattr(settings, "bi_safety_stock_days", 15) or 15)
    window_days = int(getattr(settings, "bi_analysis_window_days", 90) or 90)

    return build_product_analytics(
        item=item_payload,
        sales_entries=sales_entries,
        warehouse_stock=bins,
        pending_dn_qty=pending_dn_qty,
        china_lead_time_days=lead_days,
        safety_stock_days=safety_days,
        analysis_window_days=window_days,
    )
