# ============================================================
# Path: CORE/STRATEGY/regime_adaptive_closer.py
# Role: HRON-4 Two-Regime Adaptive AutoCloser (PEACE=MA Crossover / STRESS=Fast Ratchet + Chronos)
# ============================================================

from typing import Any, Dict, Optional, Tuple

from CORE.STRATEGY.ma_crossover_closer import MaCrossoverCloser
from CORE.STRATEGY.profit_closer_common import (
    BucketedProfitHistorySampler,
    ChronosTimeForceEvaluator,
)
from CORE.STRATEGY.trailing_system import BaseTrailingSystem


class RegimeAdaptiveCloser:
    """
    Двухуровневый адаптивный автозакрыватель (HRON-4):
      - PEACE: спокойное сопровождение по пересечению MA (Fast/Slow MA + опциональный slope-фильтр);
      - STRESS: защитное быстрое закрытие (Fibonacci Ratchet + ускоренный Chronos-выход).

    Переключение PEACE -> STRESS срабатывает при глубокой просадке/нагрузке/застое купола
    с гистерезисом на возврат STRESS -> PEACE (минимальное время удержания + разгрузка сетки/маржи).
    Во время STRESS минутные бакеты profit_history продолжают записываться без разрывов.
    """

    def __init__(self, cfg: Dict[str, Any], min_guaranteed_profit_usdt: float = 0.0):
        if not isinstance(cfg, dict):
            raise ValueError("regime_adaptive config must be a dict")
        self.cfg = cfg
        self.min_guaranteed_profit_usdt = float(min_guaranteed_profit_usdt or 0.0)

        sw = cfg["switch"]
        self.stress_level_gte = int(sw["stress_level_gte"])
        self.stress_load_gte = float(sw["stress_load_gte"])
        self.stress_age_hours_gte = float(sw["stress_age_hours_gte"])
        self.stress_age_level_gte = int(sw["stress_age_level_gte"])
        self.min_stress_hold_minutes = float(sw["min_stress_hold_minutes"])
        self.recover_level_lte = int(sw["recover_level_lte"])
        self.recover_load_lte = float(sw["recover_load_lte"])

        self.peace_cfg = cfg["peace"]
        self.peace_closer = MaCrossoverCloser(self.peace_cfg)

        stress = cfg["stress"]
        self.stress_ratchet_cfg = dict(stress["trailing_ratchet"])
        self.stress_chronos_cfg = dict(stress["chronos_exit"])
        self.stress_chronos = ChronosTimeForceEvaluator(self.stress_chronos_cfg)

    def _detect_stress_reason(
        self,
        max_active_level: int,
        load_ratio: float,
        iter_age_hours: float,
    ) -> str:
        if max_active_level >= self.stress_level_gte:
            return f"lvl={max_active_level}>={self.stress_level_gte}"
        if load_ratio >= self.stress_load_gte:
            return f"load={load_ratio:.2f}>={self.stress_load_gte:.2f}"
        if iter_age_hours >= self.stress_age_hours_gte and max_active_level >= self.stress_age_level_gte:
            return (
                f"age={iter_age_hours:.1f}h>={self.stress_age_hours_gte:.1f}h"
                f"&lvl={max_active_level}>={self.stress_age_level_gte}"
            )
        return ""

    def _update_regime(
        self,
        now_sec: float,
        iter_age_hours: float,
        state: Dict[str, Any],
        max_active_level: int,
        load_ratio: float,
    ) -> Tuple[str, str]:
        cur_regime = str(state.get("regime") or "PEACE").upper()
        if cur_regime not in ("PEACE", "STRESS"):
            cur_regime = "PEACE"

        stress_reason = self._detect_stress_reason(
            max_active_level=max_active_level,
            load_ratio=load_ratio,
            iter_age_hours=iter_age_hours,
        )

        if cur_regime == "PEACE":
            if stress_reason:
                state["regime"] = "STRESS"
                state["regime_entered_at_sec"] = float(now_sec)
                state["regime_switch_reason"] = stress_reason
                state.pop("regime_stress_state", None)
                return "STRESS", stress_reason
            state.setdefault("regime", "PEACE")
            return "PEACE", ""

        # Сейчас STRESS: обновляем причину, пока стресс держится; проверяем гистерезис при разгрузке
        if stress_reason:
            state["regime_switch_reason"] = stress_reason
            return "STRESS", stress_reason

        entered_at = float(state.get("regime_entered_at_sec") or 0.0)
        hold_elapsed = max(0.0, float(now_sec) - entered_at)
        if (
            hold_elapsed >= self.min_stress_hold_minutes * 60.0
            and max_active_level <= self.recover_level_lte
            and load_ratio <= self.recover_load_lte
        ):
            state["regime"] = "PEACE"
            state["regime_entered_at_sec"] = float(now_sec)
            state["regime_switch_reason"] = "HYSTERESIS_RECOVERED"
            state.pop("regime_stress_state", None)
            state.pop("armed_floor", None)
            return "PEACE", "HYSTERESIS_RECOVERED"

        hold_reason = str(state.get("regime_switch_reason") or f"HOLD({hold_elapsed / 60.0:.0f}m)")
        return "STRESS", hold_reason

    def evaluate(
        self,
        iter_net: float,
        now_sec: float,
        state: Dict[str, Any],
        scale_money: float,
        iter_age_hours: float = 0.0,
        max_active_level: int = 0,
        load_ratio: float = 0.0,
    ) -> Tuple[bool, Optional[float], str]:
        """Оценивает режим PEACE/STRESS и применяет соответствующую логику автозакрытия."""
        eff_scale = float(scale_money) if scale_money and float(scale_money) > 0.0 else 1.0
        regime, reason = self._update_regime(
            now_sec=now_sec,
            iter_age_hours=iter_age_hours,
            state=state,
            max_active_level=max_active_level,
            load_ratio=load_ratio,
        )

        if regime == "PEACE":
            state["active_level_idx"] = 0
            state["armed_level_desc"] = "REGIME_PEACE_MA"
            ok, trig, info = self.peace_closer.evaluate(
                iter_net=iter_net,
                now_sec=now_sec,
                state=state,
                scale_money=eff_scale,
                iter_age_hours=iter_age_hours,
            )
            if ok and iter_net <= 0.0:
                return False, None, f"[REGIME:PEACE BLOCKED: Net {iter_net:+.2f} <= 0]"
            if ok and trig is not None:
                state["active_level_idx"] = 0
                state["armed_level_desc"] = "REGIME_PEACE_MA_CROSS"
                return True, trig, f"[REGIME:PEACE] {info}"
            return False, trig, (f"[REGIME:PEACE] {info}" if info else "")

        # STRESS regime: непрерывно сэмплируем бакет для MA-истории на случай отката в PEACE
        BucketedProfitHistorySampler.sample_bucket(
            state=state,
            iter_net=iter_net,
            now_sec=now_sec,
            sample_interval_sec=self.peace_closer.sample_interval_sec,
        )

        stress_state = state.setdefault("regime_stress_state", {})
        ch_ok, ch_floor, ch_info = self.stress_chronos.evaluate(
            iter_net=iter_net,
            iter_age_hours=iter_age_hours,
            scale_money=eff_scale,
            state=stress_state,
        )
        if ch_ok and ch_floor is not None:
            if iter_net <= 0.0:
                return False, None, f"[REGIME:STRESS({reason}) CHRONOS BLOCKED: Net {iter_net:+.2f} <= 0]"
            state["active_level_idx"] = -1
            state["armed_level_desc"] = "REGIME_STRESS_CHRONOS"
            return True, ch_floor, f"[REGIME:STRESS({reason})] {ch_info}"

        eff_ratchet_cfg = dict(self.stress_ratchet_cfg)
        eff_ratchet_cfg["scale_money"] = eff_scale
        if "min_guaranteed_profit_usdt" not in eff_ratchet_cfg and self.min_guaranteed_profit_usdt > 0.0:
            eff_ratchet_cfg["min_guaranteed_profit_usdt"] = self.min_guaranteed_profit_usdt

        r_ok, r_floor, r_info, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=iter_net,
            iter_age_hours=iter_age_hours,
            ratchet_cfg=eff_ratchet_cfg,
            state=stress_state,
        )
        if "active_level_idx" in stress_state:
            state["active_level_idx"] = int(stress_state["active_level_idx"])
        if "armed_level_desc" in stress_state:
            state["armed_level_desc"] = f"STRESS:{stress_state['armed_level_desc']}"
        else:
            state["armed_level_desc"] = "REGIME_STRESS_RATCHET"

        if r_ok and r_floor is not None:
            if iter_net <= 0.0:
                return False, None, f"[REGIME:STRESS({reason}) RATCHET BLOCKED: Net {iter_net:+.2f} <= 0]"
            return True, r_floor, f"[REGIME:STRESS({reason})] {r_info}"
        return False, r_floor, (f"[REGIME:STRESS({reason})] {r_info}" if r_info else "")

    @classmethod
    def evaluate_mode(
        cls,
        pos_cfg: Dict[str, Any],
        iter_net: float,
        now_sec: float,
        iter_age_hours: float,
        state: Dict[str, Any],
        scale_money: float,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[float], str]:
        """Точка вызова режима regime_adaptive по auto_closing.positive."""
        ctx = extra_context or {}
        max_lvl = int(ctx.get("max_active_level") or 0)
        load_ratio = float(ctx.get("load_ratio") or 0.0)
        min_guard = float(pos_cfg.get("min_guaranteed_profit_usdt") or 0.0)
        closer = cls(pos_cfg["regime_adaptive"], min_guaranteed_profit_usdt=min_guard)
        return closer.evaluate(
            iter_net=iter_net,
            now_sec=now_sec,
            state=state,
            scale_money=scale_money,
            iter_age_hours=iter_age_hours,
            max_active_level=max_lvl,
            load_ratio=load_ratio,
        )

    @classmethod
    def compute_corridor_bounds(
        cls,
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
        """
        Вычисляет точный O(1) коридор [eq_low, eq_high, max_valid_t, tracks_peak]
        для ускоренного сканера бэктеста без потери бит-в-бит идентичности.
        """
        ra_cfg = pos_cfg.get("regime_adaptive")
        if not ra_cfg:
            return cutoff_wake, float("inf"), float("inf"), False
        if iter_start_time <= 0.0:
            return float("inf"), float("-inf"), 0.0, False

        min_guard = float(pos_cfg.get("min_guaranteed_profit_usdt") or 0.0)
        closer = cls(ra_cfg, min_guaranteed_profit_usdt=min_guard)
        eff_scale = float(scale_money) if scale_money and float(scale_money) > 0.0 else 1.0

        ctx = extra_context or {}
        max_lvl = int(ctx.get("max_active_level") or 0)
        load_ratio = float(ctx.get("load_ratio") or 0.0)
        iter_age_hours = (current_sim_time - iter_start_time) / 3600.0

        stored_regime = str(state.get("regime") or "PEACE").upper()
        if stored_regime not in ("PEACE", "STRESS"):
            stored_regime = "PEACE"

        stress_reason_now = closer._detect_stress_reason(
            max_active_level=max_lvl,
            load_ratio=load_ratio,
            iter_age_hours=iter_age_hours,
        )

        # Пробуждаемся немедленно, если текущий тик должен сменить режим state["regime"]
        if stored_regime == "PEACE" and stress_reason_now:
            return float("inf"), float("-inf"), 0.0, False

        entered_at = float(state.get("regime_entered_at_sec") or 0.0)
        hold_elapsed = max(0.0, current_sim_time - entered_at)
        if (
            stored_regime == "STRESS"
            and not stress_reason_now
            and hold_elapsed >= closer.min_stress_hold_minutes * 60.0
            and max_lvl <= closer.recover_level_lte
            and load_ratio <= closer.recover_load_lte
        ):
            return float("inf"), float("-inf"), 0.0, False

        last_bucket = state.get("last_sample_bucket")
        if last_bucket is None:
            return float("inf"), float("-inf"), 0.0, False
        sample_sec = closer.peace_closer.sample_interval_sec
        next_bucket_ts = float((int(last_bucket) + 1) * sample_sec)

        if stored_regime == "PEACE":
            force_wake, ch_eq_low, ch_eq_high, ch_max_t = ChronosTimeForceEvaluator.compute_corridor_bounds(
                chronos_cfg=closer.peace_cfg["chronos_exit"],
                cur_equity=cur_equity,
                iter_start_equity=iter_start_equity,
                current_sim_time=current_sim_time,
                iter_start_time=iter_start_time,
                scale_money=eff_scale,
                state=state,
            )
            if force_wake:
                return float("inf"), float("-inf"), 0.0, False

            t_age_stress = float("inf")
            if max_lvl >= closer.stress_age_level_gte and iter_age_hours < closer.stress_age_hours_gte:
                t_age_stress = iter_start_time + closer.stress_age_hours_gte * 3600.0 - 0.001
                if t_age_stress <= current_sim_time:
                    return float("inf"), float("-inf"), 0.0, False

            eq_low = max(cutoff_wake, ch_eq_low)
            eq_high = ch_eq_high
            max_valid_t = min(next_bucket_ts, ch_max_t, t_age_stress)
            if max_valid_t <= current_sim_time or eq_low >= cur_equity or cur_equity >= eq_high:
                return float("inf"), float("-inf"), 0.0, False
            return eq_low, eq_high, max_valid_t, False

        # STRESS regime corridor
        stress_state = state.get("regime_stress_state") or {}
        force_wake, ch_eq_low, ch_eq_high, ch_max_t = ChronosTimeForceEvaluator.compute_corridor_bounds(
            chronos_cfg=closer.stress_chronos_cfg,
            cur_equity=cur_equity,
            iter_start_equity=iter_start_equity,
            current_sim_time=current_sim_time,
            iter_start_time=iter_start_time,
            scale_money=eff_scale,
            state=stress_state,
        )
        if force_wake:
            return float("inf"), float("-inf"), 0.0, False

        eff_ratchet_cfg = dict(closer.stress_ratchet_cfg)
        if "min_guaranteed_profit_usdt" not in eff_ratchet_cfg and min_guard > 0.0:
            eff_ratchet_cfg["min_guaranteed_profit_usdt"] = min_guard

        r_low, r_high, r_max_t, r_tracks_peak = BaseTrailingSystem.compute_ratchet_corridor_bounds(
            ratchet_cfg=eff_ratchet_cfg,
            cur_equity=cur_equity,
            iter_start_equity=iter_start_equity,
            current_sim_time=current_sim_time,
            iter_start_time=iter_start_time,
            scale_money=eff_scale,
            ratchet_state=stress_state,
            cutoff_wake=cutoff_wake,
        )
        if r_low >= cur_equity or cur_equity >= r_high:
            return float("inf"), float("-inf"), 0.0, False

        t_recover = float("inf")
        if (
            not stress_reason_now
            and max_lvl <= closer.recover_level_lte
            and load_ratio <= closer.recover_load_lte
        ):
            t_recover = entered_at + closer.min_stress_hold_minutes * 60.0 - 0.001
            if t_recover <= current_sim_time:
                return float("inf"), float("-inf"), 0.0, False

        eq_low = max(cutoff_wake, ch_eq_low, r_low)
        eq_high = min(ch_eq_high, r_high)
        max_valid_t = min(next_bucket_ts, ch_max_t, r_max_t, t_recover)
        if max_valid_t <= current_sim_time or eq_low >= cur_equity or cur_equity >= eq_high:
            return float("inf"), float("-inf"), 0.0, False
        return eq_low, eq_high, max_valid_t, r_tracks_peak

    @staticmethod
    def extract_regime_context(
        fsm_states: Any,
        positions: Any = None,
        analytics_dict: Optional[Dict[str, Any]] = None,
        strat_cfg: Any = None,
        allowed_symbols: Optional[set] = None,
        initial_balance: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Унифицированный расчёт (max_active_level, load_ratio) для продового AutoCloser и бэктеста.
        Поддерживает два источника: PositionState (cur_step) и рыночные позиции/аналитику.
        """
        max_active_level = 0
        if isinstance(fsm_states, dict):
            for sym_k, sym_sides in fsm_states.items():
                if allowed_symbols is not None and str(sym_k).upper() not in allowed_symbols:
                    continue
                if isinstance(sym_sides, dict):
                    sides_iter = sym_sides.values()
                else:
                    sides_iter = (sym_sides,)
                for st in sides_iter:
                    if st is None or not getattr(st, "in_position", False):
                        continue
                    step_val = int(getattr(st, "cur_step", 0) or 0)
                    if step_val > max_active_level:
                        max_active_level = step_val

        total_notional = 0.0
        if isinstance(positions, dict) and positions:
            for p_key, p_val in positions.items():
                if allowed_symbols is not None:
                    sym_name = p_key[0] if isinstance(p_key, tuple) and p_key else getattr(p_val, "symbol", "")
                    if str(sym_name).upper() not in allowed_symbols:
                        continue
                qty = float(getattr(p_val, "qty", 0.0) or 0.0)
                entry_p = float(getattr(p_val, "entry_price", 0.0) or 0.0)
                if qty > 0.0 and entry_p > 0.0:
                    total_notional += qty * entry_p

        if total_notional <= 0.0 and isinstance(analytics_dict, dict):
            per_coin = analytics_dict.get("per_coin")
            if isinstance(per_coin, dict):
                for c_sym, c_data in per_coin.items():
                    if allowed_symbols is not None and str(c_sym).upper() not in allowed_symbols:
                        continue
                    if isinstance(c_data, dict):
                        c_load = float(c_data.get("current_grid_load_usdt", 0.0) or 0.0)
                        if c_load <= 0.0:
                            c_load = float(c_data.get("total_notional_usdt", 0.0) or 0.0)
                        if c_load <= 0.0:
                            c_load = float(c_data.get("current_margin_usdt", 0.0) or 0.0) * 20.0
                        total_notional += c_load

        base_bal = float(initial_balance or 0.0)
        if base_bal <= 0.0:
            base_bal = float(getattr(strat_cfg, "start_balance_usdt", 0.0) or 0.0)
        if base_bal <= 0.0 and isinstance(analytics_dict, dict):
            base_bal = float(analytics_dict.get("start_balance_usdt", 0.0) or 0.0)

        load_ratio = (total_notional / base_bal) if base_bal > 0.0 else 0.0
        return {
            "max_active_level": max_active_level,
            "load_ratio": load_ratio,
        }
