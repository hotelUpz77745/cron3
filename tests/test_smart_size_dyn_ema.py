# ============================================================
# FILE: tests/test_smart_size_dyn_ema.py
# ROLE: Dedicated unit tests for U16 Chita Smart Size (Turing Bell & grid_0 isolation)
# ============================================================

import unittest
from unittest.mock import patch, AsyncMock
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE.STRATEGY.strategy_registry import StrategyRegistry
from CORE.STRATEGY.strategy_applicator import StrategyApplicator
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

    def test_brownian_noise_factor_sleep(self):
        """Проверка работы brownian_noise_factor: броуновский джиттер [3, 13]с перед входом."""
        import asyncio
        from unittest.mock import MagicMock
        from CORE.bot import BotCore
        from POS_FSM.models import PositionState

        registry = StrategyRegistry()
        strat = registry.get_strategy("u16_chita__classic")
        bn = strat.smart_grace_cfg.get("brownian_noise_factor")
        self.assertEqual(bn, [3, 13])

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

        # ТЕСТ 1: Первый вход (closed_count == 0) — броуновский шум НЕ применяется!
        state.recent_closes = []
        mock_sys_random = MagicMock()
        mock_sys_random.uniform.return_value = 7.34

        with patch("CORE.bot.secrets.SystemRandom", return_value=mock_sys_random), \
             patch("CORE.bot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("CORE.bot.BinancePublic.get_last_price", new_callable=AsyncMock, return_value=1.05):

            asyncio.run(BotCore._process_signal(bot, "STRKUSDT", "LONG", side_cfg, 1.0))

            # Слип НЕ должен вызываться на первом входе
            mock_sleep.assert_not_called()
            mock_sys_random.uniform.assert_not_called()
            mock_client.make_order.assert_called_once()

        # ТЕСТ 2: Повторный вход сильной руки (closed_count >= 1) — броуновский шум применяется!
        import time
        state.in_position = False
        state.recent_closes = [time.time()]
        mock_client.make_order.reset_mock()

        with patch("CORE.bot.secrets.SystemRandom", return_value=mock_sys_random), \
             patch("CORE.bot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("CORE.bot.BinancePublic.get_last_price", new_callable=AsyncMock, return_value=1.05):

            asyncio.run(BotCore._process_signal(bot, "STRKUSDT", "LONG", side_cfg, 1.0))

            # Слип вызывается между ордерами сильной руки
            mock_sys_random.uniform.assert_called_once_with(3.0, 13.0)
            mock_sleep.assert_called_once_with(7.34)
            mock_client.make_order.assert_called_once()

    def test_smart_tp_trend_alignment(self):
        """Проверка расчета Smart TP с привязкой к EMA-тренду и колоколом Тюринга."""
        registry = StrategyRegistry()
        strat = registry.get_strategy("u16_chita__classic")
        tp_cfg = strat.smart_tp_cfg

        self.assertTrue(tp_cfg.get("enabled"))
        self.assertEqual(tp_cfg.get("trend_determ"), "ema_trend")

        # 1. LONG по восходящему тренду (UP) -> множитель по карточке
        mult_1 = MomentumEngine.calculate_smart_tp_multiplier(1, tp_cfg, side="LONG", trend_state="UP")
        self.assertEqual(mult_1, 1.14)

        mult_2 = MomentumEngine.calculate_smart_tp_multiplier(2, tp_cfg, side="LONG", trend_state="UP")
        self.assertEqual(mult_2, 1.28)

        mult_3 = MomentumEngine.calculate_smart_tp_multiplier(3, tp_cfg, side="LONG", trend_state="UP")
        self.assertEqual(mult_3, 1.42)

        mult_4 = MomentumEngine.calculate_smart_tp_multiplier(4, tp_cfg, side="LONG", trend_state="UP")
        self.assertEqual(mult_4, 1.14)

        # 2. LONG по нисходящему тренду (DOWN, контртренд) -> нейтральный 1.00x
        mult_ct = MomentumEngine.calculate_smart_tp_multiplier(2, tp_cfg, side="LONG", trend_state="DOWN")
        self.assertEqual(mult_ct, 1.00)

        # 3. SHORT по нисходящему тренду (DOWN) -> совпадение!
        mult_short = MomentumEngine.calculate_smart_tp_multiplier(2, tp_cfg, side="SHORT", trend_state="DOWN")
        self.assertEqual(mult_short, 1.28)

        # 4. SHORT по восходящему тренду (UP, контртренд) -> нейтральный 1.00x
        mult_short_ct = MomentumEngine.calculate_smart_tp_multiplier(2, tp_cfg, side="SHORT", trend_state="UP")
        self.assertEqual(mult_short_ct, 1.00)

        # 5. Бестрендовый режим (trend_determ = null) -> расчет без оглядки на тренд
        legacy_cfg = {"enabled": True, "increments_tp": {"==2": 1.28}}
        self.assertEqual(MomentumEngine.calculate_smart_tp_multiplier(2, legacy_cfg, side="LONG", trend_state="DOWN"), 1.28)

    def test_min_tp_indent_enforcement(self):
        """Проверка минимального гарантированного пола для тейк-профита (min_tp_indent >= 1.0)."""
        registry = StrategyRegistry()
        strat = registry.get_strategy("u16_chita__classic")
        self.assertEqual(strat.min_tp_indent, 1.0)

        applicator = StrategyApplicator()
        side_cfgs = applicator.build_side_configs(strat)

        for side in ("LONG", "SHORT"):
            tp_map = side_cfgs[side]["tp_map"]
            for lvl, cfg in tp_map.items():
                if cfg.get("indent") is not None:
                    self.assertGreaterEqual(float(cfg["indent"]), 1.0, f"Level {lvl} indent < 1.0")
                if cfg.get("fallback_indent") is not None:
                    self.assertGreaterEqual(float(cfg["fallback_indent"]), 1.0, f"Level {lvl} fallback_indent < 1.0")


    def test_smart_grace_breathing_increments(self):
        """Проверка динамического окна Smart Grace: база 150с + increments_card (25, 40, 25, 0)."""
        registry = StrategyRegistry()
        strat = registry.get_strategy("u16_chita__classic")
        sg_cfg = strat.smart_grace_cfg

        self.assertEqual(sg_cfg.get("start_period_sec"), 150)

        # closed_count == 0: 150 + 0 = 150s
        self.assertEqual(MomentumEngine.calculate_smart_grace(0, sg_cfg), 150.0)

        # closed_count == 1: 150 + 25 = 175s
        self.assertEqual(MomentumEngine.calculate_smart_grace(1, sg_cfg), 175.0)

        # closed_count == 2: 150 + 40 = 190s
        self.assertEqual(MomentumEngine.calculate_smart_grace(2, sg_cfg), 190.0)

        # closed_count == 3: 150 + 25 = 175s
        self.assertEqual(MomentumEngine.calculate_smart_grace(3, sg_cfg), 175.0)

        # closed_count >= 4: 150 + 0 = 150s
        self.assertEqual(MomentumEngine.calculate_smart_grace(4, sg_cfg), 150.0)


if __name__ == "__main__":
    unittest.main()
