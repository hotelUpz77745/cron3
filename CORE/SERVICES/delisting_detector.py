# ==============================================================================
# Path: CORE/SERVICES/delisting_detector.py
# Role: Защита: монитор делистинга Binance USDT-M инструментов активной обоймы.
#       Фаза 1 (до делистинга больше hard_close_sec) — Telegram-предупреждение.
#       Фаза 2 (до делистинга <= hard_close_sec, смена статуса биржи или
#       новостной сигнал) — жёсткая рыночная ликвидация, снятие лимиток и
#       удаление монеты из пула (app.json + рантайм-кэш + стримы + FSM).
#       Все параметры — только из CFG/app.json; единица времени группы —
#       секунды. В бэктесте (CRON3_BACKTEST=1) — полный no-op.
# ==============================================================================

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from typing import Any, Dict, Optional, Set

from c_log import UnifiedLogger
from consts import (
    DELISTING_ENABLED,
    DELISTING_POLL_INTERVAL_SEC,
    DELISTING_HARD_CLOSE_SEC,
    DELISTING_DELIVERY_HORIZON_SEC,
    DELISTING_MS_PER_SEC,
    DELISTING_SEC_PER_HOUR,
    DELISTING_NOTIFY_TELEGRAM,
    DELISTING_QUOTE_ASSET,
    DELISTING_EXCHANGE_ACTIVE_STATUSES,
    DELISTING_NEWS_CATALOG_ID,
    DELISTING_NEWS_PAGE_NO,
    DELISTING_NEWS_PAGE_SIZE,
    DELISTING_NEWS_HTTP_TIMEOUT_SEC,
    DELISTING_NEWS_KEYWORDS,
    DELISTING_NEWS_TICKER_REGEX,
    DELISTING_NEWS_TICKER_STOPWORDS,
    FLOAT_ZERO_EPSILON,
)
from API.BINANCE.public import BinancePublic

logger = UnifiedLogger("DelistingDetector")

# Регэксп тикеров компилируется один раз из конфига (Rule 3).
_NEWS_TICKER_RE = re.compile(DELISTING_NEWS_TICKER_REGEX)


def _is_backtest() -> bool:
    """True, если процесс запущен в режиме бэктеста (сеть отключена)."""
    return os.environ.get("CRON3_BACKTEST") == "1"


