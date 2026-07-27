# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\notifier.py
# Role: notifier.py module

# ==============================================================================
# Path: CORE/notifier.py
# Role: Менеджер уведомлений по просадке и профициту
# ==============================================================================

import asyncio
import os
import aiohttp
from consts import DATA_DIR, ANALYTICS_DIR
from c_utils import Utils
from c_log import UnifiedLogger

from consts import TG_ALLOWED_USERS
logger = UnifiedLogger("Notifier")

class NotifierManager:
    def __init__(self):
        self.is_running = False
        self.neg_flag = False
        self.pos_flag = False
        self._task = None
        self.last_neg_threshold = None
        self.last_pos_threshold = None

    async def start(self):
        self.is_running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[NOTIFIER] Started.")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
        logger.info("[NOTIFIER] Stopped.")

    async def _loop(self):
        while self.is_running:
            try:
                await self.check_thresholds()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[NOTIFIER] Error in loop: {e}")
            await asyncio.sleep(15)

    def _get_chat_ids(self):
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

    async def check_thresholds(self):
        # Читаем конфиг на лету, чтобы реагировать на изменения через Telegram
        app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
        notif_cfg = app_cfg.get("notifications", {})
        if not notif_cfg.get("enabled", False):
            return

        neg_threshold = float(notif_cfg.get("negative_threshold", -100.0))
        pos_threshold = float(notif_cfg.get("positive_threshold", 100.0))

        # Если пороги изменились с прошлого тика - сбрасываем флаги
        if self.last_neg_threshold is not None and self.last_neg_threshold != neg_threshold:
            self.neg_flag = False
            logger.info(f"[NOTIFIER] Negative threshold changed to {neg_threshold}. Resetting neg_flag.")
        if self.last_pos_threshold is not None and self.last_pos_threshold != pos_threshold:
            self.pos_flag = False
            logger.info(f"[NOTIFIER] Positive threshold changed to {pos_threshold}. Resetting pos_flag.")
            
        self.last_neg_threshold = neg_threshold
        self.last_pos_threshold = pos_threshold

        # Читаем текущий профит из аналитики
        analytics_data = Utils.read_json_file(ANALYTICS_DIR / "analytics.json")
        if not analytics_data:
            return
        net_profit = float(analytics_data.get("net_profit_usdt", 0.0))

        # Логика NEGATIVE
        if net_profit <= neg_threshold and not self.neg_flag:
            self.neg_flag = True
            msg = f"⚠️ <b>ВНИМАНИЕ! ПРОСАДКА!</b>\nПрофит опустился до: <b>{net_profit} USDT</b>\nПорог: {neg_threshold} USDT"
            logger.info(f"[NOTIFIER] Negative threshold reached: {net_profit} <= {neg_threshold}")
            await self.send_alert(msg)
        elif self.neg_flag and net_profit >= 0:
            self.neg_flag = False
            msg = f"✅ <b>ПРОСАДКА ВОССТАНОВЛЕНА.</b>\nТекущий профит: <b>{net_profit} USDT</b>\nФлаг тревоги снят."
            logger.info(f"[NOTIFIER] Negative drawdown recovered: {net_profit} >= 0")
            await self.send_alert(msg)

        # Логика POSITIVE
        if net_profit >= pos_threshold and not self.pos_flag:
            self.pos_flag = True
            msg = f"🎉 <b>ОТЛИЧНО! ДОСТИГНУТ ПРОФИЦИТ!</b>\nПрофит: <b>{net_profit} USDT</b>\nПорог: {pos_threshold} USDT"
            logger.info(f"[NOTIFIER] Positive threshold reached: {net_profit} >= {pos_threshold}")
            await self.send_alert(msg)
        elif self.pos_flag and net_profit <= 0:
            self.pos_flag = False
            msg = f"📉 <b>ПРОФИЦИТ ОПУСТИЛСЯ НИЖЕ НУЛЯ.</b>\nТекущий профит: <b>{net_profit} USDT</b>\nФлаг позитивного порога снят."
            logger.info(f"[NOTIFIER] Positive profit lost: {net_profit} <= 0")
            await self.send_alert(msg)

    async def send_alert(self, text: str):
        token = os.getenv("TG_TOKEN")
        if not token:
            return
            
        chat_ids = self._get_chat_ids()
        if not chat_ids:
            return
            
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        
        async with aiohttp.ClientSession() as session:
            for uid in chat_ids:
                try:
                    payload = {"chat_id": uid, "text": text, "parse_mode": "HTML"}
                    async with session.post(url, json=payload, timeout=5) as response:
                        if response.status != 200:
                            err_text = await response.text()
                            logger.error(f"[NOTIFIER] Failed to send TG alert to {uid}: {err_text}")
                except Exception as e:
                    logger.error(f"[NOTIFIER] Error sending TG alert to {uid}: {e}")

    def reset_flags(self):
        self.neg_flag = False
        self.pos_flag = False
        logger.info("[NOTIFIER] Notification flags manually reset.")
