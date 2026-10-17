# tests/test_trade_cycle_manager.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: tests/test_trade_cycle_manager.py
Role: Институциональный юнит-тест торгового цикла и машины состояний (TradeCycleManager)
==============================================================================
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.trade_cycle_manager import TradeCycleManager
from POS_FSM.models import PositionState


class TestTradeCycleManager(unittest.TestCase):
    def setUp(self):
        self.mock_bot = MagicMock()
        self.mock_bot.is_paused = False
        self.mock_bot.entry_blocked = False
        self.mock_bot.block_until_timestamp = 0.0
        self.mock_bot.symbol_blocked_until = {}
        self.mock_bot.strategy_blocked_until = {}
        self.mock_bot._closing_symbols = set()
        self.mock_bot._last_blocked_log = {}
        self.mock_bot._last_rest_syncs = {}
        self.mock_bot.current_sim_time = None

        self.mock_bot.symbols = ["BTCUSDT"]
        self.btc_long = PositionState("BTCUSDT", "LONG")
        self.btc_short = PositionState("BTCUSDT", "SHORT")
        self.mock_bot.fsm_states = {"BTCUSDT": {"LONG": self.btc_long, "SHORT": self.btc_short}}
        self.mock_bot.runtime_configs = {
            "BTCUSDT": {
                "LONG": {"enable": True, "invest_size": 400.0, "grid": {"0": {"volume": 10.0}}},
                "SHORT": {"enable": True, "invest_size": 400.0, "grid": {"0": {"volume": 10.0}}},
            }
        }
        self.mock_bot.prices = {"BTCUSDT": (50000.0, time.time())}
        self.mock_bot.spec_data = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "stepSize": "0.001",
                    "tickSize": "0.1",
                    "minQty": "0.001",
                    "minNotional": "5.0",
                }
            ]
        }

        self.mock_client = MagicMock()
        self.mock_client.make_order = AsyncMock(return_value=MagicMock(success=True))
        self.mock_client.cancel_orders_for_side = AsyncMock()
        self.mock_client.fetch_positions = AsyncMock(return_value=[])

        self.mock_bot.get_client_for_symbol = MagicMock(return_value=self.mock_client)
        self.mock_bot.leverage_manager = MagicMock()
        self.mock_bot.leverage_manager.set_leverage_and_margin = AsyncMock()

        self.mock_bot.tp_manager = MagicMock()
        self.mock_bot.tp_manager.place_take_profit = AsyncMock()

        self.mock_bot.avg_manager = MagicMock()
        self.mock_bot.avg_manager.process = AsyncMock()
        self.mock_bot.avg_manager.reset = MagicMock()

        self.mock_bot.fallback_tp_manager = MagicMock()
        self.mock_bot.fallback_tp_manager.process = AsyncMock()

        self.mock_bot.runtime_manager = MagicMock()
        self.mock_bot.runtime_manager.reset_state_to_default = MagicMock()
        self.mock_bot.runtime_manager.save_cache = AsyncMock()
        self.mock_bot.runtime_manager.caches = self.mock_bot.runtime_configs

        self.mock_bot.analytics = MagicMock()
        self.mock_bot.analytics.record_finished_position = MagicMock()

        self.mock_bot.first_order_regime = MagicMock()
        self.mock_bot.first_order_regime.evaluate_and_resolve_volume = AsyncMock(return_value=10.0)

        self.mock_bot.pos_monitor = MagicMock()
        self.mock_bot.pos_monitor.sync_from_rest = AsyncMock()

        self.mock_bot.time_control = MagicMock()
        self.mock_bot.time_control.is_new_interval.return_value = (True, 0.0)

        strat_cfg = MagicMock()
        strat_cfg.strategy_id = "u16_chita__classic"
        strat_cfg.symbols = ["BTCUSDT"]
        strat_cfg.smart_grace_cfg = {"eval_window_sec": 900}
        strat_cfg.smart_size_cfg = None
        self.mock_bot.strategy_registry = MagicMock()
        self.mock_bot.strategy_registry.get_strategy_for_symbol.return_value = strat_cfg
        self.mock_bot.strategy_registry.get_strategy.return_value = strat_cfg

        self.mock_bot._get_symbol_lock = lambda sym: asyncio.Lock()
        self.mock_bot.get_current_time = MagicMock(return_value=time.time())

        self.tc = TradeCycleManager(self.mock_bot)

    def test_check_entry_block_paused(self):
        """Если бот на паузе, вход заблокирован."""
        self.mock_bot.is_paused = True
        blocked, reason, _ = self.tc.check_entry_block("BTCUSDT")
        self.assertTrue(blocked)
        self.assertEqual(reason, "Bot is paused")

    def test_check_entry_block_closing_symbol(self):
        """Если символ в процессе закрытия, вход заблокирован."""
        self.mock_bot._closing_symbols.add("BTCUSDT")
        blocked, reason, _ = self.tc.check_entry_block("BTCUSDT")
        self.assertTrue(blocked)
        self.assertEqual(reason, "Symbol is in closing sequence")

    def test_check_entry_block_cooldowns(self):
        """Проверка блокировки по кулдаунам символа, стратегии и глобальному."""
        future_ts = time.time() + 120.0

        # Символьный кулдаун
        self.mock_bot.symbol_blocked_until["BTCUSDT"] = future_ts
        blocked, reason, rem = self.tc.check_entry_block("BTCUSDT")
        self.assertTrue(blocked)
        self.assertGreater(rem, 0.0)
        self.mock_bot.symbol_blocked_until.clear()

        # Стратегический кулдаун
        self.mock_bot.strategy_blocked_until["u16_chita__classic"] = future_ts
        blocked, reason, rem = self.tc.check_entry_block("BTCUSDT", "u16_chita__classic")
        self.assertTrue(blocked)
        self.assertGreater(rem, 0.0)
        self.mock_bot.strategy_blocked_until.clear()

        # Глобальный кулдаун
        self.mock_bot.block_until_timestamp = future_ts
        blocked, reason, rem = self.tc.check_entry_block("BTCUSDT")
        self.assertTrue(blocked)
        self.mock_bot.block_until_timestamp = 0.0

        # Чистый вход без блокировок
        blocked, reason, rem = self.tc.check_entry_block("BTCUSDT")
        self.assertFalse(blocked)
        self.assertEqual(rem, 0.0)

    def test_process_signal_aborts_if_blocked(self):
        """process_signal немедленно прерывает вход, если действует блокировка."""
        self.mock_bot.is_paused = True
        asyncio.run(
            self.tc.process_signal(
                "BTCUSDT", "LONG", self.mock_bot.runtime_configs["BTCUSDT"]["LONG"], 50000.0
            )
        )
        self.mock_client.make_order.assert_not_called()

    def test_process_signal_successful_entry(self):
        """Успешное открытие позиции при сигнале входа L0."""
        side_cfg = self.mock_bot.runtime_configs["BTCUSDT"]["LONG"]

        with patch("CORE.trade_cycle_manager.Utils.wait_for_fsm_sync", new_callable=AsyncMock, return_value=True):
            asyncio.run(self.tc.process_signal("BTCUSDT", "LONG", side_cfg, 50000.0))

            # Проверка установки плеча
            self.mock_bot.leverage_manager.set_leverage_and_margin.assert_called_once()
            # Проверка отправки рыночного ордера L0 (invest 400 * 10% = 40$ / 50000 = 0.0008 -> quant to stepSize 0.001)
            self.mock_client.make_order.assert_called_once()
            called_kwargs = self.mock_client.make_order.await_args.kwargs
            self.assertEqual(called_kwargs["symbol"], "BTCUSDT")
            self.assertEqual(called_kwargs["side"], "BUY")
            self.assertEqual(called_kwargs["position_side"], "LONG")
            self.assertEqual(called_kwargs["market_type"], "MARKET")

            # Проверка состояния FSM
            self.assertTrue(self.btc_long.in_position)
            # Проверка выставления Take Profit
            self.mock_bot.tp_manager.place_take_profit.assert_called_once()
            # Проверка сохранения рантайма
            self.mock_bot.runtime_manager.save_cache.assert_called_once_with("BTCUSDT")

    def test_process_signal_order_rejected(self):
        """Если биржа отклонила ордер, FSM state сбрасывается в false."""
        self.mock_client.make_order.return_value = MagicMock(success=False, error_msg="Insufficient margin")
        side_cfg = self.mock_bot.runtime_configs["BTCUSDT"]["LONG"]

        asyncio.run(self.tc.process_signal("BTCUSDT", "LONG", side_cfg, 50000.0))

        self.assertFalse(self.btc_long.in_position)
        self.assertFalse(self.btc_long.in_position_papper)
        self.assertGreater(self.btc_long.last_failed_entry_ts, 0.0)
        self.mock_bot.tp_manager.place_take_profit.assert_not_called()

    def test_check_and_reset_finished_positions(self):
        """check_and_reset_finished_positions фиксирует сделку, рециклирует прибыль и сбрасывает рантайм."""
        self.btc_long.is_finished = True
        self.btc_long.last_closed_volume = 0.5
        self.btc_long.last_closed_avg_price = 48000.0
        self.btc_long.tp_price = 50000.0

        with patch("CORE.trade_cycle_manager.CrossSideProfitRecycler.maybe_recycle", new_callable=AsyncMock) as mock_recycle:
            asyncio.run(
                self.tc.check_and_reset_finished_positions(
                    "BTCUSDT",
                    {"LONG": self.btc_long, "SHORT": self.btc_short},
                    self.mock_bot.runtime_configs["BTCUSDT"],
                )
            )

            # Проверка вызова рециклера для прибыльной сделки ((50000 - 48000) * 0.5 = +1000$)
            mock_recycle.assert_called_once()
            # Проверка начисления аналитики
            self.mock_bot.analytics.record_finished_position.assert_called_once()
            # Проверка отмены ордеров для закрытой стороны
            self.mock_client.cancel_orders_for_side.assert_called_once_with("BTCUSDT", "LONG")
            # Проверка сброса FSM и avg_manager
            self.mock_bot.runtime_manager.reset_state_to_default.assert_called_once_with("BTCUSDT", "LONG", self.btc_long)
            self.mock_bot.avg_manager.reset.assert_called_once_with("BTCUSDT", "LONG")
            # Проверка сохранения рантайма
            self.mock_bot.runtime_manager.save_cache.assert_called_once_with("BTCUSDT")

    def test_process_symbol_loop_dispatches_signal(self):
        """process_symbol_loop вызывает process_signal, когда позиция закрыта и есть сигнал."""
        with patch.object(self.tc, "process_signal", new_callable=AsyncMock) as mock_ps:
            asyncio.run(self.tc.process_symbol_loop("BTCUSDT"))
            # Ожидаем вызов сигналов для свободных сторон (LONG и SHORT)
            self.assertGreaterEqual(mock_ps.await_count, 1)

    def test_process_symbol_loop_dispatches_averaging_when_in_position(self):
        """process_symbol_loop вызывает AverageManager и FallbackTpManager, когда бот в позиции."""
        self.btc_long.in_position = True
        self.btc_long.total_volume = 0.5
        self.btc_long.avg_entry_price = 50000.0
        self.mock_bot.runtime_configs["BTCUSDT"]["SHORT"]["enable"] = False

        asyncio.run(self.tc.process_symbol_loop("BTCUSDT"))

        # Проверка делегирования в avg_manager и fallback_tp_manager
        self.mock_bot.avg_manager.process.assert_called_once()
        self.mock_bot.fallback_tp_manager.process.assert_called_once()


if __name__ == "__main__":
    unittest.main()
