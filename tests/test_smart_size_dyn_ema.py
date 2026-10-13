# ============================================================
# FILE: tests/test_smart_size_dyn_ema.py
# ROLE: Dedicated unit tests for U16 Chita Smart Size (Turing Bell & grid_0 isolation)
# ============================================================

import unittest
from unittest.mock import patch, AsyncMock
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE.STRATEGY.strategy_registry import StrategyRegistry
from CORE._utils import TradeMath


class TestSmartSizeDynEma(unittest.TestCase):
    """Тесты для динамического сайзинга trend_determ (Колокол Тюринга) в стратегии Chita."""

    def setUp(self):
        MomentumEngine.clear_cache()
        self.smart_size_cfg = {
            "eval_window_sec": 300,
            "trend_determ": "ema_trend",
            "ema_trend": {
                "timeframe": "4h",
                "sma_fast": 10,
                "sma_slow": 30,
                "confirmation_candles": 5,
                "require_rising": True,
                "trend_positive": True
            },
            "profiting_size_weights": {
                "==0": 1.14,
                "==1": 1.28,
                "==2": 1.42,
                "==3": 1.28,
                ">=4": 1.14
            },
            "knife_blocker_enabled": True,
            "knife_blocker_threshold": 1,
            "omni_size_all_zones": False
        }

    def test_u16_chita_config_loaded(self):
        """Проверка, что u16_chita загружается с обновленной конфигурацией smart_size и колоколом Тюринга."""
        registry = StrategyRegistry()
        strat = registry.get_strategy("u16_chita__classic")
        self.assertIsNotNone(strat)
        ss = strat.smart_size_cfg
        self.assertEqual(ss.get("trend_determ"), "ema_trend")
        self.assertEqual(ss.get("profiting_size_weights", {}).get("==0"), 1.14)
        self.assertEqual(ss.get("profiting_size_weights", {}).get("==1"), 1.28)
        self.assertEqual(ss.get("profiting_size_weights", {}).get("==2"), 1.42)
        self.assertEqual(ss.get("profiting_size_weights", {}).get("==3"), 1.28)
        self.assertEqual(ss.get("profiting_size_weights", {}).get(">=4"), 1.14)
        self.assertFalse(ss.get("omni_size_all_zones"))
        self.assertIn("ema_trend", ss)
        self.assertEqual(ss["ema_trend"].get("sma_fast"), 10)
        self.assertEqual(ss["ema_trend"].get("sma_slow"), 30)

    def test_long_trend_aligned_turing_bell_multipliers(self):
        """LONG при восходящем тренде (UP): проверка карты Колокол Тюринга."""
        weights = self.smart_size_cfg
        # Первое открытие в окне (closed_count == 0) -> x1.14 (5.7%)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, weights, side="LONG", trend_state="UP"), 1.14)
        # Второе открытие в окне (closed_count == 1) -> x1.28 (6.4%)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(1, weights, side="LONG", trend_state="UP"), 1.28)
        # Третье открытие (closed_count == 2, пик колокола) -> x1.42 (7.1%)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(2, weights, side="LONG", trend_state="UP"), 1.42)
        # Четвертое открытие (closed_count == 3, спад колокола) -> x1.28 (6.4%)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(3, weights, side="LONG", trend_state="UP"), 1.28)
        # 5+ открытий (closed_count >= 4, плато колокола) -> x1.14 (5.7%)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(4, weights, side="LONG", trend_state="UP"), 1.14)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(7, weights, side="LONG", trend_state="UP"), 1.14)

    def test_short_trend_aligned_turing_bell_multipliers(self):
        """SHORT при нисходящем тренде (DOWN): проверка карты Колокол Тюринга."""
        weights = self.smart_size_cfg
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, weights, side="SHORT", trend_state="DOWN"), 1.14)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(1, weights, side="SHORT", trend_state="DOWN"), 1.28)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(2, weights, side="SHORT", trend_state="DOWN"), 1.42)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(3, weights, side="SHORT", trend_state="DOWN"), 1.28)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(4, weights, side="SHORT", trend_state="DOWN"), 1.14)

    def test_counter_trend_neutral_sizing(self):
        """Несовпадение стороны с трендом: множитель всегда строго 1.00x."""
        weights = self.smart_size_cfg
        # LONG при падающем рынке
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, weights, side="LONG", trend_state="DOWN"), 1.00)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(2, weights, side="LONG", trend_state="DOWN"), 1.00)
        # SHORT при растущем рынке
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, weights, side="SHORT", trend_state="UP"), 1.00)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(3, weights, side="SHORT", trend_state="UP"), 1.00)
        # Флэт или неопределенность
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, weights, side="LONG", trend_state="FLAT"), 1.00)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, weights, side="SHORT", trend_state="UNSTABLE"), 1.00)

    def test_volume_calculation_math_and_grid_0_isolation(self):
        """
        Проверка:
        1. Множители масштабируют исключительно объем начального ордера (grid_0).
        2. Колена усреднения сетки (L1..L7) используют нераздутый base_invest (eff_invest).
        """
        base_invest = 400.0
        price = 100.0
        spec_data = {
            "TESTUSDT": {
                "step_size": 0.01,
                "min_qty": 0.01,
                "tick_size": 0.01
            }
        }

        grid_cfg = {
            "0": {"volume": 5.0},   # L0: 5%
            "1": {"volume": 5.0},   # L1: 5%
            "2": {"volume": 6.0},   # L2: 6%
            "3": {"volume": 9.0},   # L3: 9%
            "4": {"volume": 10.0},  # L4: 10%
            "5": {"volume": 15.0},  # L5: 15%
            "6": {"volume": 45.0},  # L6: 45%
            "7": {"volume": 5.0}    # L7: 5%
        }

        # 1. Ступень grid_0 при закрытиях == 2 (пик x1.42):
        # invest_size для grid_0 = 400 * 1.42 = 568 USDT
        # volume для grid_0 = 568 * 5% = 28.4 USDT (7.1% от base_invest)
        mult_0 = 1.42
        invest_size_grid_0 = base_invest * mult_0
        vol_0 = TradeMath.calculate_order_volume(invest_size_grid_0, grid_cfg["0"]["volume"], price, spec_data, "TESTUSDT")
        self.assertAlmostEqual(vol_0 * price, 28.4, places=2)
        self.assertAlmostEqual((vol_0 * price) / base_invest * 100.0, 7.1, places=2)

        # 2. Изоляция колен усреднения сетки (L1..L7):
        # По логике bot.py: state.eff_invest = base_invest (400.0, без применения mult_0!)
        # AvgManager рассчитывает усреднения строго от state.eff_invest:
        state_eff_invest = base_invest

        # L1: 400 * 5% = 20.0 USDT
        vol_l1 = TradeMath.calculate_order_volume(state_eff_invest, grid_cfg["1"]["volume"], price, spec_data, "TESTUSDT")
        self.assertAlmostEqual(vol_l1 * price, 20.0, places=2)

        # L2: 400 * 6% = 24.0 USDT
        vol_l2 = TradeMath.calculate_order_volume(state_eff_invest, grid_cfg["2"]["volume"], price, spec_data, "TESTUSDT")
        self.assertAlmostEqual(vol_l2 * price, 24.0, places=2)

        # L3: 400 * 9% = 36.0 USDT
        vol_l3 = TradeMath.calculate_order_volume(state_eff_invest, grid_cfg["3"]["volume"], price, spec_data, "TESTUSDT")
        self.assertAlmostEqual(vol_l3 * price, 36.0, places=2)

        # L4: 400 * 10% = 40.0 USDT
        vol_l4 = TradeMath.calculate_order_volume(state_eff_invest, grid_cfg["4"]["volume"], price, spec_data, "TESTUSDT")
        self.assertAlmostEqual(vol_l4 * price, 40.0, places=2)

        # L5: 400 * 15% = 60.0 USDT
        vol_l5 = TradeMath.calculate_order_volume(state_eff_invest, grid_cfg["5"]["volume"], price, spec_data, "TESTUSDT")
        self.assertAlmostEqual(vol_l5 * price, 60.0, places=2)

        # L6: 400 * 45% = 180.0 USDT
        vol_l6 = TradeMath.calculate_order_volume(state_eff_invest, grid_cfg["6"]["volume"], price, spec_data, "TESTUSDT")
        self.assertAlmostEqual(vol_l6 * price, 180.0, places=2)

        # Подтверждение: колена усреднения строго равны своим базовым долям и не затронуты x1.42
        total_grid_avg_usdt = sum(
            TradeMath.calculate_order_volume(state_eff_invest, grid_cfg[str(lvl)]["volume"], price, spec_data, "TESTUSDT") * price
            for lvl in range(1, 8)
        )
        # Сумма процентов L1..L7 = 5 + 6 + 9 + 10 + 15 + 45 + 5 = 95%
        # 400 * 95% = 380 USDT
        self.assertAlmostEqual(total_grid_avg_usdt, 380.0, places=1)

    def test_white_noise_factor_sleep(self):
        """Проверка работы white_noise_factor: рандомный слип в диапазоне [3, 13]с перед входом."""
        import asyncio
        from unittest.mock import MagicMock
        from CORE.bot import BotCore
        from POS_FSM.models import PositionState

        registry = StrategyRegistry()
        strat = registry.get_strategy("u16_chita__classic")
        wn = strat.smart_grace_cfg.get("white_noise_factor")
        self.assertEqual(wn, [3, 13])

        # Создаем мок бота
        bot = MagicMock(spec=BotCore)
        bot.strategy_registry = registry
        bot.prices = {"STRKUSDT": (1.0, 1000.0)}
        bot.is_paused = False
        bot.entry_blocked = False
        bot._closing_symbols = set()
        bot.block_until_timestamp = 0
        bot.radar_navigator = None
        bot.spec_data = {"STRKUSDT": {"step_size": 0.01, "min_qty": 0.01, "tick_size": 0.01}}
        bot.leverage_manager = MagicMock()
        bot.leverage_manager.set_leverage_and_margin = AsyncMock()
        bot._symbol_locks = {}
        bot._get_symbol_lock = lambda sym: asyncio.Lock()

        mock_client = MagicMock()
        mock_client.make_order = AsyncMock(return_value=MagicMock(success=True))
        bot.get_client_for_symbol = MagicMock(return_value=mock_client)
        bot.tp_manager = MagicMock()
        bot.tp_manager.place_take_profit = AsyncMock()
        bot.runtime_manager = MagicMock()
        bot.runtime_manager.save_cache = AsyncMock()
        bot.notifier = MagicMock()
        bot.analytics = MagicMock()

        state = PositionState("STRKUSDT", "LONG")
        state.in_position_papper = True
        state.avg_entry_price = 1.05
        bot.fsm_states = {"STRKUSDT": {"LONG": state, "SHORT": PositionState("STRKUSDT", "SHORT")}}

        side_cfg = {
            "invest_size": 400.0,
            "grid": {"0": {"volume": 5.0}}
        }

        # Проверяем, что вызывается random.randint(3, 13) и asyncio.sleep(sleep_val)
        with patch("CORE.bot.random.randint", return_value=7) as mock_rand, \
             patch("CORE.bot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("CORE.bot.BinancePublic.get_last_price", new_callable=AsyncMock, return_value=1.05):

            asyncio.run(BotCore._process_signal(bot, "STRKUSDT", "LONG", side_cfg, 1.0))

            mock_rand.assert_called_once_with(3, 13)
            mock_sleep.assert_called_once_with(7)
            # Ордер отправлен
            mock_client.make_order.assert_called_once()


if __name__ == "__main__":
    unittest.main()
