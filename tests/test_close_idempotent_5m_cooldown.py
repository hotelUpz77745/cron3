import unittest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock

from CORE.bot import BotCore
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

    def test_close_strategy_activates_5m_cooldown(self):
        """Проверяет, что закрытие стратегии рассчитывает ровную границу 5m свечи и блокирует входы."""
        active_sid = list(self.registry.active_strategies.keys())[0]
        strat = self.registry.get_strategy(active_sid)
        now = time.time()
        expected_boundary = ((int(now) // 300) + 1) * 300 + 5.0

        asyncio.run(BotCore.close_strategy_positions(self.bot, active_sid))

        strat_block = self.bot.strategy_blocked_until.get(active_sid, 0.0)
        self.assertAlmostEqual(strat_block, expected_boundary, delta=2.0)
        self.assertGreater(strat_block, now)

        for sym in strat.symbols:
            sym_block = self.bot.symbol_blocked_until.get(sym.upper(), 0.0)
            self.assertAlmostEqual(sym_block, expected_boundary, delta=2.0)
            is_blocked, reason, rem = BotCore.check_entry_block(self.bot, sym)
            self.assertTrue(is_blocked, f"Entry for {sym} must be blocked!")
            self.assertIn("5m candle window cooldown active", reason)

    def test_close_all_activates_cooldown_for_all(self):
        """Проверяет, что глобальный Close All активирует 5m кулдаун для всех монет портфеля."""
        self.bot.symbols = self.registry.get_all_active_symbols()
        now = time.time()
        expected_boundary = ((int(now) // 300) + 1) * 300 + 5.0

        asyncio.run(BotCore.close_all_positions(self.bot, reset_iterations=False))

        self.assertAlmostEqual(self.bot.block_until_timestamp, expected_boundary, delta=2.0)
        self.assertTrue(self.bot.entry_blocked)

        for sym in self.bot.symbols:
            is_blocked, reason, rem = BotCore.check_entry_block(self.bot, sym)
            self.assertTrue(is_blocked)
            self.assertIn("5m candle window cooldown active", reason)

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
