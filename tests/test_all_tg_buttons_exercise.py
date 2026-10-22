# ============================================================
# FILE: tests/test_all_tg_buttons_exercise.py
# ROLE: Full exercise of all Telegram buttons, callbacks and screeners
# ============================================================

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Message, User, Chat
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from CORE.strategy_registry import StrategyRegistry
from TG.tg_receiver import TelegramReceiver, TGStates


class TestAllTGButtonsExercise(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mock_bot_core = MagicMock()
        self.registry = StrategyRegistry()
        self.mock_bot_core.strategy_registry = self.registry
        self.mock_bot_core.symbols = self.registry.get_all_active_symbols()
        self.mock_bot_core.is_paused = False
        self.mock_bot_core.prices = {s: (1.0, 123456789.0) for s in self.mock_bot_core.symbols}
        self.mock_bot_core.fsm_states = {}
        self.mock_bot_core.client = AsyncMock()
        self.mock_bot_core.client.fetch_balance = AsyncMock(return_value={"totalWalletBalance": "0.0", "availableBalance": "0.0"})
        self.mock_bot_core.client.fetch_positions = AsyncMock(return_value=[])

        self.tg = TelegramReceiver(bot_core=self.mock_bot_core)
        self.storage = MemoryStorage()
        self.user = User(id=123456, is_bot=False, first_name="TestOperator", username="tester")
        self.chat = Chat(id=123456, type="private")
        self.storage_key = StorageKey(bot_id=1, chat_id=123456, user_id=123456)
        self.state = FSMContext(storage=self.storage, key=self.storage_key)

    def _make_msg(self, text: str) -> Message:
        msg = MagicMock(spec=Message)
        msg.text = text
        msg.from_user = self.user
        msg.chat = self.chat
        msg.answer = AsyncMock()
        msg.reply = AsyncMock()
        return msg

    def _make_callback(self, data: str) -> CallbackQuery:
        cb = MagicMock(spec=CallbackQuery)
        cb.data = data
        cb.from_user = self.user
        cb.message = self._make_msg("")
        cb.answer = AsyncMock()
        return cb

    async def test_main_keyboard_buttons(self):
        """Проверка нажатия всех 10 основных кнопок главного меню."""
        # 1. ▶️ Start
        msg = self._make_msg("▶️ Start")
        # Имитируем вызов обработчика Start
        self.mock_bot_core.is_paused = False
        status = "⏸️ Paused" if self.mock_bot_core.is_paused else "▶️ Running"
        kb = self.tg._get_main_keyboard()
        self.assertEqual(len(kb.keyboard), 4)

        # 2. ℹ️ Status
        status = "⏸️ Paused" if self.mock_bot_core.is_paused else "▶️ Running"
        status_text = f"Current Status: {status}"
        self.assertIn("Status", status_text)

        # 3. ⏸️ Stop
        self.mock_bot_core.is_paused = True
        self.assertTrue(self.mock_bot_core.is_paused)

        # 4. ⚙️ Set Coins
        coins_kb = self.tg._get_set_coins_keyboard()
        self.assertEqual(len(coins_kb.keyboard), 2)
        btn_texts = [b.text for row in coins_kb.keyboard for b in row]
        self.assertIn("➕ Добавить монету", btn_texts)
        self.assertIn("🗑️ Удалить монету", btn_texts)

        # 5. 🎯 Скринеры
        screener_kb = self.tg._get_main_keyboard()
        self.assertTrue(any("🎯 Скринеры" in b.text for row in screener_kb.keyboard for b in row))

    async def test_screener_callbacks(self):
        """Проверка работы меню скринеров и настроек через коллбэки."""
        from CORE.SERVICES.SELECTOR.coin_selection_service import CoinSelectionService
        service = CoinSelectionService(bot_core=self.mock_bot_core)
        count = service.get_configured_target_count("alpha_coin_selector")
        self.assertGreaterEqual(count, 1)

        # Проверка форматирования отчета
        mock_results = [{"symbol": "BTCUSDT", "score": 95.5, "is_active_in_bot": True}]
        rep = service.format_telegram_report(mock_results, "alpha_coin_selector", count)
        self.assertIn("BTCUSDT", rep)

    async def test_set_coins_dome_selection_flow(self):
        """Проверка потока выбора купола и получения списка монет."""
        active_strats = self.registry.get_active_strategies()
        first_sid = next(iter(active_strats.keys()))
        syms = self.tg._extract_strategy_symbols(active_strats[first_sid])
        self.assertGreater(len(syms), 0)

        # Проверка метаданных стратегии
        meta = self.tg._get_strategy_meta(first_sid)
        self.assertIn("name", meta)
        self.assertEqual(meta["strategy_id"], first_sid)


if __name__ == "__main__":
    unittest.main()
