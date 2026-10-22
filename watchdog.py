# ==============================================================================
# Path: watchdog.py
# Role: Фоновый контроллер состояния главного цикла (отслеживание зависаний и производительности)
# ==============================================================================

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional, List, Dict, Any, Union

from consts import TG_ALLOWED_USERS
from c_log import UnifiedLogger
from TG import TelegramAdapter

logger = UnifiedLogger("Watchdog")


class WatchdogTGAdapter(TelegramAdapter):
    """
    Адаптер для отправки алертов и heartbeat в Telegram.
    Наследует институциональный TelegramAdapter из API.TELEGRAM.
    """

    def _get_chat_ids(self) -> list[int | str]:
        if self._chat_ids:
            return self._chat_ids

        chat_ids = []
        try:
            if TG_ALLOWED_USERS:
                chat_ids.extend(TG_ALLOWED_USERS)
        except Exception:
            pass

        if not chat_ids:
            admin_env = os.getenv("TG_ADMIN_ID")
            if admin_env:
                chat_ids.append(admin_env)

        return chat_ids



class LoopWatchdog:
    """
    Монитор зависания главного цикла и производительности.
    Делегирует уведомления в Telegram через WatchdogTGAdapter.
    """
    def __init__(
        self,
        bot,
        timeout_sec: int = 60,
        check_interval_sec: int = 1,
        heartbeat_interval_sec: int = 60,
        heartbeat_autodelete_sec: int = 180,
        tg_adapter: WatchdogTGAdapter | None = None
    ):
        self.bot = bot
        self.timeout_sec = timeout_sec
        self.check_interval_sec = check_interval_sec
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.heartbeat_autodelete_sec = heartbeat_autodelete_sec
        self.tg_adapter = tg_adapter or WatchdogTGAdapter()
        self.is_running = False
        self._alert_sent = False

        # Для трекинга
        self._last_report_time = time.time()
        self._last_report_ticks = 0
        self._last_heartbeat_time: float = 0.0

    @property
    def logger(self):
        if hasattr(self.bot, "logger") and self.bot.logger:
            return self.bot.logger
        return logger

    @property
    def server_name(self) -> str:
        return getattr(self.bot, "server_name", "HronBot")

    async def start(self):
        self.is_running = True
        self.logger.info(f"[WATCHDOG] Started. Timeout={self.timeout_sec}s, Check={self.check_interval_sec}s")

        self._last_report_ticks = getattr(self.bot, "_tick_count", 0)
        self._last_report_time = time.time()

        while self.is_running and getattr(self.bot, "is_running", True):
            now = time.time()
            last_tick = getattr(self.bot, "_last_tick", now)
            current_ticks = getattr(self.bot, "_tick_count", 0)

            diff = now - last_tick

            # 1. Проверка на зависание
            if diff > self.timeout_sec:
                if not self._alert_sent:
                    msg = f"🚨 <b>ВНИМАНИЕ! Сервер {self.server_name} упал/завис!</b>\nГлавный цикл не работает уже {int(diff)} секунд!"
                    self.logger.error(msg)
                    asyncio.create_task(self.tg_adapter.send_alert(msg, logger=self.logger))
                    self._alert_sent = True
            else:
                if self._alert_sent:
                    msg = f"✅ <b>Сервер {self.server_name} отвис!</b> Главный цикл снова работает."
                    self.logger.info(msg)
                    asyncio.create_task(self.tg_adapter.send_alert(msg, logger=self.logger))
                    self._alert_sent = False

            # 2. Отчет о производительности (пропускная способность лупы) раз в час (3600 секунд)
            time_since_report = now - self._last_report_time
            if time_since_report >= 3600.0:
                ticks_passed = current_ticks - self._last_report_ticks
                if ticks_passed > 0:
                    avg_iteration_time = time_since_report / ticks_passed
                    self.logger.info(f"[WATCHDOG] Game loop performance: {ticks_passed} iterations in {time_since_report:.1f}s. Avg iteration = {avg_iteration_time:.3f}s")
                else:
                    self.logger.warning(f"[WATCHDOG] No iterations in the last {time_since_report:.1f}s!")

                self._last_report_time = now
                self._last_report_ticks = current_ticks

            # 3. Heartbeat в телеграм
            if now - self._last_heartbeat_time >= self.heartbeat_interval_sec:
                self._last_heartbeat_time = now
                asyncio.create_task(
                    self.tg_adapter.send_or_update_heartbeat(
                        self.server_name,
                        self.heartbeat_autodelete_sec,
                        logger=self.logger
                    )
                )

            await asyncio.sleep(self.check_interval_sec)

    def stop(self):
        self.is_running = False

    async def aclose(self):
        self.stop()
        if hasattr(self, "tg_adapter") and hasattr(self.tg_adapter, "aclose"):
            await self.tg_adapter.aclose()