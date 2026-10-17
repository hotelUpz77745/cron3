# CORE/STRATEGY/brownian_noise.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY.brownian_noise
ROLE: Инкапсуляция броуновского шума (Brownian Noise Jitter) между ордерами сильной руки
==============================================================================
"""

import asyncio
import logging
import secrets
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("BrownianNoise")


class BrownianNoiseController:
    """
    Инкапсулирует логику броуновского шума / джиттера (brownian_noise_factor)
    согласно паспорту стратегии.

    Правило паспорта:
    - Применяется ИСКЛЮЧИТЕЛЬНО между ордерами сильной руки (closed_count >= 1).
    - Первый сигнальный ордер (closed_count == 0) открывается строго без задержки.
    - Джиттер генерируется через secrets.SystemRandom() в диапазоне [low, high].
    - Во время ожидания проверяются прерывания (is_paused, entry_blocked, closing).
    - После окончания паузы обновляется актуальная рыночная цена.
    """

    @staticmethod
    def should_apply(closed_count: int, smart_grace_cfg: Optional[Dict[str, Any]]) -> bool:
        """
        Проверяет, должен ли применяться броуновский шум.
        Шум активен строго при наличии закрытых ордеров в окне (closed_count >= 1).
        """
        if closed_count < 1 or not isinstance(smart_grace_cfg, dict):
            return False
        bn_factor = (
            smart_grace_cfg.get("brownian_noise_factor")
            or smart_grace_cfg.get("white_noise_factor")
        )
        if isinstance(bn_factor, (list, tuple)) and len(bn_factor) == 2:
            try:
                low = float(min(bn_factor))
                high = float(max(bn_factor))
                return high > 0 and high >= low
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def get_jitter_range(smart_grace_cfg: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        """Извлекает и валидирует диапазон [low, high] из smart_grace_cfg."""
        bn_factor = (
            smart_grace_cfg.get("brownian_noise_factor")
            or smart_grace_cfg.get("white_noise_factor")
        )
        if isinstance(bn_factor, (list, tuple)) and len(bn_factor) == 2:
            low = float(min(bn_factor))
            high = float(max(bn_factor))
            if high > 0 and high >= low:
                return low, high
        return None

    @classmethod
    def generate_jitter_seconds(
        cls,
        low: float,
        high: float,
        rng: Optional[Any] = None,
    ) -> float:
        """
        Генерирует дробный джиттер броуновского шума через аппаратную энтропию secrets.
        """
        random_gen = rng if rng is not None else secrets.SystemRandom()
        return round(float(random_gen.uniform(low, high)), 2)

    @classmethod
    async def apply_noise_if_needed(
        cls,
        bot_core: Any,
        symbol: str,
        side: str,
        state: Any,
        smart_grace_cfg: Optional[Dict[str, Any]],
        closed_count: int,
        current_price: float,
        sleep_fn: Optional[Any] = None,
        rng: Optional[Any] = None,
    ) -> Optional[float]:
        """
        Исполняет паузу броуновского шума, если выполнены условия паспорта стратегии.
        Возвращает актуализированную цену current_price, либо None, если вход был прерван.
        """
        if not cls.should_apply(closed_count, smart_grace_cfg):
            return current_price

        jitter_range = cls.get_jitter_range(smart_grace_cfg)  # type: ignore[arg-type]
        if not jitter_range:
            return current_price

        low, high = jitter_range
        noise_sleep = cls.generate_jitter_seconds(low, high, rng=rng)

        logger.info(
            f"[{symbol}] {side} Brownian Noise Factor: sleeping {noise_sleep}s (range [{low}..{high}]s) "
            f"between strong-hand orders (closed orders in window: {closed_count})."
        )

        # Поддержка виртуального времени в бэктесте / симуляции
        if hasattr(bot_core, "current_sim_time") and bot_core.current_sim_time is not None:
            bot_core.current_sim_time += noise_sleep
            return current_price

        # Боевой асинхронный сон
        sleeper = sleep_fn if sleep_fn is not None else asyncio.sleep
        await sleeper(noise_sleep)

        # Проверка отмены входа во время ожидания
        sym_upper = symbol.upper()
        if (
            getattr(bot_core, "is_paused", False) is True
            or getattr(bot_core, "entry_blocked", False) is True
            or sym_upper in getattr(bot_core, "_closing_symbols", set())
            or getattr(state, "is_closing", False) is True
            or getattr(state, "in_position", False) is True
        ):
            logger.warning(f"[{symbol}] {side}: Entry aborted after brownian noise sleep.")
            if hasattr(state, "in_position_papper"):
                state.in_position_papper = False
            return None

        # Запрос свежей цены после сна
        try:
            from API.BINANCE.public import BinancePublic
            p_fresh = await BinancePublic.get_last_price(symbol)
            if p_fresh and p_fresh > 0:
                return float(p_fresh)
        except Exception as e:
            logger.warning(f"[{symbol}] Failed to fetch fresh price after noise sleep: {e}")

        return current_price
