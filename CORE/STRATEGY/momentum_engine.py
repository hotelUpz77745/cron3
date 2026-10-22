# -*- coding: utf-8 -*-
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
        sma_fast = int(cfg.get("sma_fast", 10))
        sma_slow = int(cfg.get("sma_slow", 30))
        confirmation = int(cfg.get("confirmation_candles", 5 if "4h" in timeframe else 3))
        cache_key = (symbol.upper(), timeframe, sma_fast, sma_slow, confirmation)
        sim_ts = getattr(client, "current_sim_time", 0)
        if not isinstance(sim_ts, (int, float)):
            sim_ts = 0
        now = (sim_ts / 1000.0) if sim_ts > 1000000000 else time.time()

        # 0. Instant O(1) Precomputed Trend Lookup for Backtests
        if client and hasattr(client, "get_precomputed_trend"):
            try:
                p_trend = client.get_precomputed_trend(symbol, timeframe, sma_fast, sma_slow)
            except TypeError:
                p_trend = client.get_precomputed_trend(symbol, timeframe)
            if p_trend is not None:
                return p_trend

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
                limit = max(sma_slow + confirmation + 15, 60)
                klines = None
                if client and hasattr(client, "get_klines"):
                    klines = await client.get_klines(symbol, timeframe, limit)
                if not klines:
                    klines = await BinancePublic.get_klines(symbol, timeframe, limit)

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

    # [LEGACY / EXPERIMENTAL] calculate_adx_dmi и get_adx_trend_state закомментированы.
    # В активном бою тренд оценивается строго через EMA (get_ema_trend_state).
    # @classmethod
    # def calculate_adx_dmi(
    #     cls,
    #     highs: Any,
    #     lows: Any,
    #     closes: Any,
    #     period: int = 14
    # ) -> Tuple[float, float, float]:
    #     import numpy as np
    #     h_arr = np.asarray(highs, dtype=np.float64)
    #     l_arr = np.asarray(lows, dtype=np.float64)
    #     c_arr = np.asarray(closes, dtype=np.float64)
    #     n = len(c_arr)
    #     if n < period * 2 + 1:
    #         return 0.0, 0.0, 0.0
    #
    #     tr = np.zeros(n)
    #     p_dm = np.zeros(n)
    #     m_dm = np.zeros(n)
    #
    #     for i in range(1, n):
    #         h_diff = h_arr[i] - h_arr[i - 1]
    #         l_diff = l_arr[i - 1] - l_arr[i]
    #         p_dm[i] = h_diff if (h_diff > 0 and h_diff > l_diff) else 0.0
    #         m_dm[i] = l_diff if (l_diff > 0 and l_diff > h_diff) else 0.0
    #         tr1 = h_arr[i] - l_arr[i]
    #         tr2 = abs(h_arr[i] - c_arr[i - 1])
    #         tr3 = abs(l_arr[i] - c_arr[i - 1])
    #         tr[i] = max(tr1, tr2, tr3)
    #
    #     smooth_tr = np.zeros(n)
    #     smooth_pdm = np.zeros(n)
    #     smooth_mdm = np.zeros(n)
    #
    #     smooth_tr[period] = np.sum(tr[1:period + 1])
    #     smooth_pdm[period] = np.sum(p_dm[1:period + 1])
    #     smooth_mdm[period] = np.sum(m_dm[1:period + 1])
    #
    #     for i in range(period + 1, n):
    #         smooth_tr[i] = smooth_tr[i - 1] - (smooth_tr[i - 1] / period) + tr[i]
    #         smooth_pdm[i] = smooth_pdm[i - 1] - (smooth_pdm[i - 1] / period) + p_dm[i]
    #         smooth_mdm[i] = smooth_mdm[i - 1] - (smooth_mdm[i - 1] / period) + m_dm[i]
    #
    #     dx = np.zeros(n)
    #     p_di_arr = np.zeros(n)
    #     m_di_arr = np.zeros(n)
    #
    #     for i in range(period, n):
    #         t_val = smooth_tr[i]
    #         if t_val > 0:
    #             p_di_arr[i] = 100.0 * (smooth_pdm[i] / t_val)
    #             m_di_arr[i] = 100.0 * (smooth_mdm[i] / t_val)
    #             di_sum = p_di_arr[i] + m_di_arr[i]
    #             if di_sum > 0:
    #                 dx[i] = 100.0 * abs(p_di_arr[i] - m_di_arr[i]) / di_sum
    #
    #     adx_start = period * 2
    #     if n <= adx_start:
    #         return float(dx[-1]), float(p_di_arr[-1]), float(m_di_arr[-1])
    #
    #     adx = np.zeros(n)
    #     adx[adx_start - 1] = np.mean(dx[period:adx_start])
    #     for i in range(adx_start, n):
    #         adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    #
    #     return float(adx[-1]), float(p_di_arr[-1]), float(m_di_arr[-1])

    # @classmethod
    # async def get_adx_trend_state(
    #     cls,
    #     symbol: str,
    #     adx_cfg: Optional[Dict[str, Any]] = None,
    #     client: Optional[Any] = None,
    #     cache_ttl_sec: int = 120
    # ) -> str:
    #     cfg = adx_cfg or {}
    #     timeframe = str(cfg.get("timeframe", "1d")).strip()
    #     cache_key = (symbol.upper(), f"adx_{timeframe}")
    #     sim_ts = getattr(client, "current_sim_time", 0)
    #     now = (sim_ts / 1000.0) if sim_ts > 1000000000 else time.time()
    #     if client and hasattr(client, "get_precomputed_trend"):
    #         p_trend = client.get_precomputed_trend(symbol, f"adx_{timeframe}")
    #         if p_trend is not None:
    #             return p_trend
    #     cached = cls._trend_cache.get(cache_key)
    #     if cached is not None:
    #         trend_val, ts = cached
    #         if now - ts < cache_ttl_sec:
    #             return trend_val
    #     async with cls._get_trend_lock():
    #         cached = cls._trend_cache.get(cache_key)
    #         if cached is not None:
    #             trend_val, ts = cached
    #             if now - ts < cache_ttl_sec:
    #                 return trend_val
    #         try:
    #             period = int(cfg.get("period", 14))
    #             adx_threshold = float(cfg.get("adx_threshold", 25.0))
    #             limit = max(period * 3 + 15, 60)
    #             klines = None
    #             if client and hasattr(client, "get_klines"):
    #                 klines = await client.get_klines(symbol, timeframe, limit)
    #             if not klines:
    #                 klines = await BinancePublic.get_klines(symbol, timeframe, limit)
    #             if not klines or len(klines) < period + 5:
    #                 return "FLAT"
    #             highs, lows, closes = [], [], []
    #             for k in klines:
    #                 if isinstance(k, dict):
    #                     highs.append(float(k.get("high", 0.0)))
    #                     lows.append(float(k.get("low", 0.0)))
    #                     closes.append(float(k.get("close", 0.0)))
    #                 elif isinstance(k, (list, tuple)) and len(k) > 4:
    #                     highs.append(float(k[2]))
    #                     lows.append(float(k[3]))
    #                     closes.append(float(k[4]))
    #             adx_val, p_di, m_di = cls.calculate_adx_dmi(highs, lows, closes, period=period)
    #             if adx_val >= adx_threshold:
    #                 if p_di > m_di:
    #                     trend_state = "UP"
    #                 elif m_di > p_di:
    #                     trend_state = "DOWN"
    #                 else:
    #                     trend_state = "FLAT"
    #             else:
    #                 trend_state = "FLAT"
    #             cls._trend_cache[cache_key] = (trend_state, now)
    #             return trend_state
    #         except Exception as e:
    #             logger.warning(f"[MomentumEngine][{symbol}] Error evaluating ADX trend ({e}), fallback to FLAT")
    #             return "FLAT"

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

    def __init__(
        self,
        smart_size_cfg: Optional[Dict[str, Any]] = None,
        smart_tp_cfg: Optional[Dict[str, Any]] = None,
    ):
        """
        Инициализирует математический движок Smart Size, Knife Blocker и Smart TP.
        Прямое чтение конфигураций поднято в __init__ по правилу «Работай или упади сразу» (Fail-Fast).
        Все поля и константы валидируются при запуске программы.
        """
        self.smart_size_cfg: Optional[Dict[str, Any]] = smart_size_cfg
        self.smart_tp_cfg: Optional[Dict[str, Any]] = smart_tp_cfg

        # 1. Smart Size
        self.profiting_size_weights: Dict[str, float] = {}
        self.stagnation_min_step: Optional[int] = None
        self.smart_size_trend_determ: Optional[str] = None
        self.knife_blocker_enabled: bool = False
        self.knife_blocker_threshold: int = 2

        if smart_size_cfg is not None:
            if not isinstance(smart_size_cfg, dict):
                raise TypeError(f"[MomentumEngine] smart_size_cfg must be a dict, got {type(smart_size_cfg)}")
            if "profiting_size_weights" in smart_size_cfg:
                pw = smart_size_cfg["profiting_size_weights"]
                if not isinstance(pw, dict):
                    raise TypeError(f"[MomentumEngine] profiting_size_weights must be a dict, got {type(pw)}")
                self.profiting_size_weights = {str(k): float(v) for k, v in pw.items()}

            if "stagnation_min_step" in smart_size_cfg and smart_size_cfg["stagnation_min_step"] is not None:
                self.stagnation_min_step = int(smart_size_cfg["stagnation_min_step"])

            if "trend_determ" in smart_size_cfg and smart_size_cfg["trend_determ"] is not None:
                self.smart_size_trend_determ = str(smart_size_cfg["trend_determ"]).strip()

            if "knife_blocker_enabled" in smart_size_cfg:
                self.knife_blocker_enabled = bool(smart_size_cfg["knife_blocker_enabled"])
            elif "knife_blocker" in smart_size_cfg and isinstance(smart_size_cfg["knife_blocker"], dict):
                self.knife_blocker_enabled = bool(smart_size_cfg["knife_blocker"].get("enabled", False))
                self.knife_blocker_threshold = int(smart_size_cfg["knife_blocker"].get("threshold", 2))

            if "knife_blocker_threshold" in smart_size_cfg and smart_size_cfg["knife_blocker_threshold"] is not None:
                self.knife_blocker_threshold = int(smart_size_cfg["knife_blocker_threshold"])

        # 2. Smart TP
        self.smart_tp_enabled: bool = False
        self.increments_tp: Dict[str, float] = {}
        self.smart_tp_trend_determ: Optional[str] = None

        if smart_tp_cfg is not None:
            if not isinstance(smart_tp_cfg, dict):
                raise TypeError(f"[MomentumEngine] smart_tp_cfg must be a dict, got {type(smart_tp_cfg)}")
            self.smart_tp_enabled = bool(smart_tp_cfg.get("enabled", True))
            if "increments_tp" in smart_tp_cfg:
                inc = smart_tp_cfg["increments_tp"]
                if not isinstance(inc, dict):
                    raise TypeError(f"[MomentumEngine] increments_tp must be a dict, got {type(inc)}")
                self.increments_tp = {str(k): float(v) for k, v in inc.items()}
            if "trend_determ" in smart_tp_cfg and smart_tp_cfg["trend_determ"] is not None:
                self.smart_tp_trend_determ = str(smart_tp_cfg["trend_determ"]).strip()

    @classmethod
    def calculate_smart_size_weight(
        cls,
        closed_count: int,
        smart_size_cfg: Optional[Dict[str, Any]] = None,
        side: Optional[str] = None,
        trend_state: Optional[str] = None,
        opp_in_pos: bool = False,
        opp_step: int = 0
    ) -> float:
        """
        Рассчитывает множитель объема сетки на основе профитабельности стороны.
        По умолчанию 1.00x.
        """
        if not smart_size_cfg:
            return 1.00
        engine = cls(smart_size_cfg=smart_size_cfg)
        return engine.calculate_size_multiplier(
            closed_count=closed_count,
            side=side,
            trend_state=trend_state,
            opp_in_pos=opp_in_pos,
            opp_step=opp_step,
        )

    def calculate_size_multiplier(
        self,
        closed_count: int,
        side: Optional[str] = None,
        trend_state: Optional[str] = None,
        opp_in_pos: bool = False,
        opp_step: int = 0
    ) -> float:
        """Расчет множителя объема на основе провалидированных в __init__ параметров."""
        if not self.profiting_size_weights:
            return 1.00

        # Триггер стагнации (stagnation_min_step)
        if self.stagnation_min_step is not None:
            if not opp_in_pos or int(opp_step) < int(self.stagnation_min_step):
                return 1.00

        # Проверка флага привязки к тренду (trend_determ: "ema_trend")
        if self.smart_size_trend_determ:
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

        for rule, weight in self.profiting_size_weights.items():
            if self.match_rule(rule, closed_count):
                return float(weight)

        return 1.00

    @classmethod
    def calculate_smart_size_multiplier(
        cls,
        closed_count: int,
        smart_size_cfg: Optional[Dict[str, Any]] = None,
        side: Optional[str] = None,
        trend_state: Optional[str] = None,
        opp_in_pos: bool = False,
        opp_step: int = 0
    ) -> float:
        """Алиас для calculate_smart_size_weight для полной совместимости с bot.py."""
        return cls.calculate_smart_size_weight(
            closed_count=closed_count,
            smart_size_cfg=smart_size_cfg,
            side=side,
            trend_state=trend_state,
            opp_in_pos=opp_in_pos,
            opp_step=opp_step
        )

    @classmethod
    def is_knife_blocked(cls, opposite_closed_count: int, smart_size_cfg: Optional[Dict[str, Any]] = None) -> bool:
        """
        Проверяет правило Knife Blocker (Cross-Side Awareness).
        Строго читает нормализованные поля через __init__.
        """
        if not smart_size_cfg:
            return False
        engine = cls(smart_size_cfg=smart_size_cfg)
        return engine.check_knife_blocked(opposite_closed_count)

    def check_knife_blocked(self, opposite_closed_count: int) -> bool:
        """Проверка Knife Blocker на инстансе без словарных чтений."""
        if not self.knife_blocker_enabled:
            return False
        return opposite_closed_count >= self.knife_blocker_threshold

    @classmethod
    def calculate_smart_tp_multiplier(
        cls,
        closed_count: int,
        smart_tp_cfg: Optional[Dict[str, Any]] = None,
        side: Optional[str] = None,
        trend_state: Optional[str] = None
    ) -> float:
        """
        Рассчитывает множитель Take-Profit (и страховочного Fallback TP)
        в зависимости от частоты закрытий в пределах eval_window_sec.
        """
        if not smart_tp_cfg or closed_count == 0:
            return 1.00
        engine = cls(smart_tp_cfg=smart_tp_cfg)
        return engine.calculate_tp_multiplier(
            closed_count=closed_count,
            side=side,
            trend_state=trend_state,
        )

    def calculate_tp_multiplier(
        self,
        closed_count: int,
        side: Optional[str] = None,
        trend_state: Optional[str] = None
    ) -> float:
        """Расчет множителя TP на основе провалидированных в __init__ параметров."""
        if not self.smart_tp_enabled or closed_count == 0:
            return 1.00

        if not self.increments_tp:
            return 1.00

        # Проверка флага привязки к тренду (trend_determ: "ema_trend")
        if self.smart_tp_trend_determ:
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

        matched_multiplier = 1.00
        for rule, mult in self.increments_tp.items():
            if self.match_rule(rule, closed_count):
                mult_val = float(mult)
                if mult_val > matched_multiplier:
                    matched_multiplier = mult_val

        return matched_multiplier

