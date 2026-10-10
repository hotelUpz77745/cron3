# tests/test_risk_system.py
# Unit tests for Risk System: Stop-loss handbrake and Miami Take profit stop

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import json
import asyncio
from pathlib import Path

from CORE.auto_closer import AutoCloser


class TestRiskSystem(unittest.TestCase):
    def setUp(self):
        self.mock_bot = MagicMock()
        self.mock_bot.is_paused = False
        self.mock_bot.entry_blocked = False
        self.mock_bot.block_until_timestamp = 0
        self.mock_bot.close_all_positions = AsyncMock()
        self.mock_bot.notifier = MagicMock()
        self.mock_bot.notifier.send_alert = AsyncMock()
        self.mock_bot.strategy_registry = MagicMock()
        self.mock_bot.strategy_registry.active_strategies = {}

        self.auto_closer = AutoCloser(bot_core=self.mock_bot)

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("CORE.auto_closer.Utils.write_json_file")
    @patch("CORE.auto_closer.Utils.read_json_file")
    def test_profit_stop_miami_take_triggers(self, mock_read_json, mock_write_json, mock_sleep):
        """Проверка: при достижении целевого профицита max_balance_threshold_usdt бот фиксирует прибыль в 100% кэш и останавливается."""
        # Моделируем app.json с включенным max_balance_threshold_usdt = 250.0
        app_data = {
            "risk_system": {
                "enabled": True,
                "min_balance_threshold_usdt": 100.0,
                "max_balance_threshold_usdt": 250.0
            }
        }
        # Моделируем баланс в analytics.json, превысивший порог (260.5 USDT)
        analytics_data = {
            "cur_balance_usdt": 260.5
        }

        def side_effect_read(path):
            p_str = str(path)
            if "app.json" in p_str:
                return app_data
            if "analytics.json" in p_str:
                return analytics_data
            return {}

        mock_read_json.side_effect = side_effect_read

        # Запускаем проверку
        asyncio.run(self.auto_closer.check())

        # Проверяем срабатывание
        self.assertTrue(self.mock_bot.is_paused)
        self.assertTrue(self.mock_bot.entry_blocked)
        self.assertGreater(self.mock_bot.block_until_timestamp, 0)
        self.mock_bot.close_all_positions.assert_called_once_with(reset_iterations=False)
        self.mock_bot.notifier.send_alert.assert_called_once()
        alert_msg = self.mock_bot.notifier.send_alert.call_args[0][0]
        self.assertIn("СТОП ПО ПРОФИЦИТУ (MIAMI PROFIT TAKE)", alert_msg)
        self.assertIn("260.50 USDT", alert_msg)
        self.assertIn("250.00 USDT", alert_msg)

        # Проверяем, что флаг enabled сброшен в False после срабатывания
        self.assertFalse(app_data["risk_system"]["enabled"])
        mock_write_json.assert_called_once()

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("CORE.auto_closer.Utils.write_json_file")
    @patch("CORE.auto_closer.Utils.read_json_file")
    def test_risk_stop_min_threshold_triggers(self, mock_read_json, mock_write_json, mock_sleep):
        """Проверка: при падении баланса ниже min_balance_threshold_usdt срабатывает глобальный ручник."""
        app_data = {
            "risk_system": {
                "enabled": True,
                "min_balance_threshold_usdt": 100.0,
                "max_balance_threshold_usdt": 300.0
            }
        }
        analytics_data = {
            "cur_balance_usdt": 85.0
        }

        def side_effect_read(path):
            p_str = str(path)
            if "app.json" in p_str:
                return app_data
            if "analytics.json" in p_str:
                return analytics_data
            return {}

        mock_read_json.side_effect = side_effect_read

        asyncio.run(self.auto_closer.check())

        self.assertTrue(self.mock_bot.is_paused)
        self.assertTrue(self.mock_bot.entry_blocked)
        self.mock_bot.close_all_positions.assert_called_once_with(reset_iterations=False)
        self.mock_bot.notifier.send_alert.assert_called_once()
        alert_msg = self.mock_bot.notifier.send_alert.call_args[0][0]
        self.assertIn("ГЛОБАЛЬНЫЙ РУЧНИК (RISK STOP)", alert_msg)
        self.assertIn("85.00 USDT", alert_msg)
        self.assertIn("100.00 USDT", alert_msg)
        self.assertFalse(app_data["risk_system"]["enabled"])

    @patch("CORE.auto_closer.Utils.write_json_file")
    @patch("CORE.auto_closer.Utils.read_json_file")
    def test_within_corridor_no_trigger(self, mock_read_json, mock_write_json):
        """Проверка: пока баланс находится внутри коридора [min, max], бот работает штатно."""
        app_data = {
            "risk_system": {
                "enabled": True,
                "min_balance_threshold_usdt": 100.0,
                "max_balance_threshold_usdt": 300.0
            }
        }
        analytics_data = {
            "cur_balance_usdt": 180.0
        }

        def side_effect_read(path):
            p_str = str(path)
            if "app.json" in p_str:
                return app_data
            if "analytics.json" in p_str:
                return analytics_data
            return {}

        mock_read_json.side_effect = side_effect_read

        asyncio.run(self.auto_closer.check())

        self.assertFalse(self.mock_bot.is_paused)
        self.assertFalse(self.mock_bot.entry_blocked)
        self.mock_bot.close_all_positions.assert_not_called()
        self.mock_bot.notifier.send_alert.assert_not_called()
        self.assertTrue(app_data["risk_system"]["enabled"])

    @patch("CORE.auto_closer.Utils.write_json_file")
    @patch("CORE.auto_closer.Utils.read_json_file")
    def test_disabled_risk_system_no_trigger(self, mock_read_json, mock_write_json):
        """Проверка: если risk_system отключена (enabled=False), триггеры не срабатывают."""
        app_data = {
            "risk_system": {
                "enabled": False,
                "min_balance_threshold_usdt": 100.0,
                "max_balance_threshold_usdt": 200.0
            }
        }
        # Баланс сильно выше порога, но система отключена
        analytics_data = {
            "cur_balance_usdt": 350.0
        }

        def side_effect_read(path):
            p_str = str(path)
            if "app.json" in p_str:
                return app_data
            if "analytics.json" in p_str:
                return analytics_data
            return {}

        mock_read_json.side_effect = side_effect_read

        asyncio.run(self.auto_closer.check())

        self.assertFalse(self.mock_bot.is_paused)
        self.mock_bot.close_all_positions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
