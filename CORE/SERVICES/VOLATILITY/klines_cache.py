# CORE/SERVICES/VOLATILITY/klines_cache.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.VOLATILITY.klines_cache
ROLE: Высокопроизводительный потокобезопасный кэш свечей (klines) с настраиваемым TTL
==============================================================================
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from c_log import UnifiedLogger
from consts import DATA_DIR
from c_utils import Utils

logger = UnifiedLogger("KlinesCache")


class KlinesCache:
    """
    In-memory кэш биржевых свечей для устранения избыточных сетевых запросов.
    Ключ кэша: (symbol.upper(), interval, limit).
    TTL по умолчанию: 300 секунд (5 минут), настраивается в app.json (klines_cache_ttl_sec).
    """

    _cache: Dict[Tuple[str, str, int], Tuple[float, List[Dict[str, Any]]]] = {}
    _default_ttl_sec: float = 300.0

    @classmethod
    def get_configured_ttl(cls) -> float:
        """Читает актуальный TTL кэша из app.json (секция volatility_scanner)."""
        app_json = DATA_DIR / "app.json"
        if app_json.exists():
            try:
                data = Utils.read_json_file(app_json)
                if data and "volatility_scanner" in data:
                    vs_cfg = data["volatility_scanner"]
                    if "klines_cache_ttl_sec" in vs_cfg:
                        val = float(vs_cfg["klines_cache_ttl_sec"])
                        if val > 0:
                            return val
            except Exception:
                pass
        return cls._default_ttl_sec

    @classmethod
    def get(
        cls,
        symbol: str,
        interval: str,
        limit: int,
        ttl_sec: Optional[float] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """Возвращает свечи из кэша, если срок жизни TTL не истек."""
        if not symbol:
            return None
        key = (symbol.upper().strip(), str(interval).strip(), int(limit))
        entry = cls._cache.get(key)
        if not entry:
            return None

        ts, data = entry
        effective_ttl = ttl_sec if ttl_sec is not None else cls.get_configured_ttl()
        if (time.time() - ts) > effective_ttl:
            # Кэш устарел
            cls._cache.pop(key, None)
            return None

        return data

    @classmethod
    def set(
        cls,
        symbol: str,
        interval: str,
        limit: int,
        klines: List[Dict[str, Any]]
    ) -> None:
        """Сохраняет свечи в кэш с текущей временной меткой."""
        if not symbol or not klines:
            return
        key = (symbol.upper().strip(), str(interval).strip(), int(limit))
        cls._cache[key] = (time.time(), klines)

    @classmethod
    async def get_or_fetch(
        cls,
        symbol: str,
        interval: str,
        limit: int,
        fetcher_coro_fn: Callable[[], Any],
        ttl_sec: Optional[float] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Пытается получить свечи из кэша. Если нет или протухли — вызывает асинхронный fetcher
        и атомарно обновляет кэш.
        """
        cached = cls.get(symbol, interval, limit, ttl_sec=ttl_sec)
        if cached is not None:
            return cached

        try:
            klines = await fetcher_coro_fn()
            if klines and isinstance(klines, list):
                cls.set(symbol, interval, limit, klines)
                return klines
            return None
        except Exception as e:
            logger.warning(f"[KlinesCache] Ошибка при fetcher для {symbol} {interval}: {e}")
            return None

    @classmethod
    def clear(cls) -> None:
        """Полная очистка кэша свечей (для тестов или сброса сессий)."""
        cls._cache.clear()

    @classmethod
    def size(cls) -> int:
        """Количество закэшированных серий свечей."""
        return len(cls._cache)
