# ANALYTICS/circuit_breaker.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.circuit_breaker
ROLE: Binance API IP ban tracker & circuit breaker (OOP)
==============================================================================
"""

import asyncio
import re
import time as _time_module
from datetime import datetime, timezone
from typing import Optional

from c_log import UnifiedLogger

logger = UnifiedLogger("CircuitBreaker")

_ban_until_ms: int = 0
_BAN_PATTERN = re.compile(r"banned until (\d{10,13})")


class CircuitBreaker:
    """
    Класс защиты от банов Binance API (Circuit Breaker).
    Отслеживает временные баны IP по сообщениям об ошибках,
    сохраняет срок действия блокировки и приостанавливает выполнение
    запросов до снятия бана.
    """

    BAN_PATTERN = re.compile(r"banned until (\d{10,13})")
    _default_instance: Optional["CircuitBreaker"] = None

    def __init__(self, name: str = "CircuitBreaker", sync_global: bool = True):
        self.logger = UnifiedLogger(name)
        self._ban_until_ms: int = 0
        self._lock = asyncio.Lock()
        self._sync_global = sync_global

    @classmethod
    def get_default(cls) -> "CircuitBreaker":
        """Возвращает синглтон CircuitBreaker по умолчанию."""
        if cls._default_instance is None:
            cls._default_instance = cls(sync_global=True)
        return cls._default_instance

    @property
    def ban_until_ms(self) -> int:
        if self._sync_global:
            global _ban_until_ms
            if _ban_until_ms == 0:
                self._ban_until_ms = 0
                return 0
            return max(self._ban_until_ms, _ban_until_ms)
        return self._ban_until_ms

    @ban_until_ms.setter
    def ban_until_ms(self, val: int) -> None:
        self._ban_until_ms = val
        if self._sync_global:
            global _ban_until_ms
            _ban_until_ms = val

    def update_ban_from_error(self, error_msg: str) -> None:
        """
        Парсит timestamp бана из сообщения об ошибке Binance API.
        Если обнаружен бан, устанавливает ban_until_ms.
        """
        if not error_msg:
            return
        m = self.BAN_PATTERN.search(str(error_msg))
        if m:
            ts = int(m.group(1))
            if ts < 1_000_000_000_000:
                ts *= 1000
            if ts > self.ban_until_ms:
                self.ban_until_ms = ts
                ban_dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%H:%M:%S UTC")
                self.logger.warning(
                    f"[CIRCUIT_BREAKER] IP ban registered. Sleeping until {ban_dt} before next analytics REST call."
                )

    async def wait_ban_lifted(self) -> bool:
        """
        Если IP в данный момент забанен, засыпает до истечения бана (+ 5 сек буфер).
        Возвращает True, если ожидание состоялось.
        """
        now_ms = int(_time_module.time() * 1000)
        if self.ban_until_ms <= now_ms:
            return False

        async with self._lock:
            now_ms = int(_time_module.time() * 1000)
            if self.ban_until_ms <= now_ms:
                return False
            sleep_sec = (self.ban_until_ms - now_ms) / 1000.0 + 5.0
            self.logger.warning(
                f"[CIRCUIT_BREAKER] IP still banned. Waiting {sleep_sec:.0f}s before retrying..."
            )
            await asyncio.sleep(sleep_sec)
            self.ban_until_ms = 0
            return True

    def is_banned(self) -> bool:
        """Проверяет, активен ли бан IP в текущий момент."""
        now_ms = int(_time_module.time() * 1000)
        return self.ban_until_ms > now_ms

    def reset(self) -> None:
        """Сброс состояния бана."""
        self.ban_until_ms = 0


# Глобальный инстанс по умолчанию и фасады обратной совместимости
default_circuit_breaker = CircuitBreaker.get_default()


def update_ban_from_error(error_msg: str) -> None:
    """Парсит timestamp бана из сообщения об ошибке и обновляет трекер."""
    default_circuit_breaker.update_ban_from_error(error_msg)


async def wait_ban_lifted() -> bool:
    """Если IP забанен, засыпает до окончания бана."""
    return await default_circuit_breaker.wait_ban_lifted()


def is_banned() -> bool:
    """Проверяет, активен ли бан IP."""
    return default_circuit_breaker.is_banned()


# Backward compatibility aliases
_update_ban_from_error = update_ban_from_error
_wait_ban_lifted = wait_ban_lifted
