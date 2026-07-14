import asyncio
import aiohttp
from consts import TG_TOKEN, TG_ALLOWED_USERS, DATA_DIR, ANALYTICS_DIR
from c_utils import Utils
from c_log import UnifiedLogger

logger = UnifiedLogger("Notifier")

class NotifierManager:
    def __init__(self):
        self.is_running = False
        self.neg_flag = False
        self.pos_flag = False
        self._task = None

    async def start(self):
        self.is_running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("NotifierManager started.")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
        logger.info("NotifierManager stopped.")

    async def _loop(self):
        while self.is_running:
            try:
                await self.check_thresholds()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in NotifierManager loop: {e}")
            await asyncio.sleep(15)

    async def check_thresholds(self):
        # Read app.json config
        app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
        notif_cfg = app_cfg.get("notifications", {})
        if not notif_cfg.get("enabled", False):
            return

        neg_threshold = float(notif_cfg.get("negative_threshold", -100.0))
        pos_threshold = float(notif_cfg.get("positive_threshold", 100.0))

        # Reset flag if threshold changed
        if getattr(self, 'last_neg_threshold', None) is not None and self.last_neg_threshold != neg_threshold:
            self.neg_flag = False
            logger.info(f"Negative threshold changed from {self.last_neg_threshold} to {neg_threshold}. Resetting neg_flag.")
        if getattr(self, 'last_pos_threshold', None) is not None and self.last_pos_threshold != pos_threshold:
            self.pos_flag = False
            logger.info(f"Positive threshold changed from {self.last_pos_threshold} to {pos_threshold}. Resetting pos_flag.")
            
        self.last_neg_threshold = neg_threshold
        self.last_pos_threshold = pos_threshold

        # Read analytics.json
        analytics_data = Utils.read_json_file(ANALYTICS_DIR / "analytics.json")
        if not analytics_data:
            return
        net_profit = float(analytics_data.get("net_profit_usdt", 0.0))

        # Logic for negative threshold
        if net_profit <= neg_threshold and not self.neg_flag:
            self.neg_flag = True
            logger.info(f"Negative threshold reached: {net_profit} <= {neg_threshold}")
            await self.send_alert(f"⚠️ <b>ВНИМАНИЕ!</b>\nДостигнут отрицательный порог профита: <b>{net_profit} USDT</b> (Порог: {neg_threshold})")
        elif self.neg_flag and net_profit >= 0:
            self.neg_flag = False
            logger.info(f"Negative drawdown recovered: {net_profit} >= 0")
            await self.send_alert(f"✅ <b>Просадка восстановлена.</b>\nNet Profit: <b>{net_profit} USDT</b>.\nФлаг отрицательного порога снят.")

        # Logic for positive threshold
        if net_profit >= pos_threshold and not self.pos_flag:
            self.pos_flag = True
            logger.info(f"Positive threshold reached: {net_profit} >= {pos_threshold}")
            await self.send_alert(f"🎉 <b>ОТЛИЧНО!</b>\nДостигнут положительный порог профита: <b>{net_profit} USDT</b> (Порог: {pos_threshold})")
        elif self.pos_flag and net_profit <= 0:
            self.pos_flag = False
            logger.info(f"Positive profit lost: {net_profit} <= 0")
            await self.send_alert(f"📉 <b>Профит опустился ниже нуля.</b>\nNet Profit: <b>{net_profit} USDT</b>.\nФлаг положительного порога снят.")

    async def send_alert(self, text: str):
        if not TG_TOKEN or not TG_ALLOWED_USERS:
            return
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        
        async with aiohttp.ClientSession() as session:
            for uid in TG_ALLOWED_USERS:
                try:
                    payload = {"chat_id": uid, "text": text, "parse_mode": "HTML"}
                    async with session.post(url, json=payload, timeout=5) as response:
                        if response.status != 200:
                            err_text = await response.text()
                            logger.error(f"Failed to send TG alert to {uid}: {err_text}")
                except Exception as e:
                    logger.error(f"Error sending TG alert to {uid}: {e}")

    def reset_flags(self):
        self.neg_flag = False
        self.pos_flag = False
        logger.info("Notification flags manually reset.")
