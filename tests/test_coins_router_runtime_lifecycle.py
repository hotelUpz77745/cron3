# ==============================================================================
# Path: tests/test_coins_router_runtime_lifecycle.py
# Role: Юнит-тесты интеграции CoinsRouterHandler с жизненным циклом рантаймов монет
# ==============================================================================

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from CORE.strategy_applicator import StrategyApplicator
from CORE.strategy_registry import StrategyRegistry
from TG.HANDLERS.coins_router import CoinsRouterHandler, get_coins_router
from TG.states import TGStates


class TestCoinsRouterRuntimeLifecycle(unittest.IsolatedAsyncioTestCase):
    """
    Институциональное тестирование CoinsRouterHandler:
    - Проверка создания рантайма нового символа и заступления в работу (add_symbol).
    - Проверка удаления монеты, очистки рантайм-файлов и исключения из обоймы (delete_symbol).
    """

    async def asyncSetUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_coins_lifecycle_"))
        self.runtime_dir = self.temp_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.strat_runtime_dir = self.runtime_dir / "u17_stephensonE__trailing_ratchet"
        self.strat_runtime_dir.mkdir(parents=True, exist_ok=True)

        self.app_json_path = self.temp_dir / "app.json"
        self.initial_app_data = {
            "active_strategies": {
                "u17_stephensonE__trailing_ratchet": {
                    "name": "Stephenson-E",
                    "template": "u17_stephensonE",
                    "enabled": True,
                    "default_invest_size": 400.0,
                    "symbols": ["BTCUSDT"],
                    "LONG": {"default_invest_size": 400.0, "invest_size": {"BTCUSDT": 400.0}},
                    "SHORT": {"default_invest_size": 400.0, "invest_size": {"BTCUSDT": 400.0}},
                }
            }
        }
        self.app_json_path.write_text(json.dumps(self.initial_app_data, indent=2), encoding="utf-8")

        # Mock BotCore
        self.mock_bot = MagicMock()
        self.mock_bot.symbols = ["BTCUSDT"]
        self.mock_bot.is_paused = False
        self.mock_bot.prices = {"BTCUSDT": (60000.0, 1000.0)}
        self.mock_bot.fsm_states = {"BTCUSDT": MagicMock()}
        self.mock_bot.runtime_configs = {}

        # Strategy registry
        self.registry = StrategyRegistry()
        self.registry.reload(self.initial_app_data["active_strategies"])
        self.mock_bot.strategy_registry = self.registry

        # Lifecycle simulation
        async def mock_add_symbol(sym, strategy_id=None):
            sym = sym.upper()
            if sym in self.mock_bot.symbols:
                return False
            self.mock_bot.symbols.append(sym)
            strat_cfg = self.registry.get_strategy(strategy_id or "u17_stephensonE__trailing_ratchet")
            if strat_cfg:
                strat_cfg.symbols.append(sym)
                # Build real runtime config file via StrategyApplicator
                StrategyApplicator().build_runtime_for_symbol(
                    sym, strat_cfg, target_dir=self.strat_runtime_dir
                )
            return True

        async def mock_delete_symbol(sym):
            sym = sym.upper()
            if sym not in self.mock_bot.symbols:
                return False
            self.mock_bot.symbols.remove(sym)
            strat_cfg = self.registry.get_strategy("u17_stephensonE__trailing_ratchet")
            if strat_cfg and sym in strat_cfg.symbols:
                strat_cfg.symbols.remove(sym)
            # Remove runtime file
            sym_file = self.strat_runtime_dir / f"{sym.lower()}.json"
            if sym_file.exists():
                sym_file.unlink()
            return True

        self.mock_bot.add_symbol = AsyncMock(side_effect=mock_add_symbol)
        self.mock_bot.delete_symbol = AsyncMock(side_effect=mock_delete_symbol)

        # Template manager mock
        self.mock_tm = MagicMock()
        self.mock_tm.get_runtime_path = lambda sym, sid="u17_stephensonE__trailing_ratchet": self.strat_runtime_dir / f"{sym.lower()}.json"

        # Initialize CoinsRouterHandler class under test
        self.handler = CoinsRouterHandler(bot_core=self.mock_bot, template_manager=self.mock_tm)

        # Telegram FSM setup
        self.storage = MemoryStorage()
        self.user = User(id=777, is_bot=False, first_name="Tester", username="tester")
        self.chat = Chat(id=777, type="private")
        self.storage_key = StorageKey(bot_id=1, chat_id=777, user_id=777)
        self.state = FSMContext(storage=self.storage, key=self.storage_key)

    async def asyncTearDown(self):
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_msg(self, text: str) -> Message:
        msg = MagicMock(spec=Message)
        msg.text = text
        msg.from_user = self.user
        msg.chat = self.chat
        msg.answer = AsyncMock()
        msg.reply = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.delete = AsyncMock()
        return msg

    def _make_callback(self, data: str) -> CallbackQuery:
        cb = MagicMock(spec=CallbackQuery)
        cb.data = data
        cb.from_user = self.user
        cb.message = self._make_msg("")
        cb.answer = AsyncMock()
        return cb

    @patch("TG.HANDLERS.coins_router.DATA_DIR")
    async def test_add_symbol_creates_runtime_and_activates(self, mock_data_dir):
        """Проверка: добавление монеты создает изолированный рантайм и заступает в работу."""
        mock_data_dir.__truediv__.return_value = self.app_json_path

        # Устанавливаем контекст выбранного купола в FSM
        await self.state.update_data(selected_dome="u17_stephensonE__trailing_ratchet")
        await self.state.set_state(TGStates.waiting_for_add_symbol)

        # Пользователь вводит тикер монеты
        msg = self._make_msg("SOLUSDT")
        await self.handler.process_add_symbol(msg, self.state)

        # 1. Проверяем вызов add_symbol на BotCore
        self.mock_bot.add_symbol.assert_called_once_with("SOLUSDT", strategy_id="u17_stephensonE__trailing_ratchet")

        # 2. Проверяем, что символ заступил в обойму
        self.assertIn("SOLUSDT", self.mock_bot.symbols)

        # 3. Проверяем создание рантайм-файла
        sol_runtime = self.strat_runtime_dir / "solusdt.json"
        self.assertTrue(sol_runtime.exists(), "Рантайм-файл solusdt.json должен быть создан")

        with open(sol_runtime, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("LONG", cfg)
        self.assertIn("SHORT", cfg)
        self.assertEqual(cfg["LONG"]["invest_size"], 400.0)

        # 4. Проверяем ответ бота пользователю
        msg.answer.assert_called()
        ans_text = msg.answer.call_args[0][0]
        self.assertIn("SOLUSDT успешно добавлена", ans_text)

    @patch("TG.HANDLERS.coins_router.DATA_DIR")
    async def test_delete_symbol_cleans_runtime_and_deactivates(self, mock_data_dir):
        """Проверка: удаление монеты уничтожает кэш рантайма и исключает символ из торговли."""
        mock_data_dir.__truediv__.return_value = self.app_json_path

        # Сначала добавляем монету
        await self.mock_bot.add_symbol("DOGEUSDT", strategy_id="u17_stephensonE__trailing_ratchet")
        doge_runtime = self.strat_runtime_dir / "dogeusdt.json"
        self.assertTrue(doge_runtime.exists())
        self.assertIn("DOGEUSDT", self.mock_bot.symbols)

        # Пользователь нажимает инлайн-кнопку удаления
        cb = self._make_callback("del_dome_coin:u17_stephensonE__trailing_ratchet:DOGEUSDT")
        await self.handler.process_del_dome_coin(cb)

        # 1. Проверяем вызов delete_symbol на BotCore
        self.mock_bot.delete_symbol.assert_called_once_with("DOGEUSDT")

        # 2. Символ должен быть исключен из списка активных
        self.assertNotIn("DOGEUSDT", self.mock_bot.symbols)

        # 3. Файл рантайма должен быть удален
        self.assertFalse(doge_runtime.exists(), "Рантайм-файл dogeusdt.json должен быть удален")

        # 4. Сообщение пользователю обновлено с подтверждением
        cb.message.edit_text.assert_called()
        self.assertIn("DOGEUSDT", cb.message.edit_text.call_args[0][0])
        self.assertIn("успешно удалена", cb.message.edit_text.call_args[0][0])

    def test_factory_function_returns_router(self):
        """Фабричная функция get_coins_router возвращает настроенный aiogram Router."""
        r = get_coins_router(self.mock_bot, self.mock_tm)
        self.assertIsNotNone(r)
        self.assertEqual(r.name, "coins_router")


if __name__ == "__main__":
    unittest.main()
