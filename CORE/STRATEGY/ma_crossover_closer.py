# ==============================================================================
# Path: CORE/STRATEGY/ma_crossover_closer.py
# Role: Автономный модуль автозакрытия купола по пересечению простых MA (ma_crossover) с масштабированием 5m -> 1m
# ==============================================================================

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from CORE.STRATEGY.profit_closer_common import (
    BucketedProfitHistorySampler,
    ChronosTimeForceEvaluator,
)


class MaCrossoverCloser:
    """
    Автономный оценщик фиксации профита по пересечению простых арифметических скользящих средних:
    - Периоды задаются в опорном масштабе reference_interval_sec (например, 300с = 5m: MA3, MA50/MA100)
      и автоматически масштабируются в sample_interval_sec (например, 60с = 1m => step_scale=5: MA15, MA250/MA500);
    - Фильтрует минимальное время прогрева (min_samples) и строго положительный порог профита (min_profit_usdt * scale_money);
    - Опционально требует не-рост медленной MA (MA_slow_now <= MA_slow_prev) через флаг require_slow_ma_non_growing;
    - Поддерживает отключаемое временное форсирование выхода chronos_exit (E1 Chronos).
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.enabled: bool = bool(cfg["enabled"])
        self.sample_interval_sec: int = int(cfg["sample_interval_sec"])
        self.reference_interval_sec: int = int(cfg["reference_interval_sec"])
        self.min_periods: int = int(cfg["min_periods"])
        self.fast_ma_periods: int = int(cfg["fast_ma_periods"])
        self.slow_ma_periods: int = int(cfg["slow_ma_periods"])
        self.slow_non_growing_lookback_periods: int = int(cfg["slow_non_growing_lookback_periods"])
        self.min_profit_usdt: float = float(cfg["min_profit_usdt"])
        self.require_slow_ma_non_growing: bool = bool(cfg["require_slow_ma_non_growing"])
        self.chronos: ChronosTimeForceEvaluator = ChronosTimeForceEvaluator(cfg["chronos_exit"])

        if self.sample_interval_sec <= 0 or self.reference_interval_sec <= 0:
            raise ValueError(
                f"Intervals must be > 0, got sample={self.sample_interval_sec}, ref={self.reference_interval_sec}"
            )
        if self.reference_interval_sec < self.sample_interval_sec or (
            self.reference_interval_sec % self.sample_interval_sec != 0
        ):
            raise ValueError(
                f"reference_interval_sec ({self.reference_interval_sec}) must be an integer multiple "
                f"of sample_interval_sec ({self.sample_interval_sec})"
            )

        self.step_scale: int = self.reference_interval_sec // self.sample_interval_sec
        if self.fast_ma_periods <= 0 or self.slow_ma_periods <= 0:
            raise ValueError(f"MA periods must be > 0, got fast={self.fast_ma_periods}, slow={self.slow_ma_periods}")
        if self.fast_ma_periods >= self.slow_ma_periods:
            raise ValueError(
                f"fast_ma_periods ({self.fast_ma_periods}) must be < slow_ma_periods ({self.slow_ma_periods})"
            )
        if self.min_periods < self.slow_ma_periods:
            raise ValueError(
                f"min_periods ({self.min_periods}) must be >= slow_ma_periods ({self.slow_ma_periods})"
            )
        if self.slow_non_growing_lookback_periods <= 0:
            raise ValueError(
                f"slow_non_growing_lookback_periods must be > 0, got {self.slow_non_growing_lookback_periods}"
            )
        if self.min_profit_usdt <= 0.0:
            raise ValueError(f"min_profit_usdt must be strictly positive (> 0), got {self.min_profit_usdt}")

        self.min_samples: int = self.min_periods * self.step_scale
        self.fast_ma_samples: int = self.fast_ma_periods * self.step_scale
        self.slow_ma_samples: int = self.slow_ma_periods * self.step_scale
        self.slow_non_growing_lookback_samples: int = self.slow_non_growing_lookback_periods * self.step_scale

    @classmethod
    def should_take_profit(
        cls,
        profit_history: List[float],
        min_samples: int,
        fast_ma_samples: int,
        slow_ma_samples: int,
        min_profit_usdt: float,
        require_slow_ma_non_growing: bool,
        slow_non_growing_lookback_samples: int,
    ) -> Tuple[bool, Optional[float], float, float, Optional[float]]:
        """
        Чистая математическая функция расчёта сигнала пересечения простых арифметических MA.
        Возвращает (should_close, trigger_level, ma_fast, ma_slow, ma_slow_prev).
        """
        n = len(profit_history)
        if n == 0:
            return False, None, 0.0, 0.0, None

        fast_win = min(n, fast_ma_samples)
        slow_win = min(n, slow_ma_samples)
        ma_fast = sum(profit_history[-fast_win:]) / float(fast_win)
        ma_slow = sum(profit_history[-slow_win:]) / float(slow_win)

        required_len = max(
            min_samples,
            slow_ma_samples + (slow_non_growing_lookback_samples if require_slow_ma_non_growing else 0),
        )
        if n < required_len:
            return False, None, ma_fast, ma_slow, None

        ma_slow_prev: Optional[float] = None
        if require_slow_ma_non_growing:
            lback = slow_non_growing_lookback_samples
            ma_slow_prev = sum(profit_history[-(slow_ma_samples + lback) : -lback]) / float(slow_ma_samples)

        # Строго положительный порог прибыли и защита от закрытия ниже min_profit_usdt
        if ma_fast <= 0.0 or ma_fast <= min_profit_usdt or profit_history[-1] <= min_profit_usdt:
            return False, ma_slow, ma_fast, ma_slow, ma_slow_prev

        if ma_fast > ma_slow:
            return False, ma_slow, ma_fast, ma_slow, ma_slow_prev

        if require_slow_ma_non_growing and ma_slow_prev is not None and ma_slow > ma_slow_prev:
            return False, ma_slow, ma_fast, ma_slow, ma_slow_prev

        return True, ma_slow, ma_fast, ma_slow, ma_slow_prev

    def evaluate(
        self,
        iter_net: float,
        now_sec: float,
        state: Dict[str, Any],
        scale_money: float,
        iter_age_hours: float = 0.0,
    ) -> Tuple[bool, Optional[float], str]:
        """Оценивает Chronos-форсаж (если включен) и MA-пересечение по закрытому бакету."""
        if not self.enabled:
            return False, None, ""

        ch_ok, ch_floor, ch_info = self.chronos.evaluate(
            iter_net=iter_net,
            iter_age_hours=iter_age_hours,
            scale_money=scale_money,
            state=state,
        )
        if ch_ok and ch_floor is not None:
            return True, ch_floor, ch_info

        is_new, history, peak = BucketedProfitHistorySampler.sample_bucket(
            state=state,
            iter_net=iter_net,
            now_sec=now_sec,
            sample_interval_sec=self.sample_interval_sec,
        )
        if not is_new:
            return False, None, ""

        eff_min = self.min_profit_usdt * scale_money
        state["active_level_idx"] = 0
        state["armed_level_desc"] = f"MA{self.fast_ma_periods}/{self.slow_ma_periods}"
        state["eff_min_profit"] = eff_min

        triggered, trigger_lvl, ma_fast, ma_slow, ma_slow_prev = self.should_take_profit(
            profit_history=history,
            min_samples=self.min_samples,
            fast_ma_samples=self.fast_ma_samples,
            slow_ma_samples=self.slow_ma_samples,
            min_profit_usdt=eff_min,
            require_slow_ma_non_growing=self.require_slow_ma_non_growing,
            slow_non_growing_lookback_samples=self.slow_non_growing_lookback_samples,
        )

        required_len = max(
            self.min_samples,
            self.slow_ma_samples + (self.slow_non_growing_lookback_samples if self.require_slow_ma_non_growing else 0),
        )
        if len(history) < required_len:
            return False, None, f"[MA_CROSS WARMUP: {len(history)}/{required_len} samples, Peak={peak:+.2f}$]"

        slope_note = (
            f", SlowPrev={ma_slow_prev:+.2f}$" if (self.require_slow_ma_non_growing and ma_slow_prev is not None) else ""
        )
        if triggered and trigger_lvl is not None:
            info = (
                f"[MA_CROSS TRIGGER 'MA{self.fast_ma_periods}<=MA{self.slow_ma_periods}': "
                f"Fast({self.fast_ma_samples})={ma_fast:+.2f}$ <= Slow({self.slow_ma_samples})={ma_slow:+.2f}$"
                f"{slope_note} (Net={iter_net:+.2f}$, Min={eff_min:+.2f}$, Age={iter_age_hours:.1f}h)]"
            )
            return True, trigger_lvl, info

        return False, trigger_lvl, (
            f"[MA_CROSS MONITOR: Fast({self.fast_ma_samples})={ma_fast:+.2f}$, "
            f"Slow({self.slow_ma_samples})={ma_slow:+.2f}${slope_note}, Min={eff_min:+.2f}$]"
        )

    @classmethod
    def evaluate_mode(
        cls,
        pos_cfg: Dict[str, Any],
        iter_net: float,
        now_sec: float,
        iter_age_hours: float,
        state: Dict[str, Any],
        scale_money: float,
    ) -> Tuple[bool, Optional[float], str]:
        """Точка вызова режима ma_crossover напрямую по auto_closing.positive."""
        closer = cls(pos_cfg["ma_crossover"])
        return closer.evaluate(
            iter_net=iter_net,
            now_sec=now_sec,
            state=state,
            scale_money=scale_money,
            iter_age_hours=iter_age_hours,
        )
