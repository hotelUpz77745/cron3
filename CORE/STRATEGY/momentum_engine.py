# ============================================================
# FILE: CORE/STRATEGY/momentum_engine.py
# ROLE: Mathematical engine for Smart Grace, Smart Size, Knife Blocker, and Smart TP
# ============================================================

import time
import asyncio
from typing import Dict, Any, Optional, Tuple, List
from c_log import UnifiedLogger
from API.BINANCE.public import BinancePublic
from CORE.STRATEGY.trend_size_depender import TrendCalculator

logger = UnifiedLogger("MomentumEngine")


class MomentumEngine:
    """
    Чистый математический движок для расчета импульсных адаптивных параметров:
    - Smart Grace: расчет длительности окна по карточке приращений
    - Smart Size: расчет коэффициента объема позиции (включая dynamic_sizing_mode по EMA)
    - Knife Blocker: блокировка входа на противоположном пампе/дампе
    - Smart TP: динамическое расширение тейк-профита при частом исполнении
    """

    # Кэш тренда: (symbol, timeframe) -> (trend_state, cached_at_ts)
    _trend_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
    _trend_lock: Optional[asyncio.Lock] = None

    @classmethod
    def _get_trend_lock(cls) -> asyncio.Lock:
        if cls._trend_lock is None:
            cls._trend_lock = asyncio.Lock()
        return cls._trend_lock

    @classmethod
    def clear_cache(cls) -> None:
        """Сброс кэша макро-тренда (для тестов и рестарта сессий)."""
        cls._trend_cache.clear()

    @classmethod
    async def get_ema_trend_state(
        cls,
        symbol: str,
        ema_cfg: Optional[Dict[str, Any]] = None,
        client: Optional[Any] = None,
        cache_ttl_sec: int = 120
    ) -> str:
        """
        Изолированно определяет текущий макро-тренд (UP / DOWN / FLAT / UNSTABLE)
        по правилу Dual EMA (по умолчанию 4h, 10/30, confirmation=5, require_rising=True)
        с потокобезопасным TTL-кэшированием свечей.
        """
        cfg = ema_cfg or {}
        timeframe = str(cfg.get("timeframe", "4h")).strip()
        cache_key = (symbol.upper(), timeframe)
        now = time.time()

        cached = cls._trend_cache.get(cache_key)
        if cached is not None:
            trend_val, ts = cached
            if now - ts < cache_ttl_sec:
                return trend_val

        async with cls._get_trend_lock():
            cached = cls._trend_cache.get(cache_key)
            if cached is not None:
                trend_val, ts = cached
                if now - ts < cache_ttl_sec:
                    return trend_val

            try:
                sma_slow = int(cfg.get("sma_slow", 30))
                confirmation = int(cfg.get("confirmation_candles", 5))
                limit = max(35, sma_slow + confirmation + 5)

                klines = await BinancePublic.get_klines(symbol, timeframe, limit)
                if not klines and client and hasattr(client, "get_klines"):
                    klines = await client.get_klines(symbol, timeframe, limit)

                if not klines or len(klines) < 15:
                    logger.debug(
                        f"[MomentumEngine][{symbol}] Not enough {timeframe} klines "
                        f"({len(klines) if klines else 0}) for EMA trend, fallback to FLAT"
                    )
                    return "FLAT"

                closes: List[float] = []
                for k in klines:
                    if isinstance(k, dict):
                        closes.append(float(k.get("close", 0.0)))
                    elif isinstance(k, (list, tuple)) and len(k) > 4:
                        closes.append(float(k[4]))

                trend_calc_cfg = {
                    "is_active": True,
                    "timeframe": timeframe,
                    "sma_fast": int(cfg.get("sma_fast", 10)),
                    "sma_slow": sma_slow,
                    "confirmation_candles": confirmation,
                    "require_rising": bool(cfg.get("require_rising", True))
                }
                calc = TrendCalculator(trend_calc_cfg)
                trend_state = calc.calculate(closes)

                cls._trend_cache[cache_key] = (trend_state, now)
                logger.debug(
                    f"[MomentumEngine][{symbol}] Evaluated EMA trend "
                    f"({timeframe} {cfg.get('sma_fast', 10)}/{sma_slow}): {trend_state}"
                )
                return trend_state

            except Exception as e:
                logger.warning(f"[MomentumEngine][{symbol}] Error evaluating EMA trend ({e}), fallback to FLAT")
                return "FLAT"

    @classmethod
    def match_rule(cls, rule: str, count: int) -> bool:
        """
        Проверяет соответствие числа закрытых сделок правилу строки.
        Поддерживает операторы: '<', '<=', '==', '>=', '>'.
        """
        rule = rule.strip()
        try:
            if rule.startswith("<="):
                return count <= int(rule[2:])
            elif rule.startswith(">="):
                return count >= int(rule[2:])
            elif rule.startswith("=="):
                return count == int(rule[2:])
            elif rule.startswith("<"):
                return count < int(rule[1:])
            elif rule.startswith(">"):
                return count > int(rule[1:])
            elif rule.isdigit():
                return count == int(rule)
        except ValueError:
            logger.warning(f"Invalid rule format in MomentumEngine: '{rule}'")
        return False

    @classmethod
    def calculate_smart_grace(cls, closed_count: int, smart_grace_cfg: Dict[str, Any]) -> float:
        """
        Рассчитывает длительность окна Smart Grace по карточке increments_card.
        Базовый период: start_period_sec.
        Если карточка не задана, возвращает start_period_sec.
        """
        start_period = float(smart_grace_cfg["start_period_sec"])
        increments_card = smart_grace_cfg.get("increments_card", {})
        if not increments_card:
            return start_period

        max_extra = 0.0
        for rule, extra_sec in increments_card.items():
            if cls.match_rule(rule, closed_count):
                extra_val = float(extra_sec)
                if extra_val > max_extra:
                    max_extra = extra_val

        return start_period + max_extra

    @classmethod
    def calculate_smart_size_weight(
        cls,
        closed_count: int,
        smart_size_cfg: Optional[Dict[str, Any]],
        side: Optional[str] = None,
        trend_state: Optional[str] = None
    ) -> float:
        """
        Рассчитывает множитель объема сетки на основе профитабельности стороны.
        По умолчанию 1.00x.
        
        Если включен dynamic_sizing_mode (например, 'dyn_ema_71'):
          - Применяем множитель из profiting_size_weights ТОЛЬКО если сторона ордера
            совпадает с текущим EMA-трендом (LONG при UP или SHORT при DOWN).
          - Если тренд не совпадает (контртренд, флэт, нестабилен) -> строго 1.00x.
          
        Если dynamic_sizing_mode выключен или null:
          - Классический бестрендовый расчет строго по closed_count.
        """
        if not smart_size_cfg:
            return 1.00

        weights_card = smart_size_cfg.get("profiting_size_weights", {})
        if not weights_card:
            return 1.00

        # Проверка флага привязки к тренду (trend_determ: "ema_trend")
        trend_determ = smart_size_cfg.get("trend_determ") or smart_size_cfg.get("dynamic_sizing_mode")
        if trend_determ:
            is_aligned = False
            if side and trend_state:
                norm_side = str(side).upper().strip()
                norm_trend = str(trend_state).upper().strip()
                if norm_side == "LONG" and norm_trend == "UP":
                    is_aligned = True
                elif norm_side == "SHORT" and norm_trend == "DOWN":
                    is_aligned = True

            if not is_aligned:
                return 1.00

        for rule, weight in weights_card.items():
            if cls.match_rule(rule, closed_count):
                return float(weight)

        return 1.00

    @classmethod
    def calculate_smart_size_multiplier(
        cls,
        closed_count: int,
        smart_size_cfg: Optional[Dict[str, Any]],
        side: Optional[str] = None,
        trend_state: Optional[str] = None
    ) -> float:
        """Алиас для calculate_smart_size_weight для полной совместимости с bot.py."""
        return cls.calculate_smart_size_weight(
            closed_count=closed_count,
            smart_size_cfg=smart_size_cfg,
            side=side,
            trend_state=trend_state
        )

    @classmethod
    def is_knife_blocked(cls, opposite_closed_count: int, smart_size_cfg: Optional[Dict[str, Any]]) -> bool:
        """
        Проверяет правило Knife Blocker (Cross-Side Awareness).
        Конфигурируется через единую плоскую сигнатуру:
        - 'knife_blocker_enabled': bool
        - 'knife_blocker_threshold': int (порог закрытых противоположных тейков)
        Если выключено, всегда возвращает False.
        Если включено и opposite_closed_count >= threshold, блокирует вход в Шаг 0.
        """
        if not smart_size_cfg:
            return False

        is_enabled = smart_size_cfg.get("knife_blocker_enabled")
        if is_enabled is None and "knife_blocker" in smart_size_cfg:
            kb = smart_size_cfg["knife_blocker"]
            is_enabled = kb.get("enabled", False) if isinstance(kb, dict) else bool(kb)

        if not is_enabled:
            return False

        threshold = smart_size_cfg.get("knife_blocker_threshold")
        if threshold is None and "knife_blocker" in smart_size_cfg:
            kb = smart_size_cfg["knife_blocker"]
            threshold = kb.get("threshold", 2) if isinstance(kb, dict) else 2

        return opposite_closed_count >= int(threshold if threshold is not None else 2)

    @classmethod
    def calculate_smart_tp_multiplier(cls, closed_count: int, smart_tp_cfg: Optional[Dict[str, Any]]) -> float:
        """
        Рассчитывает множитель Take-Profit (и страховочного Fallback TP)
        в зависимости от частоты закрытий в пределах eval_window_sec.
        Если closed_count == 0 (спокойный рынок) -> 1.00x.
        """
        if not smart_tp_cfg or not smart_tp_cfg.get("enabled", True) or closed_count == 0:
            return 1.00

        increments = smart_tp_cfg.get("increments_tp", {})
        if not increments:
            return 1.00

        matched_multiplier = 1.00
        for rule, mult in increments.items():
            if cls.match_rule(rule, closed_count):
                mult_val = float(mult)
                if mult_val > matched_multiplier:
                    matched_multiplier = mult_val

        return matched_multiplier
