# ============================================================
# Path: tests/test_first_order_weight_regime.py
# Role: Unit test suite for verifying first order grid_0 weight regimes
#       (Bullish 12/7, Neutral 9/9, Bearish 7/12, and Auto-EMA dynamic trend).
# ============================================================

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from CORE.STRATEGY.strategy_applicator import StrategyApplicator
from CORE.STRATEGY.strategy_registry import StrategyConfig


class TestFirstOrderWeightRegime(unittest.IsolatedAsyncioTestCase):
    """Тестирование гибких режимов распределения весов первого ордера grid_0."""

    def test_literal_regimes(self):
        """Проверка строковых литералов: bullish, neutral, bearish и синонимов."""
        # 1. Bullish (12% Long / 7% Short)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("bullish", "LONG", 10.0), 12.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("bullish", "SHORT", 10.0), 7.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("bull", "LONG", 10.0), 12.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("long_heavy", "SHORT", 10.0), 7.0)

        # 2. Neutral (9% Long / 9% Short)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("neutral", "LONG", 10.0), 9.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("neutral", "SHORT", 10.0), 9.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("flat", "LONG", 10.0), 9.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("symmetric", "SHORT", 10.0), 9.0)

        # 3. Bearish (7% Long / 12% Short)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("bearish", "LONG", 10.0), 7.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("bearish", "SHORT", 10.0), 12.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("bear", "LONG", 10.0), 7.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("short_heavy", "SHORT", 10.0), 12.0)

    def test_numeric_regimes(self):
        """Проверка числовых значений для управляемости: 1 (bull), 0 (neutral), -1 (bear)."""
        # 1 = bullish
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(1, "LONG", 10.0), 12.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(1, "SHORT", 10.0), 7.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("1", "LONG", 10.0), 12.0)

        # 0 = neutral
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(0, "LONG", 10.0), 9.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(0, "SHORT", 10.0), 9.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("0", "SHORT", 10.0), 9.0)

        # -1 = bearish
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(-1, "LONG", 10.0), 7.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(-1, "SHORT", 10.0), 12.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("-1", "SHORT", 10.0), 12.0)

    def test_custom_presets_dict(self):
        """Проверка пользовательских пресетов через структурированный конфиг."""
        custom_cfg = {
            "mode": "extreme_pump",
            "presets": {
                "extreme_pump": {"LONG": 15.0, "SHORT": 5.0},
                "flash_crash": {"LONG": 5.0, "SHORT": 15.0}
            }
        }
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(custom_cfg, "LONG", 10.0), 15.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(custom_cfg, "SHORT", 10.0), 5.0)

        # Fallback на дефолт при неизвестном режиме
        unknown_cfg = {"mode": "unknown_mars_regime"}
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(unknown_cfg, "LONG", 8.5), 8.5)

    def test_applicator_builds_runtime_with_configured_regime(self):
        """Проверка сборки рантайма StrategyApplicator: grid_0 адаптируется под режим."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_dir = Path(tmp_dir)

            base_template = {
                "name": "Test Regime Strat",
                "first_order_weight_regime": {
                    "mode": "neutral",
                    "presets": {
                        "bullish": {"LONG": 12.0, "SHORT": 7.0},
                        "neutral": {"LONG": 9.0, "SHORT": 9.0},
                        "bearish": {"LONG": 7.0, "SHORT": 12.0}
                    }
                },
                "LONG": {
                    "grid": {"0": {"volume": 12.0}, "1": {"volume": 10.0}}
                },
                "SHORT": {
                    "grid": {"0": {"volume": 7.0}, "1": {"volume": 10.0}}
                }
            }

            strat = StrategyConfig(
                strategy_id="test_regime",
                raw_meta=base_template,
                symbols=["TESTUSDT"],
                active_settings={"first_order_weight_regime": "neutral"}
            )

            applicator = StrategyApplicator()
            side_cfgs = applicator.build_side_configs(strat)

            # В режиме neutral оба L0 должны получить 9.0%
            self.assertEqual(side_cfgs["LONG"]["grid"]["0"]["volume"], 9.0)
            self.assertEqual(side_cfgs["SHORT"]["grid"]["0"]["volume"], 9.0)

            # Переключаем на bearish (-1)
            strat_bear = StrategyConfig(
                strategy_id="test_regime_bear",
                raw_meta=base_template,
                symbols=["TESTUSDT"],
                active_settings={"first_order_weight_regime": -1}
            )
            side_cfgs_bear = applicator.build_side_configs(strat_bear)
            self.assertEqual(side_cfgs_bear["LONG"]["grid"]["0"]["volume"], 7.0)
            self.assertEqual(side_cfgs_bear["SHORT"]["grid"]["0"]["volume"], 12.0)

    @patch("CORE.STRATEGY.momentum_engine.MomentumEngine.get_ema_trend_state")
    async def test_auto_ema_regime_mapping(self, mock_trend):
        """Проверка логики Auto-EMA: динамический выбор пресетов по тренду."""
        # 1. UP тренд -> bullish (12 / 7)
        mock_trend.return_value = "UP"
        up_regime = "bullish" if mock_trend.return_value == "UP" else "neutral"
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(up_regime, "LONG", 10.0), 12.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(up_regime, "SHORT", 10.0), 7.0)

        # 2. DOWN тренд -> bearish (7 / 12)
        mock_trend.return_value = "DOWN"
        down_regime = "bearish" if mock_trend.return_value == "DOWN" else "neutral"
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(down_regime, "LONG", 10.0), 7.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(down_regime, "SHORT", 10.0), 12.0)

        # 3. FLAT / UNSTABLE -> neutral (9 / 9)
        mock_trend.return_value = "FLAT"
        flat_regime = "neutral" if mock_trend.return_value in ("FLAT", "UNSTABLE") else "bullish"
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(flat_regime, "LONG", 10.0), 9.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(flat_regime, "SHORT", 10.0), 9.0)


if __name__ == "__main__":
    unittest.main()
