# CORE/STRATEGY/trend_size_depender.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY.trend_size_depender
ROLE: Асимметричный балансир инвестиционных сайзов LONG / SHORT по силе макро-тренда 4h.
==============================================================================

Принцип работы:
1. Инвариант капитала (Money Management):
   Суммарный капитал сторон всегда равен 100% выделенного лимита:
   Invest_LONG + Invest_SHORT = 2 * BaseInvestSize = Const.
2. Процентная линейка (33 градации, индекс k in [-16, +16]):
   - k = 0   -> 50.0% LONG / 50.0% SHORT (Флэт / Баланс)
   - k = +16 -> 66.67% LONG / 33.33% SHORT (Экстремальный аптренд)
   - k = -16 -> 33.33% LONG / 66.67% SHORT (Экстремальный даунтренд)
3. Документированные режимы оценки тренда на 4h свечах:
   - В активном бою строго используются: EMA ('ema_trend', 'dyn_ema_16', 'ema_spread_4h').
   - ADX ('adx_di_4h') в стадии тестирования.
"""

import math
import time
import asyncio
from typing import Dict, List, Tuple, Any, Optional
import numpy as np
from c_log import UnifiedLogger
from API.BINANCE.public import BinancePublic

logger = UnifiedLogger("TrendSizeDepender")


class NativeMath:
    """Высокопроизводительный фасад низкоуровневых вычислений индикаторов."""

    @staticmethod
    def fast_ema(prices: List[float] | np.ndarray, length: int) -> List[float]:
        if not isinstance(prices, np.ndarray):
            arr = np.asarray(prices, dtype=np.float64)
        else:
            arr = prices.astype(np.float64, copy=False)

        n = len(arr)
        if n < length:
            return []

        k = 2.0 / (length + 1.0)
        out = [float(np.mean(arr[:length]))]
        ema = out[0]
        for p in arr[length:]:
            ema = p * k + ema * (1.0 - k)
            out.append(ema)
        return out


class TrendCalculator:
    """Изолированный калькулятор тренда на базе быстрой и медленной EMA (4h)."""

    def __init__(self, cfg: Dict[str, Any]):
        self.is_active: bool = bool(cfg.get("is_active", True))
        self.timeframe: str = str(cfg.get("timeframe", "4h"))
        self.sma_fast: int = int(cfg.get("sma_fast", 10))
        self.sma_slow: int = int(cfg.get("sma_slow", 30))
        self.confirmation_candles: int = int(cfg.get("confirmation_candles", 5))
        self.require_rising: bool = bool(cfg.get("require_rising", True))

    def calculate(self, closes: List[float]) -> str:
        """
        Классифицирует состояние рынка по ценам закрытия:
        - UP: быстрая EMA выше медленной на протяжении N свечей (и растет, если require_rising=True)
        - DOWN: быстрая EMA ниже медленной на протяжении N свечей (и падает, если require_rising=True)
        - FLAT: расхождение условий либо боковое движение
        - UNSTABLE: недостаточно свечей для расчета
        """
        if not self.is_active or not closes:
            return "UNSTABLE"

        ema_fast = NativeMath.fast_ema(closes, self.sma_fast)
        ema_slow = NativeMath.fast_ema(closes, self.sma_slow)

        if not ema_fast or not ema_slow or len(ema_fast) < self.confirmation_candles or len(ema_slow) < self.confirmation_candles:
            return "UNSTABLE"

        ef_tail = ema_fast[-self.confirmation_candles:]
        es_tail = ema_slow[-self.confirmation_candles:]

        is_above = all(f > s for f, s in zip(ef_tail, es_tail))
        is_below = all(f < s for f, s in zip(ef_tail, es_tail))
        is_rising = all(ef_tail[i] > ef_tail[i - 1] for i in range(1, len(ef_tail)))
        is_falling = all(ef_tail[i] < ef_tail[i - 1] for i in range(1, len(ef_tail)))

        if is_above and (not self.require_rising or is_rising):
            return "UP"
        elif is_below and (not self.require_rising or is_falling):
            return "DOWN"
        else:
            return "FLAT"


class TrendSizeDepender:
    """
    Институциональный балансир объемов между LONG и SHORT для боевого бота.
    """

    MIN_SIDE_PCT: float = 33.3333
    MAX_SIDE_PCT: float = 66.6667
    NEUTRAL_PCT: float = 50.0
    STEPS_HALF: int = 16
    STEP_PCT: float = (MAX_SIDE_PCT - NEUTRAL_PCT) / STEPS_HALF  # ~1.04167%

    SUPPORTED_MODES: List[str] = [
        "adx_di_4h",
        "ema_spread_4h",
        "ema_drift_4h",
        "ema_trend_4h",
        "ema_trend",
        "dyn_ema_16",
        "dyn_ema_trend",
    ]

    # Кэш: (symbol, timeframe, trend_mode, scale) -> (inv_long: float, inv_short: float, cached_at: float)
    _cache: Dict[Tuple[str, str, str, int], Tuple[float, float, float]] = {}
    _lock = asyncio.Lock()

    @classmethod
    def clear_cache(cls) -> None:
        """Очистка внутреннего кэша (для тестов и рестарта сессий)."""
        cls._cache.clear()

    @classmethod
    def calculate_weights_from_grade(
        cls,
        grade: int,
        trend_mode: str = "",
        scale: Optional[int] = None
    ) -> Tuple[float, float]:
        """
        Переводит целочисленный индекс тренда k in [-16, +16] в веса сторон в процентах.
        """
        tm = str(trend_mode).lower().strip()
        if scale is None:
            scale = 16

        delta_pct = (cls.MAX_SIDE_PCT - cls.NEUTRAL_PCT)

        if tm in ("ema_trend_4h", "dyn_ema_16", "dyn_ema_trend", "ema_trend"):
            if grade > 0:
                w_long = round(cls.NEUTRAL_PCT + delta_pct, 4)
                return w_long, round(100.0 - w_long, 4)
            elif grade < 0:
                w_long = round(cls.NEUTRAL_PCT - delta_pct, 4)
                return w_long, round(100.0 - w_long, 4)
            else:
                return 50.0, 50.0

        steps = scale if scale else cls.STEPS_HALF
        clamped_k = max(-steps, min(steps, int(grade)))
        step_pct = delta_pct / steps
        w_long = round(cls.NEUTRAL_PCT + clamped_k * step_pct, 4)
        min_pct = round(cls.NEUTRAL_PCT - delta_pct, 4)
        max_pct = round(cls.NEUTRAL_PCT + delta_pct, 4)
        w_long = max(min_pct, min(max_pct, w_long))
        w_short = round(100.0 - w_long, 4)
        return w_long, w_short

    @classmethod
    def grade_to_weights(cls, grade: int, trend_mode: str = "", scale: Optional[int] = None) -> Tuple[float, float]:
        """Алиас для calculate_weights_from_grade."""
        return cls.calculate_weights_from_grade(grade, trend_mode=trend_mode, scale=scale)

    @classmethod
    def calculate_invest_sizes(
        cls,
        base_invest: float,
        w_long_pct: float,
        w_short_pct: float
    ) -> Tuple[float, float]:
        """
        Рассчитывает эффективные объемы в USDT для LONG и SHORT при заданном базовом сайзе.
        Инвариант: invest_long + invest_short == 2.0 * base_invest.
        """
        if base_invest <= 0:
            raise ValueError(f"base_invest must be positive, got {base_invest}")

        total_invest = 2.0 * float(base_invest)
        inv_long = round(total_invest * (float(w_long_pct) / 100.0), 2)
        inv_short = round(total_invest * (float(w_short_pct) / 100.0), 2)

        # Компенсация возможной погрешности округления для строгого сохранения инварианта капитала
        residual = round(total_invest - (inv_long + inv_short), 2)
        if residual != 0:
            inv_short = round(inv_short + residual, 2)

        return inv_long, inv_short

    @classmethod
    def evaluate_grade_adx(
        cls,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int = 14,
        threshold_clamp: float = 50.0
    ) -> int:
        """
        Вычисляет силу и направление тренда через ADX/DMI Уайлдера.
        Направленный импульс: RawScore = ((+DI - -DI) / (+DI + -DI)) * ADX.
        Нормализуется по threshold_clamp (50.0) и квантуется в целое k in [-16, 16].
        """
        n = len(closes)
        if n < period + 2:
            return 0

        # 1. True Range и Directional Movement
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)

        for i in range(1, n):
            h_diff = highs[i] - highs[i - 1]
            l_diff = lows[i - 1] - lows[i]

            plus_dm[i] = h_diff if (h_diff > 0 and h_diff > l_diff) else 0.0
            minus_dm[i] = l_diff if (l_diff > 0 and l_diff > h_diff) else 0.0

            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i - 1])
            tr3 = abs(lows[i] - closes[i - 1])
            tr[i] = max(tr1, tr2, tr3)

        # 2. Сглаживание Уайлдера
        smooth_tr = np.zeros(n)
        smooth_pdm = np.zeros(n)
        smooth_mdm = np.zeros(n)

        smooth_tr[period] = np.sum(tr[1:period + 1])
        smooth_pdm[period] = np.sum(plus_dm[1:period + 1])
        smooth_mdm[period] = np.sum(minus_dm[1:period + 1])

        for i in range(period + 1, n):
            smooth_tr[i] = smooth_tr[i - 1] - (smooth_tr[i - 1] / period) + tr[i]
            smooth_pdm[i] = smooth_pdm[i - 1] - (smooth_pdm[i - 1] / period) + plus_dm[i]
            smooth_mdm[i] = smooth_mdm[i - 1] - (smooth_mdm[i - 1] / period) + minus_dm[i]

        # 3. +DI, -DI и DX
        dx = np.zeros(n)
        for i in range(period, n):
            tr_val = smooth_tr[i]
            if tr_val > 1e-8:
                p_di = 100.0 * (smooth_pdm[i] / tr_val)
                m_di = 100.0 * (smooth_mdm[i] / tr_val)
                di_sum = p_di + m_di
                if di_sum > 1e-8:
                    dx[i] = 100.0 * abs(p_di - m_di) / di_sum

        # 4. ADX (сглаживание DX)
        adx_start = period * 2
        if n <= adx_start:
            last_p_di = 100.0 * (smooth_pdm[-1] / max(smooth_tr[-1], 1e-8))
            last_m_di = 100.0 * (smooth_mdm[-1] / max(smooth_tr[-1], 1e-8))
            di_diff = last_p_di - last_m_di
            di_sum = last_p_di + last_m_di
            raw_score = (di_diff / max(di_sum, 1e-8)) * dx[-1]
        else:
            adx = np.zeros(n)
            adx[adx_start - 1] = np.mean(dx[period:adx_start])
            for i in range(adx_start, n):
                adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

            last_p_di = 100.0 * (smooth_pdm[-1] / max(smooth_tr[-1], 1e-8))
            last_m_di = 100.0 * (smooth_mdm[-1] / max(smooth_tr[-1], 1e-8))
            di_diff = last_p_di - last_m_di
            di_sum = last_p_di + last_m_di
            current_adx = adx[-1]
            raw_score = (di_diff / max(di_sum, 1e-8)) * current_adx

        clamped_score = max(-threshold_clamp, min(threshold_clamp, raw_score))
        grade = int(round((clamped_score / threshold_clamp) * cls.STEPS_HALF))
        return max(-cls.STEPS_HALF, min(cls.STEPS_HALF, grade))

    @classmethod
    def evaluate_grade_ema_spread(
        cls,
        closes: np.ndarray,
        fast_period: int = 50,
        slow_period: int = 200,
        spread_clamp_pct: float = 6.0
    ) -> int:
        """
        Вычисляет процентный спред между быстрой и медленной EMA (Dual EMA Spread).
        SpreadPct = ((EMA50 - EMA200) / EMA200) * 100.
        Нормализуется по spread_clamp_pct (6.0%) и квантуется в k in [-16, 16].
        """
        n = len(closes)
        if n < slow_period:
            fast_period = max(5, int(n * 0.25))
            slow_period = max(10, int(n * 0.8))

        def calc_ema(arr: np.ndarray, p: int) -> float:
            alpha = 2.0 / (p + 1.0)
            ema = float(arr[0])
            for val in arr[1:]:
                ema = alpha * float(val) + (1.0 - alpha) * ema
            return ema

        ema_fast = calc_ema(closes, fast_period)
        ema_slow = calc_ema(closes, slow_period)

        if ema_slow < 1e-8:
            return 0

        spread_pct = (ema_fast - ema_slow) / ema_slow * 100.0
        clamped_spread = max(-spread_clamp_pct, min(spread_clamp_pct, spread_pct))
        grade = int(round((clamped_spread / spread_clamp_pct) * cls.STEPS_HALF))
        return max(-cls.STEPS_HALF, min(cls.STEPS_HALF, grade))

    @classmethod
    def evaluate_trend_grade(
        cls,
        trend_mode: str,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Единый диспетчер оценки тренда.
        Бросает громкий KeyError при неподдерживаемом режиме (Правило 2).
        """
        cfg = cfg or {}
        mode = trend_mode.lower().strip()

        if mode not in cls.SUPPORTED_MODES:
            raise KeyError(
                f"[TrendSizeDepender] Неизвестный trend_mode: '{trend_mode}'. "
                f"Поддерживаемые режимы: {cls.SUPPORTED_MODES}"
            )

        if mode in ("ema_trend_4h", "dyn_ema_16", "dyn_ema_trend", "ema_trend"):
            return cls.evaluate_grade_ema_trend(closes=closes, cfg=cfg)

        elif mode == "adx_di_4h":
            period = int(cfg.get("eval_window", 14))
            threshold_clamp = float(cfg.get("trend_threshold_clamp", 50.0))
            return cls.evaluate_grade_adx(
                highs=highs,
                lows=lows,
                closes=closes,
                period=period,
                threshold_clamp=threshold_clamp
            )

        elif mode in ("ema_spread_4h", "ema_drift_4h"):
            fast = int(cfg.get("fast_period", 50))
            slow = int(cfg.get("slow_period", 200))
            clamp_pct = float(cfg.get("spread_clamp_pct", 6.0))
            return cls.evaluate_grade_ema_spread(
                closes=closes,
                fast_period=fast,
                slow_period=slow,
                spread_clamp_pct=clamp_pct
            )

        raise KeyError(f"[TrendSizeDepender] Необработанный режим: '{trend_mode}'")

    @classmethod
    def evaluate_grade_ema_trend(
        cls,
        closes: np.ndarray,
        cfg: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Оценивает состояние тренда 4h Dual EMA 10/30 (TrendCalculator).
        Возвращает:
          - +scale при UP (восходящий тренд)
          - -scale при DOWN (нисходящий тренд)
          - 0 при FLAT или UNSTABLE
        """
        scale = int((cfg or {}).get("scale", 16))
        trend_cfg = {
            "is_active": True,
            "timeframe": "4h",
            "sma_fast": int((cfg or {}).get("sma_fast", 10)),
            "sma_slow": int((cfg or {}).get("sma_slow", 30)),
            "confirmation_candles": int((cfg or {}).get("confirmation_candles", 5)),
            "require_rising": bool((cfg or {}).get("require_rising", True))
        }
        calc = TrendCalculator(trend_cfg)
        close_list = closes.tolist() if isinstance(closes, np.ndarray) else list(closes)
        state = calc.calculate(close_list)
        if state == "UP":
            return scale
        elif state == "DOWN":
            return -scale
        else:
            return 0

    @classmethod
    async def get_effective_invest_size(
        cls,
        client: Optional[object],
        symbol: str,
        side: str,
        base_invest_size: float,
        trend_size_cfg: Optional[Dict[str, Any]],
        cache_ttl_sec: int = 120
    ) -> float:
        """
        Асинхронно рассчитывает эффективный invest_size для указанной стороны (LONG или SHORT)
        на основе 4h макро-тренда с потокобезопасным TTL-кэшированием свечей.
        Если trend_size_cfg отключен или режим static, возвращает базовый base_invest_size.
        """
        if not trend_size_cfg or not trend_size_cfg.get("enabled", True):
            return float(base_invest_size)

        trend_mode = str(trend_size_cfg.get("trend_mode") or trend_size_cfg.get("mode", "static")).lower().strip()
        if trend_mode == "static":
            return float(base_invest_size)

        timeframe = str(trend_size_cfg.get("timeframe", "4h")).strip()
        scale = int(trend_size_cfg.get("scale", 16))
        cache_key = (symbol.upper(), timeframe, trend_mode, scale)
        now = time.time()

        cached = cls._cache.get(cache_key)
        if cached is not None:
            inv_l, inv_s, ts = cached
            if now - ts < cache_ttl_sec:
                return inv_l if side == "LONG" else inv_s

        async with cls._lock:
            # Двойная проверка под локом
            cached = cls._cache.get(cache_key)
            if cached is not None:
                inv_l, inv_s, ts = cached
                if now - ts < cache_ttl_sec:
                    return inv_l if side == "LONG" else inv_s

            try:
                eval_window = int(trend_size_cfg.get("eval_window", 20))
                limit = max(35, eval_window + 10)

                klines = await BinancePublic.get_klines(symbol, timeframe, limit)
                if not klines and client and hasattr(client, "get_klines"):
                    klines = await client.get_klines(symbol, timeframe, limit)

                if not klines or len(klines) < 15:
                    logger.debug(
                        f"[TrendSizeDepender][{symbol}] Not enough {timeframe} klines "
                        f"({len(klines) if klines else 0}), using base {base_invest_size:.2f} USDT"
                    )
                    return float(base_invest_size)

                highs_list, lows_list, closes_list = [], [], []
                for k in klines:
                    if isinstance(k, dict):
                        highs_list.append(float(k.get("high", 0.0)))
                        lows_list.append(float(k.get("low", 0.0)))
                        closes_list.append(float(k.get("close", 0.0)))
                    elif isinstance(k, (list, tuple)) and len(k) > 4:
                        highs_list.append(float(k[2]))
                        lows_list.append(float(k[3]))
                        closes_list.append(float(k[4]))
                highs = np.array(highs_list)
                lows = np.array(lows_list)
                closes = np.array(closes_list)

                grade = cls.evaluate_trend_grade(trend_mode, highs, lows, closes, trend_size_cfg)
                w_long, w_short = cls.calculate_weights_from_grade(grade, trend_mode=trend_mode, scale=scale)
                inv_l, inv_s = cls.calculate_invest_sizes(base_invest_size, w_long, w_short)

                cls._cache[cache_key] = (inv_l, inv_s, now)
                logger.info(
                    f"[TrendSizeDepender][{symbol}] Evaluated {trend_mode} (scale={scale}, {timeframe}): "
                    f"Grade k={grade:+d} -> LONG {w_long:.2f}% ({inv_l:.2f}$), SHORT {w_short:.2f}% ({inv_s:.2f}$)"
                )
                return inv_l if side == "LONG" else inv_s

            except Exception as e:
                logger.warning(
                    f"[TrendSizeDepender][{symbol}] Error computing trend size ({e}), "
                    f"fallback to base {base_invest_size:.2f} USDT"
                )
                return float(base_invest_size)
