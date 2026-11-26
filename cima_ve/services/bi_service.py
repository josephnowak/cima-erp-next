from __future__ import annotations

import math
from datetime import date, timedelta


def analyze_inventory_intelligence(
    items: list[dict],
    sales_rows: list[dict],
    china_lead_time_days: int = 45,
    safety_stock_days: int = 15,
    analysis_window_days: int = 90,
    reorder_cycle_days: int = 30,
    today: date | None = None,
) -> dict:
    """
    Analiza demanda, velocidad de venta, fecha estimada de agotamiento,
    clasificación ABC y cantidad sugerida de compra para importación desde China.
    """
    ref_date = today or date.today()
    lead_days = max(1, int(china_lead_time_days or 45))
    safety_days = max(0, int(safety_stock_days or 15))
    window_days = max(1, int(analysis_window_days or 90))
    cycle_days = max(1, int(reorder_cycle_days or 30))

    sales_by_item: dict[str, dict] = {}
    for row in sales_rows:
        code = str(row.get("item_code") or "").strip()
        if not code:
            continue
        entry = sales_by_item.setdefault(code, {"units_sold": 0.0, "revenue": 0.0})
        entry["units_sold"] += float(row.get("qty") or 0.0)
        entry["revenue"] += float(row.get("amount") or 0.0)

    analyzed: list[dict] = []
    total_revenue_all = 0.0
    total_inventory_value = 0.0
    total_recommended_investment = 0.0

    for item in items:
        code = str(item.get("item_code") or "").strip()
        if not code:
            continue
        name = str(item.get("item_name") or code)
        group = str(item.get("item_group") or "General")
        stock = max(0.0, float(item.get("stock_qty") or 0.0))
        incoming = max(0.0, float(item.get("incoming_qty") or 0.0))
        cost = max(0.0, float(item.get("valuation_rate") or item.get("cost") or 0.0))
        price = max(0.0, float(item.get("price") or item.get("standard_rate") or 0.0))

        s_info = sales_by_item.get(code, {"units_sold": 0.0, "revenue": 0.0})
        units_sold = round(float(s_info["units_sold"]), 2)
        revenue = round(float(s_info["revenue"]), 2)
        total_revenue_all += revenue
        total_inventory_value += stock * cost

        daily_velocity = round(units_sold / window_days, 4) if units_sold > 0 else 0.0

        if daily_velocity > 0:
            days_until_stockout = round(stock / daily_velocity, 1)
            est_stockout_date = (ref_date + timedelta(days=int(round(days_until_stockout)))).isoformat()
        else:
            days_until_stockout = None
            est_stockout_date = None

        target_coverage_days = lead_days + safety_days + cycle_days
        target_stock = daily_velocity * target_coverage_days
        effective_stock = stock + incoming
        raw_reorder = max(0.0, target_stock - effective_stock)
        recommended_order_qty = int(math.ceil(raw_reorder)) if raw_reorder > 0 else 0
        est_order_cost = round(recommended_order_qty * cost, 2)
        total_recommended_investment += est_order_cost

        if daily_velocity == 0:
            alert_level = "SIN MOVIMIENTO" if stock > 0 else "INACTIVO"
        elif days_until_stockout is not None and days_until_stockout <= lead_days:
            alert_level = "CRÍTICO"
        elif days_until_stockout is not None and days_until_stockout <= (lead_days + safety_days):
            alert_level = "ALERTA"
        else:
            alert_level = "SALUDABLE"

        analyzed.append(
            {
                "item_code": code,
                "item_name": name,
                "item_group": group,
                "current_stock": round(stock, 2),
                "incoming_qty": round(incoming, 2),
                "units_sold_window": units_sold,
                "revenue_window": revenue,
                "daily_velocity": daily_velocity,
                "monthly_velocity": round(daily_velocity * 30.0, 1),
                "days_until_stockout": days_until_stockout,
                "est_stockout_date": est_stockout_date,
                "alert_level": alert_level,
                "will_stockout_before_china": bool(
                    days_until_stockout is not None and days_until_stockout < lead_days
                ),
                "recommended_order_qty": recommended_order_qty,
                "unit_cost": round(cost, 2),
                "unit_price": round(price, 2),
                "est_order_cost": est_order_cost,
                "stock_value": round(stock * cost, 2),
                "abc_class": "C",
            }
        )

    # ABC classification by revenue
    analyzed_by_rev = sorted(analyzed, key=lambda x: x["revenue_window"], reverse=True)
    cum_rev = 0.0
    for row in analyzed_by_rev:
        if total_revenue_all <= 0 or row["revenue_window"] <= 0:
            row["abc_class"] = "C"
            continue
        cum_rev += row["revenue_window"]
        pct = (cum_rev / total_revenue_all) * 100.0
        if pct <= 80.0:
            row["abc_class"] = "A"
        elif pct <= 95.0:
            row["abc_class"] = "B"
        else:
            row["abc_class"] = "C"

    critical_items = sorted(
        [x for x in analyzed if x["will_stockout_before_china"]],
        key=lambda x: (x["days_until_stockout"] if x["days_until_stockout"] is not None else 99999),
    )
    warning_items = [x for x in analyzed if x["alert_level"] == "ALERTA"]
    top_revenue_items = analyzed_by_rev[:10]
    fastest_selling_items = sorted(
        [x for x in analyzed if x["daily_velocity"] > 0],
        key=lambda x: x["daily_velocity"],
        reverse=True,
    )[:10]
    slow_dead_stock = sorted(
        [x for x in analyzed if x["alert_level"] == "SIN MOVIMIENTO"],
        key=lambda x: x["stock_value"],
        reverse=True,
    )[:10]
    purchase_plan = sorted(
        [x for x in analyzed if x["recommended_order_qty"] > 0],
        key=lambda x: (0 if x["will_stockout_before_china"] else 1, -x["revenue_window"]),
    )

    return {
        "config": {
            "china_lead_time_days": lead_days,
            "safety_stock_days": safety_days,
            "analysis_window_days": window_days,
            "reorder_cycle_days": cycle_days,
        },
        "kpis": {
            "total_items_analyzed": len(analyzed),
            "critical_count": len(critical_items),
            "warning_count": len(warning_items),
            "dead_stock_count": len([x for x in analyzed if x["alert_level"] == "SIN MOVIMIENTO"]),
            "total_revenue_window": round(total_revenue_all, 2),
            "total_inventory_value": round(total_inventory_value, 2),
            "total_recommended_investment": round(total_recommended_investment, 2),
        },
        "critical_items": critical_items,
        "top_revenue_items": top_revenue_items,
        "fastest_selling_items": fastest_selling_items,
        "slow_dead_stock": slow_dead_stock,
        "purchase_plan": purchase_plan,
        "all_items": analyzed_by_rev,
    }


