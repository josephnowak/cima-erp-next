import unittest
from datetime import date

from cima_ve.api.bi import _resolve_window_days, get_product_bi_analytics
from cima_ve.services.bi_service import (
    analyze_inventory_intelligence,
    build_product_analytics,
    build_weekly_stockout_email_html,
)


class BiServiceTests(unittest.TestCase):
    def test_china_lead_time_stockout_and_reorder_calculation(self) -> None:
        items = [
            {
                "item_code": "ITEM-FAST",
                "item_name": "Cargador Rápido 65W",
                "item_group": "Accesorios",
                "stock_qty": 30,
                "incoming_qty": 0,
                "valuation_rate": 8.0,
                "standard_rate": 20.0,
            },
            {
                "item_code": "ITEM-SLOW",
                "item_name": "Cable VGA 5m",
                "item_group": "Cables",
                "stock_qty": 50,
                "incoming_qty": 0,
                "valuation_rate": 3.0,
                "standard_rate": 8.0,
            },
        ]
        sales_rows = [
            {"item_code": "ITEM-FAST", "qty": 90, "amount": 1800.0},  # 1/day over 90d -> 30d left < 45d China
        ]
        res = analyze_inventory_intelligence(
            items=items,
            sales_rows=sales_rows,
            china_lead_time_days=45,
            safety_stock_days=15,
            analysis_window_days=90,
            reorder_cycle_days=30,
            today=date(2026, 11, 20),
        )
        self.assertEqual(res["kpis"]["critical_count"], 1)
        self.assertEqual(res["kpis"]["dead_stock_count"], 1)
        crit = res["critical_items"][0]
        self.assertEqual(crit["item_code"], "ITEM-FAST")
        self.assertTrue(crit["will_stockout_before_china"])
        self.assertEqual(crit["days_until_stockout"], 30.0)
        # Target = 90 days * 1/day = 90; current stock = 30 -> order 60
        self.assertEqual(crit["recommended_order_qty"], 60)

        email_html = build_weekly_stockout_email_html(res)
        self.assertIn("Cargador Rápido 65W", email_html)
        self.assertIn("45 días", email_html)

    def test_build_product_analytics_complete_breakdown_and_predictions(self) -> None:
        item = {
            "item_code": "ITEM-FAST",
            "item_name": "Cargador Rápido 65W",
            "item_group": "Accesorios",
            "stock_qty": 20,
            "incoming_qty": 10,
            "valuation_rate": 8.0,
            "price": 20.0,
        }
        sales_entries = [
            {
                "posting_date": "2026-11-18",
                "invoice_name": "ACC-SINV-0003",
                "customer_name": "Ana Pérez",
                "qty": 30,
                "rate": 20.0,
                "amount": 600.0,
            },
            {
                "posting_date": "2026-11-05",
                "invoice_name": "ACC-SINV-0002",
                "customer_name": "Carlos Ruiz",
                "qty": 30,
                "rate": 20.0,
                "amount": 600.0,
            },
            {
                "posting_date": "2026-10-10",
                "invoice_name": "ACC-SINV-0001",
                "customer_name": "Luis Gómez",
                "qty": 30,
                "rate": 20.0,
                "amount": 600.0,
            },
            {
                "posting_date": "2026-07-01",
                "invoice_name": "ACC-SINV-0000",
                "customer_name": "Viejo Cliente",
                "qty": 10,
                "rate": 18.0,
                "amount": 180.0,
            },
        ]
        warehouses = [
            {"warehouse": "Almacén Principal - CEV", "actual_qty": 20, "ordered_qty": 10},
        ]
        res = build_product_analytics(
            item=item,
            sales_entries=sales_entries,
            warehouse_stock=warehouses,
            pending_dn_qty=4,
            china_lead_time_days=45,
            safety_stock_days=15,
            analysis_window_days=90,
            reorder_cycle_days=30,
            today=date(2026, 11, 20),
        )

        self.assertEqual(res["item"]["item_code"], "ITEM-FAST")
        self.assertEqual(res["item"]["effective_stock"], 30.0)
        self.assertEqual(res["item"]["pending_dn_qty"], 4.0)
        self.assertEqual(res["item"]["margin_per_unit"], 12.0)
        self.assertEqual(res["item"]["margin_pct"], 60.0)

        self.assertEqual(res["sales_summary"]["units_7d"], 30.0)
        self.assertEqual(res["sales_summary"]["units_30d"], 60.0)
        self.assertEqual(res["sales_summary"]["units_window"], 90.0)
        self.assertEqual(res["sales_summary"]["units_all_time"], 100.0)
        self.assertEqual(res["sales_summary"]["revenue_all_time"], 1980.0)
        self.assertEqual(res["sales_summary"]["invoices_all_time"], 4)

        self.assertEqual(res["velocity_and_trend"]["daily_velocity"], 1.0)
        self.assertEqual(res["velocity_and_trend"]["trend_direction"], "up")
        self.assertEqual(res["velocity_and_trend"]["trend_pct"], 100.0)

        self.assertEqual(res["predictions"]["alert_level"], "CRÍTICO")
        self.assertEqual(res["predictions"]["days_until_stockout"], 20.0)
        self.assertTrue(res["predictions"]["will_stockout_before_china"])
        self.assertEqual(res["predictions"]["recommended_order_date"], "INMEDIATO")
        self.assertEqual(res["predictions"]["recommended_order_qty"], 60)
        self.assertEqual(res["predictions"]["projected_demand_30d"], 30.0)
        self.assertEqual(res["predictions"]["projected_revenue_30d"], 600.0)
        self.assertEqual(res["predictions"]["projected_demand_lead_time"], 45.0)
        self.assertEqual(res["predictions"]["projected_demand_60d"], 60.0)
        self.assertEqual(res["predictions"]["projected_demand_90d"], 90.0)

        self.assertEqual(len(res["monthly_series"]), 6)
        self.assertEqual(res["monthly_series"][-1]["month"], "2026-11")
        self.assertEqual(res["monthly_series"][-1]["units_sold"], 60.0)
        self.assertEqual(len(res["warehouse_breakdown"]), 1)
        self.assertEqual(len(res["recent_sales"]), 4)
        self.assertTrue(callable(get_product_bi_analytics))

    def test_build_product_analytics_zero_sales(self) -> None:
        res = build_product_analytics(
            item={"item_code": "DEAD-1", "item_name": "Cable Viejo", "stock_qty": 15, "valuation_rate": 2.0, "price": 5.0},
            sales_entries=[],
            today=date(2026, 11, 20),
        )
        self.assertEqual(res["predictions"]["alert_level"], "SIN MOVIMIENTO")
        self.assertIsNone(res["predictions"]["days_until_stockout"])
        self.assertEqual(res["predictions"]["recommended_order_qty"], 0)
        self.assertEqual(res["velocity_and_trend"]["trend_direction"], "stable")
        self.assertEqual(len(res["monthly_series"]), 6)

    def test_build_product_analytics_custom_window_30d_and_resolve_window_days(self) -> None:
        class DummySettings:
            bi_analysis_window_days = 90

        self.assertEqual(_resolve_window_days(DummySettings(), None), 90)
        self.assertEqual(_resolve_window_days(DummySettings(), "30"), 30)
        self.assertEqual(_resolve_window_days(DummySettings(), 15), 15)
        self.assertEqual(_resolve_window_days(DummySettings(), "invalid"), 90)
        self.assertEqual(_resolve_window_days(DummySettings(), 0), 90)

        item = {"item_code": "ITEM-W", "item_name": "Producto Ventana", "stock_qty": 30, "valuation_rate": 5.0, "price": 15.0}
        sales_entries = [
            {"posting_date": "2026-11-15", "invoice_name": "INV-1", "qty": 30, "rate": 15.0, "amount": 450.0},
            {"posting_date": "2026-09-25", "invoice_name": "INV-2", "qty": 60, "rate": 15.0, "amount": 900.0},
        ]
        res_30 = build_product_analytics(
            item=item,
            sales_entries=sales_entries,
            analysis_window_days=30,
            today=date(2026, 11, 20),
        )
        self.assertEqual(res_30["config"]["analysis_window_days"], 30)
        self.assertEqual(res_30["sales_summary"]["units_window"], 30.0)
        self.assertEqual(res_30["sales_summary"]["revenue_window"], 450.0)
        self.assertEqual(res_30["velocity_and_trend"]["daily_velocity"], 1.0)
        self.assertEqual(res_30["predictions"]["projected_demand_30d"], 30.0)

        res_90 = build_product_analytics(
            item=item,
            sales_entries=sales_entries,
            analysis_window_days=90,
            today=date(2026, 11, 20),
        )
        self.assertEqual(res_90["config"]["analysis_window_days"], 90)
        self.assertEqual(res_90["sales_summary"]["units_window"], 90.0)
        self.assertEqual(res_90["sales_summary"]["revenue_window"], 1350.0)
        self.assertEqual(res_90["velocity_and_trend"]["daily_velocity"], 1.0)


if __name__ == "__main__":
    unittest.main()
