# tests/test_canonical_strategy_parity.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: tests.test_canonical_strategy_parity
ROLE: Unit tests for strategy passport parity and CrossSideProfitRecycler
==============================================================================
"""

import unittest
from pathlib import Path

from CORE.STRATEGY.strategy_registry import StrategyRegistry
from CORE.STRATEGY.cross_recycler import CrossSideProfitRecycler


class TestCanonicalStrategyParity(unittest.TestCase):
    """
    Гарантирует 100% паритет и целостность сигнатур паспортов стратегий
    и математической рециркуляции кросс-тейков.
    """

    def setUp(self):
        self.registry = StrategyRegistry()
        self.chita36 = self.registry.get_strategy("u16_chita_v36")
        self.chita31 = self.registry.get_strategy("u16_chita_v31")

    def test_chita36_canonical_keys_existence(self):
        """Проверяет наличие всех канонических нормализованных полей в объекте стратегии Читы 3.6."""
        for attr in [
            "name", "invest_size", "leverage", "min_tp_indent",
            "grid_long", "grid_short", "tp_map_long", "tp_map_short",
            "cross_side_recycling", "first_order_weight_regime"
        ]:
            self.assertTrue(hasattr(self.chita36, attr), f"Атрибут '{attr}' обязан быть в StrategyConfig")

    def test_recycling_tier_resolution_parity_with_live_bot(self):
        """
        Проверяет, что CrossSideProfitRecycler возвращает расчетный burn_ratio на всех уровнях сетки.
        """
        rec_cfg = self.chita36.cross_side_recycling
        self.assertTrue(rec_cfg.get("enabled", True))

        # Проверка канонических значений Читы 3.6:
        # L0, L1: 0.0 (до L2 не сжигаем)
        # L2, L3, L4: 0.50 (50% прибыли)
        # L5, L6, L7: 0.75 (75% прибыли)
        self.assertEqual(CrossSideProfitRecycler.resolve_recycle_ratio(rec_cfg, 0), 0.0)
        self.assertEqual(CrossSideProfitRecycler.resolve_recycle_ratio(rec_cfg, 1), 0.0)
        self.assertEqual(CrossSideProfitRecycler.resolve_recycle_ratio(rec_cfg, 2), 0.50)
        self.assertEqual(CrossSideProfitRecycler.resolve_recycle_ratio(rec_cfg, 3), 0.50)
        self.assertEqual(CrossSideProfitRecycler.resolve_recycle_ratio(rec_cfg, 4), 0.50)
        self.assertEqual(CrossSideProfitRecycler.resolve_recycle_ratio(rec_cfg, 5), 0.75)
        self.assertEqual(CrossSideProfitRecycler.resolve_recycle_ratio(rec_cfg, 6), 0.75)

    def test_min_burn_level_parity(self):
        """Проверка минимального уровня для начала рециркуляции."""
        rec_cfg = self.chita36.cross_side_recycling
        live_min = CrossSideProfitRecycler.resolve_min_burn_level(rec_cfg)
        self.assertEqual(live_min, 2, "В Чите 3.6 минимальный уровень сжигания обязан быть L2")

    def test_burn_slice_calculation_exact_match(self):
        """
        Проверяет математику расчета среза:
        Прибыль +$100, страдающая нога SHORT: avg=1.0, current_price=1.20 (DD -20%), vol=100.
        """
        rec_cfg = self.chita36.cross_side_recycling
        # На шаге 3 burn_ratio = 0.50 -> бюджет $50
        burn_qty, spent, burned_notional = CrossSideProfitRecycler.calculate_burn_slice(
            net_profit=100.0,
            opp_side="SHORT",
            opp_avg_price=1.0,
            opp_vol_qty=100.0,
            opp_cur_step=3,
            current_price=1.20,
            recycling_cfg=rec_cfg,
            taker_fee=0.0005
        )

        loss_per_unit = (1.20 - 1.0) + (1.20 * 0.0005)  # 0.2006
        expected_qty = min(50.0 / loss_per_unit, 100.0 * 0.35)  # max_slice 35%
        self.assertAlmostEqual(burn_qty, expected_qty, places=4)
        self.assertAlmostEqual(spent, burn_qty * loss_per_unit, places=4)
        self.assertAlmostEqual(burned_notional, burn_qty * 1.20, places=4)

    def test_trailing_golden_cut_triggers_presence(self):
        """Проверяет наличие курков Golden-Cut (38.2%) в Чите 3.6."""
        grid_long = self.chita36.grid_long
        grid_short = self.chita36.grid_short

        # Long L3: floor -13.5%, trigger -17.5%
        self.assertEqual(float(grid_long["3"]["indent"]), -13.5)
        self.assertEqual(float(grid_long["3"]["activation"]), -17.5)

        # Long L4: floor -24.0%, trigger -35.8%
        self.assertEqual(float(grid_long["4"]["indent"]), -24.0)
        self.assertEqual(float(grid_long["4"]["activation"]), -35.8)

        # Short L3: floor -21.0%, trigger -34.0%
        self.assertEqual(float(grid_short["3"]["indent"]), -21.0)
        self.assertEqual(float(grid_short["3"]["activation"]), -34.0)

    def test_half_peel_parameters_presence(self):
        """Проверяет параметры Half-Peel 50% на мостике Змея L3."""
        l3_long = self.chita36.grid_long["3"]
        self.assertEqual(float(l3_long["peel_ratio"]), 0.50)
        self.assertEqual(float(l3_long["peel_tp_pct"]), 1.20)
        self.assertEqual(int(l3_long["peel_num"]), 1)


if __name__ == "__main__":
    unittest.main()