def build_weekly_stockout_email_html(analysis: dict) -> str:
    """Genera el HTML del correo semanal de alerta de reposición para importación desde China."""
    lead_days = analysis["config"]["china_lead_time_days"]
    critical = analysis["critical_items"]

    rows_html = ""
    for item in critical:
        days_left = item["days_until_stockout"]
        rows_html += f"""
        <tr>
            <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;"><code>{item['item_code']}</code></td>
            <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;"><strong>{item['item_name']}</strong></td>
            <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;">{item['current_stock']}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;">{item['daily_velocity']:.2f}/día</td>
            <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;color:#b91c1c;font-weight:700;">{days_left} días</td>
            <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;">{item['est_stockout_date'] or '-'}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:700;color:#0b6e68;">{item['recommended_order_qty']} uds</td>
        </tr>
        """

    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;max-width:780px;margin:0 auto;color:#1c2532;">
      <div style="background:#1c2532;color:#ffffff;padding:20px 24px;border-radius:12px 12px 0 0;border-bottom:4px solid #06c686;">
        <h2 style="margin:0 0 6px;font-size:20px;">Alerta Semanal de Inventario — CIMA Electronic VE</h2>
        <p style="margin:0;color:#cbd5e1;font-size:13px;">
          Productos que se agotarán en menos de <strong>{lead_days} días</strong> (tiempo configurado de llegada desde China).
        </p>
      </div>
      <div style="padding:20px 24px;border:1px solid #d9e6e5;border-top:none;border-radius:0 0 12px 12px;background:#ffffff;">
        <p style="margin-top:0;">Se detectaron <strong>{len(critical)}</strong> productos en estado crítico de reposición:</p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead>
            <tr style="background:#f4f8f8;text-align:left;">
              <th style="padding:8px 10px;">Código</th>
              <th style="padding:8px 10px;">Producto</th>
              <th style="padding:8px 10px;text-align:right;">Stock Actual</th>
              <th style="padding:8px 10px;text-align:right;">Rotación</th>
              <th style="padding:8px 10px;text-align:right;">Días Restantes</th>
              <th style="padding:8px 10px;">Agotamiento Est.</th>
              <th style="padding:8px 10px;text-align:right;">Pedido Sugerido</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div>
    """


def _coerce_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    raw = str(value).strip()[:10]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta
    return total // 12, (total % 12) + 1


def build_product_analytics(
    item: dict,
    sales_entries: list[dict],
    warehouse_stock: list[dict] | None = None,
    pending_dn_qty: float = 0.0,
    china_lead_time_days: int = 45,
    safety_stock_days: int = 15,
    analysis_window_days: int = 90,
    reorder_cycle_days: int = 30,
    today: date | None = None,
) -> dict:
    """
    Calcula analítica completa para un producto específico:
    ventas multi-período, tendencia, serie mensual, stock por almacén,
    proyecciones de demanda/ingresos y recomendación de compra a China.
    """
    ref_date = today or date.today()
    lead_days = max(1, int(china_lead_time_days or 45))
    safety_days = max(0, int(safety_stock_days or 15))
    window_days = max(1, int(analysis_window_days or 90))
    cycle_days = max(1, int(reorder_cycle_days or 30))

    code = str(item.get("item_code") or "").strip()
    name = str(item.get("item_name") or code)
    group = str(item.get("item_group") or "General")
    stock = max(0.0, float(item.get("stock_qty") or 0.0))
    incoming = max(0.0, float(item.get("incoming_qty") or 0.0))
    cost = max(0.0, float(item.get("valuation_rate") or item.get("cost") or 0.0))
    price = max(0.0, float(item.get("price") or item.get("standard_rate") or 0.0))
    pending_dn = max(0.0, float(pending_dn_qty or 0.0))

    d_7 = ref_date - timedelta(days=7)
    d_30 = ref_date - timedelta(days=30)
    d_60 = ref_date - timedelta(days=60)
    d_win = ref_date - timedelta(days=window_days)

    units_7d = revenue_7d = 0.0
    units_30d = revenue_30d = 0.0
    units_prev_30d = 0.0
    units_window = revenue_window = 0.0
    units_all = revenue_all = 0.0
    invoices_window: set[str] = set()
    invoices_all: set[str] = set()
    sale_dates: list[date] = []
    monthly_map: dict[str, dict] = {}
    parsed_entries: list[tuple[date | None, dict]] = []

    for row in sales_entries or []:
        qty = float(row.get("qty") or 0.0)
        amount = float(row.get("amount") or 0.0)
        pdate = _coerce_date(row.get("posting_date"))
        inv = str(row.get("invoice_name") or "").strip()

        units_all += qty
        revenue_all += amount
        if inv:
            invoices_all.add(inv)
        if pdate:
            sale_dates.append(pdate)
            ym = f"{pdate.year:04d}-{pdate.month:02d}"
            m_entry = monthly_map.setdefault(ym, {"units_sold": 0.0, "revenue": 0.0})
            m_entry["units_sold"] += qty
            m_entry["revenue"] += amount

            if pdate >= d_7:
                units_7d += qty
                revenue_7d += amount
            if pdate >= d_30:
                units_30d += qty
                revenue_30d += amount
            elif pdate >= d_60:
                units_prev_30d += qty
            if pdate >= d_win:
                units_window += qty
                revenue_window += amount
                if inv:
                    invoices_window.add(inv)
        parsed_entries.append((pdate, row))

    daily_velocity = round(units_window / window_days, 4) if units_window > 0 else 0.0
    weekly_velocity = round(daily_velocity * 7.0, 2)
    monthly_velocity = round(daily_velocity * 30.0, 1)

    recent_30d_vel = round(units_30d / 30.0, 4) if units_30d > 0 else 0.0
    prev_30d_vel = round(units_prev_30d / 30.0, 4) if units_prev_30d > 0 else 0.0
    if prev_30d_vel > 0:
        trend_pct = round(((recent_30d_vel - prev_30d_vel) / prev_30d_vel) * 100.0, 1)
    elif recent_30d_vel > 0:
        trend_pct = 100.0
    else:
        trend_pct = 0.0

    if trend_pct >= 5.0:
        trend_direction = "up"
    elif trend_pct <= -5.0:
        trend_direction = "down"
    else:
        trend_direction = "stable"

    if daily_velocity > 0:
        days_until_stockout = round(stock / daily_velocity, 1)
        est_stockout_dt = ref_date + timedelta(days=int(round(days_until_stockout)))
        est_stockout_date = est_stockout_dt.isoformat()
        stockout_gap_days = round(days_until_stockout - lead_days, 1)
        order_by_dt = est_stockout_dt - timedelta(days=lead_days)
        recommended_order_date = "INMEDIATO" if order_by_dt <= ref_date else order_by_dt.isoformat()
    else:
        days_until_stockout = None
        est_stockout_date = None
        stockout_gap_days = None
        recommended_order_date = None

    target_coverage_days = lead_days + safety_days + cycle_days
    target_stock = daily_velocity * target_coverage_days
    effective_stock = stock + incoming
    raw_reorder = max(0.0, target_stock - effective_stock)
    recommended_order_qty = int(math.ceil(raw_reorder)) if raw_reorder > 0 else 0
    est_order_cost = round(recommended_order_qty * cost, 2)

    if daily_velocity == 0:
        alert_level = "SIN MOVIMIENTO" if stock > 0 else "INACTIVO"
    elif days_until_stockout is not None and days_until_stockout <= lead_days:
        alert_level = "CRÍTICO"
    elif days_until_stockout is not None and days_until_stockout <= (lead_days + safety_days):
        alert_level = "ALERTA"
    else:
        alert_level = "SALUDABLE"

    avg_selling_price = round(revenue_all / units_all, 2) if units_all > 0 else round(price, 2)
    ref_price = price if price > 0 else avg_selling_price
    margin_per_unit = round(ref_price - cost, 2) if ref_price > 0 else 0.0
    margin_pct = round((margin_per_unit / ref_price) * 100.0, 1) if ref_price > 0 else 0.0

    profit_window = round(revenue_window - (units_window * cost), 2)
    profit_all = round(revenue_all - (units_all * cost), 2)

    proj_30d = round(daily_velocity * 30.0, 1)
    proj_lead = round(daily_velocity * lead_days, 1)
    proj_60d = round(daily_velocity * 60.0, 1)
    proj_90d = round(daily_velocity * 90.0, 1)

    monthly_series = []
    for offset in range(-5, 1):
        y, m = _shift_month(ref_date.year, ref_date.month, offset)
        ym = f"{y:04d}-{m:02d}"
        m_data = monthly_map.get(ym, {"units_sold": 0.0, "revenue": 0.0})
        monthly_series.append(
            {
                "month": ym,
                "units_sold": round(float(m_data["units_sold"]), 2),
                "revenue": round(float(m_data["revenue"]), 2),
            }
        )

    parsed_entries.sort(key=lambda pair: pair[0] or date.min, reverse=True)
    recent_sales = []
    for pdate, row in parsed_entries[:10]:
        recent_sales.append(
            {
                "posting_date": pdate.isoformat() if pdate else "",
                "invoice_name": str(row.get("invoice_name") or ""),
                "customer_name": str(row.get("customer_name") or row.get("customer") or "Cliente"),
                "qty": round(float(row.get("qty") or 0.0), 2),
                "rate": round(float(row.get("rate") or 0.0), 2),
                "amount": round(float(row.get("amount") or 0.0), 2),
            }
        )

    warehouses = [
        {
            "warehouse": str(w.get("warehouse") or ""),
            "actual_qty": round(float(w.get("actual_qty") or 0.0), 2),
            "ordered_qty": round(float(w.get("ordered_qty") or 0.0), 2),
        }
        for w in (warehouse_stock or [])
        if float(w.get("actual_qty") or 0.0) != 0 or float(w.get("ordered_qty") or 0.0) != 0
    ]

    return {
        "config": {
            "china_lead_time_days": lead_days,
            "safety_stock_days": safety_days,
            "analysis_window_days": window_days,
            "reorder_cycle_days": cycle_days,
        },
        "item": {
            "item_code": code,
            "item_name": name,
            "item_group": group,
            "current_stock": round(stock, 2),
            "incoming_qty": round(incoming, 2),
            "effective_stock": round(effective_stock, 2),
            "pending_dn_qty": round(pending_dn, 2),
            "unit_cost": round(cost, 2),
            "unit_price": round(price, 2),
            "avg_selling_price": avg_selling_price,
            "margin_per_unit": margin_per_unit,
            "margin_pct": margin_pct,
            "stock_value": round(stock * cost, 2),
            "potential_revenue": round(stock * ref_price, 2),
        },
        "sales_summary": {
            "units_7d": round(units_7d, 2),
            "revenue_7d": round(revenue_7d, 2),
            "units_30d": round(units_30d, 2),
            "revenue_30d": round(revenue_30d, 2),
            "units_window": round(units_window, 2),
            "revenue_window": round(revenue_window, 2),
            "profit_window": profit_window,
            "invoices_window": len(invoices_window),
            "units_all_time": round(units_all, 2),
            "revenue_all_time": round(revenue_all, 2),
            "profit_all_time": profit_all,
            "invoices_all_time": len(invoices_all),
            "last_sale_date": max(sale_dates).isoformat() if sale_dates else None,
            "first_sale_date": min(sale_dates).isoformat() if sale_dates else None,
        },
        "velocity_and_trend": {
            "daily_velocity": daily_velocity,
            "weekly_velocity": weekly_velocity,
            "monthly_velocity": monthly_velocity,
            "recent_30d_daily_velocity": recent_30d_vel,
            "prev_30d_daily_velocity": prev_30d_vel,
            "trend_pct": trend_pct,
            "trend_direction": trend_direction,
        },
        "predictions": {
            "alert_level": alert_level,
            "days_until_stockout": days_until_stockout,
            "est_stockout_date": est_stockout_date,
            "will_stockout_before_china": bool(
                days_until_stockout is not None and days_until_stockout < lead_days
            ),
            "stockout_gap_days": stockout_gap_days,
            "recommended_order_qty": recommended_order_qty,
            "est_order_cost": est_order_cost,
            "recommended_order_date": recommended_order_date,
            "projected_demand_30d": proj_30d,
            "projected_revenue_30d": round(proj_30d * ref_price, 2),
            "projected_profit_30d": round(proj_30d * margin_per_unit, 2),
            "projected_demand_lead_time": proj_lead,
            "projected_revenue_lead_time": round(proj_lead * ref_price, 2),
            "projected_demand_60d": proj_60d,
            "projected_revenue_60d": round(proj_60d * ref_price, 2),
            "projected_profit_60d": round(proj_60d * margin_per_unit, 2),
            "projected_demand_90d": proj_90d,
            "projected_revenue_90d": round(proj_90d * ref_price, 2),
            "projected_profit_90d": round(proj_90d * margin_per_unit, 2),
        },
        "monthly_series": monthly_series,
        "warehouse_breakdown": warehouses,
        "recent_sales": recent_sales,
    }
