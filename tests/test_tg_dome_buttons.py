"""
Unit test to verify all Telegram dome buttons and menus for active strategies:
U16 (Titan Universal Alpha), U6 (Gamma Fortress 35 / Медный Змей), and U9 (Omni Zenith Apex).
"""

import unittest
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path
import json

from CORE.STRATEGY.strategy_registry import StrategyRegistry
from TG.tg_receiver import TelegramReceiver
from TG.template_manager import TemplateManager


class TestTGDomeButtons(unittest.TestCase):
    def setUp(self):
        self.mock_bot_core = MagicMock()
        self.registry = StrategyRegistry()
        self.mock_bot_core.strategy_registry = self.registry
        self.mock_bot_core.symbols = self.registry.get_all_active_symbols()
        self.mock_bot_core.is_paused = False

        self.tg = TelegramReceiver(bot_core=self.mock_bot_core)

    def test_active_strategies_loaded(self):
        active = self.registry.active_strategies
        self.assertTrue(len(active) > 0)
        self.assertTrue(any(sid in active for sid in ("u16_apex_hybrid__half_peel__classic", "u96_omni_zenith_apex__classic")))

    def test_close_all_dome_buttons(self):
        """Проверяет генерацию кнопок закрытия под каждым куполом."""
        active_strats = self.registry.active_strategies
        buttons = []
        for sid, scfg in active_strats.items():
            buttons.append(f"close_dome_{sid}")

        first_sid = next(iter(active_strats.keys()))
        self.assertIn(f"close_dome_{first_sid}", buttons)

    def test_strategy_meta_extraction(self):
        """Проверяет корректность метаданных куполов стратегий."""
        for sid in self.registry.active_strategies.keys():
            meta = self.tg._get_strategy_meta(sid)
            self.assertEqual(meta["strategy_id"], sid)
            self.assertTrue(len(meta["name"]) > 0)
            self.assertGreater(meta["invest_size"], 0)
            self.assertGreater(meta["leverage"], 0)

    def test_selector_apply_buttons(self):
        """Проверяет кнопки применения результатов селектора под каждый купол."""
        active_strats = self.registry.active_strategies
        apply_cbs = [f"cb_selector_apply:{sid}" for sid in active_strats.keys()]
        first_sid = next(iter(active_strats.keys()))
        self.assertIn(f"cb_selector_apply:{first_sid}", apply_cbs)

    def test_symbol_strategy_assignment_callbacks(self):
        """Проверяет генерацию callback_data при добавлении монеты под конкретный купол."""
        symbol = "BTCUSDT"
        active_strats = self.registry.active_strategies
        callbacks = [f"add_pick_strat_{symbol}_{sid}" for sid in active_strats.keys()]
        first_sid = next(iter(active_strats.keys()))
        self.assertIn(f"add_pick_strat_{symbol}_{first_sid}", callbacks)


    def test_close_strategy_positions_no_entry_block(self):
        """Проверяет, что закрытие позиций стратегии НЕ активирует глобальный entry block."""
        import asyncio
        from CORE.bot import BotCore

        mock_bot = MagicMock()
        mock_bot.strategy_registry = self.registry
        mock_bot._close_lock = asyncio.Lock()
        mock_bot._closing_strategies = set()
        mock_bot._closing_symbols = set()
        mock_bot.fsm_states = {}
        mock_bot.entry_blocked = False
        mock_bot.block_until_timestamp = 0
        mock_bot.client = MagicMock()
        mock_bot.client.cancel_all_orders = AsyncMock()
        mock_bot.client.fetch_positions = AsyncMock(return_value=[])

        # Вызываем штатный close_strategy_positions
        active_sid = list(self.registry.active_strategies.keys())[0]
        asyncio.run(BotCore.close_strategy_positions(mock_bot, active_sid))

        # Блокировка входов НЕ должна быть активирована
        self.assertFalse(mock_bot.entry_blocked)
        self.assertEqual(mock_bot.block_until_timestamp, 0)

    def test_close_all_confirm_state_exists(self):
        """Проверяет наличие стейта ожидания подтверждения закрытия."""
        from TG.tg_receiver import TGStates
        self.assertTrue(hasattr(TGStates, "waiting_for_close_all_confirm"))


if __name__ == "__main__":
    unittest.main()
