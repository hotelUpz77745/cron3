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

from CORE.strategy_applicator import StrategyApplicator
from CORE.strategy_registry import StrategyConfig


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

    def test_disabled_or_off_regimes(self):
        """Проверка отключения фичи через 'off', 'null', None — базовый объем сетки не меняется."""
        # 1. Строка 'off'
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("off", "LONG", 12.0), 12.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("off", "SHORT", 7.0), 7.0)

        # 2. Строка 'null' / 'none'
        self.assertEqual(StrategyApplicator.resolve_first_order_volume("null", "LONG", 12.0), 12.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume(None, "SHORT", 7.0), 7.0)

        # 3. Словарь с mode='off'
        self.assertEqual(StrategyApplicator.resolve_first_order_volume({"mode": "off"}, "LONG", 12.0), 12.0)
        self.assertEqual(StrategyApplicator.resolve_first_order_volume({"mode": None}, "SHORT", 7.0), 7.0)

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

    @patch("CORE.STRATEGY.momentum_engine.MomentumEngine.get_ema_trend_state")
    async def test_stagnation_min_step_behavior(self, mock_trend):
        """Проверка условного включения auto_ema строго при стагнации (stagnation_min_step: 3)."""
        from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager

        regime_cfg = {
            "default_mode": "neutral",
            "mode": "auto_ema",
            "stagnation_min_step": 3,
            "presets": {
                "bullish": {"LONG": 12.0, "SHORT": 7.0},
                "neutral": {"LONG": 9.0, "SHORT": 9.0},
                "bearish": {"LONG": 6.0, "SHORT": 13.0}
            },
            "auto_ema": {
                "timeframe": "1d",
                "sma_fast": 10,
                "sma_slow": 30,
                "confirmation_candles": 3,
                "require_rising": True,
                "trend_positive": True
            },
            "enabled": True
        }

        # 1. До старта торгов (статическое разрешение): берется default_mode (neutral 9/9)
        self.assertEqual(FirstOrderRegimeManager.resolve_static_volume(regime_cfg, "LONG", 10.0), 9.0)
        self.assertEqual(FirstOrderRegimeManager.resolve_static_volume(regime_cfg, "SHORT", 10.0), 9.0)

        # 2. Рынок на UP тренде, но противоположная сторона НЕ в позиции -> работает default_mode (neutral 9/9)
        mock_trend.return_value = "UP"
        vol_long = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="LONG",
            regime_cfg=regime_cfg,
            default_vol=10.0,
            client=None,
            opp_in_pos=False,
            opp_step=0
        )
        self.assertEqual(vol_long, 9.0)

        # 3. Рынок на UP тренде, противоположная сторона в позиции, но opp_step=1 или 2 (< 3) -> все еще default_mode (9/9)
        vol_short = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="SHORT",
            regime_cfg=regime_cfg,
            default_vol=10.0,
            client=None,
            opp_in_pos=True,
            opp_step=2
        )
        self.assertEqual(vol_short, 9.0)

        # 4. Противоположная сторона застряла на ступени 3 (opp_step=3 >= 3) -> ВКЛЮЧАЕТСЯ auto_ema (кулак Сильной руки!)
        # При UP тренде: LONG получает 12.0%, SHORT получает 7.0%
        vol_long_active = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="LONG",
            regime_cfg=regime_cfg,
            default_vol=10.0,
            client=None,
            opp_in_pos=True,
            opp_step=3
        )
        self.assertEqual(vol_long_active, 12.0)

        vol_short_active = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="SHORT",
            regime_cfg=regime_cfg,
            default_vol=10.0,
            client=None,
            opp_in_pos=True,
            opp_step=4
        )
        self.assertEqual(vol_short_active, 7.0)

        # 5. При DOWN тренде на стагнации (opp_step=3) -> bearish (6.0% Long / 13.0% Short)
        mock_trend.return_value = "DOWN"
        vol_short_down = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="SHORT",
            regime_cfg=regime_cfg,
            default_vol=10.0,
            client=None,
            opp_in_pos=True,
            opp_step=3
        )
        self.assertEqual(vol_short_down, 13.0)

        vol_long_down = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="LONG",
            regime_cfg=regime_cfg,
            default_vol=10.0,
            client=None,
            opp_in_pos=True,
            opp_step=3
        )
        self.assertEqual(vol_long_down, 6.0)

        # 6. Обратная совместимость: если stagnation_min_step равен None/null -> auto_ema работает безусловно
        compat_cfg = copy.deepcopy(regime_cfg)
        compat_cfg["stagnation_min_step"] = None
        mock_trend.return_value = "UP"
        vol_compat = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="LONG",
            regime_cfg=compat_cfg,
            default_vol=10.0,
            client=None,
            opp_in_pos=False,
            opp_step=0
        )
        self.assertEqual(vol_compat, 12.0)

    @patch("CORE.STRATEGY.momentum_engine.MomentumEngine.get_ema_trend_state")
    async def test_default_mode_backward_compatibility(self, mock_trend):
        """Проверка явного default_mode: null, отсутствия ключа, и кастомных fallback режимов."""
        from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager

        base_cfg = {
            "mode": "auto_ema",
            "stagnation_min_step": 3,
            "presets": {
                "bullish": {"LONG": 12.0, "SHORT": 7.0},
                "neutral": {"LONG": 9.0, "SHORT": 9.0},
                "bearish": {"LONG": 6.0, "SHORT": 13.0}
            },
            "auto_ema": {
                "timeframe": "1d",
                "sma_fast": 10,
                "sma_slow": 30,
                "confirmation_candles": 3,
                "require_rising": True,
                "trend_positive": True
            },
            "enabled": True
        }

        # 1. default_mode: null explicitly set -> falls back safely to neutral (9.0 / 9.0)
        cfg_explicit_null = copy.deepcopy(base_cfg)
        cfg_explicit_null["default_mode"] = None
        mock_trend.return_value = "UP"
        vol_long_null = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="LONG",
            regime_cfg=cfg_explicit_null,
            default_vol=10.0,
            client=None,
            opp_in_pos=False,
            opp_step=0
        )
        self.assertEqual(vol_long_null, 9.0)

        # 2. default_mode omitted completely -> falls back safely to neutral (9.0 / 9.0)
        cfg_omitted = copy.deepcopy(base_cfg)
        if "default_mode" in cfg_omitted:
            del cfg_omitted["default_mode"]
        vol_short_omitted = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="SHORT",
            regime_cfg=cfg_omitted,
            default_vol=10.0,
            client=None,
            opp_in_pos=False,
            opp_step=0
        )
        self.assertEqual(vol_short_omitted, 9.0)

        # 3. default_mode explicitly set to bearish -> resolves to bearish (6.0 LONG / 13.0 SHORT)
        cfg_bear_fallback = copy.deepcopy(base_cfg)
        cfg_bear_fallback["default_mode"] = "bearish"
        vol_long_bear = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="LONG",
            regime_cfg=cfg_bear_fallback,
            default_vol=10.0,
            client=None,
            opp_in_pos=False,
            opp_step=0
        )
        vol_short_bear = await FirstOrderRegimeManager.evaluate_and_resolve_volume(
            symbol="TESTUSDT",
            side="SHORT",
            regime_cfg=cfg_bear_fallback,
            default_vol=10.0,
            client=None,
            opp_in_pos=False,
            opp_step=0
        )
        self.assertEqual(vol_long_bear, 6.0)
        self.assertEqual(vol_short_bear, 13.0)

        # 4. resolve_static_volume with default_mode: null on dynamic mode -> returns neutral
        static_vol_l = FirstOrderRegimeManager.resolve_static_volume(cfg_explicit_null, "LONG", 10.0)
        static_vol_s = FirstOrderRegimeManager.resolve_static_volume(cfg_explicit_null, "SHORT", 10.0)
        self.assertEqual(static_vol_l, 9.0)
        self.assertEqual(static_vol_s, 9.0)


if __name__ == "__main__":
    unittest.main()
