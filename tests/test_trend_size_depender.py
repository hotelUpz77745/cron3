# tests/test_trend_size_depender.py
# Unit tests for TrendSizeDepender, dynamic 4h trend sizing [DYN_BB], and BotCore integration

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import numpy as np

from CORE.STRATEGY.trend_size_depender import TrendSizeDepender
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

    def test_grade_to_weights_extremes(self):
        """Проверка экстремумов Grade +16 (бычий) и -16 (медвежий): диапазон [33.3333%, 66.6667%]."""
        # Максимальный бычий тренд (+16)
        w_l_max, w_s_min = TrendSizeDepender.grade_to_weights(16)
        self.assertAlmostEqual(w_l_max, 66.6667, places=3)
        self.assertAlmostEqual(w_s_min, 33.3333, places=3)
        self.assertAlmostEqual(w_l_max + w_s_min, 100.0, places=4)

        # Максимальный медвежий тренд (-16)
        w_l_min, w_s_max = TrendSizeDepender.grade_to_weights(-16)
        self.assertAlmostEqual(w_l_min, 33.3333, places=3)
        self.assertAlmostEqual(w_s_max, 66.6667, places=3)
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

    def test_evaluate_grade_bb(self):
        """Проверка расчета грейда на основе полос Боллинджера 4h."""
        # 1. 25 свечей с одинаковой ценой 100.0 (std=0) -> нейтраль grade 0
        flat_closes = np.array([100.0] * 25)
        grade = TrendSizeDepender.evaluate_grade_bb(flat_closes, period=20, multiplier=2.0)
        w_l, w_s = TrendSizeDepender.calculate_weights_from_grade(grade)
        self.assertEqual(grade, 0)
        self.assertAlmostEqual(w_l, 50.0, places=4)

        # 2. Восходящий ряд, последняя цена сильно выше Upper Band -> grade +16
        bull_closes = np.array([10.0 + i for i in range(24)] + [200.0])
        grade_bull = TrendSizeDepender.evaluate_grade_bb(bull_closes, period=20, multiplier=2.0)
        w_l_bull, w_s_bull = TrendSizeDepender.calculate_weights_from_grade(grade_bull)
        self.assertEqual(grade_bull, 16)
        self.assertAlmostEqual(w_l_bull, 66.6667, places=3)
        self.assertAlmostEqual(w_s_bull, 33.3333, places=3)

        # 3. Нисходящий ряд, последняя цена сильно ниже Lower Band -> grade -16
        bear_closes = np.array([100.0 - i for i in range(24)] + [10.0])
        grade_bear = TrendSizeDepender.evaluate_grade_bb(bear_closes, period=20, multiplier=2.0)
        w_l_bear, w_s_bear = TrendSizeDepender.calculate_weights_from_grade(grade_bear)
        self.assertEqual(grade_bear, -16)
        self.assertAlmostEqual(w_l_bear, 33.3333, places=3)
        self.assertAlmostEqual(w_s_bear, 66.6667, places=3)

    def test_strategy_config_sizing_mode_string(self):
        """Проверка: sizing_mode парсится строго как строка ('dyn_bb' / 'static') и настраивает trend_size_cfg."""
        raw_meta = {
            "name": "Test Strategy",
            "LONG": {"invest_size": 600.0},
            "SHORT": {"invest_size": 600.0},
        }

        # 1. Явно dyn_bb
        act_dyn = {"sizing_mode": "dyn_bb", "mode": "classic"}
        cfg_dyn = StrategyConfig("test_strat", raw_meta, {}, act_dyn)
        self.assertEqual(cfg_dyn.sizing_mode, "dyn_bb")
        self.assertTrue(cfg_dyn.trend_size_cfg["enabled"])
        self.assertEqual(cfg_dyn.trend_size_cfg["trend_mode"], "bb_deviation_4h")
        self.assertEqual(cfg_dyn.sizing_modes, ["dyn_bb"])

        # 2. Явно static
        act_stat = {"sizing_mode": "static", "mode": "classic"}
        cfg_stat = StrategyConfig("test_strat", raw_meta, {}, act_stat)
        self.assertEqual(cfg_stat.sizing_mode, "static")
        self.assertFalse(cfg_stat.trend_size_cfg["enabled"])
        self.assertEqual(cfg_stat.sizing_modes, ["static"])

        # 3. Дефолт при отсутствии параметра в active_settings -> dyn_bb
        act_def = {"mode": "classic"}
        cfg_def = StrategyConfig("test_strat", raw_meta, {}, act_def)
        self.assertEqual(cfg_def.sizing_mode, "dyn_bb")
        self.assertTrue(cfg_def.trend_size_cfg["enabled"])

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
        state.eff_invest = 720.0  # трендовый сайз выше базового 600

        avg_mgr = AverageManager()
        mock_client = AsyncMock()
        mock_client.cancel_orders_for_side = AsyncMock()
        mock_client.make_order = AsyncMock(return_value=MagicMock(success=True))

        mock_rm = MagicMock()
        mock_rm.caches = {"STRKUSDT": {"LONG": {"invest_size": 600.0, "eff_invest": 720.0}}}

        mock_tp = MagicMock()
        mock_tp.place_take_profit = AsyncMock()

        spec_data = {"price_tick_size": 0.001, "lot_size": 1.0, "min_notional": 5.0}
        level_data = {"volume": 10.0}  # 10% от сайза

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

        # Объем должен быть рассчитан от 720.0 * 10% = 72.0 USDT / 1.0 = 72.0 шт
        mock_client.make_order.assert_called_once()
        called_kwargs = mock_client.make_order.call_args[1]
        self.assertEqual(called_kwargs["qty"], 72.0)

    @patch("CORE.STRATEGY.trend_size_depender.BinancePublic.get_klines", new_callable=AsyncMock)
    def test_get_effective_invest_size_bullish(self, mock_get_klines):
        """Проверка get_effective_invest_size: при бычьем тренде LONG получает ~66.67%, SHORT ~33.33%."""
        # Моделируем 25 свечей с резким пампом
        klines = [[0, 10.0 + i, 12.0 + i, 9.0 + i, 10.0 + i, 100.0] for i in range(25)]
        klines[-1][4] = 200.0  # памп
        mock_get_klines.return_value = klines

        trend_cfg = {
            "enabled": True,
            "trend_mode": "bb_deviation_4h",
            "timeframe": "4h",
            "eval_window": 20,
            "multiplier": 2.0
        }

        import asyncio
        mock_client = MagicMock()

        eff_long = asyncio.run(
            TrendSizeDepender.get_effective_invest_size(mock_client, "STRKUSDT", "LONG", 600.0, trend_cfg)
        )
        eff_short = asyncio.run(
            TrendSizeDepender.get_effective_invest_size(mock_client, "STRKUSDT", "SHORT", 600.0, trend_cfg)
        )

        # Капитал LONG должен быть ~800.0 (66.6667% от 1200 USDT общего капитала)
        self.assertAlmostEqual(eff_long, 800.0, delta=1.0)
        # Капитал SHORT должен быть ~400.0 (33.3333% от 1200 USDT общего капитала)
        self.assertAlmostEqual(eff_short, 400.0, delta=1.0)
        # Инвариант: сумма капиталов обеих сторон = 2 * 600 = 1200
        self.assertAlmostEqual(eff_long + eff_short, 1200.0, places=2)


if __name__ == "__main__":
    unittest.main()
