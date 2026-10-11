# ==============================================================================
# Path: tests/test_trend_size_depender.py
# Role: Модульные тесты для TrendSizeDepender, режимов сайзинга ("static", "dyn_inv_16",
#       "dyn_ema_16", "dyn_ema_10") и их интеграции со StrategyConfig и FSM.
# ==============================================================================

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import numpy as np

from CORE.STRATEGY.trend_size_depender import TrendSizeDepender, TrendCalculator
from CORE.STRATEGY.strategy_registry import StrategyRegistry, StrategyConfig
from POS_FSM.models import PositionState
from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
from CORE.GRID.avg_manager import AverageManager


class TestTrendSizeDepender(unittest.TestCase):
    def setUp(self):
        TrendSizeDepender.clear_cache()

    def test_grade_to_weights_neutral(self):
        """Проверка нейтрального уровня (Grade 0): строго 50.0% / 50.0%."""
        w_l, w_s = TrendSizeDepender.grade_to_weights(0)
        self.assertAlmostEqual(w_l, 50.0, places=4)
        self.assertAlmostEqual(w_s, 50.0, places=4)
        self.assertAlmostEqual(w_l + w_s, 100.0, places=4)

    def test_grade_to_weights_extremes_scale_16(self):
        """Проверка экстремумов Scale 16: диапазон [33.3333%, 66.6667%]."""
        # Максимальный бычий грейд (+16)
        w_l_max, w_s_min = TrendSizeDepender.calculate_weights_from_grade(16, scale=16)
        self.assertAlmostEqual(w_l_max, 66.6667, places=3)
        self.assertAlmostEqual(w_s_min, 33.3333, places=3)
        self.assertAlmostEqual(w_l_max + w_s_min, 100.0, places=4)

        # Максимальный медвежий грейд (-16)
        w_l_min, w_s_max = TrendSizeDepender.calculate_weights_from_grade(-16, scale=16)
        self.assertAlmostEqual(w_l_min, 33.3333, places=3)
        self.assertAlmostEqual(w_s_max, 66.6667, places=3)
        self.assertAlmostEqual(w_l_min + w_s_max, 100.0, places=4)

    def test_grade_to_weights_extremes_scale_10(self):
        """Проверка экстремумов Scale 10: диапазон [40.0%, 60.0%]."""
        # Максимальный бычий грейд (+10)
        w_l_max, w_s_min = TrendSizeDepender.calculate_weights_from_grade(10, scale=10)
        self.assertAlmostEqual(w_l_max, 60.0, places=3)
        self.assertAlmostEqual(w_s_min, 40.0, places=3)
        self.assertAlmostEqual(w_l_max + w_s_min, 100.0, places=4)

        # Максимальный медвежий грейд (-10)
        w_l_min, w_s_max = TrendSizeDepender.calculate_weights_from_grade(-10, scale=10)
        self.assertAlmostEqual(w_l_min, 40.0, places=3)
        self.assertAlmostEqual(w_s_max, 60.0, places=3)
        self.assertAlmostEqual(w_l_min + w_s_max, 100.0, places=4)

    def test_grade_weights_symmetry_and_invariants(self):
        """Инвариант: для всех 33 градаций k in [-16, 16] сумма весов равна 100% и капитал инвариантен."""
        base_invest = 600.0
        for k in range(-16, 17):
            w_l, w_s = TrendSizeDepender.grade_to_weights(k)
            self.assertAlmostEqual(w_l + w_s, 100.0, places=4, msg=f"Grade {k}: sum != 100%")
            self.assertTrue(33.0 <= w_l <= 67.0, msg=f"Grade {k}: w_l {w_l} out of bounds")
            self.assertTrue(33.0 <= w_s <= 67.0, msg=f"Grade {k}: w_s {w_s} out of bounds")

            # Проверка инварианта капитала: Invest_LONG + Invest_SHORT = 2 * BaseInvest
            inv_l, inv_s = TrendSizeDepender.calculate_invest_sizes(base_invest, w_l, w_s)
            self.assertAlmostEqual(inv_l + inv_s, 2.0 * base_invest, places=4, msg=f"Grade {k}: capital invariant violated")

    def test_evaluate_grade_bb_inverted(self):
        """Проверка расчета грейда на основе инвертированного Боллинджера 4h."""
        flat_closes = np.array([100.0] * 25)
        grade = TrendSizeDepender.evaluate_grade_bb_inverted_lockout(flat_closes, period=20, multiplier=2.0)
        self.assertEqual(grade, 0)

        # Памп вверх -> инвертированный грейд уменьшает LONG (грейд 0 из-за локаута)
        bull_closes = np.array([10.0 + i for i in range(24)] + [200.0])
        grade_bull = TrendSizeDepender.evaluate_grade_bb_inverted_lockout(bull_closes, period=20, multiplier=2.0)
        self.assertEqual(grade_bull, 0)

    def test_evaluate_grade_ema_trend(self):
        """Проверка расчета грейда EMA Trend (быстрая 10 / медленная 30)."""
        closes = np.array([10.0 + i * 2.0 for i in range(40)])
        grade = TrendSizeDepender.evaluate_grade_ema_trend(closes, cfg={"sma_fast": 10, "sma_slow": 30, "scale": 16})
        self.assertEqual(grade, 16)

        down_closes = np.array([100.0 - i * 2.0 for i in range(40)])
        grade_down = TrendSizeDepender.evaluate_grade_ema_trend(down_closes, cfg={"sma_fast": 10, "sma_slow": 30, "scale": 16})
        self.assertEqual(grade_down, -16)

    def test_strategy_config_sizing_mode_string(self):
        """Проверка: 4 строковых sizing_mode ('static', 'dyn_inv_16', 'dyn_ema_16', 'dyn_ema_10') и legacy-маппинг."""
        raw_meta = {
            "name": "Test Strategy",
            "LONG": {"invest_size": 600.0},
            "SHORT": {"invest_size": 600.0},
        }

        # 1. dyn_inv_16
        act_inv = {"sizing_mode": "dyn_inv_16", "mode": "classic"}
        cfg_inv = StrategyConfig("test_strat", raw_meta, {}, act_inv)
        self.assertEqual(cfg_inv.sizing_mode, "dyn_inv_16")
        self.assertTrue(cfg_inv.trend_size_cfg["enabled"])
        self.assertEqual(cfg_inv.trend_size_cfg["trend_mode"], "bb_inverted_lockout")
        self.assertEqual(cfg_inv.sizing_modes, ["dyn_inv_16"])

        # 2. dyn_ema_16
        act_ema16 = {"sizing_mode": "dyn_ema_16", "mode": "classic"}
        cfg_ema16 = StrategyConfig("test_strat", raw_meta, {}, act_ema16)
        self.assertEqual(cfg_ema16.sizing_mode, "dyn_ema_16")
        self.assertTrue(cfg_ema16.trend_size_cfg["enabled"])
        self.assertEqual(cfg_ema16.trend_size_cfg["trend_mode"], "ema_trend_4h")
        self.assertEqual(cfg_ema16.trend_size_cfg["scale"], 16)
        self.assertEqual(cfg_ema16.sizing_modes, ["dyn_ema_16"])

        # 3. dyn_ema_10
        act_ema10 = {"sizing_mode": "dyn_ema_10", "mode": "classic"}
        cfg_ema10 = StrategyConfig("test_strat", raw_meta, {}, act_ema10)
        self.assertEqual(cfg_ema10.sizing_mode, "dyn_ema_10")
        self.assertTrue(cfg_ema10.trend_size_cfg["enabled"])
        self.assertEqual(cfg_ema10.trend_size_cfg["trend_mode"], "ema_trend_4h")
        self.assertEqual(cfg_ema10.trend_size_cfg["scale"], 10)
        self.assertEqual(cfg_ema10.sizing_modes, ["dyn_ema_10"])

        # 4. static
        act_stat = {"sizing_mode": "static", "mode": "classic"}
        cfg_stat = StrategyConfig("test_strat", raw_meta, {}, act_stat)
        self.assertEqual(cfg_stat.sizing_mode, "static")
        self.assertFalse(cfg_stat.trend_size_cfg["enabled"])
        self.assertEqual(cfg_stat.sizing_modes, ["static"])

        # 5. Legacy dyn_bb -> dyn_inv_16
        act_legacy = {"sizing_mode": "dyn_bb", "mode": "classic"}
        cfg_legacy = StrategyConfig("test_strat", raw_meta, {}, act_legacy)
        self.assertEqual(cfg_legacy.sizing_mode, "dyn_inv_16")
        self.assertTrue(cfg_legacy.trend_size_cfg["enabled"])

    def test_position_state_eff_invest_field(self):
        """Проверка поля eff_invest в PositionState и сброса в reset()."""
        pos = PositionState(symbol="STRKUSDT", side="LONG")
        self.assertEqual(pos.eff_invest, 0.0)

        pos.eff_invest = 750.5
        self.assertEqual(pos.eff_invest, 750.5)

        pos.reset()
        self.assertEqual(pos.eff_invest, 0.0)

    def test_runtime_manager_eff_invest_roundtrip(self):
        """Проверка синхронизации eff_invest через RuntimeManager (FSM -> кэш -> FSM)."""
        rm = RuntimeFsmManager()
        symbol = "STRKUSDT"
        rm.caches[symbol] = {
            "LONG": {"invest_size": 600.0, "eff_invest": 720.0},
            "SHORT": {"invest_size": 600.0, "eff_invest": 480.0}
        }

        fsm_states = {
            symbol: {
                "LONG": PositionState(symbol=symbol, side="LONG"),
                "SHORT": PositionState(symbol=symbol, side="SHORT")
            }
        }

        # 1. Загрузка из кэша в FSM
        rm.populate_fsm_from_cache(fsm_states)
        self.assertEqual(fsm_states[symbol]["LONG"].eff_invest, 720.0)
        self.assertEqual(fsm_states[symbol]["SHORT"].eff_invest, 480.0)

        # 2. Изменение и синхронизация обратно в кэш
        fsm_states[symbol]["LONG"].eff_invest = 750.0
        import asyncio
        asyncio.run(rm.sync_with_fsm(fsm_states, force_save=False))
        self.assertEqual(rm.caches[symbol]["LONG"]["eff_invest"], 750.0)

    @patch("CORE.GRID.avg_manager.Utils.wait_for_fsm_sync", new_callable=AsyncMock)
    def test_avg_manager_inherits_eff_invest(self, mock_wait_sync):
        """Проверка того, что усреднение берет eff_invest из стейта позиции."""
        mock_wait_sync.return_value = True
        state = PositionState(symbol="STRKUSDT", side="LONG")
        state.eff_invest = 720.0

        avg_mgr = AverageManager()
        mock_client = AsyncMock()
        mock_client.cancel_orders_for_side = AsyncMock()
        mock_client.make_order = AsyncMock(return_value=MagicMock(success=True))

        mock_rm = MagicMock()
        mock_rm.caches = {"STRKUSDT": {"LONG": {"invest_size": 600.0, "eff_invest": 720.0}}}

        mock_tp = MagicMock()
        mock_tp.place_take_profit = AsyncMock()

        spec_data = {"price_tick_size": 0.001, "lot_size": 1.0, "min_notional": 5.0}
        level_data = {"volume": 10.0}

        import asyncio
        asyncio.run(
            avg_mgr._execute_averaging_order(
                client=mock_client,
                runtime_manager=mock_rm,
                symbol="STRKUSDT",
                side="LONG",
                level_str="1",
                level_data=level_data,
                current_price=1.0,
                spec_data=spec_data,
                state=state,
                opposite_state=PositionState(symbol="STRKUSDT", side="SHORT"),
                tp_manager=mock_tp
            )
        )

        mock_client.make_order.assert_called_once()
        called_kwargs = mock_client.make_order.call_args[1]
        self.assertEqual(called_kwargs["qty"], 72.0)

    @patch("CORE.STRATEGY.trend_size_depender.BinancePublic.get_klines", new_callable=AsyncMock)
    def test_get_effective_invest_size_ema_trend(self, mock_get_klines):
        """Проверка get_effective_invest_size c EMA Trend."""
        klines = [[0, 10.0 + i, 12.0 + i, 9.0 + i, 10.0 + i, 100.0] for i in range(40)]
        mock_get_klines.return_value = klines

        trend_cfg = {
            "enabled": True,
            "trend_mode": "ema_trend",
            "timeframe": "4h",
            "eval_window": 30,
            "sma_fast": 10,
            "sma_slow": 30,
            "scale": 16,
            "min_side_pct": 33.3333,
            "max_side_pct": 66.6667
        }

        import asyncio
        mock_client = MagicMock()

        eff_long = asyncio.run(
            TrendSizeDepender.get_effective_invest_size(mock_client, "STRKUSDT", "LONG", 600.0, trend_cfg)
        )
        eff_short = asyncio.run(
            TrendSizeDepender.get_effective_invest_size(mock_client, "STRKUSDT", "SHORT", 600.0, trend_cfg)
        )

        self.assertAlmostEqual(eff_long + eff_short, 1200.0, places=2)
        self.assertGreater(eff_long, eff_short)


if __name__ == "__main__":
    unittest.main()
