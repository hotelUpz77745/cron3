# ==============================================================================
# Path: CORE/STRATEGY/smoothed_profit_closer.py
# Role: Автономные модули автозакрытия купола по сглаженному откату MA от пика профита (smoothed_drawdown, ratchet_smoothed_drawdown)
# ==============================================================================

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from CORE.STRATEGY.profit_closer_common import (
    BucketedProfitHistorySampler,
    ChronosTimeForceEvaluator,
)


class SmoothedProfitDrawdownCloser:
    """
    Чистый оценщик автозакрытия портфеля/купола по сглаженной скользящей средней (MA)
    при откате от исторического пика итерации:
    - Сэмплирует прибыль строго по закрытию бакета sample_interval_sec (например, 60с = 1 мин);
    - Поддерживает 2 режима:
      1) smoothed_drawdown: фиксированный порог min_profit_usdt * scale_money;
      2) ratchet_smoothed_drawdown: плавающий порог от карты уровней trailing_ratchet.levels (Вариант А);
    - Дополнительно поддерживает отключаемое временное форсирование выхода chronos_exit (E1).
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.sample_interval_sec: int = int(cfg["sample_interval_sec"])
        self.min_samples: int = int(cfg["min_samples"])
        self.ma_window_samples: int = int(cfg["ma_window_samples"])
        self.max_drawdown_ratio: float = float(cfg["max_drawdown_ratio"])
        self.min_profit_usdt: Optional[float] = (
            float(cfg["min_profit_usdt"]) if "min_profit_usdt" in cfg and cfg["min_profit_usdt"] is not None else None
        )
        self.chronos: ChronosTimeForceEvaluator = ChronosTimeForceEvaluator(cfg["chronos_exit"])

        if self.sample_interval_sec <= 0:
            raise ValueError(f"sample_interval_sec must be > 0, got {self.sample_interval_sec}")
        if self.min_samples <= 0 or self.ma_window_samples <= 0 or self.min_samples < self.ma_window_samples:
            raise ValueError(
                f"Invalid min_samples={self.min_samples} / ma_window_samples={self.ma_window_samples}"
            )
        if not (0.0 < self.max_drawdown_ratio < 1.0):
            raise ValueError(f"max_drawdown_ratio must be in (0, 1), got {self.max_drawdown_ratio}")

    @staticmethod
    def _ensure_sample(
        state: Dict[str, Any],
        iter_net: float,
        now_sec: float,
        sample_interval_sec: int,
    ) -> Tuple[bool, List[float], float]:
        """Делегирует в единый BucketedProfitHistorySampler без дублирования."""
        return BucketedProfitHistorySampler.sample_bucket(
            state=state,
            iter_net=iter_net,
            now_sec=now_sec,
            sample_interval_sec=sample_interval_sec,
        )

    @staticmethod
    def resolve_ratchet_min_profit(
        peak_profit: float,
        iter_age_hours: float,
        levels: List[Dict[str, Any]],
        scale_money: float,
    ) -> Tuple[Optional[float], int, str]:
        """
        Вариант А (Подход 2B без курков):
        - Среди активных Fibo-ступеней (enabled=True, min_age_hours == 0 или <= 0), чей порог
          activation * scale_money <= peak_profit, выбирается максимальный floor * scale_money.
        - Временные уровни Chronos (min_age_hours > 0) управляются строго через chronos_exit.
        - Если ни один уровень еще не достигнут, ориентир равен минимальному активному activation * scale_money.
        Возвращает (eff_min_profit_usdt, level_idx, level_desc).
        """
        active: List[Tuple[int, float, float, str]] = []
        for idx, lvl in enumerate(levels):
            if not bool(lvl["enabled"]):
                continue
            min_age = float(lvl["min_age_hours"]) if lvl["min_age_hours"] is not None else 0.0
            if min_age > 0.0:
                continue
            if iter_age_hours < min_age:
                continue
            act_val = float(lvl["activation"]) * scale_money
            flr_val = float(lvl["floor"]) * scale_money
            desc = str(lvl["_desc"]) if "_desc" in lvl and lvl["_desc"] else f"L{idx}"
            active.append((idx, act_val, flr_val, desc))

        if not active:
            return None, 0, "NONE"

        reached = [item for item in active if peak_profit >= item[1]]
        if reached:
            best = max(reached, key=lambda item: (item[2], item[0]))
            return best[2], best[0], best[3]

        lowest_target = min(active, key=lambda item: (item[1], item[0]))
        return lowest_target[1], lowest_target[0], lowest_target[3]

    @classmethod
    def should_take_profit(
        cls,
        profit_history: List[float],
        min_samples: int,
        ma_window_samples: int,
        min_profit_usdt: float,
        max_drawdown_ratio: float,
        peak_profit: Optional[float] = None,
    ) -> Tuple[bool, Optional[float], float, float]:
        """
        Чистая математическая функция решения о фиксации цикла:
        Возвращает (should_close, trigger_level, ma_val, peak_val).
        """
        if len(profit_history) < min_samples:
            ma_val = sum(profit_history[-ma_window_samples:]) / float(min(len(profit_history), ma_window_samples)) if profit_history else 0.0
            pk = float(peak_profit if peak_profit is not None else (max(profit_history) if profit_history else 0.0))
            return False, None, ma_val, pk

        ma_val = sum(profit_history[-ma_window_samples:]) / float(ma_window_samples)
        if ma_val <= min_profit_usdt:
            pk = float(peak_profit if peak_profit is not None else max(profit_history))
            return False, None, ma_val, pk

        pk = float(peak_profit if peak_profit is not None else max(profit_history))
        if pk <= min_profit_usdt:
            return False, None, ma_val, pk

        trigger_level = pk * (1.0 - max_drawdown_ratio)
        return (ma_val <= trigger_level), trigger_level, ma_val, pk

    def evaluate(
        self,
        iter_net: float,
        now_sec: float,
        state: Dict[str, Any],
        scale_money: float,
        iter_age_hours: float = 0.0,
        ratchet_levels: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, Optional[float], str]:
        """
        Выполняет 1-минутное сэмплирование и оценку режима smoothed_drawdown / ratchet_smoothed_drawdown.
        Возвращает (should_close, eff_min_or_trigger, info).
        """
        ch_ok, ch_floor, ch_info = self.chronos.evaluate(
            iter_net=iter_net,
            iter_age_hours=iter_age_hours,
            scale_money=scale_money,
            state=state,
        )
        if ch_ok and ch_floor is not None:
            return True, ch_floor, ch_info

        is_new, history, peak = self._ensure_sample(
            state=state,
            iter_net=iter_net,
            now_sec=now_sec,
            sample_interval_sec=self.sample_interval_sec,
        )
        if not is_new:
            return False, None, ""

        lvl_idx = 0
        lvl_desc = "FIXED_MIN"
        if ratchet_levels is not None:
            eff_min, lvl_idx, lvl_desc = self.resolve_ratchet_min_profit(
                peak_profit=peak,
                iter_age_hours=iter_age_hours,
                levels=ratchet_levels,
                scale_money=scale_money,
            )
            if eff_min is None:
                return False, None, "[RATCHET_SMOOTH_DD: no active levels for current age]"
        else:
            if self.min_profit_usdt is None:
                raise KeyError("smoothed_drawdown config must define min_profit_usdt")
            eff_min = float(self.min_profit_usdt) * scale_money

        state["active_level_idx"] = lvl_idx
        state["armed_level_desc"] = lvl_desc
        state["eff_min_profit"] = eff_min

        triggered, trigger_lvl, ma_val, pk = self.should_take_profit(
            profit_history=history,
            min_samples=self.min_samples,
            ma_window_samples=self.ma_window_samples,
            min_profit_usdt=eff_min,
            max_drawdown_ratio=self.max_drawdown_ratio,
            peak_profit=peak,
        )

        if len(history) < self.min_samples:
            return False, None, f"[SMOOTH_DD WARMUP: {len(history)}/{self.min_samples} samples, Peak={pk:+.2f}$]"

        if triggered and trigger_lvl is not None:
            info = (
                f"[SMOOTH_DD TRIGGER '{lvl_desc}': MA{self.ma_window_samples}={ma_val:+.2f}$ <= "
                f"Trigger={trigger_lvl:+.2f}$ (Peak={pk:+.2f}$, Min={eff_min:+.2f}$, DD={self.max_drawdown_ratio:.0%}, Age={iter_age_hours:.1f}h)]"
            )
            return True, trigger_lvl, info

        return False, trigger_lvl, (
            f"[SMOOTH_DD MONITOR '{lvl_desc}': MA{self.ma_window_samples}={ma_val:+.2f}$, "
            f"Peak={pk:+.2f}$, Min={eff_min:+.2f}$, Trig={trigger_lvl if trigger_lvl is not None else 0.0:+.2f}$]"
        )

    @classmethod
    def evaluate_mode(
        cls,
        mode: str,
        pos_cfg: Dict[str, Any],
        iter_net: float,
        now_sec: float,
        iter_age_hours: float,
        state: Dict[str, Any],
        scale_money: float,
    ) -> Tuple[bool, Optional[float], str]:
        """Единая точка вызова для режимов smoothed_drawdown и ratchet_smoothed_drawdown."""
        mode_norm = str(mode).lower().strip()
        if mode_norm == "smoothed_drawdown":
            closer = cls(pos_cfg["smoothed_drawdown"])
            return closer.evaluate(
                iter_net=iter_net,
                now_sec=now_sec,
                state=state,
                scale_money=scale_money,
                iter_age_hours=iter_age_hours,
                ratchet_levels=None,
            )
        if mode_norm == "ratchet_smoothed_drawdown":
            closer = cls(pos_cfg["ratchet_smoothed_drawdown"])
            levels = pos_cfg["trailing_ratchet"]["levels"]
            return closer.evaluate(
                iter_net=iter_net,
                now_sec=now_sec,
                state=state,
                scale_money=scale_money,
                iter_age_hours=iter_age_hours,
                ratchet_levels=levels,
            )
        raise ValueError(f"Unsupported smoothed closer mode: {mode}")

    @staticmethod
    def resolve_sample_interval_sec(mode: str, pos_cfg: Dict[str, Any]) -> int:
        """Возвращает шаг дискретизации sample_interval_sec строго из конфига режима."""
        return BucketedProfitHistorySampler.resolve_sample_interval_sec(mode, pos_cfg)

