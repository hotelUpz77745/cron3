# ==============================================================================
# Path: tests/test_coin_grid_analytics.py
# Role: Unit tests for CoinGridRiskAnalytics service
# ==============================================================================

import unittest
from CORE.SERVICES.coin_grid_analytics import CoinGridRiskAnalytics


class TestCoinGridRiskAnalytics(unittest.TestCase):
    def setUp(self):
        self.analytics = CoinGridRiskAnalytics(strategy_id="test_zenith13")

    def test_single_cycle_l0_take_profit(self):
        """Проверка простого цикла L0 без усреднений: вход -> TP."""
        # 1. Вход в SHORT по цене 100, объем 10 (номинал 1000$)
        self.analytics.record_fill(
            symbol="BTCUSDT",
            side="SHORT",
            fill_price=100.0,
            qty=10.0,
            is_opening=True,
            pos_qty_after=10.0,
            pos_entry_after=100.0,
            fee=0.5,
            level_idx=0,
            timestamp=1000.0,
        )

        self.assertIn("BTCUSDT", self.analytics.open_symbols)
        self.assertEqual(len(self.analytics.active_cycles), 1)
        self.assertEqual(self.analytics.level_fills_count["L0"], 1)

        # 2. Цена колеблется: памп до 105 (откат 5% против шорта)
        self.analytics.update_price("BTCUSDT", price=105.0, timestamp=1100.0)
        cycle = self.analytics.active_cycles[("BTCUSDT", "SHORT")]
        self.assertEqual(cycle["max_adverse_from_entry0_pct"], 5.0)
        self.assertEqual(cycle["max_floating_dd_usdt"], 50.0) # (105 - 100) * 10 = 50

        # 3. Закрытие по Take Profit на цене 98 (прибыль (100 - 98) * 10 = 20)
        self.analytics.record_fill(
            symbol="BTCUSDT",
            side="SHORT",
            fill_price=98.0,
            qty=10.0,
            is_opening=False,
            pos_qty_after=0.0,
            pos_entry_after=0.0,
            fee=0.2,
            timestamp=1600.0,
            close_reason="TP",
        )

        self.assertNotIn("BTCUSDT", self.analytics.open_symbols)
        self.assertEqual(len(self.analytics.completed_cycles), 1)
        c = self.analytics.completed_cycles[0]
        self.assertEqual(c["max_level_reached"], 0)
        self.assertEqual(c["realized_pnl"], 20.0)
        self.assertAlmostEqual(c["net_pnl"], 19.3, places=2) # 20 - 0.5 - 0.2
        self.assertTrue(c["is_win"])
        self.assertEqual(self.analytics.cycle_depth_counts["L0"], 1)

    def test_averaging_cycle_l1_l2_and_adverse_excursion(self):
        """Проверка цикла с усреднением до L2 и глубоким откатом."""
        # L0 вход в LONG по 100, qty=1.0
        self.analytics.record_fill(
            symbol="ETHUSDT",
            side="LONG",
            fill_price=100.0,
            qty=1.0,
            is_opening=True,
            pos_qty_after=1.0,
            pos_entry_after=100.0,
            level_idx=0,
            timestamp=1000.0,
        )

        # Цена падает до 96.5 (-3.5%) -> срабатывает усреднение L1
        self.analytics.update_price("ETHUSDT", price=96.5, timestamp=1200.0)
        self.analytics.record_fill(
            symbol="ETHUSDT",
            side="LONG",
            fill_price=96.5,
            qty=1.0,
            is_opening=True,
            pos_qty_after=2.0,
            pos_entry_after=98.25,
            level_idx=1,
            timestamp=1201.0,
        )

        # Цена падает до 94.5 (-5.5%) -> срабатывает усреднение L2
        self.analytics.update_price("ETHUSDT", price=94.5, timestamp=1400.0)
        self.analytics.record_fill(
            symbol="ETHUSDT",
            side="LONG",
            fill_price=94.5,
            qty=1.0,
            is_opening=True,
            pos_qty_after=3.0,
            pos_entry_after=97.0,
            level_idx=2,
            timestamp=1401.0,
        )

        # Экстремальный сквиз вниз до 80 (-20% от L0)
        self.analytics.update_price("ETHUSDT", price=80.0, timestamp=1500.0)
        cycle = self.analytics.active_cycles[("ETHUSDT", "LONG")]
        self.assertEqual(cycle["max_level_reached"], 2)
        self.assertEqual(cycle["max_adverse_from_entry0_pct"], 20.0) # (100 - 80) / 100 = 20%
        # Floating DD: (97 - 80) * 3 = 51.0
        self.assertEqual(cycle["max_floating_dd_usdt"], 51.0)

        # Отскок вверх и закрытие по тейку на 99.0
        self.analytics.record_fill(
            symbol="ETHUSDT",
            side="LONG",
            fill_price=99.0,
            qty=3.0,
            is_opening=False,
            pos_qty_after=0.0,
            pos_entry_after=0.0,
            level_idx=None,
            timestamp=1800.0,
            close_reason="TP",
        )

        self.assertEqual(self.analytics.cycle_depth_counts["L2"], 1)
        summary = self.analytics.get_summary()
        self.assertEqual(summary["total_cycles"], 1)
        self.assertEqual(summary["average_cycle_level"], 2.0)
        self.assertEqual(summary["grid_level_distribution"]["L0"]["fills_count"], 1)
        self.assertEqual(summary["grid_level_distribution"]["L1"]["fills_count"], 1)
        self.assertEqual(summary["grid_level_distribution"]["L2"]["fills_count"], 1)

    def test_critical_thresholds_l8_and_sl100(self):
        """Проверка фиксации превышения уровня L8 (-89%) и стоп-лосса 120% / 130%."""
        # SHORT по 10.0
        self.analytics.record_fill(
            symbol="MEMEUSDT",
            side="SHORT",
            fill_price=10.0,
            qty=100.0,
            is_opening=True,
            pos_qty_after=100.0,
            pos_entry_after=10.0,
            level_idx=0,
            timestamp=1000.0,
        )

        # Памп на 120% (цена улетает с 10.0 до 22.0)
        self.analytics.update_price("MEMEUSDT", price=22.0, timestamp=2000.0)

        # Принудительное закрытие (например, аварийный сброс или ротация)
        self.analytics.force_close_all_cycles(close_reason="STOP_LOSS", timestamp=2100.0)

        summary = self.analytics.get_summary()
        coin_info = summary["per_coin_summary"]["MEMEUSDT"]
        self.assertEqual(coin_info["exceeded_l8_count"], 1)
        self.assertEqual(coin_info["would_hit_sl_120_count"], 1)
        self.assertEqual(coin_info["would_hit_sl_130_count"], 0)
        self.assertEqual(summary["total_exceeded_l8"], 1)
        self.assertEqual(summary["total_would_hit_sl_120"], 1)
        self.assertEqual(summary["total_would_hit_sl_130"], 0)

        # Дополнительный цикл с пампом на 135% (касание SL 130%)
        self.analytics.record_fill(
            symbol="ROCKETUSDT",
            side="SHORT",
            fill_price=10.0,
            qty=10.0,
            is_opening=True,
            pos_qty_after=10.0,
            pos_entry_after=10.0,
            level_idx=0,
            timestamp=3000.0,
        )
        self.analytics.update_price("ROCKETUSDT", price=23.5, timestamp=3100.0)
        self.analytics.force_close_symbol_cycle("ROCKETUSDT", close_reason="STOP_LOSS", timestamp=3200.0)

        summary2 = self.analytics.get_summary()
        r_info = summary2["per_coin_summary"]["ROCKETUSDT"]
        self.assertEqual(r_info["would_hit_sl_120_count"], 1)
        self.assertEqual(r_info["would_hit_sl_130_count"], 1)
        self.assertEqual(summary2["total_would_hit_sl_130"], 1)

    def test_dynamic_grid_schema_loading(self):
        """Проверка динамической загрузки схемы сетки из конфига вселенной без рассинхрона."""
        zenith = CoinGridRiskAnalytics(strategy_id="u96_omni_zenith13")
        meta = zenith.level_metadata
        self.assertIn("L0", meta)
        self.assertIn("L8", meta)
        # Проверяем устранение бага двойного источника истины:
        self.assertEqual(meta["L0"]["volume_pct"], 7.0)
        self.assertEqual(meta["L1"]["indent_pct"], -3.0)

    def test_merge_analytics(self):
        """Проверка корректного слияния данных между декадами."""
        a1 = CoinGridRiskAnalytics("strat1")
        a2 = CoinGridRiskAnalytics("strat1")

        # Dec 1
        a1.record_fill("SOLUSDT", "LONG", 100.0, 1.0, True, 1.0, 100.0, level_idx=0)
        a1.record_fill("SOLUSDT", "LONG", 102.0, 1.0, False, 0.0, 0.0)

        # Dec 2
        a2.record_fill("SOLUSDT", "LONG", 102.0, 1.0, True, 1.0, 102.0, level_idx=0)
        a2.record_fill("SOLUSDT", "LONG", 98.0, 1.0, True, 2.0, 100.0, level_idx=1)
        a2.record_fill("SOLUSDT", "LONG", 101.0, 2.0, False, 0.0, 0.0)

        a1.merge(a2)
        summary = a1.get_summary()

        self.assertEqual(summary["total_cycles"], 2)
        self.assertEqual(summary["total_fills"], 3)
        self.assertEqual(summary["grid_level_distribution"]["L0"]["fills_count"], 2)
        self.assertEqual(summary["grid_level_distribution"]["L1"]["fills_count"], 1)
        self.assertEqual(summary["grid_level_distribution"]["L0"]["cycles_max_depth_count"], 1)
        self.assertEqual(summary["grid_level_distribution"]["L1"]["cycles_max_depth_count"], 1)

    def test_table_formatting(self):
        """Проверка генерации ASCII-таблиц без исключений."""
        self.analytics.record_fill("BTCUSDT", "SHORT", 100.0, 1.0, True, 1.0, 100.0, level_idx=0)
        self.analytics.record_fill("BTCUSDT", "SHORT", 95.0, 1.0, False, 0.0, 0.0)

        tbl_grid = self.analytics.format_grid_distribution_table()
        tbl_coin = self.analytics.format_per_coin_table()
        tbl_worst = self.analytics.format_worst_drawdowns_table()

        self.assertIn("РАСПРЕДЕЛЕНИЕ УРОВНЕЙ СЕТКИ", tbl_grid)
        self.assertIn("BTCUSDT", tbl_coin)
        self.assertIn("ТОП НАИХУДШИХ ПРОСАДОК", tbl_worst)


if __name__ == "__main__":
    unittest.main()

