# ==============================================================================
# Path: CORE/STRATEGY/profit_closer_common.py
# Role: Общие компоненты бакетного сэмплирования прибыли и временного форсирования выхода (Chronos E1)
# ==============================================================================

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class BucketedProfitHistorySampler:
    """Единый сэмплер истории прибыли строго по границам временных бакетов sample_interval_sec."""

    @staticmethod
    def sample_bucket(
        state: Dict[str, Any],
        iter_net: float,
        now_sec: float,
        sample_interval_sec: int,
    ) -> Tuple[bool, List[float], float]:
        """
        Добавляет новый сэмпл ровно один раз на интервал sample_interval_sec.
        Возвращает (is_new_sample, profit_history, peak_profit).
        """
        if sample_interval_sec <= 0:
            raise ValueError(f"sample_interval_sec must be > 0, got {sample_interval_sec}")

        history: List[float] = state.setdefault("profit_history", [])
        last_bucket = state.get("last_sample_bucket")
        cur_bucket = int(now_sec // sample_interval_sec)

        if last_bucket is not None and cur_bucket <= int(last_bucket):
            peak = float(state["peak_profit"]) if (history and "peak_profit" in state) else 0.0
            return False, history, peak

        val = float(iter_net)
        history.append(val)
        state["last_sample_bucket"] = cur_bucket
        if "peak_profit" not in state or len(history) == 1:
            peak = val
        else:
            prev_peak = float(state["peak_profit"])
            peak = val if val > prev_peak else prev_peak
        state["peak_profit"] = peak
        state["highest_net"] = peak if peak > 0.0 else 0.0
        return True, history, peak

    @staticmethod
    def resolve_sample_interval_sec(mode: str, pos_cfg: Dict[str, Any]) -> int:
        """Возвращает шаг дискретизации sample_interval_sec строго из конфига режима."""
        mode_norm = str(mode).lower().strip()
        if mode_norm == "regime_adaptive":
            interval = int(pos_cfg["regime_adaptive"]["peace"]["sample_interval_sec"])
        else:
            interval = int(pos_cfg[mode_norm]["sample_interval_sec"])
        if interval <= 0:
            raise ValueError(f"sample_interval_sec must be > 0, got {interval}")
        return interval


class ChronosTimeForceEvaluator:
    """
    Отключаемый модуль временной эвакуации зависшего купола (аналог E1 Chronos в trailing_ratchet):
    при достижении возраста итерации >= min_age_hours и чистой прибыли >= activation * scale_money
    активируется/мгновенно срабатывает защитный пол floor * scale_money.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.enabled: bool = bool(cfg["enabled"])
        self.activation: float = float(cfg["activation"])
        self.floor: float = float(cfg["floor"])
        self.min_age_hours: float = float(cfg["min_age_hours"])
        self.desc: str = str(cfg["_desc"]) if "_desc" in cfg and cfg["_desc"] else "E1: Chronos"

        if self.activation <= 0.0 or self.floor <= 0.0:
            raise ValueError(f"Chronos activation/floor must be > 0, got act={self.activation}, floor={self.floor}")
        if self.min_age_hours < 0.0:
            raise ValueError(f"Chronos min_age_hours must be >= 0, got {self.min_age_hours}")

    def evaluate(
        self,
        iter_net: float,
        iter_age_hours: float,
        scale_money: float,
        state: Dict[str, Any],
    ) -> Tuple[bool, Optional[float], str]:
        """Оценивает форсированный выход по возрасту итерации (не зависит от бакета MA)."""
        if not self.enabled:
            return False, None, ""
        if iter_age_hours < self.min_age_hours:
            return False, None, ""

        act_val = self.activation * scale_money
        floor_val = self.floor * scale_money

        if iter_net <= 0.0:
            state.pop("chronos_armed_floor", None)
            return False, None, ""

        if iter_net >= act_val:
            prev_floor = state.get("chronos_armed_floor")
            if prev_floor is None or floor_val > float(prev_floor):
                state["chronos_armed_floor"] = floor_val
                state["active_level_idx"] = -1
                state["armed_level_desc"] = self.desc

            if act_val <= floor_val:
                state["highest_net"] = max(float(state.get("highest_net", 0.0) or 0.0), float(iter_net))
                info = (
                    f"[CHRONOS INSTANT '{self.desc}': Net {iter_net:+.2f}$ >= Act {act_val:.2f}$ "
                    f"(Age={iter_age_hours:.1f}h >= {self.min_age_hours:.1f}h)]"
                )
                return True, floor_val, info

        armed = state.get("chronos_armed_floor")
        if armed is not None and iter_net <= float(armed):
            armed_f = float(armed)
            state["highest_net"] = max(float(state.get("highest_net", 0.0) or 0.0), float(iter_net))
            info = (
                f"[CHRONOS PULLBACK '{self.desc}': Net {iter_net:+.2f}$ <= Floor {armed_f:.2f}$ "
                f"(Age={iter_age_hours:.1f}h >= {self.min_age_hours:.1f}h)]"
            )
            return True, armed_f, info

        return False, None, ""

    @classmethod
    def compute_corridor_bounds(
        cls,
        chronos_cfg: Dict[str, Any],
        cur_equity: float,
        iter_start_equity: float,
        current_sim_time: float,
        iter_start_time: float,
        scale_money: float,
        state: Dict[str, Any],
    ) -> Tuple[bool, float, float, float]:
        """
        Возвращает границы эквити/времени для O(1) сканера коридоров в бэктесте:
        (force_wake_now, eq_low_limit, eq_high_limit, max_valid_t).
        """
        evaluator = cls(chronos_cfg)
        if not evaluator.enabled or iter_start_time <= 0.0:
            return False, float("-inf"), float("inf"), float("inf")

        iter_age_hours = (current_sim_time - iter_start_time) / 3600.0
        if iter_age_hours < evaluator.min_age_hours:
            wake_t = iter_start_time + evaluator.min_age_hours * 3600.0 - 0.001
            if wake_t <= current_sim_time:
                return True, float("-inf"), float("inf"), 0.0
            return False, float("-inf"), float("inf"), wake_t

        iter_net = cur_equity - iter_start_equity
        act_val = evaluator.activation * scale_money
        floor_val = evaluator.floor * scale_money
        armed = state.get("chronos_armed_floor")

        if iter_net <= 0.0:
            if armed is not None:
                return True, float("-inf"), float("inf"), 0.0
            eq_high = iter_start_equity + max(0.0, act_val) - 1e-6
            return (cur_equity >= eq_high), float("-inf"), eq_high, float("inf")

        if iter_net >= act_val and (armed is None or act_val <= floor_val):
            return True, float("-inf"), float("inf"), 0.0

        eq_low = float("-inf")
        eq_high = iter_start_equity + act_val - 1e-6
        if armed is not None:
            armed_f = float(armed)
            if iter_net <= armed_f:
                return True, float("-inf"), float("inf"), 0.0
            eq_low = max(eq_low, iter_start_equity + max(0.0, armed_f) + 1e-6)
            if floor_val <= armed_f:
                eq_high = float("inf")

        if eq_low >= cur_equity or cur_equity >= eq_high:
            return True, float("-inf"), float("inf"), 0.0
        return False, eq_low, eq_high, float("inf")


class BucketedCloserRouter:
    """Единая точка маршрутизации для бакетных положительных модов автозакрытия."""

    SUPPORTED_MODES: Tuple[str, ...] = (
        "smoothed_drawdown",
        "ratchet_smoothed_drawdown",
        "ma_crossover",
        "regime_adaptive",
    )

    @classmethod
    def is_supported(cls, mode: str) -> bool:
        return str(mode).lower().strip() in cls.SUPPORTED_MODES

    @classmethod
    def evaluate(
        cls,
        mode: str,
        pos_cfg: Dict[str, Any],
        iter_net: float,
        now_sec: float,
        iter_age_hours: float,
        state: Dict[str, Any],
        scale_money: float,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[float], str]:
        mode_norm = str(mode).lower().strip()
        if mode_norm == "regime_adaptive":
            from CORE.STRATEGY.regime_adaptive_closer import RegimeAdaptiveCloser

            return RegimeAdaptiveCloser.evaluate_mode(
                pos_cfg=pos_cfg,
                iter_net=iter_net,
                now_sec=now_sec,
                iter_age_hours=iter_age_hours,
                state=state,
                scale_money=scale_money,
                extra_context=extra_context,
            )
        if mode_norm == "ma_crossover":
            from CORE.STRATEGY.ma_crossover_closer import MaCrossoverCloser

            return MaCrossoverCloser.evaluate_mode(
                pos_cfg=pos_cfg,
                iter_net=iter_net,
                now_sec=now_sec,
                iter_age_hours=iter_age_hours,
                state=state,
                scale_money=scale_money,
            )
        if mode_norm in ("smoothed_drawdown", "ratchet_smoothed_drawdown"):
            from CORE.STRATEGY.smoothed_profit_closer import SmoothedProfitDrawdownCloser

            return SmoothedProfitDrawdownCloser.evaluate_mode(
                mode=mode_norm,
                pos_cfg=pos_cfg,
                iter_net=iter_net,
                now_sec=now_sec,
                iter_age_hours=iter_age_hours,
                state=state,
                scale_money=scale_money,
            )
        raise ValueError(f"Unsupported bucketed closer mode: {mode}")

    @classmethod
    def get_corridor(
        cls,
        mode: str,
        pos_cfg: Dict[str, Any],
        cur_equity: float,
        iter_start_equity: float,
        current_sim_time: float,
        iter_start_time: float,
        state: Dict[str, Any],
        cutoff_wake: float,
        scale_money: float,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[float, float, float, bool]:
        """Единый расчёт [eq_low, eq_high, max_valid_t, tracks_peak] с учётом бакета и chronos_exit."""
        mode_norm = str(mode).lower().strip()
        if mode_norm == "regime_adaptive":
            from CORE.STRATEGY.regime_adaptive_closer import RegimeAdaptiveCloser

            return RegimeAdaptiveCloser.compute_corridor_bounds(
                pos_cfg=pos_cfg,
                cur_equity=cur_equity,
                iter_start_equity=iter_start_equity,
                current_sim_time=current_sim_time,
                iter_start_time=iter_start_time,
                state=state,
                cutoff_wake=cutoff_wake,
                scale_money=scale_money,
                extra_context=extra_context,
            )
        mode_cfg = pos_cfg.get(mode_norm)
        if not mode_cfg or not mode_cfg.get("enabled", True):
            return cutoff_wake, float("inf"), float("inf"), False
        if mode_norm == "ratchet_smoothed_drawdown" and not (pos_cfg.get("trailing_ratchet") or {}).get("levels"):
            return cutoff_wake, float("inf"), float("inf"), False
        if iter_start_time <= 0.0:
            return float("inf"), float("-inf"), 0.0, False

        force_wake, ch_eq_low, ch_eq_high, ch_max_t = ChronosTimeForceEvaluator.compute_corridor_bounds(
            chronos_cfg=mode_cfg["chronos_exit"],
            cur_equity=cur_equity,
            iter_start_equity=iter_start_equity,
            current_sim_time=current_sim_time,
            iter_start_time=iter_start_time,
            scale_money=scale_money,
            state=state,
        )
        if force_wake:
            return float("inf"), float("-inf"), 0.0, False

        sample_sec = BucketedProfitHistorySampler.resolve_sample_interval_sec(mode_norm, pos_cfg)
        last_bucket = state.get("last_sample_bucket")
        if last_bucket is None:
            return float("inf"), float("-inf"), 0.0, False

        next_bucket_ts = float((int(last_bucket) + 1) * sample_sec)
        eq_low = max(cutoff_wake, ch_eq_low)
        eq_high = ch_eq_high
        max_valid_t = min(next_bucket_ts, ch_max_t)
        if max_valid_t <= current_sim_time or eq_low >= cur_equity or cur_equity >= eq_high:
            return float("inf"), float("-inf"), 0.0, False
        return eq_low, eq_high, max_valid_t, False
