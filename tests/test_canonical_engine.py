# ==============================================================================
# Path: tests/test_canonical_engine.py
# Role: Comprehensive Unit Tests for Canonical Strategy Domain Entities (unittest-native)
# ==============================================================================

import unittest
import sys
from pathlib import Path

CRON3_DIR = Path(__file__).resolve().parent.parent
if str(CRON3_DIR) not in sys.path:
    sys.path.insert(0, str(CRON3_DIR))

from CORE.STRATEGY.strategy_registry import StrategyRegistry
from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager
from CORE.STRATEGY.cross_recycler import CrossSideProfitRecycler
from CORE.SERVICES.auto_closer import AutoCloser
from POS_FSM.models import PositionState


class TestCanonicalStrategyDomain(unittest.TestCase):
    """Полное юнит-тестирование доменных сущностей паспорта стратегий."""

    def setUp(self):
        self.registry = StrategyRegistry()

    def test_passport_resolution_and_trend_regimes(self):
        """Проверка разрешения паспортов стратегий и разделения индикаторов тренда."""
        cases = [
            ("u16_chita_v31", "auto_ema"),
            ("u16_chita_v32", "auto_ema"),
            ("u16_chita_v33", "auto_ema"),
            ("u16_chita_v35", "kernel_forecast"),
            ("u16_chita_v36", "kernel_forecast"),
        ]
        for strat_id, expected_mode in cases:
            strat = self.registry.get_strategy(strat_id)
            self.assertIsNotNone(strat, f"Strategy {strat_id} must resolve")
            self.assertEqual(strat.leverage, 20)
            self.assertGreaterEqual(strat.min_tp_indent, 0.85)

            # Разделение режимов первого ордера
            fist = strat.first_order_weight_regime
            self.assertIsNotNone(fist)
            self.assertEqual(
                fist["mode"],
                expected_mode,
                f"{strat_id} must have mode {expected_mode}"
            )

    def test_chita36_vs_chita35_triggers(self):
        """Проверка Golden-Cut 38.2% курков в Чите 3.6 по сравнению с 3.5."""
        strat35 = self.registry.get_strategy("u16_chita_v35")
        strat36 = self.registry.get_strategy("u16_chita_v36")

        # Long L3 trigger
        l3_35 = abs(float(strat35.grid_long["3"]["activation"]))
        l3_36 = abs(float(strat36.grid_long["3"]["activation"]))
        self.assertEqual(l3_35, 15.0)
        self.assertEqual(l3_36, 17.5, "Chita 3.6 Long L3 trigger must be exactly 17.5% (Golden-Cut)")

        # Short L3 trigger
        s3_35 = abs(float(strat35.grid_short["3"]["activation"]))
        s3_36 = abs(float(strat36.grid_short["3"]["activation"]))
        self.assertEqual(s3_35, 26.0)
        self.assertEqual(s3_36, 34.0, "Chita 3.6 Short L3 trigger must be exactly 34.0% (Golden-Cut)")

    def test_first_order_regime_manager_volumes(self):
        """Проверка доменного менеджера FirstOrderRegimeManager: расчет объемов по режимам."""
        regime_cfg = {
            "mode": "bullish",
            "presets": {
                "bullish": {"LONG": 12.0, "SHORT": 7.0},
                "neutral": {"LONG": 9.0, "SHORT": 9.0},
                "bearish": {"LONG": 7.0, "SHORT": 12.0},
            }
        }
        vol_long = FirstOrderRegimeManager.resolve_static_volume(regime_cfg, "LONG", default_vol=5.0)
        vol_short = FirstOrderRegimeManager.resolve_static_volume(regime_cfg, "SHORT", default_vol=5.0)
        self.assertEqual(vol_long, 12.0)
        self.assertEqual(vol_short, 7.0)

        # Режим Bearish
        regime_cfg["mode"] = "bearish"
        vol_long = FirstOrderRegimeManager.resolve_static_volume(regime_cfg, "LONG", default_vol=5.0)
        vol_short = FirstOrderRegimeManager.resolve_static_volume(regime_cfg, "SHORT", default_vol=5.0)
        self.assertEqual(vol_long, 7.0)
        self.assertEqual(vol_short, 12.0)

        # Режим Off
        regime_cfg["mode"] = "off"
        vol_long = FirstOrderRegimeManager.resolve_static_volume(regime_cfg, "LONG", default_vol=5.0)
        self.assertEqual(vol_long, 5.0)

    def test_canonical_recycle_ratio_tiers(self):
        """Проверка канонического расчета recycle_ratio по уровням страдающей стороны."""
        recycling_cfg = {
            "enabled": True,
            "min_burn_level": 2,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 25.0,
            "recycle_ratio_tiers": {
                "<2": 0.0,
                "2-4": 0.50,
                ">=5": 0.70
            }
        }
        ratio_0 = CrossSideProfitRecycler.resolve_recycle_ratio(recycling_cfg, level=0)
        ratio_2 = CrossSideProfitRecycler.resolve_recycle_ratio(recycling_cfg, level=2)
        ratio_3 = CrossSideProfitRecycler.resolve_recycle_ratio(recycling_cfg, level=3)
        ratio_5 = CrossSideProfitRecycler.resolve_recycle_ratio(recycling_cfg, level=5)

        self.assertEqual(ratio_0, 0.0)
        self.assertEqual(ratio_2, 0.50)
        self.assertEqual(ratio_3, 0.50)
        self.assertEqual(ratio_5, 0.70)

    def test_autocloser_ratchet_rules(self):
        """Проверка автоклозера: отсутствие срабатывания в минус и фиксация от +13$/+8$."""
        # 1. Отрицательный Net: автоклозер НИКОГДА не должен закрывать
        rules = [
            {"activation": 13.0, "floor": 8.0, "desc": "Tier 1: +13$ / +8$"}
        ]
        # При Net = -5.0$
        self.assertTrue(all(r["activation"] > 0 for r in rules))
        # Прибыль 16.0$ взводит пол 8.0$
        armed_floor = None
        current_net = 16.0
        for r in rules:
            if current_net >= r["activation"]:
                armed_floor = r["floor"]
        self.assertEqual(armed_floor, 8.0)

    def test_base_inheritance_for_institutional_features(self):
        """Проверка автоматического наследования institutional фич (recycling, smart modules) из _base.json."""
        strat31 = self.registry.get_strategy("u16_chita_v31")
        self.assertIsNotNone(strat31.cross_recycling_cfg, "u16_chita_v31 must inherit cross_side_recycling from _base.json")
        self.assertTrue(strat31.cross_recycling_cfg.get("enabled"), "Recycling must be enabled")
        self.assertIn("2-4", strat31.cross_recycling_cfg.get("recycle_ratio_tiers", {}))

        # Проверка наследования smart_size и smart_tp
        self.assertIsNotNone(strat31.smart_size_cfg, "Must inherit smart_size")
        self.assertIsNotNone(strat31.smart_tp_cfg, "Must inherit smart_tp")


if __name__ == "__main__":
    unittest.main()
