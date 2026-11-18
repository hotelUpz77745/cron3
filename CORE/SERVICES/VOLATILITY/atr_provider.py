# CORE/SERVICES/VOLATILITY/atr_provider.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.VOLATILITY.atr_provider
ROLE: Сервис асинхронного расчета и кеширования Average True Range (ATR%)
==============================================================================
"""

import os
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
        sim_ts = getattr(client, "current_sim_time", None) if client is not None else None
        is_sim = bool(os.environ.get("CRON3_BACKTEST") == "1" or isinstance(sim_ts, (int, float)))
        now = (float(sim_ts) / 1000.0) if isinstance(sim_ts, (int, float)) and sim_ts > 1e9 else time.time()
        cache_key = (symbol, timeframe, period)

        if client and hasattr(client, "get_precomputed_atr"):
            try:
                pre_atr = client.get_precomputed_atr(symbol, timeframe, period)
                if pre_atr is not None and pre_atr > 0:
                    return float(pre_atr)
            except Exception:
                pass

        cached = None if is_sim else cls._cache.get(cache_key)
        if cached is not None:
            val, ts = cached
            if now - ts < cache_ttl_sec:
                return val

        async with cls._lock:
            # Двойная проверка под локом
            cached = None if is_sim else cls._cache.get(cache_key)
            if cached is not None:
                val, ts = cached
                if now - ts < cache_ttl_sec:
                    return val

            try:
                limit = period + 2
                klines = None
                if is_sim and client and hasattr(client, "get_klines"):
                    klines = await client.get_klines(symbol, timeframe, limit)
                else:
                    klines = await BinancePublic.get_klines(symbol, timeframe, limit)
                    if not klines and client and hasattr(client, "get_klines"):
                        klines = await client.get_klines(symbol, timeframe, limit)

                if not klines or len(klines) < period:
                    logger.debug(
                        f"[ATRProvider] [{symbol}] Not enough klines ({len(klines) if klines else 0}/{period}), "
                        f"using fallback {fallback_atr_pct:.2f}%"
                    )
                    return fallback_atr_pct

                highs = [float(k.get("high", 0.0)) for k in klines]
                lows = [float(k.get("low", 0.0)) for k in klines]
                closes = [float(k.get("close", 0.0)) for k in klines]
                if not any(h > 0.0 for h in highs) or not any(l > 0.0 for l in lows):
                    logger.debug(
                        f"[ATRProvider] [{symbol}] Close-only klines without OHLC high/low, "
                        f"using fallback {fallback_atr_pct:.2f}%"
                    )
                    return fallback_atr_pct

                from CORE.STRATEGY.CPP.native_indicators import NATIVE_INDICATORS
                raw_atr_pct = NATIVE_INDICATORS.atr_pct_sma(highs, lows, closes, period)
                if raw_atr_pct is None:
                    return fallback_atr_pct

                atr_pct = round(float(raw_atr_pct), 4)
                if not is_sim:
                    cls._cache[cache_key] = (atr_pct, now)
                return atr_pct

            except Exception as e:
                logger.warning(
                    f"[ATRProvider] [{symbol}] Error computing ATR ({e}), fallback to {fallback_atr_pct:.2f}%"
                )
                return fallback_atr_pct
