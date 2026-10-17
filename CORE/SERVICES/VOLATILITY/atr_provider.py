# CORE/SERVICES/VOLATILITY/atr_provider.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.VOLATILITY.atr_provider
ROLE: Сервис асинхронного расчета и кеширования Average True Range (ATR%)
==============================================================================
"""

import time
import asyncio
from typing import Dict, Tuple, Optional
from c_log import UnifiedLogger
from API.BINANCE.public import BinancePublic

logger = UnifiedLogger("ATRProvider")


class ATRProvider:
    """
    Сервис асинхронного расчета и кеширования волатильности (ATR%) по символам.
    Параметры timeframe, period и cache_ttl_sec передаются напрямую из конфигурации стратегии.
    """

    # Кеш структуры: (symbol, timeframe, period) -> (atr_pct: float, cached_at: float)
    _cache: Dict[Tuple[str, str, int], Tuple[float, float]] = {}
    _lock = asyncio.Lock()

    @classmethod
    def clear_cache(cls) -> None:
        """Очистка внутреннего кеша (для тестов и смены сессий)."""
        cls._cache.clear()

    @classmethod
    async def get_atr_pct(
        cls,
        client: Optional[object],
        symbol: str,
        timeframe: str,
        period: int,
        cache_ttl_sec: int,
        fallback_atr_pct: float,
    ) -> float:
        """
        Возвращает процентный ATR: (ATR_value / current_price) * 100.
        Если в кеше есть свежее значение (< cache_ttl_sec), возвращает его O(1).
        В случае недоступности свежих свечей возвращает строго fallback_atr_pct из конфига стратегии.
        """
        cache_key = (symbol, timeframe, period)
        now = time.time()

        cached = cls._cache.get(cache_key)
        if cached is not None:
            val, ts = cached
            if now - ts < cache_ttl_sec:
                return val

        async with cls._lock:
            # Двойная проверка под локом
            cached = cls._cache.get(cache_key)
            if cached is not None:
                val, ts = cached
                if now - ts < cache_ttl_sec:
                    return val

            try:
                limit = period + 2
                klines = await BinancePublic.get_klines(symbol, timeframe, limit)
                if not klines and client and hasattr(client, "get_klines"):
                    klines = await client.get_klines(symbol, timeframe, limit)

                if not klines or len(klines) < period:
                    logger.debug(
                        f"[ATRProvider] [{symbol}] Not enough klines ({len(klines) if klines else 0}/{period}), "
                        f"using fallback {fallback_atr_pct:.2f}%"
                    )
                    return fallback_atr_pct

                trs = []
                for i in range(1, len(klines)):
                    h = float(klines[i].get("high", 0.0))
                    l = float(klines[i].get("low", 0.0))
                    prev_c = float(klines[i - 1].get("close", 0.0))
                    if h <= 0 or l <= 0 or prev_c <= 0:
                        continue
                    tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                    trs.append(tr)

                if len(trs) < period:
                    return fallback_atr_pct

                target_trs = trs[-period:]
                atr_val = sum(target_trs) / len(target_trs)
                cur_price = float(klines[-1].get("close", 0.0))
                if cur_price <= 0:
                    return fallback_atr_pct

                atr_pct = round((atr_val / cur_price) * 100.0, 4)
                cls._cache[cache_key] = (atr_pct, now)
                return atr_pct

            except Exception as e:
                logger.warning(
                    f"[ATRProvider] [{symbol}] Error computing ATR ({e}), fallback to {fallback_atr_pct:.2f}%"
                )
                return fallback_atr_pct
