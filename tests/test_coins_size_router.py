# ==============================================================================
# Path: tests/test_coins_size_router.py
# Role: Юнит-тесты меню управления сайзами монет купола (Invest Size)
# ==============================================================================

import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from consts import DATA_DIR
from c_utils import Utils
from TG.HANDLERS.coins_size_router import CoinsSizeRouterHandler, get_coins_size_router
from TG.keyboards import (
    get_coins_sizes_menu_keyboard,
    get_dynamic_coins_keyboard,
    get_set_coins_keyboard,
)
from TG.states import TGStates


class TestCoinsSizeRouter(unittest.TestCase):
    """Тестирование интерфейса и синхронизации сайзов монет купола."""

    def setUp(self):
        self.mock_bot_core = MagicMock()
        self.mock_bot_core.strategy_registry = MagicMock()
        self.mock_bot_core.runtime_manager = MagicMock()
        self.mock_bot_core.position_monitor = MagicMock()
        self.mock_bot_core.runtime_configs = {}

        # Настраиваем mock кэшей рантайма
        self.mock_bot_core.runtime_manager.caches = {
            "SOLVUSDT": {
                "LONG": {"invest_size": 400.0, "eff_invest": 400.0},
                "SHORT": {"invest_size": 400.0, "eff_invest": 400.0},
            },
            "IOTXUSDT": {
                "LONG": {"invest_size": 200.0, "eff_invest": 200.0},
                "SHORT": {"invest_size": 200.0, "eff_invest": 200.0},
            },
        }

        # Настраиваем mock активных состояний позиций FSM
        mock_solv_state_long = MagicMock()
        mock_solv_state_long.eff_invest = 400.0
        mock_solv_state_short = MagicMock()
        mock_solv_state_short.eff_invest = 400.0
        self.mock_bot_core.position_monitor.states = {
            "SOLVUSDT": {
                "LONG": mock_solv_state_long,
                "SHORT": mock_solv_state_short,
            }
        }

        self.handler = CoinsSizeRouterHandler(bot_core=self.mock_bot_core)

    def test_keyboards_structure(self):
        """Проверяет корректность генерации клавиатур меню сайзов."""
        # 1. get_set_coins_keyboard содержит кнопку сайзов
        set_coins_kb = get_set_coins_keyboard()
        button_texts = [btn.text for row in set_coins_kb.keyboard for btn in row]
        self.assertIn("💰 Сайзы (Invest Size)", button_texts)
        self.assertIn("➕ Добавить монету", button_texts)
        self.assertIn("🗑️ Удалить монету", button_texts)
        self.assertIn("🔙 Назад", button_texts)

        # 2. get_coins_sizes_menu_keyboard содержит нужные разделы
        sizes_kb = get_coins_sizes_menu_keyboard()
        sizes_texts = [btn.text for row in sizes_kb.keyboard for btn in row]
        self.assertIn("🟢 Настроить LONG", sizes_texts)
        self.assertIn("🔴 Настроить SHORT", sizes_texts)
        self.assertIn("⚖️ Обе стороны (BOTH)", sizes_texts)
        self.assertIn("🌐 Базовый дефолт", sizes_texts)
        self.assertIn("🔙 Назад в Set Coins", sizes_texts)

        # 3. get_dynamic_coins_keyboard динамически формирует кнопки для монет
        sample_coins = ["SOLVUSDT", "IOTXUSDT", "BILLUSDT", "GTCUSDT", "XNYUSDT"]
        dyn_kb = get_dynamic_coins_keyboard(sample_coins)
        dyn_texts = [btn.text for row in dyn_kb.keyboard for btn in row]

        for coin in sample_coins:
            self.assertIn(coin, dyn_texts)
        self.assertIn("🔙 Назад к сайзам", dyn_texts)

        # Проверка, что кнопки раскладываются динамически по 2 в ряд
        for row in dyn_kb.keyboard[:-1]:
            self.assertLessEqual(len(row), 2)

    def test_render_dome_sizes_summary(self):
        """Проверяет формирование сводки настроек сайзов купола."""
        app_data = {
            "active_strategies": {
                "u17_stephensonE__trailing_ratchet": {
                    "enabled": True,
                    "LONG": {
                        "enabled": True,
                        "default_invest_size": 200.0,
                        "invest_size": {"SOLVUSDT": 400.0, "IOTXUSDT": 200.0},
                    },
                    "SHORT": {
                        "enabled": True,
                        "default_invest_size": 200.0,
                        "invest_size": {"SOLVUSDT": 400.0, "IOTXUSDT": 200.0},
                    },
                }
            }
        }
        with patch.object(self.handler, "_get_dome_symbols", return_value=["IOTXUSDT", "SOLVUSDT"]):
            summary = self.handler._render_dome_sizes_summary("u17_stephensonE__trailing_ratchet", app_data)
            self.assertIn("СВОДКА САЙЗОВ КУПОЛА СТРАТЕГИИ", summary)
            self.assertIn("u17_stephensonE__trailing_ratchet", summary)
            self.assertIn("🟢 <b>LONG</b>", summary)
            self.assertIn("🔴 <b>SHORT</b>", summary)
            self.assertIn("SOLVUSDT", summary)
            self.assertIn("400.0 USDT", summary)
            self.assertIn("IOTXUSDT", summary)
            self.assertIn("200.0 USDT", summary)

    def test_apply_invest_size_update_seamless_propagation(self):
        """Проверяет бесшовное применение обновленного сайза в app.json, кэши и FSM-состояния."""
        app_json_path = DATA_DIR / "app.json"
        original_data = Utils.read_json_file(app_json_path)

        test_data = copy.deepcopy(original_data)
        dome_id = "u17_stephensonE__trailing_ratchet"
        if "active_strategies" not in test_data or dome_id not in test_data["active_strategies"]:
            test_data.setdefault("active_strategies", {})[dome_id] = {
                "LONG": {"enabled": True, "default_invest_size": 200.0, "invest_size": {}},
                "SHORT": {"enabled": True, "default_invest_size": 200.0, "invest_size": {}},
            }

        with patch("c_utils.Utils.read_json_file", return_value=test_data), \
             patch("c_utils.Utils.write_json_file") as mock_write, \
             patch("CORE.strategy_applicator.StrategyApplicator.build_runtime_for_symbol") as mock_build_rt:

            # Обновляем сайз для SOLVUSDT на 550.0 USDT
            asyncio.run(self.handler._apply_invest_size_update(dome_id, "SOLVUSDT", "LONG", 550.0))

            # 1. Проверяем запись в app.json
            self.assertTrue(mock_write.called)
            saved_data = mock_write.call_args[0][1]
            self.assertEqual(saved_data["active_strategies"][dome_id]["LONG"]["invest_size"]["SOLVUSDT"], 550.0)

            # 2. Проверяем перезагрузку реестра и пересборку рантайма
            self.mock_bot_core.strategy_registry.reload.assert_called_once()
            mock_build_rt.assert_called_once()

            # 3. Проверяем горячее обновление кэша рантайма
            self.assertEqual(self.mock_bot_core.runtime_manager.caches["SOLVUSDT"]["LONG"]["invest_size"], 550.0)
            self.assertEqual(self.mock_bot_core.runtime_manager.caches["SOLVUSDT"]["LONG"]["eff_invest"], 550.0)

            # 4. Проверяем горячее обновление активного состояния FSM позиции
            p_state = self.mock_bot_core.position_monitor.states["SOLVUSDT"]["LONG"]
            self.assertEqual(p_state.eff_invest, 550.0)

    def test_apply_default_invest_size_update(self):
        """Проверяет обновление базового default_invest_size купола."""
        test_data = {
            "active_strategies": {
                "u17_stephensonE__trailing_ratchet": {
                    "enabled": True,
                    "LONG": {"enabled": True, "default_invest_size": 200.0, "invest_size": {"SOLVUSDT": 400.0}},
                    "SHORT": {"enabled": True, "default_invest_size": 200.0, "invest_size": {"SOLVUSDT": 400.0}},
                }
            }
        }
        dome_id = "u17_stephensonE__trailing_ratchet"

        with patch("c_utils.Utils.read_json_file", return_value=test_data), \
             patch("c_utils.Utils.write_json_file") as mock_write, \
             patch.object(self.handler, "_get_dome_symbols", return_value=["IOTXUSDT", "SOLVUSDT"]), \
             patch("CORE.strategy_applicator.StrategyApplicator.build_runtime_for_symbol"):

            asyncio.run(self.handler._apply_default_invest_size_update(dome_id, 300.0))

            # Проверяем запись в app.json
            self.assertTrue(mock_write.called)
            saved_data = mock_write.call_args[0][1]
            self.assertEqual(saved_data["active_strategies"][dome_id]["LONG"]["default_invest_size"], 300.0)
            self.assertEqual(saved_data["active_strategies"][dome_id]["SHORT"]["default_invest_size"], 300.0)

            # Для IOTXUSDT (у которого нет кастомного оверрайда) кэш должен обновиться до нового дефолта
            self.assertEqual(self.mock_bot_core.runtime_manager.caches["IOTXUSDT"]["LONG"]["invest_size"], 300.0)
            self.assertEqual(self.mock_bot_core.runtime_manager.caches["IOTXUSDT"]["LONG"]["eff_invest"], 300.0)

    def test_fail_fast_input_validation(self):
        """Проверяет строгую валидацию отрицательных и нулевых значений."""
        mock_msg = MagicMock()
        mock_msg.text = "0"
        mock_msg.answer = AsyncMock()

        mock_state = MagicMock()
        mock_state.get_data = AsyncMock(return_value={"selected_dome": "u17_stephensonE__trailing_ratchet", "target_symbol": "SOLVUSDT"})

        asyncio.run(self.handler.on_size_value_input(mock_msg, mock_state))

        # При нуле должно отправляться сообщение об ошибке
        mock_msg.answer.assert_called_once()
        self.assertIn("Некорректный размер", mock_msg.answer.call_args[0][0])

        mock_msg.reset_mock()
        mock_msg.text = "-100"
        asyncio.run(self.handler.on_size_value_input(mock_msg, mock_state))
        mock_msg.answer.assert_called_once()
        self.assertIn("Некорректный размер", mock_msg.answer.call_args[0][0])

        mock_msg.reset_mock()
        mock_msg.text = "abc"
        asyncio.run(self.handler.on_size_value_input(mock_msg, mock_state))
        mock_msg.answer.assert_called_once()
        self.assertIn("Некорректный размер", mock_msg.answer.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
