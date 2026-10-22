# CORE/STRATEGY/brownian_noise.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY.brownian_noise
ROLE: Инкапсуляция броуновского шума (Brownian Noise Jitter) между ордерами сильной руки
==============================================================================
"""

import asyncio
import secrets
from typing import Any, Dict, Optional, Tuple

from c_log import UnifiedLogger

logger = UnifiedLogger("BrownianNoise")


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

    def __init__(
        self,
        smart_grace_cfg: Optional[Dict[str, Any]] = None,
        jitter_range: Optional[Tuple[float, float]] = None,
    ):
        """
        Инициализирует контроллер броуновского шума.
        Прямое чтение и валидация конфигурации подняты сюда по правилу «Работай или упади сразу» (Fail-Fast).
        Любая некорректная конфигурация броуновского шума вызывает исключение на старте программы.
        """
        self.enabled: bool = False
        self.low: float = 0.0
        self.high: float = 0.0

        if jitter_range is not None:
            if not (isinstance(jitter_range, (list, tuple)) and len(jitter_range) == 2):
                raise ValueError(f"[BrownianNoiseController] jitter_range must be a 2-element tuple/list, got {jitter_range}")
            low = float(min(jitter_range))
            high = float(max(jitter_range))
            if low < 0 or high < low:
                raise ValueError(f"[BrownianNoiseController] Invalid jitter range: [{low}..{high}]")
            self.low = low
            self.high = high
            self.enabled = self.high > 0
        elif smart_grace_cfg is not None:
            if not isinstance(smart_grace_cfg, dict):
                raise TypeError(f"[BrownianNoiseController] smart_grace_cfg must be a dict, got {type(smart_grace_cfg)}")
            if "brownian_noise_factor" in smart_grace_cfg:
                bn_factor = smart_grace_cfg["brownian_noise_factor"]
                if bn_factor is not None:
                    if not (isinstance(bn_factor, (list, tuple)) and len(bn_factor) == 2):
                        raise ValueError(f"[BrownianNoiseController] 'brownian_noise_factor' must be a 2-element [low, high] list, got {bn_factor}")
                    low = float(min(bn_factor))
                    high = float(max(bn_factor))
                    if low < 0 or high < low:
                        raise ValueError(f"[BrownianNoiseController] Invalid jitter range: [{low}..{high}]")
                    self.low = low
                    self.high = high
                    self.enabled = self.high > 0

    def should_apply(
        self,
        closed_count: int,
        smart_grace_cfg: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Проверяет, должен ли применяться броуновский шум.
        Шум активен строго при наличии закрытых ордеров в окне (closed_count >= 1)
        и включенном диапазоне джиттера (high > 0).
        Конфигурация уже прочитана и провалидирована в __init__.
        """
        # Если передан внешний конфиг для вызова без инстанса, поднимаем валидацию через конструктор
        if smart_grace_cfg is not None:
            ctrl = self.__class__(smart_grace_cfg=smart_grace_cfg)
            return ctrl.should_apply(closed_count)
        return self.enabled and closed_count >= 1

    def get_jitter_range(
        self,
        smart_grace_cfg: Optional[Dict[str, Any]] = None,
    ) -> Optional[Tuple[float, float]]:
        """
        Возвращает диапазон [low, high], провалидированный в __init__.
        """
        if smart_grace_cfg is not None:
            ctrl = self.__class__(smart_grace_cfg=smart_grace_cfg)
            return ctrl.get_jitter_range()
        return (self.low, self.high) if self.enabled else None

    def generate_jitter_seconds(
        self,
        low: Optional[float] = None,
        high: Optional[float] = None,
        rng: Optional[Any] = None,
    ) -> float:
        """
        Генерирует дробный джиттер броуновского шума через аппаратную энтропию secrets.
        """
        l_val = self.low if low is None else float(low)
        h_val = self.high if high is None else float(high)
        random_gen = rng if rng is not None else secrets.SystemRandom()
        return round(float(random_gen.uniform(l_val, h_val)), 2)

    async def apply_noise_if_needed(
        self,
        bot_core: Any,
        symbol: str,
        side: str,
        state: Any,
        closed_count: int,
        current_price: float,
        smart_grace_cfg: Optional[Dict[str, Any]] = None,
        sleep_fn: Optional[Any] = None,
        rng: Optional[Any] = None,
    ) -> Optional[float]:
        """
        Исполняет паузу броуновского шума, если выполнены условия паспорта стратегии.
        Возвращает актуализированную цену current_price, либо None, если вход был прерван.
        Вся конфигурация поднята в __init__ и доступна через self.
        """
        ctrl = self
        if smart_grace_cfg is not None:
            ctrl = self.__class__(smart_grace_cfg=smart_grace_cfg)

        if not ctrl.should_apply(closed_count):
            return current_price

        jitter_range = ctrl.get_jitter_range()
        if not jitter_range:
            return current_price

        low, high = jitter_range
        noise_sleep = ctrl.generate_jitter_seconds(low=low, high=high, rng=rng)

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