def _safe_int(value: Any) -> Optional[int]:
    """Безопасное приведение к int (для deliveryDate). None при невалидном значении."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt_eta(sec: Optional[float]) -> str:
    """Человекочитаемый остаток до делистинга (секунды -> часы)."""
    if sec is None:
        return "неизвестно (новостной сигнал)"
    return f"≈{max(sec, 0.0) / DELISTING_SEC_PER_HOUR:.2f}ч"


class DelistingDetector:
    """
    Монитор делистинга Binance USDT-M инструментов активной обоймы бота.

    Сигналы (первый сработавший побеждает):
      1. exchangeInfo: статус вне exchange_active_statuses -> немедленная Фаза 2.
      2. exchangeInfo: deliveryDate в пределах delivery_horizon_sec ->
         Фаза 2 если <= hard_close_sec, иначе Фаза 1 (предупреждение).
      3. Новостной CMS Binance -> немедленная Фаза 2.

    Работает только по монетам текущей обоймы (bot.symbols).
    """

    def __init__(self, bot_core: Any):
        self.bot = bot_core
        self.poll_interval_sec = DELISTING_POLL_INTERVAL_SEC
        self.hard_close_sec = DELISTING_HARD_CLOSE_SEC
        self.delivery_horizon_sec = DELISTING_DELIVERY_HORIZON_SEC
        self.notify_telegram = DELISTING_NOTIFY_TELEGRAM

        self._task: Optional[asyncio.Task] = None
        self._last_check_ts: Optional[float] = None
        self._cached: Dict[str, dict] = {}
        self._warned: Set[str] = set()
        self._closed: Set[str] = set()
        self._parsed_article_codes: Set[str] = set()
        self._news_delisted_symbols: Set[str] = set()

    # ============================ life-cycle ============================
    async def start(self) -> None:
        """Запускает фоновый монитор (no-op в бэктесте и при enabled=false)."""
        if not DELISTING_ENABLED or _is_backtest():
            logger.info("[DELISTING] Монитор делистингов отключён (disabled или бэктест).")
            return
        if self._task and not self._task.done():
            return
        logger.info(
            f"🛡️ [DELISTING] Запуск монитора делистингов Binance "
            f"(интервал {self.poll_interval_sec:.0f}s, порог {self.hard_close_sec:.0f}s)..."
        )
        self._task = asyncio.create_task(self._loop(), name="delisting_detector")

    async def stop(self) -> None:
        """Останавливает фоновый монитор."""
        if self._task and not self._task.done():
            logger.info("🛑 [DELISTING] Остановка монитора делистингов...")
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[DELISTING] Ошибка итерации монитора: {e}", exc_info=True)
            await asyncio.sleep(self.poll_interval_sec)

    # ============================ detection ============================
    async def get_delisted_symbols(self, force_refresh: bool = False) -> Dict[str, dict]:
        """Возвращает {symbol: info} по монетам обоймы с признаком делистинга (TTL-кэш)."""
        now = time.time()
        if (
            not force_refresh
            and self._cached
            and self._last_check_ts is not None
            and (now - self._last_check_ts) < self.poll_interval_sec
        ):
            return self._cached
        signals = await self._collect_signals()
        self._cached = signals
        self._last_check_ts = now
        return signals

    async def _collect_signals(self) -> Dict[str, dict]:
        """Опрашивает exchangeInfo и новостной CMS, фильтруя ТОЛЬКО монеты обоймы."""
        if _is_backtest():
            return {}
        ours = {str(s).upper() for s in (getattr(self.bot, "symbols", None) or [])}
        if not ours:
            return {}

        now_sec = time.time()
        signals: Dict[str, dict] = {}

        instruments = await BinancePublic.get_instruments()
        if isinstance(instruments, list):
            for item in instruments:
                if not isinstance(item, dict):
                    continue
                sym = str(item.get("symbol", "")).upper()
                if sym not in ours:
                    continue
                status = str(item.get("status", ""))
                if status and status not in DELISTING_EXCHANGE_ACTIVE_STATUSES:
                    signals[sym] = {"reason": "STATUS_CHANGE", "status": status, "delist_time_sec": now_sec}
                    continue
                delivery_ms = _safe_int(item.get("deliveryDate"))
                if delivery_ms is None:
                    continue
                delivery_sec = delivery_ms / DELISTING_MS_PER_SEC
                if now_sec < delivery_sec <= now_sec + self.delivery_horizon_sec:
                    signals[sym] = {
                        "reason": "DELIST_SCHEDULED",
                        "status": status,
                        "delist_time_sec": delivery_sec,
                    }

        # Новостной сигнал приоритетен (нет точной даты -> трактуем как немедленный).
        for sym in await self._fetch_news_delistings():
            if sym in ours:
                signals[sym] = {"reason": "NEWS_DELIST", "status": "", "delist_time_sec": now_sec}

        return signals

    async def _fetch_news_delistings(self) -> Set[str]:
        """Через API-адаптер читает новостной CMS Binance и извлекает тикеры-кандидаты (с кэшированием проверенных статей)."""
        if _is_backtest():
            return set()
        try:
            articles = await BinancePublic.get_cms_articles(
                DELISTING_NEWS_CATALOG_ID,
                DELISTING_NEWS_PAGE_NO,
                DELISTING_NEWS_PAGE_SIZE,
                DELISTING_NEWS_HTTP_TIMEOUT_SEC,
            )
            if not articles:
                return set(self._news_delisted_symbols)
            for art in articles:
                if not isinstance(art, dict):
                    continue
                code = art.get("code")
                if not code or code in self._parsed_article_codes:
                    continue
                cat_title = str(art.get("title") or "")
                if cat_title and not any(kw in cat_title.lower() for kw in DELISTING_NEWS_KEYWORDS):
                    self._parsed_article_codes.add(code)
                    continue
                payload = await BinancePublic.get_cms_article_detail(code, DELISTING_NEWS_HTTP_TIMEOUT_SEC)
                if not payload:
                    continue
                title = str(payload.get("title") or cat_title)
                body = str(payload.get("body") or "")
                self._parsed_article_codes.add(code)
                if not any(kw in title.lower() for kw in DELISTING_NEWS_KEYWORDS):
                    continue
                for token in _NEWS_TICKER_RE.findall(f"{title} {body}"):
                    if token in DELISTING_NEWS_TICKER_STOPWORDS:
                        continue
                    base = token[: -len(DELISTING_QUOTE_ASSET)] if token.endswith(DELISTING_QUOTE_ASSET) else token
                    self._news_delisted_symbols.add(f"{base}{DELISTING_QUOTE_ASSET}")
        except Exception as e:
            logger.warning(f"[DELISTING] Ошибка разбора новостей Binance: {e}")
        return set(self._news_delisted_symbols)

    # ============================ reaction ============================
    async def _check_once(self) -> None:
        """Одна итерация: собирает сигналы и применяет Фазу 1 или Фазу 2."""
        signals = await self.get_delisted_symbols(force_refresh=True)
        if not signals:
            return
        now_sec = time.time()
        for sym, info in signals.items():
            reason = str(info.get("reason", ""))
            if reason == "NEWS_DELIST":
                await self._escalate(sym, info, time_left_sec=None)
                continue
            delist_sec = info.get("delist_time_sec")
            time_left_sec = (delist_sec if delist_sec is not None else now_sec) - now_sec
            if reason == "STATUS_CHANGE" or time_left_sec <= self.hard_close_sec:
                await self._escalate(sym, info, time_left_sec=time_left_sec)
            else:
                await self._warn(sym, info, time_left_sec=time_left_sec)

    async def _escalate(self, sym: str, info: dict, time_left_sec: Optional[float]) -> None:
        """Фаза 2: жёсткое закрытие (однократно на монету)."""
        sym = sym.upper()
        if sym in self._closed:
            return
        self._closed.add(sym)
        self._warned.discard(sym)
        await self._hard_close(sym, info, time_left_sec)

    async def _warn(self, sym: str, info: dict, time_left_sec: float) -> None:
        """Фаза 1: Telegram-предупреждение (однократно на монету)."""
        sym = sym.upper()
        if sym in self._warned or sym in self._closed:
            return
        self._warned.add(sym)
        logger.warning(
            f"⚠️ [DELISTING][{sym}] Признак делистинга ({info.get('reason', '')}, "
            f"status={info.get('status', '')}), осталось {_fmt_eta(time_left_sec)} "
            f"(порог {_fmt_eta(self.hard_close_sec)})."
        )
        if self.notify_telegram:
            await self._notify(self._build_warn_msg(sym, info, time_left_sec))

    async def _hard_close(self, sym: str, info: dict, time_left_sec: Optional[float]) -> None:
        """Фаза 2: рыночная ликвидация, снятие лимиток и удаление монеты из пула."""
        sym = sym.upper()
        reason = str(info.get("reason", ""))
        logger.critical(
            f"🛑 [DELISTING][{sym}] ЖЁСТКОЕ ЗАКРЫТИЕ и вывод из пула! "
            f"Причина: {reason} (status={info.get('status', '')}), осталось: {_fmt_eta(time_left_sec)}."
        )
        client = self._client_for(sym)
        closing_set = getattr(self.bot, "_closing_symbols", None)
        if isinstance(closing_set, set):
            closing_set.add(sym)
        states = getattr(self.bot, "fsm_states", None)
        if isinstance(states, dict) and sym in states and isinstance(states[sym], dict):
            for s_side in ("LONG", "SHORT"):
                st = states[sym].get(s_side)
                if st is not None:
                    st.is_closing = True

        try:
            # 1. Отмена всех отложенных лимитных ордеров
            try:
                async with self._symbol_lock(sym):
                    await client.cancel_all_orders(sym)
                logger.info(f"[DELISTING][{sym}] Все лимитные ордера сняты.")
            except Exception as e:
                logger.error(f"[DELISTING][{sym}] Ошибка отмены ордеров: {e}")

            # 2. Мгновенная рыночная ликвидация открытых позиций
            try:
                positions = await client.fetch_positions()
                for p in positions or []:
                    if not isinstance(p, dict):
                        continue
                    if (p.get("symbol") or "").upper() != sym:
                        continue
                    amt_raw = p.get("positionAmt")
                    if amt_raw is None:
                        continue
                    amt = float(amt_raw)
                    if abs(amt) <= FLOAT_ZERO_EPSILON:
                        continue
                    pos_side = p.get("positionSide")
                    order_side = "SELL" if pos_side == "LONG" else "BUY"
                    logger.critical(f"[DELISTING][{sym}] Рыночное закрытие {pos_side} позиции: Qty {abs(amt)}")
                    async with self._symbol_lock(sym):
                        await client.make_order(
                            symbol=sym,
                            qty=abs(amt),
                            side=order_side,
                            position_side=pos_side,
                            market_type="MARKET",
                        )
            except Exception as e:
                logger.error(f"[DELISTING][{sym}] Ошибка ликвидации позиции: {e}")

            # 3. Сброс FSM/AvgManager (снимает блокировку delete_symbol)
            self._reset_fsm(sym)

            # 4. Полное удаление монеты из пула (app.json + дисковый кэш + стримы + FSM)
            try:
                deleted = await self.bot.delete_symbol(sym)
                logger.info(f"[DELISTING][{sym}] Символ выведен из пула бота: {deleted}")
            except Exception as e:
                logger.error(f"[DELISTING][{sym}] Ошибка удаления из пула: {e}")
        finally:
            if isinstance(closing_set, set):
                closing_set.discard(sym)

        # 5. Telegram-уведомление оператору
        if self.notify_telegram:
            await self._notify(self._build_hard_msg(sym, info, time_left_sec))

    # ============================ internal helpers ============================
    def _client_for(self, sym: str) -> Any:
        if hasattr(self.bot, "get_client_for_symbol"):
            return self.bot.get_client_for_symbol(sym)
        return getattr(self.bot, "client", None)

    def _symbol_lock(self, sym: str) -> asyncio.Lock:
        if hasattr(self.bot, "_get_symbol_lock"):
            return self.bot._get_symbol_lock(sym)
        return asyncio.Lock()

    def _reset_fsm(self, sym: str) -> None:
        states = getattr(self.bot, "fsm_states", None)
        rm = getattr(self.bot, "runtime_manager", None)
        if not isinstance(states, dict) or sym not in states or rm is None:
            logger.warning(f"[DELISTING][{sym}] FSM не сброшен (runtime_manager/fsm_states недоступны).")
            return
        avg = getattr(self.bot, "avg_manager", None)
        for s_side in ("LONG", "SHORT"):
            st = states[sym].get(s_side) if isinstance(states[sym], dict) else None
            if st is None:
                continue
            rm.reset_state_to_default(sym, s_side, st)
            if avg:
                avg.reset(sym, s_side)

    async def _notify(self, text: str) -> None:
        try:
            notifier = getattr(self.bot, "notifier", None)
            if notifier and hasattr(notifier, "send_alert"):
                await notifier.send_alert(text)
        except Exception as e:
            logger.error(f"[DELISTING] Ошибка отправки Telegram-алерта: {e}")

    def _build_warn_msg(self, sym: str, info: dict, time_left_sec: float) -> str:
        return (
            f"⚠️ <b>[DELISTING WARNING] Монета {sym} готовится к делистингу!</b>\n\n"
            f"• Причина: <b>{info.get('reason', '')}</b>\n"
            f"• Статус биржи: <code>{info.get('status', '')}</code>\n"
            f"• До делистинга: <b>{_fmt_eta(time_left_sec)}</b> (порог жёсткого закрытия: {_fmt_eta(self.hard_close_sec)})\n"
            f"• Действие: <b>предупреждение</b>. При достижении порога монета будет закрыта по рынку и выведена из пула."
        )

    def _build_hard_msg(self, sym: str, info: dict, time_left_sec: Optional[float]) -> str:
        return (
            f"🚨 <b>[DELISTING → HARD CLOSE] Монета {sym} ликвидирована и удалена из пула!</b>\n\n"
            f"• Причина: <b>{info.get('reason', '')}</b>\n"
            f"• Статус биржи: <code>{info.get('status', '')}</code>\n"
            f"• До делистинга: <b>{_fmt_eta(time_left_sec)}</b>\n"
            f"• Исполнение: <b>рыночная ликвидация MARKET</b>, все лимитки сняты.\n"
            f"• Пул: <b>символ удалён</b> из app.json, кэш очищен с диска, квота освобождена."
        )
