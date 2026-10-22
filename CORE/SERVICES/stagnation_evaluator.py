# ============================================================
# FILE: CORE/SERVICES/stagnation_evaluator.py
# ROLE: Evaluates dynamic targets against stagnation curves and velocity rules
# ============================================================

import time
from typing import Any, Optional, Tuple

from c_log import UnifiedLogger

logger = UnifiedLogger("StagnationEvaluator")


class StagnationEvaluator:
    """
    Калькулятор динамических порогов прибыли по картам стагнации:
    - Градиентный каскад по таймфреймам (1h -> 6h -> 12h -> 24h)
    - Легаси режим: однородная карта со статическим min_floor
    - Сравнение условий скорости (velocity, $/h) и времени (peak_age, h)
    """

    last_matched_rule: str = ""

    @classmethod
    def match_rule_adaptive(cls, rule: str, eval_val: float, ref_target: float) -> Tuple[bool, float]:
        """
        Парсит строковое правило (напр. '>=0.5', '<4.0', '==2.5', '>=50%')
        и сопоставляет с вычисленным значением eval_val.
        """
        rule = rule.strip()
        is_pct = rule.endswith("%")
        clean_rule = rule[:-1].strip() if is_pct else rule

        op = "=="
        raw_num = clean_rule
        for candidate_op in ("<=", ">=", "==", "<", ">"):
            if clean_rule.startswith(candidate_op):
                op = candidate_op
                raw_num = clean_rule[len(candidate_op):].strip()
                break

        try:
            val_num = float(raw_num)
            threshold_val = (val_num / 100.0 * ref_target) if is_pct else val_num
        except ValueError:
            logger.warning(f"[STAGNATION_EVALUATOR] Invalid rule format: '{rule}'")
            return False, 0.0

        ops = {
            "<=": eval_val <= threshold_val,
            ">=": eval_val >= threshold_val,
            "==": abs(eval_val - threshold_val) < 1e-4,
            "<": eval_val < threshold_val,
            ">": eval_val > threshold_val,
        }
        return ops.get(op, False), threshold_val

    @classmethod
    def match_rule_float(cls, rule: str, val: float, ref_target: float = 1.0) -> bool:
        """Упрощенная проверка правила без возврата вычисленного порога."""
        matched, _ = cls.match_rule_adaptive(rule, val, ref_target)
        return matched

    @staticmethod
    def parse_card_target(raw_target: Any) -> Tuple[Optional[float], bool]:
        """Парсит цель из значения карты стагнации (число или словарь с enabled/target)."""
        if raw_target is None or raw_target is False:
            return None, False
        if isinstance(raw_target, (int, float)):
            return float(raw_target), True
        if isinstance(raw_target, dict) and raw_target.get("enabled", True):
            for k in ("target", "threshold", "val", "value"):
                if k in raw_target and raw_target[k] is not None:
                    return float(raw_target[k]), True
        return None, False

    @classmethod
    def evaluate_stagnation_target(
        cls,
        analytics_dict: dict,
        pos_cfg: dict
    ) -> Tuple[Optional[float], bool, str, float]:
        """
        Вычисляет динамический целевой порог по карте стагнации (stagnation_card).
        Возвращает (effective_threshold, is_stagnation_active, stag_info, reference_top_target).
        """
        if not pos_cfg.get("enabled", True):
            return None, False, "", 0.0

        base_th = pos_cfg.get("threshold")
        if base_th == 0 or base_th == 0.0:
            base_th = None

        if pos_cfg.get("stagnation_enabled") is False:
            return base_th, False, "", (base_th or 0.0)

        stag_cfg = pos_cfg.get("stagnation")
        if stag_cfg is None:
            stag_cfg = pos_cfg if ("stagnation_card" in pos_cfg or "card" in pos_cfg or "timeframes" in pos_cfg) else {}

        if not stag_cfg or not stag_cfg.get("enabled", True):
            return base_th, False, "", (base_th or 0.0)

        now_ms = int(time.time() * 1000)
        iter_net = float(analytics_dict.get("iteration_net_profit_usdt", analytics_dict.get("net_profit_usdt", 0.0)))
        peak_age_h = float(analytics_dict.get("iteration_peak_age_hours", analytics_dict.get("peak_age_hours", 0.0)))
        velocity = float(analytics_dict.get("iteration_net_velocity", analytics_dict.get("net_velocity", 0.0)))
        if "iteration_net_velocity" not in analytics_dict and "net_velocity" not in analytics_dict:
            first_ts = analytics_dict.get("iteration_start_ts", analytics_dict.get("first_trade_ts", now_ms))
            active_h = max(0.05, (now_ms - first_ts) / 3600000.0)
            velocity = round(iter_net / active_h, 2)

        # ------------------------------------------------------------------
        # 1. РЕЖИМ ГРАДИЕНТНОГО КАСКАДА ПО ТАЙМФРЕЙМАМ (1h -> 6h -> 12h -> 24h)
        # ------------------------------------------------------------------
        tf_dict = stag_cfg.get("timeframes") or pos_cfg.get("timeframes")
        if tf_dict and isinstance(tf_dict, dict):
            if iter_net <= 0.0:
                return None, False, "Net <= 0", 0.0

            first_ts = analytics_dict.get("iteration_start_ts", analytics_dict.get("first_trade_ts", now_ms))
            iter_age_hours = max(0.0, (now_ms - first_ts) / 3600000.0)
            eval_val = velocity if stag_cfg.get("mode", "velocity") == "velocity" else peak_age_h

            tf_keys = list(tf_dict.keys())
            priority_order = ["1h", "6h", "12h", "24h"]
            sorted_keys = [k for k in priority_order if k in tf_dict] + [k for k in tf_keys if k not in priority_order]

            matched_any_target = None
            matched_any_info = ""

            for tf_key in sorted_keys:
                tf_cfg = tf_dict[tf_key]
                if not isinstance(tf_cfg, dict) or not tf_cfg.get("enabled", True):
                    continue
                min_age = float(tf_cfg.get("min_age_hours", 0.0))
                if iter_age_hours < min_age:
                    continue

                card = tf_cfg.get("card") or tf_cfg.get("stagnation_card", {})
                if not card:
                    continue

                for rule, raw_target in card.items():
                    target_float, is_en = cls.parse_card_target(raw_target)
                    if not is_en or target_float is None:
                        continue

                    is_matched, _ = cls.match_rule_adaptive(rule, eval_val, target_float)
                    if is_matched:
                        safe_rule = rule.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        matched_any_target = target_float
                        matched_any_info = f"[{tf_key.upper()} matched '{safe_rule}' (vel {eval_val:.2f}$/h, age {iter_age_hours:.1f}h) -> target {target_float} USDT]"
                        if iter_net >= target_float:
                            cls.last_matched_rule = f"{tf_key}:{rule}"
                            return target_float, True, matched_any_info, target_float
                        else:
                            break

            return matched_any_target, False, matched_any_info, (matched_any_target or 0.0)

        # ------------------------------------------------------------------
        # 2. ЛЕГАСИ РЕЖИМ: ОДНОРОДНАЯ КАРТА СО СТАТИЧЕСКИМ MIN_FLOOR
        # ------------------------------------------------------------------
        card = stag_cfg.get("stagnation_card") or stag_cfg.get("card", {})
        if not card:
            return base_th, False, "", (base_th or 0.0)

        min_floor = float(stag_cfg.get("min_floor", 7.0))
        mode = stag_cfg.get("mode", "auto")
        velocity_pct_of = stag_cfg.get("velocity_pct_of", "top_target")

        if mode == "auto":
            first_key = next(iter(card.keys()), "")
            mode = "time" if ("<" in first_key and any(c.isdigit() and int(c) >= 3 for c in first_key) and "%" not in first_key) else "velocity"

        eval_val = velocity if mode == "velocity" else peak_age_h
        active_targets = [
            float(t) for rt in card.values()
            for t, is_en in [cls.parse_card_target(rt)]
            if is_en and t is not None
        ]
        if not active_targets:
            return base_th, False, "", float(base_th or 0.0)

        top_target = max(active_targets)
        reference_top = float(base_th if base_th is not None else top_target)

        for rule, raw_target in card.items():
            target_float, is_en = cls.parse_card_target(raw_target)
            if not is_en or target_float is None:
                continue
            ref_target = reference_top if velocity_pct_of == "top_target" else target_float
            is_matched, calc_threshold = cls.match_rule_adaptive(rule, eval_val, ref_target)
            if is_matched:
                cls.last_matched_rule = rule
                effective_target = float(max(target_float, min_floor))
                unit = "$/h" if mode == "velocity" else "h"
                safe_rule = rule.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                info = (
                    f"[{mode.upper()} {eval_val:.2f}{unit} matched '{safe_rule}' ({calc_threshold:.2f}{unit}) -> target {effective_target} USDT]"
                    if "%" in rule else
                    f"[{mode.upper()} {eval_val:.2f}{unit} matched '{safe_rule}' -> target {effective_target} USDT]"
                )
                return effective_target, effective_target < reference_top, info, reference_top

        cls.last_matched_rule = ""
        return (base_th if base_th is not None else reference_top), False, "", reference_top
