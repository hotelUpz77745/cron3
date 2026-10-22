# ==============================================================================
# Path: TG/tg_adapter.py
# Role: Адаптер для взаимодействия с Telegram Bot API (отправка сообщений, алертов, heartbeat)
# ==============================================================================

from __future__ import annotations

import asyncio
import datetime
import json
import os
import urllib.request
from typing import Optional, List, Dict, Any, Union
import aiohttp

from consts import TG_TOKEN, TG_ALLOWED_USERS
from c_log import UnifiedLogger

logger = UnifiedLogger("TelegramAdapter")


class TelegramAdapter:
    """
    Институциональный адаптер взаимодействия с Telegram Bot API.
    Инкапсулирует эндпоинты, форматирование payload, управление сессиями aiohttp
    и аварийную синхронную отправку фатальных ошибок запуска.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        chat_ids: Optional[List[Union[int, str]]] = None,
    ):
        self.token = token or TG_TOKEN or os.getenv("TG_TOKEN") or ""
        self._chat_ids = chat_ids
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock: Optional[asyncio.Lock] = None
        self._heartbeat_msg_id: Optional[int] = None
        self._heartbeat_msg_time: float = 0.0

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    @property
    def send_message_url(self) -> str:
        return f"{self.base_url}/sendMessage"

    @property
    def edit_message_url(self) -> str:
        return f"{self.base_url}/editMessageText"

    @property
    def delete_message_url(self) -> str:
        return f"{self.base_url}/deleteMessage"

    @property
    def send_document_url(self) -> str:
        return f"{self.base_url}/sendDocument"

    def _get_chat_ids(self) -> List[Union[int, str]]:
        if self._chat_ids:
            return self._chat_ids

        chat_ids: List[Union[int, str]] = []
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

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session
            self._session = aiohttp.ClientSession()
            return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    async def send_message(
        self,
        chat_id: Union[int, str],
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
        timeout_sec: int = 10,
    ) -> bool:
        if not self.token:
            return False
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        session = await self._get_session()
        try:
            async with session.post(
                self.send_message_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as resp:
                if resp.status == 200:
                    return True
                err_text = await resp.text()
                logger.error(f"Failed to send TG message to {chat_id}: status={resp.status}, err={err_text}")
                return False
        except Exception as e:
            logger.error(f"Error sending TG message to {chat_id}: {e}")
            return False

    async def send_alert(self, msg: str, logger: Optional[Any] = None) -> None:
        """Отправляет алерт всем зарегистрированным получателям."""
        log = logger or globals().get("logger")
        token = self.token or os.getenv("TG_TOKEN")
        if not token:
            if log:
                log.warning("[TELEGRAM] TG_TOKEN is missing in .env")
            return

        chat_ids = self._get_chat_ids()
        if not chat_ids:
            if log:
                log.warning("[TELEGRAM] No recipient chat IDs found for alert")
            return

        try:
            session = await self._get_session()
            for chat_id in chat_ids:
                try:
                    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
                    async with session.post(
                        self.send_message_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        pass
                except Exception as e:
                    if log:
                        log.error(f"[TELEGRAM] Failed to send alert to {chat_id}: {e}")
        except Exception as e:
            if log:
                log.error(f"[TELEGRAM] Session error sending alert: {e}")

    async def send_alert_with_keyboard(
        self, text: str, reply_markup: Dict[str, Any], logger: Optional[Any] = None
    ) -> None:
        log = logger or globals().get("logger")
        token = self.token or os.getenv("TG_TOKEN")
        if not token:
            return
        chat_ids = self._get_chat_ids()
        if not chat_ids:
            return

        session = await self._get_session()
        for uid in chat_ids:
            try:
                payload = {
                    "chat_id": uid,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup,
                }
                async with session.post(
                    self.send_message_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        if log:
                            log.error(f"[TELEGRAM] Failed to send TG alert with keyboard to {uid}: {err_text}")
            except Exception as e:
                if log:
                    log.error(f"[TELEGRAM] Error sending TG alert with keyboard to {uid}: {e}")

    async def send_or_update_heartbeat(
        self, server_name: str, autodelete_sec: int = 180, logger: Optional[Any] = None
    ) -> None:
        """Отправляет или обновляет сообщение heartbeat в Telegram."""
        log = logger or globals().get("logger")
        token = self.token or os.getenv("TG_TOKEN")
        if not token:
            return

        chat_ids = self._get_chat_ids()
        if not chat_ids:
            return

        chat_id = chat_ids[0]

        try:
            utc_now = datetime.datetime.now(datetime.timezone.utc)
            now = datetime.datetime.now().timestamp()

            date_str = utc_now.strftime("%Y-%m-%d")
            time_utc_str = utc_now.strftime("%H:%M:%S UTC")

            msg = (
                f"🟢 <b>{date_str}</b>\n\n"
                f"Server: <b>{server_name}</b> is running smoothly.\n\n"
                f"🕒 {time_utc_str}"
            )

            session = await self._get_session()
            if self._heartbeat_msg_id and (now - self._heartbeat_msg_time) >= autodelete_sec:
                payload = {"chat_id": chat_id, "message_id": self._heartbeat_msg_id}
                try:
                    async with session.post(
                        self.delete_message_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ):
                        pass
                except Exception:
                    pass

                self._heartbeat_msg_id = None
                self._heartbeat_msg_time = 0.0

            if self._heartbeat_msg_id:
                payload = {
                    "chat_id": chat_id,
                    "message_id": self._heartbeat_msg_id,
                    "text": msg,
                    "parse_mode": "HTML",
                }
                async with session.post(
                    self.edit_message_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    res = await resp.json()
                    if not res.get("ok"):
                        self._heartbeat_msg_id = None

            if not self._heartbeat_msg_id:
                payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
                async with session.post(
                    self.send_message_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    res = await resp.json()
                    if res.get("ok"):
                        self._heartbeat_msg_id = res["result"]["message_id"]
                        self._heartbeat_msg_time = now

        except Exception as e:
            if log:
                log.error(f"[TELEGRAM] Failed to send heartbeat: {e}")

    async def send_document(
        self,
        chat_id: Union[int, str],
        file_path_or_bytes: Union[str, bytes, Any],
        filename: str = "document.json",
        caption: str = "",
        parse_mode: str = "HTML",
        timeout_sec: int = 30,
    ) -> bool:
        if not self.token:
            return False

        session = await self._get_session()
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id))
            if caption:
                form.add_field("caption", caption)
                form.add_field("parse_mode", parse_mode)

            if isinstance(file_path_or_bytes, (str, os.PathLike)):
                with open(file_path_or_bytes, "rb") as fp:
                    form.add_field("document", fp, filename=filename, content_type="application/json")
                    async with session.post(
                        self.send_document_url,
                        data=form,
                        timeout=aiohttp.ClientTimeout(total=timeout_sec),
                    ) as resp:
                        return resp.status == 200
            else:
                form.add_field("document", file_path_or_bytes, filename=filename, content_type="application/json")
                async with session.post(
                    self.send_document_url,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=timeout_sec),
                ) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.error(f"[TELEGRAM] Failed to send document to {chat_id}: {e}")
            return False

    def send_fatal_error(self, msg: str) -> None:
        """
        Синхронный метод отправки критической стартовой ошибки.
        Используется в main.py при падении бота до/вне asyncio цикла.
        """
        token = self.token or os.getenv("TG_TOKEN")
        chat_ids = self._get_chat_ids()
        if not token or not chat_ids:
            return

        text = f"🚨 СТАРТОВАЯ ОШИБКА!\nБот упал при запуске:\n\n{msg}"
        url = self.send_message_url
        for user_id in chat_ids:
            try:
                data = json.dumps({"chat_id": user_id, "text": text}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5):
                    pass
            except Exception:
                pass

    @classmethod
    def send_fatal_alert(
        cls,
        msg: str,
        token: Optional[str] = None,
        chat_ids: Optional[List[Union[int, str]]] = None,
    ) -> None:
        """
        Фасадный вызов для мгновенной синхронной отправки стартовой ошибки.
        """
        adapter = cls(token=token, chat_ids=chat_ids)
        adapter.send_fatal_error(msg)
