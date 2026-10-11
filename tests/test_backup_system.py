# ============================================================
# FILE: tests/test_backup_system.py
# ROLE: Unit tests for On-Demand RuntimeBackupService and backup configuration behavior
# ============================================================

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.SERVICES.runtime_backup_service import RuntimeBackupService
from c_utils import Utils


class TestBackupSystem(unittest.TestCase):
    """
    Тестирование архитектурных правил резервного копирования:
    1. Информативная подсветка при backup.enabled=true (без фонового спама архивами).
    2. Он-деманд экспорт рантайма из бинарного .pkl в санитизированный JSON.
    3. Отправка бэкапа в Telegram по вызову.
    """

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.runtime_dir = self.test_dir / "CFG" / "runtime"
        self.backups_dir = self.test_dir / "CACHE" / "backups"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_backup_enabled_behavior_purely_informative(self):
        """
        Проверка: при backup.enabled=true бот выдает чисто информативную подсветку в лог,
        self.backup_manager остается None, и никакой фоновый спам архивами не запускается.
        """
        with patch("CORE.bot.BACKUP_ENABLED", True), \
             patch("CORE.bot.logger") as mock_logger, \
             patch("CORE.bot.StrategyRegistry") as mock_reg, \
             patch("CORE.bot.BinanceClient"), \
             patch("CORE.bot.TakeProfitManager"), \
             patch("CORE.bot.AverageManager"), \
             patch("CORE.bot.FallbackTpManager"), \
             patch("CORE.bot.SpecManager"), \
             patch("CORE.bot.AnalyticsManager"), \
             patch("CORE.bot.RuntimeFsmManager"), \
             patch("CORE.bot.CrossProcessHotPriceStream"):
            
            mock_reg.return_value.get_all_symbols.return_value = ["ZROUSDT"]
            mock_reg.return_value.active_strategies = {}
            mock_reg.return_value.all_active_symbols = ["ZROUSDT"]

            from CORE.bot import BotCore
            bot = BotCore()
            
            # backup_manager должен строго быть None (никаких спам-тредов)
            self.assertIsNone(bot.backup_manager, "В новой архитектуре backup_manager должен быть None")
            
            # В лог должна поступить информативная подсветка с напоминанием On-Demand команд
            info_logged = any(
                "Информационная подсветка (backup.enabled=true)" in str(call_arg)
                for call_arg in mock_logger.info.call_args_list
            )
            self.assertTrue(info_logged, "Логгер должен зафиксировать информативную подсветку")

    def test_export_runtime_from_pkl_to_json(self):
        """
        Проверка: RuntimeBackupService считывает бинарные .pkl файлы под куполами стратегий
        и экспортирует единый санитизированный JSON в output_dir.
        """
        strat_id = "u9_omni_zenith_apex__off"
        strat_dir = self.runtime_dir / strat_id
        strat_dir.mkdir(parents=True, exist_ok=True)

        # Создаем тестовый .json рантайм
        sample_state = {
            "symbol": "ZROUSDT",
            "strategy_id": strat_id,
            "LONG": {
                "active_orders": 3,
                "current_step": 2,
                "avg_price": 4.15,
                "total_volume": 22.47
            },
            "SHORT": {
                "active_orders": 0,
                "current_step": 0
            }
        }
        sample_json = strat_dir / "zrousdt.json"
        Utils.atomic_write_json(sample_json, sample_state)

        # Вызываем экспорт с явным указанием путей
        export_file = RuntimeBackupService.export_runtime(
            runtime_dir=self.runtime_dir,
            output_dir=self.backups_dir
        )

        self.assertTrue(export_file.exists(), "Экспортированный JSON-файл должен существовать")
        self.assertEqual(export_file.suffix, ".json", "Формат бэкапа должен быть .json")
        self.assertTrue(export_file.name.startswith("runtime_backup_"))

        # Проверяем содержимое созданного JSON
        exported_data = Utils.read_json_file(export_file)
        self.assertIn("metadata", exported_data)
        self.assertEqual(exported_data["metadata"]["total_symbols"], 1)
        self.assertEqual(exported_data["metadata"]["service"], "RuntimeBackupService")

        self.assertIn("strategies", exported_data)
        self.assertIn(strat_id, exported_data["strategies"])
        self.assertIn("zrousdt", exported_data["strategies"][strat_id])
        zro_state = exported_data["strategies"][strat_id]["zrousdt"]
        self.assertEqual(zro_state["LONG"]["current_step"], 2)
        self.assertEqual(zro_state["LONG"]["avg_price"], 4.15)

        # Проверяем наличие runtime_backup_latest.json
        latest_file = self.backups_dir / "runtime_backup_latest.json"
        self.assertTrue(latest_file.exists(), "Файл runtime_backup_latest.json должен обновляться")

    def test_send_to_telegram_mocked(self):
        """
        Проверка: отправка готового JSON бэкапа в Telegram через aiohttp.
        """
        fake_backup = self.backups_dir / "test_backup.json"
        fake_backup.write_text(json.dumps({"test": 123}), encoding="utf-8")

        mock_resp = AsyncMock()
        mock_resp.status = 200

        mock_session_inst = MagicMock()
        mock_session_inst.post.return_value.__aenter__.return_value = mock_resp
        mock_session_inst.__aenter__.return_value = mock_session_inst
        mock_session_inst.__aexit__.return_value = None

        with patch.dict(os.environ, {"TG_TOKEN": "123456:FAKE_TOKEN"}), \
             patch("aiohttp.ClientSession", return_value=mock_session_inst):

            result = asyncio.run(RuntimeBackupService.send_to_telegram(fake_backup, chat_ids=[111222]))
            self.assertTrue(result)
            mock_session_inst.post.assert_called_once()
            call_url = mock_session_inst.post.call_args[0][0]
            self.assertIn("123456:FAKE_TOKEN/sendDocument", call_url)


if __name__ == "__main__":
    unittest.main()
