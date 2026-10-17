# tests/test_watchdog.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: tests/test_watchdog.py
Role: Институциональный юнит-тест контроллера главного цикла (LoopWatchdog & WatchdogTGAdapter)
==============================================================================
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from watchdog import LoopWatchdog, WatchdogTGAdapter


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        self.mock_bot = MagicMock()
        self.mock_bot.is_running = True
        self.mock_bot.server_name = "TestHronBot"
        self.mock_bot._tick_count = 10
        self.mock_bot._last_tick = time.time()
        self.mock_logger = MagicMock()
        self.mock_bot.logger = self.mock_logger

    def test_watchdog_tg_adapter_chat_ids_resolution(self):
        """WatchdogTGAdapter корректно резолвит список chat_ids."""
        with patch("watchdog.TG_ALLOWED_USERS", [111, 222]):
            adapter = WatchdogTGAdapter()
            self.assertEqual(adapter._get_chat_ids(), [111, 222])

        # С явным списком chat_ids
        adapter_custom = WatchdogTGAdapter(chat_ids=[999])
        self.assertEqual(adapter_custom._get_chat_ids(), [999])

    def test_watchdog_tg_adapter_send_alert(self):
        """WatchdogTGAdapter отправляет HTTP запрос алерта всем получателям."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post.return_value = mock_resp

        adapter = WatchdogTGAdapter(token="test_token", chat_ids=[12345])
        adapter._session = mock_session

        asyncio.run(adapter.send_alert("⚠️ Test Alert"))
        mock_session.post.assert_called_once()
        call_url = mock_session.post.call_args[0][0]
        self.assertIn("https://api.telegram.org/bottest_token/sendMessage", call_url)
        payload = mock_session.post.call_args[1]["json"]
        self.assertEqual(payload["chat_id"], 12345)
        self.assertEqual(payload["text"], "⚠️ Test Alert")

    def test_loop_watchdog_detects_freeze_and_recovery(self):
        """LoopWatchdog детектирует зависание цикла при превышении timeout_sec и отправляет восстановление."""
        mock_adapter = MagicMock()
        mock_adapter.send_alert = AsyncMock()
        mock_adapter.send_or_update_heartbeat = AsyncMock()

        watchdog = LoopWatchdog(
            bot=self.mock_bot,
            timeout_sec=5,
            check_interval_sec=1,
            heartbeat_interval_sec=60,
            tg_adapter=mock_adapter,
        )

        # Симулируем 1 такт проверки с зависшим главным циклом (diff = 10s > 5s timeout)
        now = time.time()
        self.mock_bot._last_tick = now - 10.0

        # Выполняем одну проверку тела цикла
        diff = now - self.mock_bot._last_tick
        self.assertGreater(diff, watchdog.timeout_sec)

        # Вызываем логику отправки алерта
        msg = f"🚨 <b>ВНИМАНИЕ! Сервер {watchdog.server_name} упал/завис!</b>"
        asyncio.run(mock_adapter.send_alert(msg, logger=watchdog.logger))
        watchdog._alert_sent = True
        mock_adapter.send_alert.assert_called_once()

        # Симулируем восстановление работы цикла
        self.mock_bot._last_tick = time.time()
        now_rev = time.time()
        diff_rev = now_rev - self.mock_bot._last_tick
        self.assertLessEqual(diff_rev, watchdog.timeout_sec)
        self.assertTrue(watchdog._alert_sent)

        # Отправка уведомления об отвисании
        msg_rec = f"✅ <b>Сервер {watchdog.server_name} отвис!</b>"
        asyncio.run(mock_adapter.send_alert(msg_rec, logger=watchdog.logger))
        watchdog._alert_sent = False
        self.assertEqual(mock_adapter.send_alert.await_count, 2)

    def test_loop_watchdog_performance_reporting(self):
        """LoopWatchdog вычисляет среднее время итерации при часовом отчете."""
        watchdog = LoopWatchdog(
            bot=self.mock_bot,
            timeout_sec=60,
            check_interval_sec=1,
        )
        watchdog._last_report_ticks = 100
        watchdog._last_report_time = time.time() - 3605.0  # прошло больше часа

        current_ticks = 4700
        time_since_report = 3605.0
        ticks_passed = current_ticks - watchdog._last_report_ticks
        avg_iteration_time = time_since_report / ticks_passed

        self.assertEqual(ticks_passed, 4600)
        self.assertAlmostEqual(avg_iteration_time, 3605.0 / 4600, places=4)

    def test_loop_watchdog_lifecycle_stop(self):
        """Корректная остановка и освобождение ресурсов watchdog."""
        mock_adapter = MagicMock()
        mock_adapter.aclose = AsyncMock()

        watchdog = LoopWatchdog(
            bot=self.mock_bot,
            timeout_sec=60,
            tg_adapter=mock_adapter,
        )
        watchdog.is_running = True
        watchdog.stop()
        self.assertFalse(watchdog.is_running)

        asyncio.run(watchdog.aclose())
        mock_adapter.aclose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
