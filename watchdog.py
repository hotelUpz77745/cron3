# ============================================================
# File: watchdog.py
# Role: Фоновый контроллер состояния главного цикла (отслеживание зависаний и производительности)
# ============================================================

import asyncio
import os
import time
import datetime
import pytz
import aiohttp

class WatchdogTGAdapter:
    """
    Адаптер для отправки алертов и heartbeat в Telegram.
    Токен берется строго из .env (TG_TOKEN).
    """
    def __init__(self, token: str | None = None, chat_ids: list[int | str] | None = None):
        # Токен строго из .env (TG_TOKEN) без фолбэков на сторонние конфиги
        self.token = token or os.getenv("TG_TOKEN") or ""
        self._chat_ids = chat_ids
        self._heartbeat_msg_id = None
        self._heartbeat_msg_time = 0

    def _get_chat_ids(self) -> list[int | str]:
        if self._chat_ids:
            return self._chat_ids

        chat_ids = []
        # Из consts (TG_ALLOWED_USERS)
        try:
            from consts import TG_ALLOWED_USERS
            if TG_ALLOWED_USERS:
                chat_ids.extend(TG_ALLOWED_USERS)
        except Exception:
            pass

        # Дополнительно из переменной окружения, если есть
        if not chat_ids:
            admin_env = os.getenv("TG_ADMIN_ID")
            if admin_env:
                chat_ids.append(admin_env)

        return chat_ids

    async def send_alert(self, msg: str, logger=None):
        token = self.token or os.getenv("TG_TOKEN")
        if not token:
            if logger:
                logger.warning("[WATCHDOG] TG_TOKEN is missing in .env")
            return

        chat_ids = self._get_chat_ids()
        if not chat_ids:
            if logger:
                logger.warning("[WATCHDOG] No recipient chat IDs found for alert")
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            for chat_id in chat_ids:
                try:
                    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        pass
                except Exception as e:
                    if logger:
                        logger.error(f"[WATCHDOG] Failed to send admin alert to {chat_id}: {e}")

    async def send_or_update_heartbeat(self, server_name: str, autodelete_sec: int = 180, logger=None):
        token = self.token or os.getenv("TG_TOKEN")
        if not token:
            return

        chat_ids = self._get_chat_ids()
        if not chat_ids:
            return

        chat_id = chat_ids[0]

        try:
            utc_now = datetime.datetime.now(pytz.utc)
            try:
                tz_kyiv = pytz.timezone("Europe/Kyiv")
                kyiv_now = utc_now.astimezone(tz_kyiv)
            except Exception:
                kyiv_now = utc_now

            now = time.time()

            date_str = kyiv_now.strftime("%Y-%m-%d")
            time_utc_str = utc_now.strftime("%H:%M:%S UTC")
            time_kyiv_str = kyiv_now.strftime("%H:%M:%S Kyiv")

            msg = (
                f"🟢 <b>{date_str}</b>\n\n"
                f"Server: <b>{server_name}</b> is running smoothly.\n\n"
                f"🕒 {time_utc_str}\n"
                f"🕒 {time_kyiv_str}"
            )

            async with aiohttp.ClientSession() as session:
                if self._heartbeat_msg_id and (now - self._heartbeat_msg_time) >= autodelete_sec:
                    delete_url = f"https://api.telegram.org/bot{token}/deleteMessage"
                    payload = {"chat_id": chat_id, "message_id": self._heartbeat_msg_id}
                    try:
                        async with session.post(delete_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)):
                            pass
                    except Exception:
                        pass

                    self._heartbeat_msg_id = None
                    self._heartbeat_msg_time = 0

                if self._heartbeat_msg_id:
                    url = f"https://api.telegram.org/bot{token}/editMessageText"
                    payload = {"chat_id": chat_id, "message_id": self._heartbeat_msg_id, "text": msg, "parse_mode": "HTML"}
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        res = await resp.json()
                        if not res.get("ok"):
                            self._heartbeat_msg_id = None

                if not self._heartbeat_msg_id:
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        res = await resp.json()
                        if res.get("ok"):
                            self._heartbeat_msg_id = res["result"]["message_id"]
                            self._heartbeat_msg_time = now

        except Exception as e:
            if logger:
                logger.error(f"[WATCHDOG] Failed to send heartbeat: {e}")


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
        self._last_heartbeat_time = 0

    @property
    def logger(self):
        if hasattr(self.bot, "logger") and self.bot.logger:
            return self.bot.logger
        import logging
        return logging.getLogger("Watchdog")

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