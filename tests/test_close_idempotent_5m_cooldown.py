import unittest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock

from CORE.bot import BotCore
from CORE.trade_cycle_manager import TradeCycleManager
from CORE.STRATEGY.strategy_registry import StrategyRegistry


class TestCloseIdempotent5mCooldown(unittest.TestCase):
    """
    Тестирование гарантий идемпотентности и защиты от преждевременного переоткрытия
    позиций до открытия следующего пятиминутного окна.
    """

    def setUp(self):
        self.registry = StrategyRegistry()
        self.bot = MagicMock()
        self.bot.strategy_registry = self.registry
        self.bot._close_lock = asyncio.Lock()
        self.bot._closing_strategies = set()
        self.bot._closing_symbols = set()
        self.bot.fsm_states = {}
        self.bot.entry_blocked = False
        self.bot.block_until_timestamp = 0.0
        self.bot.symbol_blocked_until = {}
        self.bot.strategy_blocked_until = {}
        self.bot.time_control = MagicMock()
        self.bot.time_control.interval_seconds = 300
        self.bot.client = MagicMock()
        self.bot.client.cancel_all_orders = AsyncMock()
        self.bot.client.fetch_positions = AsyncMock(return_value=[])
        self.bot.client.make_order = AsyncMock(return_value=MagicMock(success=True))
        self.bot.get_client_for_symbol = MagicMock(return_value=self.bot.client)
        self.bot.get_client_for_strategy = MagicMock(return_value=self.bot.client)
        self.bot._get_symbol_lock = MagicMock(side_effect=lambda s: asyncio.Lock())
        self.bot.runtime_manager = MagicMock()
        self.bot.runtime_manager.save_cache = AsyncMock()
        self.bot.avg_manager = MagicMock()
        self.bot.current_sim_time = None
        self.bot.get_strategy_heartbeat_seconds = lambda strategy_id=None, symbol=None: BotCore.get_strategy_heartbeat_seconds(self.bot, strategy_id, symbol)
        self.bot.calc_heartbeat_window_cooldown = BotCore.calc_heartbeat_window_cooldown
        self.bot.trade_cycle = TradeCycleManager(self.bot)
        self.bot.is_paused = False

    def test_close_strategy_activates_5m_cooldown(self):
        """Проверяет, что закрытие стратегии рассчитывает ровную границу свечи хартбита и блокирует входы."""
        active_sid = list(self.registry.active_strategies.keys())[0]
        strat = self.registry.get_strategy(active_sid)
        now = time.time()
        heartbeat_sec = strat.heartbeat_seconds
        expected_boundary = ((int(now) // heartbeat_sec) + 1) * heartbeat_sec + 5.0

        asyncio.run(BotCore.close_strategy_positions(self.bot, active_sid))

        strat_block = self.bot.strategy_blocked_until.get(active_sid, 0.0)
        self.assertAlmostEqual(strat_block, expected_boundary, delta=2.0)
        self.assertGreater(strat_block, now)

        for sym in strat.symbols:
            sym_block = self.bot.symbol_blocked_until.get(sym.upper(), 0.0)
            self.assertAlmostEqual(sym_block, expected_boundary, delta=2.0)
            is_blocked, reason, rem = BotCore.check_entry_block(self.bot, sym)
            self.assertTrue(is_blocked, f"Entry for {sym} must be blocked!")
            self.assertIn("candle window cooldown active", reason)

    def test_close_all_activates_cooldown_for_all(self):
        """Проверяет, что глобальный Close All активирует кулдаун хартбита для всех монет портфеля."""
        self.bot.symbols = self.registry.get_all_active_symbols()
        now = time.time()
        heartbeat_sec = BotCore.get_strategy_heartbeat_seconds(self.bot)
        expected_boundary = ((int(now) // heartbeat_sec) + 1) * heartbeat_sec + 5.0

        asyncio.run(BotCore.close_all_positions(self.bot, reset_iterations=False))

        self.assertAlmostEqual(self.bot.block_until_timestamp, expected_boundary, delta=2.0)
        self.assertTrue(self.bot.entry_blocked)

        for sym in self.bot.symbols:
            is_blocked, reason, rem = BotCore.check_entry_block(self.bot, sym)
            self.assertTrue(is_blocked)
            self.assertIn("candle window cooldown active", reason)

    def test_dynamic_cooldown_for_arbitrary_timeframes(self):
        """Проверяет, что calc_heartbeat_window_cooldown адаптируется под любой таймфрейм (10m, 15m, 1m)."""
        now = 1000.0
        # 10m = 600s: следующее окно 1200 + 5.0 = 1205.0
        cd_10m = BotCore.calc_heartbeat_window_cooldown(now, 600)
        self.assertEqual(cd_10m, 1205.0)

        # 15m = 900s: следующее окно 1800 + 5.0 = 1805.0
        cd_15m = BotCore.calc_heartbeat_window_cooldown(now, 900)
        self.assertEqual(cd_15m, 1805.0)

        # 1m = 60s: следующее окно 1020 + 5.0 = 1025.0, но min cooldown = now + 30.0 = 1030.0
        cd_1m = BotCore.calc_heartbeat_window_cooldown(now, 60)
        self.assertEqual(cd_1m, 1030.0)

    def test_cooldown_expires_cleanly_on_new_candle(self):
        """Проверяет, что после истечения метки времени блокировка снимается автоматически."""
        symbol = "AVAUSDT"
        self.bot.block_until_timestamp = time.time() - 1.0
        self.bot.symbol_blocked_until = {symbol: time.time() - 1.0}
        self.bot.strategy_blocked_until = {}
        self.bot.entry_blocked = True
        self.bot.is_paused = False

        is_blocked, reason, rem = BotCore.check_entry_block(self.bot, symbol)
        self.assertFalse(is_blocked)
        self.assertEqual(reason, "")
        self.assertFalse(self.bot.entry_blocked)


if __name__ == "__main__":
    unittest.main()
