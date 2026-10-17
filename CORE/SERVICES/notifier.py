# CORE/SERVICES/notifier.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.notifier
ROLE: Сервис Telegram-уведомлений по просадке и профициту портфеля и куполов
==============================================================================
"""

import asyncio
import os
import aiohttp
from consts import DATA_DIR, ANALYTICS_DIR
from c_utils import Utils
from c_log import UnifiedLogger
from consts import TG_ALLOWED_USERS

logger = UnifiedLogger("Notifier")


class NotifierManager:
    """
    Менеджер фоновых Telegram-уведомлений при пересечении порогов просадки / профицита
    как на уровне всего портфеля, так и в разрезе изолированных куполов стратегий.
    """

    def __init__(self, bot_core=None):
        self.bot_core = bot_core
        self.is_running = False
        self.neg_flag = False
        self.pos_flag = False
        self._task = None
        self.last_neg_threshold = None
        self.last_pos_threshold = None
        self.strat_flags = {}

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
        if not app_cfg:
            return

        # 1. Уведомления по куполам стратегий (PER-STRATEGY DOME)
        active_strats = {}
        if hasattr(self, "bot_core") and getattr(self.bot_core, "strategy_registry", None):
            active_strats = self.bot_core.strategy_registry.active_strategies

        for strat_id, strat_cfg in active_strats.items():
            strat_app_cfg = app_cfg.get("active_strategies", {}).get(strat_id, {})
            strat_notif_cfg = strat_app_cfg.get("notifications") or strat_cfg.notifications_cfg
            if not strat_notif_cfg or not strat_notif_cfg.get("enabled", True):
                continue

            neg_threshold = float(strat_notif_cfg["negative_threshold"])
            pos_threshold = float(strat_notif_cfg["positive_threshold"])

            if strat_id not in self.strat_flags:
                self.strat_flags[strat_id] = {
                    "neg_flag": False,
                    "pos_flag": False,
                    "last_neg_th": neg_threshold,
                    "last_pos_th": pos_threshold,
                }
            sflags = self.strat_flags[strat_id]
            if sflags["last_neg_th"] != neg_threshold:
                sflags["neg_flag"] = False
                sflags["last_neg_th"] = neg_threshold
            if sflags["last_pos_th"] != pos_threshold:
                sflags["pos_flag"] = False
                sflags["last_pos_th"] = pos_threshold

            strat_stats_path = ANALYTICS_DIR / "STATS" / f"analytics_{strat_id}.json"
            if not strat_stats_path.exists():
                continue
            strat_data = Utils.read_json_file(strat_stats_path) or {}
            strat_net = float(strat_data.get("net_profit_usdt", 0.0))

            # Логика NEGATIVE для купола
            if strat_net <= neg_threshold and not sflags["neg_flag"]:
                sflags["neg_flag"] = True
                msg = (
                    f"⚠️ <b>ВНИМАНИЕ! ПРОСАДКА КУПОЛА [{strat_cfg.name}]</b>\n"
                    f"Профит стратегии опустился до: <b>{strat_net} USDT</b>\n"
                    f"Порог: {neg_threshold} USDT (Монеты: {strat_cfg.symbols})"
                )
                logger.info(f"[NOTIFIER][{strat_id}] Negative threshold reached: {strat_net} <= {neg_threshold}")
                await self.send_alert(msg)
            elif sflags["neg_flag"] and strat_net >= 0:
                sflags["neg_flag"] = False
                msg = (
                    f"✅ <b>ПРОСАДКА КУПОЛА ВОССТАНОВЛЕНА [{strat_cfg.name}]</b>\n"
                    f"Текущий профит стратегии: <b>{strat_net} USDT</b>\n"
                    f"Флаг тревоги купола снят."
                )
                logger.info(f"[NOTIFIER][{strat_id}] Drawdown recovered: {strat_net} >= 0")
                await self.send_alert(msg)

            # Логика POSITIVE для купола
            if strat_net >= pos_threshold and not sflags["pos_flag"]:
                sflags["pos_flag"] = True
                msg = (
                    f"🎉 <b>ПРОФИЦИТ КУПОЛА [{strat_cfg.name}]!</b>\n"
                    f"Профит стратегии: <b>{strat_net} USDT</b>\n"
                    f"Порог: {pos_threshold} USDT (Монеты: {strat_cfg.symbols})"
                )
                logger.info(f"[NOTIFIER][{strat_id}] Positive threshold reached: {strat_net} >= {pos_threshold}")
                await self.send_alert(msg)
            elif sflags["pos_flag"] and strat_net <= 0:
                sflags["pos_flag"] = False
                msg = (
                    f"📉 <b>ПРОФИЦИТ КУПОЛА ОПУСТИЛСЯ НИЖЕ НУЛЯ [{strat_cfg.name}]</b>\n"
                    f"Текущий профит: <b>{strat_net} USDT</b>\n"
                    f"Флаг позитивного порога купола снят."
                )
                logger.info(f"[NOTIFIER][{strat_id}] Positive profit lost: {strat_net} <= 0")
                await self.send_alert(msg)

        # 2. Глобальные уведомления портфеля (если заданы)
        notif_cfg = app_cfg.get("notifications")
        if not notif_cfg or not notif_cfg.get("enabled", False):
            return

        neg_threshold = float(notif_cfg["negative_threshold"])
        pos_threshold = float(notif_cfg["positive_threshold"])

        if self.last_neg_threshold is not None and self.last_neg_threshold != neg_threshold:
            self.neg_flag = False
        if self.last_pos_threshold is not None and self.last_pos_threshold != pos_threshold:
            self.pos_flag = False

        self.last_neg_threshold = neg_threshold
        self.last_pos_threshold = pos_threshold

        analytics_data = Utils.read_json_file(ANALYTICS_DIR / "analytics.json")
        if not analytics_data:
            return
        net_profit = float(analytics_data.get("net_profit_usdt", 0.0))

        if net_profit <= neg_threshold and not self.neg_flag:
            self.neg_flag = True
            msg = f"⚠️ <b>ВНИМАНИЕ! ПРОСАДКА ПОРТФЕЛЯ!</b>\nОбщий профит опустился до: <b>{net_profit} USDT</b>\nПорог: {neg_threshold} USDT"
            await self.send_alert(msg)
        elif self.neg_flag and net_profit >= 0:
            self.neg_flag = False
            msg = f"✅ <b>ПРОСАДКА ПОРТФЕЛЯ ВОССТАНОВЛЕНА.</b>\nТекущий профит: <b>{net_profit} USDT</b>"
            await self.send_alert(msg)

        if net_profit >= pos_threshold and not self.pos_flag:
            self.pos_flag = True
            msg = f"🎉 <b>ПРОФИЦИТ ПОРТФЕЛЯ!</b>\nОбщий профит: <b>{net_profit} USDT</b>\nПорог: {pos_threshold} USDT"
            await self.send_alert(msg)
        elif self.pos_flag and net_profit <= 0:
            self.pos_flag = False
            msg = f"📉 <b>ПРОФИЦИТ ПОРТФЕЛЯ НИЖЕ НУЛЯ.</b>\nОбщий профит: <b>{net_profit} USDT</b>"
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

    async def send_alert_with_keyboard(self, text: str, reply_markup: dict):
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
                    payload = {"chat_id": uid, "text": text, "parse_mode": "HTML", "reply_markup": reply_markup}
                    async with session.post(url, json=payload, timeout=5) as response:
                        if response.status != 200:
                            err_text = await response.text()
                            logger.error(f"[NOTIFIER] Failed to send TG alert with keyboard to {uid}: {err_text}")
                except Exception as e:
                    logger.error(f"[NOTIFIER] Error sending TG alert with keyboard to {uid}: {e}")

    def reset_flags(self):
        self.neg_flag = False
        self.pos_flag = False
        for sflags in self.strat_flags.values():
            sflags["neg_flag"] = False
            sflags["pos_flag"] = False
        logger.info("[NOTIFIER] Notification flags (portfolio and strategy domes) manually reset.")


# Алиас для единообразия сервисов
Notifier = NotifierManager
