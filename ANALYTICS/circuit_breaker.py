# ANALYTICS/circuit_breaker.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.circuit_breaker
ROLE: Binance API IP ban tracker & circuit breaker
==============================================================================
"""

import asyncio
import re
import time as _time_module
from datetime import datetime, timezone

from c_log import UnifiedLogger

logger = UnifiedLogger("CircuitBreaker")

_ban_until_ms: int = 0
_BAN_PATTERN = re.compile(r"banned until (\d{10,13})")


def update_ban_from_error(error_msg: str) -> None:
    """Parse ban timestamp from Binance error message and update global ban tracker."""
    global _ban_until_ms
    if not error_msg:
        return
    m = _BAN_PATTERN.search(str(error_msg))
    if m:
        ts = int(m.group(1))
        if ts < 1_000_000_000_000:
            ts *= 1000
        if ts > _ban_until_ms:
            _ban_until_ms = ts
            ban_dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%H:%M:%S UTC")
            logger.warning(f"[CIRCUIT_BREAKER] IP ban registered. Sleeping until {ban_dt} before next analytics REST call.")


async def wait_ban_lifted() -> bool:
    """If IP is currently banned, sleep until ban expires. Returns True if had to wait."""
    global _ban_until_ms
    now_ms = int(_time_module.time() * 1000)
    if _ban_until_ms <= now_ms:
        return False
    sleep_sec = (_ban_until_ms - now_ms) / 1000.0 + 5.0
    logger.warning(f"[CIRCUIT_BREAKER] IP still banned. Waiting {sleep_sec:.0f}s before retrying...")
    await asyncio.sleep(sleep_sec)
    _ban_until_ms = 0
    return True


def is_banned() -> bool:
    """Check if IP is currently registered as banned."""
    now_ms = int(_time_module.time() * 1000)
    return _ban_until_ms > now_ms


# Backward compatibility aliases
_update_ban_from_error = update_ban_from_error
_wait_ban_lifted = wait_ban_lifted
