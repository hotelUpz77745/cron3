# CORE/SERVICES/auto_closer.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.auto_closer
ROLE: Сервис автоматического закрытия позиций (PnL куполов, таймауты, трейлинг-рэтчет, убытки)
==============================================================================
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional, Dict
from consts import DATA_DIR, ANALYTICS_DIR, CACHE_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.STRATEGY.trailing_system import BaseTrailingSystem, ToxicityEngine

logger = UnifiedLogger("AutoCloser")


class AutoCloser:
    """
    Институциональный сервис автоматического мониторинга и закрытия позиций:
    - Контроль PnL изолированных стратегических куполов (Classic, Toxic, Fixed, Off).
    - Трейлинг-Храповик (Trailing Ratchet) по шкале чистого Net Profit.
    - Toxicity Adaptive фиксация просадки.
    - Аварийный ручник риск-системы (Miami Profit Take / Risk Stop).
    """

    def __init__(self, bot_core):
        self.bot_core = bot_core
        self._is_closing = False
        self._last_close_ts: dict[str, float] = {}
        self._trailing_ratchet_state: dict[str, dict] = {}

    @staticmethod
    def _build_close_msg(
        name: str,
        symbols_str: str,
        iter_idx: int,
        next_iter_idx: int,
        iter_net: float,
        strat_net: float,
        new_iter_start_net: float,
        close_type: str,
        detail_info: str = ""
    ) -> str:
        icon = "🟢" if "STOP" not in close_type else "🔴"
        sym_line = f"Закрыты позиции монет: {symbols_str}.\n" if symbols_str else ""
        detail_line = f"{detail_info}\n" if detail_info else ""
        return (
            f"{icon} <b>AUTO-CLOSE {name} ({close_type})</b>\n"
            f"Завершен <b>Цикл #{iter_idx}</b>!\n"
            f"Результат цикла: <b>{iter_net:+.2f} USDT</b>.\n"
            f"Глобальный Net Profit: <b>{strat_net:+.2f} USDT</b>.\n"
            f"{detail_line}"
            f"{sym_line}"
            f"Старт <b>Цикла #{next_iter_idx}</b> (базовая точка Net: <b>{new_iter_start_net:+.2f} USDT</b>)."
        )

    @classmethod
    def evaluate_trailing_ratchet(
        cls, iter_net: float, iter_age_hours: float, ratchet_cfg: dict, state: dict
    ) -> tuple[bool, Optional[float], str, bool]:
        """
        Ступенчатый Трейлинг-Храповик по чистому Net (Фибо-лестница).
        Делегирует расчет базовой абстракции BaseTrailingSystem.
        """
        should_close, trigger_floor, info, is_stag = BaseTrailingSystem.evaluate_ratchet(
            iter_net, iter_age_hours, ratchet_cfg, state
        )
        if should_close:
            idx = state.get("active_level_idx", 0)
            cls._last_matched_rule = f"ratchet:L{idx}"
        return should_close, trigger_floor, info, is_stag

    @classmethod
    def match_rule_adaptive(cls, rule: str, eval_val: float, ref_target: float) -> tuple[bool, float]:
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
            logger.warning(f"[AUTO_CLOSER] Invalid rule format: '{rule}'")
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
        matched, _ = cls.match_rule_adaptive(rule, val, ref_target)
        return matched

    @staticmethod
    def parse_card_target(raw_target: Any) -> tuple[Optional[float], bool]:
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
    ) -> tuple[Optional[float], bool, str, float]:
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
            # Поддержка плоской структуры: stagnation_card или timeframes прямо внутри pos_cfg
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
            # Главный инвариант: фиксация строго при положительном Net
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
                            cls._last_matched_rule = f"{tf_key}:{rule}"
                            return target_float, True, matched_any_info, target_float
                        else:
                            # Правило скорости подошло, но профита не хватает -> спуск к следующему таймфрейму
                            break

            return matched_any_target, False, matched_any_info, (matched_any_target or 0.0)

        # ------------------------------------------------------------------
        # 2. ЛЕГАСИ РЕЖИМ: ОДНОРОДНАЯ КАРТА СО СТАТИЧЕСКИМ MIN_FLOOR
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
        active_targets: list[float] = [
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
                cls._last_matched_rule = rule
                effective_target = float(max(target_float, min_floor))
                unit = "$/h" if mode == "velocity" else "h"
                safe_rule = rule.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                info = f"[{mode.upper()} {eval_val:.2f}{unit} matched '{safe_rule}' ({calc_threshold:.2f}{unit}) -> target {effective_target} USDT]" if "%" in rule else f"[{mode.upper()} {eval_val:.2f}{unit} matched '{safe_rule}' -> target {effective_target} USDT]"
                return effective_target, effective_target < reference_top, info, reference_top

        cls._last_matched_rule = ""
        return (base_th if base_th is not None else reference_top), False, "", reference_top

    @classmethod
    def extract_toxicity_metrics(cls, strat_cfg, analytics_dict: dict, bot_core) -> tuple[float, float, float, float]:
        """
        Извлекает компоненты инвентаря для расчета Toxicity Score:
          - margin_load: текущая загрузка маржи (0.0..1.0)
          - depth_ratio: максимальное погружение сетки (0.0..1.0)
          - float_ratio: отношение висящего минуса к накопленному профиту
          - delta_recovery: степень рассасывания ямы просадки (0.0..1.0)
        """
        cur_floating = float(analytics_dict.get("unrealized_pnl_usdt", 0.0))
        max_dd = float(analytics_dict.get("max_drawdown_usdt", cur_floating))
        realized = float(analytics_dict.get("iteration_realized_pnl_usdt", analytics_dict.get("realized_pnl_usdt", 0.0)))
        deposit = float(getattr(strat_cfg, "start_balance_usdt", 4000.0))

        # Margin Load
        total_margin = 0.0
        per_coin = analytics_dict.get("per_coin", {})
        if isinstance(per_coin, dict) and per_coin:
            for c_data in per_coin.values():
                if isinstance(c_data, dict):
                    total_margin += float(c_data.get("current_margin_usdt", 0.0))
        margin_load = (total_margin / deposit) if deposit > 0 else 0.0

        # Max Depth Ratio
        max_step = 0
        fsm_mgr = getattr(bot_core, "runtime_manager", None)
        fsm_states = getattr(fsm_mgr, "fsm_states", None) if fsm_mgr else getattr(bot_core, "position_states", None)
        if isinstance(fsm_states, dict) and fsm_states:
            for sym_sides in fsm_states.values():
                if isinstance(sym_sides, dict):
                    for st in sym_sides.values():
                        step = getattr(st, "active_step", 0)
                        if isinstance(step, int) and step > max_step:
                            max_step = step
                elif hasattr(sym_sides, "active_step"):
                    step = getattr(sym_sides, "active_step", 0)
                    if isinstance(step, int) and step > max_step:
                        max_step = step
        elif isinstance(per_coin, dict) and per_coin:
            for c_data in per_coin.values():
                if isinstance(c_data, dict):
                    g_load = float(c_data.get("current_grid_load_pct", 0.0))
                    approx_step = int(g_load / 12.5)
                    if approx_step > max_step:
                        max_step = approx_step
        depth_ratio = min(1.0, max_step / 8.0)

        # Float Ratio
        float_ratio = abs(cur_floating) / (abs(cur_floating) + max(0.0, realized) + 1.0)

        # Delta Recovery
        delta_recovery = ToxicityEngine.calculate_delta_recovery(cur_floating, max_dd)

        return margin_load, depth_ratio, float_ratio, delta_recovery

    async def check(self):
        if self._is_closing:
            return

        # Машина состояний аналитики (FSM): AutoCloser разрешен ТОЛЬКО в 4-м состоянии (AUTOCLOSER_READY)
        fsm = getattr(getattr(self.bot_core, "analytics", None), "fsm", None)
        if fsm and not fsm.is_autocloser_ready():
            logger.debug(f"[AUTO_CLOSER] ⏳ Analytics state is {fsm.state.name} (not AUTOCLOSER_READY). AutoCloser execution blocked.")
            return

        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        if not app_data:
            return
        analytics_data = Utils.read_json_file(ANALYTICS_DIR / "analytics.json")
        if not analytics_data:
            return

        # RISK SYSTEM CHECK (Глобальный ручник по балансу в $: риск-стоп снизу и профит-стоп сверху)
        risk_cfg = app_data.get("risk_system") or app_data.get("stop_bot_rirk_system") or {}
        if risk_cfg.get("enabled"):
            min_thresh = risk_cfg.get("min_balance_threshold_usdt")
            max_thresh = risk_cfg.get("max_balance_threshold_usdt")
            cur_bal = float(analytics_data.get("cur_balance_usdt", 0.0))

            trigger_min = (min_thresh is not None and cur_bal > 0 and cur_bal < float(min_thresh))
            trigger_max = (max_thresh is not None and cur_bal > 0 and cur_bal >= float(max_thresh))

            if (trigger_min or trigger_max) and not getattr(self.bot_core, "is_paused", False):
                self._is_closing = True
                if trigger_max:
                    reason_log = f"[PROFIT_STOP] Balance {cur_bal:.2f} >= max profit threshold {max_thresh}! Locking profit into 100% cash and stopping bot."
                    reason_msg = (
                        f"🌴 <b>СТОП ПО ПРОФИЦИТУ (MIAMI PROFIT TAKE)</b> 🌴\n\n"
                        f"Баланс достиг целевого максимума: <b>{cur_bal:.2f} USDT</b> >= <b>{float(max_thresh):.2f} USDT</b>.\n"
                        f"Все позиции закрыты по рынку в 100% кэш. Бот <b>ОСТАНОВЛЕН С ПРИБЫЛЬЮ</b>."
                    )
                else:
                    reason_log = f"[RISK_SYSTEM] Balance {cur_bal:.2f} < min threshold {min_thresh}! Triggering emergency stop."
                    reason_msg = (
                        f"⛔ <b>ГЛОБАЛЬНЫЙ РУЧНИК (RISK STOP)</b> ⛔\n\n"
                        f"Баланс упал ниже порога: <b>{cur_bal:.2f} USDT</b> < <b>{float(min_thresh):.2f} USDT</b>.\n"
                        f"Все позиции закрыты. Бот <b>ОСТАНОВЛЕН</b>."
                    )

                logger.critical(reason_log)
                self.bot_core.is_paused = True
                self.bot_core.entry_blocked = True
                self.bot_core.block_until_timestamp = time.time() + 9999999.0
                try:
                    await self.bot_core.close_all_positions(reset_iterations=False)
                    logger.info("[RISK_SYSTEM] Waiting for analytics sync (12s)...")
                    await asyncio.sleep(12.0)
                    if "risk_system" in app_data:
                        app_data["risk_system"]["enabled"] = False
                    elif "stop_bot_rirk_system" in app_data:
                        app_data["stop_bot_rirk_system"]["enabled"] = False
                    Utils.write_json_file(app_json_path, app_data)
                    if getattr(self.bot_core, 'notifier', None):
                        await self.bot_core.notifier.send_alert(reason_msg)
                except Exception as e:
                    logger.error(f"[RISK_SYSTEM] Error during emergency/profit stop: {e}")
                finally:
                    self._is_closing = False
                return

        CLOSE_COOLDOWN_SEC = 45.0
        now_sec = time.time()
        now_ms = int(now_sec * 1000)

        # 1. СТРАТЕГИЧЕСКИЕ КУПОЛА (PER-STRATEGY AUTO-CLOSING)
        active_strats = {}
        if hasattr(self.bot_core, "strategy_registry"):
            active_strats = self.bot_core.strategy_registry.active_strategies

        for strat_id, strat_cfg in active_strats.items():
            if now_sec - self._last_close_ts.get(strat_id, 0.0) < CLOSE_COOLDOWN_SEC:
                continue

            # Определение режима автозакрытия (4 режима: classic, toxic, fixed, off)
            strat_app_cfg = app_data.get("active_strategies", {}).get(strat_id, {})
            strat_auto_cfg = strat_app_cfg.get("auto_closing") or getattr(strat_cfg, "auto_closing_cfg", {})

            if strat_id.endswith("__off") or "__off" in strat_id:
                mode = "off"
            elif strat_id.endswith("__trailing_ratchet") or "__trailing_ratchet" in strat_id:
                mode = "trailing_ratchet"
            elif strat_id.endswith("__classic") or "__classic" in strat_id:
                mode = "trailing_ratchet"
            elif strat_id.endswith("__toxic") or "__toxic" in strat_id:
                mode = "toxic"
            elif strat_id.endswith("__fixed") or "__fixed" in strat_id:
                mode = "fixed"
            else:
                raw_m = (
                    strat_app_cfg.get("auto_closing_mode")
                    or strat_app_cfg.get("mode")
                    or getattr(strat_cfg, "auto_closing_mode", None)
                    or getattr(strat_cfg, "mode", None)
                    or (strat_app_cfg.get("modes")[0] if isinstance(strat_app_cfg.get("modes"), list) and strat_app_cfg.get("modes") else None)
                    or ((strat_auto_cfg or {}).get("mode") if isinstance(strat_auto_cfg, dict) else None)
                    or (((strat_auto_cfg or {}).get("positive") or {}).get("mode") if isinstance(strat_auto_cfg, dict) else None)
                )
                raw_m_str = str(raw_m).lower().strip() if raw_m else "off"
                if raw_m_str in ("trailing_ratchet", "classic", "iter_net_classic"):
                    mode = "trailing_ratchet"
                elif raw_m_str in ("toxic", "toxicity_adaptive"):
                    mode = "toxic"
                elif raw_m_str in ("fixed", "fixed_net"):
                    mode = "fixed"
                else:
                    mode = "off"

            if mode == "off":
                continue

            strat_stats_path = ANALYTICS_DIR / "STATS" / f"analytics_{strat_id}.json"
            strat_analytics = Utils.read_json_file(strat_stats_path) if strat_stats_path.exists() else analytics_data
            if not strat_analytics:
                strat_analytics = analytics_data

            if strat_analytics.get("_floating_synced") is False or strat_analytics.get("net_profit_usdt") is None:
                logger.debug(f"[AUTO_CLOSER][{strat_id}] ⏳ Floating stats unverified by API. Skipping auto-close evaluation.")
                continue

            strat_net = float(strat_analytics.get("net_profit_usdt", 0.0))
            iter_idx = int(strat_analytics.get("iteration_index", 1))
            if fsm:
                cached_idx = fsm.get_iteration_index(strat_id)
                if cached_idx > iter_idx:
                    iter_idx = cached_idx

            # Идемпотентность: защита от повторного срабатывания закрытия уже закрытой итерации
            if fsm and fsm.is_iteration_closed(strat_id, iter_idx):
                logger.debug(f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} already closed in history. Skipping duplicate auto-close.")
                continue

            baseline_s = fsm.get_iteration_baseline_net(strat_id, iter_idx) if fsm and hasattr(fsm, "get_iteration_baseline_net") else 0.0
            iter_start_net = float(strat_analytics.get("iteration_start_net_profit_usdt", 0.0))
            if baseline_s > iter_start_net:
                iter_start_net = baseline_s

            iter_net = round(strat_net - iter_start_net, 4)

            pos_cfg = strat_auto_cfg.get("positive", {}) if isinstance(strat_auto_cfg, dict) else {}
            neg_cfg = strat_auto_cfg.get("negative", {}) if isinstance(strat_auto_cfg, dict) else {}

            strat_triggered = False
            strat_msg = ""
            is_stagnation = False
            next_iter_idx = iter_idx + 1

            # Исполнение выбранного режима автозакрытия
            if mode != "off":
                r_cfg = pos_cfg.get("trailing_ratchet") or {}
                raw_scale = pos_cfg.get("scale_money", getattr(strat_cfg, "auto_closing_cfg", {}).get("scale_money") if hasattr(strat_cfg, "auto_closing_cfg") else None)
                auto_scale = bool(pos_cfg.get("auto_scale_money", False) or raw_scale is None or (isinstance(pos_cfg.get("inverted_notional"), dict) and pos_cfg["inverted_notional"].get("enabled", False)) or (isinstance(pos_cfg.get("margin_relative"), dict) and pos_cfg["margin_relative"].get("enabled", False)))

                if auto_scale:
                    mode_scale = pos_cfg.get("mode_scale", "inverted_notional")
                    inv_cfg = pos_cfg.get("inverted_notional") if isinstance(pos_cfg.get("inverted_notional"), dict) else {}
                    margin_rel_cfg = pos_cfg.get("margin_relative") if isinstance(pos_cfg.get("margin_relative"), dict) else {}
                    base_mult = float(raw_scale) if (raw_scale is not None and float(raw_scale) > 0) else 1.0

                    if mode_scale == "inverted_notional" or (inv_cfg.get("enabled", False) and not margin_rel_cfg.get("enabled", False)):
                        # Инвертированное масштабирование по совокупному сайзу корзины (деэскалация при перегрузке)
                        total_notional = 0.0
                        per_coin = strat_analytics.get("per_coin", {})
                        if isinstance(per_coin, dict) and per_coin:
                            for c_data in per_coin.values():
                                if isinstance(c_data, dict):
                                    c_load = float(c_data.get("current_grid_load_usdt", 0.0))
                                    if c_load <= 0.0:
                                        c_load = float(c_data.get("current_margin_usdt", 0.0)) * 20.0
                                    total_notional += c_load
                        base_bal = float(getattr(strat_cfg, "start_balance_usdt", 0.0) or strat_analytics.get("start_balance_usdt", 600.0))
                        base_bal = max(1.0, base_bal)
                        load_ratio = total_notional / base_bal
                        low_ratio = float(inv_cfg.get("low_load_ratio", 1.67))    # ~1000$ сайз при 600$ депозите
                        high_ratio = float(inv_cfg.get("high_load_ratio", 3.50))  # ~2100$ сайз при 600$ депозите
                        max_s = float(inv_cfg.get("max_scale", 1.5))
                        min_s = float(inv_cfg.get("min_scale", 0.5))

                        if load_ratio <= low_ratio:
                            calc_scale = max_s * base_mult
                        elif load_ratio >= high_ratio:
                            calc_scale = min_s * base_mult
                        else:
                            frac = (load_ratio - low_ratio) / max(0.001, (high_ratio - low_ratio))
                            calc_scale = (max_s - frac * (max_s - min_s)) * base_mult
                        scale_money = round(max(min_s, min(max_s, calc_scale)), 3)
                    else:
                        # Прямое масштабирование от задействованной маржи (устаревший режим)
                        ref_margin = float(margin_rel_cfg.get("ref_margin_usdt", 100.0))
                        min_scale = float(margin_rel_cfg.get("min_scale", 0.5))
                        max_scale = float(margin_rel_cfg.get("max_scale", 2.5))
                        total_margin = 0.0
                        per_coin = strat_analytics.get("per_coin", {})
                        if isinstance(per_coin, dict) and per_coin:
                            for c_data in per_coin.values():
                                if isinstance(c_data, dict):
                                    total_margin += float(c_data.get("current_margin_usdt", 0.0))
                        if ref_margin > 0 and total_margin > 0:
                            calc_scale = (total_margin / ref_margin) * base_mult
                            scale_money = round(max(min_scale, min(max_scale, calc_scale)), 3)
                        else:
                            scale_money = min_scale
                else:
                    scale_money = float(raw_scale) if (raw_scale is not None and float(raw_scale) > 0) else 1.0

                r_state = self._trailing_ratchet_state.setdefault(strat_id, {})
                if r_state.get("iteration_index") != iter_idx:
                    r_state.clear()
                    r_state["iteration_index"] = iter_idx
                first_ts = strat_analytics.get("iteration_start_ts", strat_analytics.get("first_trade_ts", now_ms))
                iter_age_h = max(0.0, (now_ms - first_ts) / 3600000.0)

                # -------------------------------------------------------------
                # РЕЖИМ 2: TOXICITY ADAPTIVE (Институциональный адаптивный скор)
                # -------------------------------------------------------------
                if mode in ("toxic", "toxicity_adaptive"):
                    margin_load, depth_ratio, f_ratio, d_rec = self.extract_toxicity_metrics(strat_cfg, strat_analytics, self.bot_core)
                    t_cfg = pos_cfg.get("toxicity_adaptive", {})
                    weights = t_cfg.get("score_weights") or pos_cfg.get("toxicity_weights")
                    t_score = ToxicityEngine.calculate_toxicity_score(margin_load, depth_ratio, f_ratio, d_rec, weights)

                    should_close, trig_floor, r_info, is_stagnation = ToxicityEngine.evaluate_toxicity_adaptive(
                        iter_net=iter_net,
                        iter_age_hours=iter_age_h,
                        ratchet_cfg=pos_cfg,
                        state=r_state,
                        toxicity_score=t_score,
                        delta_recovery=d_rec,
                        scale_money=scale_money,
                    )
                    if should_close and trig_floor is not None:
                        strat_triggered = True
                        cls_rule = getattr(self, "_last_matched_rule", "") or "toxicity_adaptive"
                        self._last_matched_rule = cls_rule
                        logger.info(f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} {r_info}. Auto-closing dome!")
                        strat_msg = self._build_close_msg(
                            name=f"КУПОЛА [{strat_cfg.name}]",
                            symbols_str=", ".join(strat_cfg.symbols) if hasattr(strat_cfg, "symbols") else "",
                            iter_idx=iter_idx, next_iter_idx=next_iter_idx, iter_net=iter_net, strat_net=strat_net,
                            new_iter_start_net=0.0, close_type="TOXICITY ADAPTIVE",
                            detail_info=f"Зафиксирована прибыль: <b>+{iter_net:.2f} USDT</b> (Floor: <b>{trig_floor:.2f} USDT</b>).\nИнвентарь: Toxicity Score: <b>{t_score:.2f}</b>, Delta Recovery: <b>{d_rec:.0%}</b>.\nСобытие: {r_info}."
                        )

                # -------------------------------------------------------------
                # РЕЖИМ 1: ITER NET CLASSIC / TRAILING RATCHET
                # -------------------------------------------------------------
                elif mode in ("trailing_ratchet", "classic", "iter_net_classic"):
                    import copy
                    r_cfg_scaled = copy.deepcopy(r_cfg)
                    r_cfg_scaled["scale_money"] = scale_money
                    r_cfg_scaled["scale_factor"] = scale_money
                    should_close, trig_floor, r_info, is_stagnation = self.evaluate_trailing_ratchet(
                        iter_net=iter_net, iter_age_hours=iter_age_h, ratchet_cfg=r_cfg_scaled, state=r_state
                    )
                    if should_close and trig_floor is not None:
                        strat_triggered = True
                        logger.info(f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} {r_info}. Auto-closing dome!")
                        strat_msg = self._build_close_msg(
                            name=f"КУПОЛА [{strat_cfg.name}]",
                            symbols_str=", ".join(strat_cfg.symbols) if hasattr(strat_cfg, "symbols") else "",
                            iter_idx=iter_idx, next_iter_idx=next_iter_idx, iter_net=iter_net, strat_net=strat_net,
                            new_iter_start_net=0.0, close_type="TRAILING RATCHET",
                            detail_info=f"Зафиксирована прибыль: <b>+{iter_net:.2f} USDT</b> (Floor: <b>{trig_floor:.2f} USDT</b>).\nСобытие: {r_info}."
                        )

                # -------------------------------------------------------------
                # РЕЖИМ 3: FIXED NET (Прямой тейк корзины по фиксированному Net профиту)
                # -------------------------------------------------------------
                elif mode in ("fixed", "fixed_net"):
                    fixed_cfg = pos_cfg.get("fixed_net", {})
                    is_fixed, trig_target, f_info = ToxicityEngine.evaluate_fixed_net(
                        iter_net=iter_net, fixed_cfg=fixed_cfg, scale_money=scale_money
                    )
                    if is_fixed and trig_target is not None:
                        strat_triggered = True
                        self._last_matched_rule = "fixed_net"
                        logger.info(f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} {f_info}. Auto-closing dome!")
                        strat_msg = self._build_close_msg(
                            name=f"КУПОЛА [{strat_cfg.name}]",
                            symbols_str=", ".join(strat_cfg.symbols) if hasattr(strat_cfg, "symbols") else "",
                            iter_idx=iter_idx, next_iter_idx=next_iter_idx, iter_net=iter_net, strat_net=strat_net,
                            new_iter_start_net=0.0, close_type="FIXED NET PROFIT",
                            detail_info=f"Зафиксирована прибыль: <b>+{iter_net:.2f} USDT</b> (Цель: <b>+{trig_target:.2f} USDT</b>).\nСобытие: {f_info}."
                        )

                # -------------------------------------------------------------
                # ЛЕГАСИ РЕЖИМ: STAGNATION CARD
                # -------------------------------------------------------------
                else:
                    th_pos, is_stagnation, stag_info, ref_top = self.evaluate_stagnation_target(strat_analytics, pos_cfg)
                    if th_pos is not None:
                        th_pos = round(th_pos * scale_money, 2)
                    if th_pos is not None and iter_net >= th_pos:
                        strat_triggered = True
                        logger.info(f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} Net ({iter_net:+.2f}) >= {th_pos} (Global: {strat_net:+.2f}). Auto-closing dome!")
                        close_tag = "STAGNATION RATCHET" if is_stagnation else "TARGET REACHED"
                        detail = f"Сработал затухающий порог {stag_info}." if is_stagnation else f"Достигнута цель {th_pos} USDT."
                        strat_msg = self._build_close_msg(
                            name=f"КУПОЛА [{strat_cfg.name}]",
                            symbols_str=", ".join(strat_cfg.symbols) if hasattr(strat_cfg, "symbols") else "",
                            iter_idx=iter_idx, next_iter_idx=next_iter_idx, iter_net=iter_net, strat_net=strat_net,
                            new_iter_start_net=0.0, close_type=close_tag, detail_info=detail
                        )

            # Check Negative for Strategy Dome
            neg_enabled = neg_cfg.get("enabled", True)
            th_neg = neg_cfg.get("threshold") if neg_enabled else None
            if th_neg == 0 or th_neg == 0.0:
                th_neg = None
            if th_neg is not None:
                th_neg = round(float(th_neg) * scale_money, 2)

            if not strat_triggered and th_neg is not None and iter_net <= th_neg:
                strat_triggered = True
                is_stagnation = False
                logger.info(f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} Net ({iter_net:+.2f}) <= {th_neg}. Auto-closing dome!")
                strat_msg = self._build_close_msg(
                    name=f"КУПОЛА [{strat_cfg.name}]",
                    symbols_str=", ".join(strat_cfg.symbols) if hasattr(strat_cfg, "symbols") else "",
                    iter_idx=iter_idx, next_iter_idx=next_iter_idx, iter_net=iter_net, strat_net=strat_net,
                    new_iter_start_net=0.0, close_type="NEGATIVE STOP",
                    detail_info=f"Просадка цикла: <b>{iter_net:.2f} USDT</b> (Порог стопа: <b>{th_neg:.2f} USDT</b>)."
                )

            # ЖЕЛЕЗНЫЙ ИНВАРИАНТ: Секция positive ни при каких обстоятельствах не закрывается в минус или ноль!
            is_negative_stop = ("NEGATIVE STOP" in strat_msg)
            if strat_triggered and not is_negative_stop and iter_net <= 0.0:
                logger.critical(
                    f"[AUTO_CLOSER][{strat_id}] 🛡️ [POSITIVE PROFIT GUARD] Blocked positive auto-close! "
                    f"iter_net={iter_net:+.2f} <= 0.00 USDT. Section 'positive' is forbidden from closing in loss/zero!"
                )
                strat_triggered = False

            if strat_triggered:
                try:
                    self._is_closing = True
                    self._last_close_ts[strat_id] = time.time()
                    await self.bot_core.close_strategy_positions(strat_id)
                    self._trailing_ratchet_state[strat_id] = {}

                    # Считываем свежие данные аналитики после закрытия ордеров
                    fresh_strat_analytics = Utils.read_json_file(strat_stats_path) if strat_stats_path.exists() else strat_analytics
                    settled_net = round(float(fresh_strat_analytics.get("net_profit_usdt", strat_net)), 4)
                    settled_realized = round(float(fresh_strat_analytics.get("realized_pnl_net_usdt", fresh_strat_analytics.get("realized_pnl_usdt", 0.0))), 4)

                    now_ms = int(time.time() * 1000)
                    fresh_strat_analytics["iteration_index"] = next_iter_idx
                    fresh_strat_analytics["iteration_start_net_profit_usdt"] = settled_net
                    fresh_strat_analytics["iteration_net_profit_usdt"] = 0.0
                    fresh_strat_analytics["iteration_start_realized_pnl_usdt"] = settled_realized
                    fresh_strat_analytics["iteration_realized_pnl_usdt"] = 0.0
                    fresh_strat_analytics["iteration_start_ts"] = now_ms
                    fresh_strat_analytics["iteration_peak_net_profit_usdt"] = 0.0
                    fresh_strat_analytics["iteration_peak_ts"] = now_ms
                    fresh_strat_analytics["iteration_peak_age_hours"] = 0.0
                    fresh_strat_analytics["iteration_net_velocity"] = 0.0
                    if strat_stats_path:
                        Utils.atomic_write_json(strat_stats_path, fresh_strat_analytics, indent=4)

                    try:
                        c_file = CACHE_DIR / "iteration_state.json"
                        c_dict = json.loads(c_file.read_text(encoding="utf-8")) if c_file.exists() else {}
                        prev_entry = c_dict.get(strat_id, {})
                        c_dict[strat_id] = {
                            "iteration_index": next_iter_idx,
                            "iteration_start_net_profit_usdt": settled_net,
                            "iteration_start_realized_pnl_usdt": settled_realized,
                            "iteration_realized_pnl_usdt": 0.0,
                            "iteration_start_ts": now_ms,
                            "iteration_peak_net_profit_usdt": 0.0,
                            "iteration_peak_ts": now_ms,
                            "historical_peak_net_profit_usdt": prev_entry.get("historical_peak_net_profit_usdt", 0.0),
                            "historical_peak_net_ts": prev_entry.get("historical_peak_net_ts", now_ms),
                            "historical_max_drawdown_usdt": prev_entry.get("historical_max_drawdown_usdt", 0.0),
                            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        }
                        Utils.atomic_write_json(c_file, c_dict, indent=4)
                    except Exception as ce:
                        logger.error(f"[AUTO_CLOSER][{strat_id}] Error saving iteration cache: {ce}")

                    if fsm:
                        c_type = strat_msg.split("(")[1].split(")")[0] if "(" in strat_msg else "AUTO-CLOSE"
                        fsm.mark_iteration_closed(
                            strat_id=strat_id,
                            iter_idx=iter_idx,
                            details={
                                "close_type": c_type,
                                "iter_net": iter_net,
                                "strat_net": settled_net,
                                "settled_realized": settled_realized,
                            }
                        )

                    if hasattr(self.bot_core, "fsm_states"):
                        for sym in strat_cfg.symbols:
                            if sym in self.bot_core.fsm_states:
                                for s_side in ("LONG", "SHORT"):
                                    if s_side in self.bot_core.fsm_states[sym]:
                                        self.bot_core.fsm_states[sym][s_side].recent_closes = []

                    Utils.write_json_file(app_json_path, app_data)
                    if strat_msg and getattr(self.bot_core, 'notifier', None):
                        strat_msg = strat_msg.replace("базовая точка Net: <b>0.00 USDT</b>", f"базовая точка Net: <b>{settled_net:+.2f} USDT</b>")
                        await self.bot_core.notifier.send_alert(strat_msg)

                except Exception as e:
                    logger.error(f"[AUTO_CLOSER][{strat_id}] Error in strategy auto-close: {e}")
                finally:
                    self._is_closing = False

        # 2. ГЛОБАЛЬНЫЙ ПОРТФЕЛЬНЫЙ AUTO-CLOSING (если задан на верхнем уровне)
        auto_cfg = app_data.get("auto_closing")
        if not auto_cfg:
            return

        if now_sec - self._last_close_ts.get("__global__", 0.0) < CLOSE_COOLDOWN_SEC:
            return

        if analytics_data.get("_floating_synced") is False or analytics_data.get("net_profit_usdt") is None:
            logger.debug("[AUTO_CLOSER] ⏳ Global floating stats unverified by API. Skipping global auto-close.")
            return

        net_usdt = float(analytics_data.get("net_profit_usdt", 0.0))
        iter_idx_g = int(analytics_data.get("iteration_index", 1))
        if fsm:
            cached_g_idx = fsm.get_iteration_index("__global__")
            if cached_g_idx > iter_idx_g:
                iter_idx_g = cached_g_idx
        if fsm and fsm.is_iteration_closed("__global__", iter_idx_g):
            logger.debug(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} already closed in history. Skipping duplicate auto-close.")
            return

        baseline_g = fsm.get_iteration_baseline_net("__global__", iter_idx_g) if fsm and hasattr(fsm, "get_iteration_baseline_net") else 0.0
        iter_start_net_g = float(analytics_data.get("iteration_start_net_profit_usdt", 0.0))
        if baseline_g > iter_start_net_g:
            iter_start_net_g = baseline_g
        iter_net_g = round(net_usdt - iter_start_net_g, 4)

        pos_cfg = auto_cfg.get("positive", {})
        neg_cfg = auto_cfg.get("negative", {})
        
        triggered = False
        action_msg = ""
        is_stagnation = False
        next_iter_idx_g = iter_idx_g + 1
        new_iter_start_net_g = round(net_usdt, 4)
        
        # Check Positive Portfolio
        mode_g = auto_cfg.get("auto_closing_mode") or auto_cfg.get("mode") or pos_cfg.get("mode", "trailing_ratchet")
        if mode_g == "classic":
            mode_g = "trailing_ratchet"
        r_cfg_g = pos_cfg.get("trailing_ratchet") or {}
        r_state_g = self._trailing_ratchet_state.setdefault("__global__", {})
        if r_state_g.get("iteration_index") != iter_idx_g:
            r_state_g.clear()
            r_state_g["iteration_index"] = iter_idx_g
        first_ts_g = analytics_data.get("iteration_start_ts", analytics_data.get("first_trade_ts", now_ms))
        iter_age_h_g = max(0.0, (now_ms - first_ts_g) / 3600000.0)

        if mode_g == "toxicity_adaptive":
            deposit_g = float(analytics_data.get("cur_balance_usdt", analytics_data.get("start_balance_usdt", 4000.0)))
            cur_floating_g = float(analytics_data.get("unrealized_pnl_usdt", 0.0))
            max_dd_g = float(analytics_data.get("max_drawdown_usdt", cur_floating_g))
            realized_g = float(analytics_data.get("iteration_realized_pnl_usdt", analytics_data.get("realized_pnl_usdt", 0.0)))

            total_margin_g = 0.0
            per_coin_g = analytics_data.get("per_coin", {})
            if isinstance(per_coin_g, dict):
                for c_data in per_coin_g.values():
                    if isinstance(c_data, dict):
                        total_margin_g += float(c_data.get("current_margin_usdt", 0.0))
            margin_load_g = (total_margin_g / deposit_g) if deposit_g > 0 else 0.0
            depth_ratio_g = 0.5
            float_ratio_g = abs(cur_floating_g) / (abs(cur_floating_g) + max(0.0, realized_g) + 1.0)
            delta_rec_g = ToxicityEngine.calculate_delta_recovery(cur_floating_g, max_dd_g)

            scale_money_g = float(pos_cfg.get("scale_money", auto_cfg.get("scale_money", 1.0)))
            if scale_money_g <= 0:
                scale_money_g = 1.0

            t_cfg_g = pos_cfg.get("toxicity_adaptive", {})
            weights_g = t_cfg_g.get("score_weights") or pos_cfg.get("toxicity_weights")
            t_score_g = ToxicityEngine.calculate_toxicity_score(margin_load_g, depth_ratio_g, float_ratio_g, delta_rec_g, weights_g)

            should_close, trig_floor, r_info, is_stagnation = ToxicityEngine.evaluate_toxicity_adaptive(
                iter_net=iter_net_g,
                iter_age_hours=iter_age_h_g,
                ratchet_cfg=pos_cfg,
                state=r_state_g,
                toxicity_score=t_score_g,
                delta_recovery=delta_rec_g,
                scale_money=scale_money_g,
            )
            if should_close and trig_floor is not None:
                self._is_closing = True
                triggered = True
                logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} {r_info}. Auto-closing portfolio!")
                action_msg = self._build_close_msg(
                    name="ПОРТФЕЛЯ", symbols_str="", iter_idx=iter_idx_g, next_iter_idx=next_iter_idx_g,
                    iter_net=iter_net_g, strat_net=net_usdt, new_iter_start_net=new_iter_start_net_g,
                    close_type="TOXICITY ADAPTIVE",
                    detail_info=f"Зафиксирована прибыль: <b>+{iter_net_g:.2f} USDT</b> (Floor: <b>{trig_floor:.2f} USDT</b>).\nИнвентарь: Score: <b>{t_score_g:.2f}</b>, Recovery: <b>{delta_rec_g:.0%}</b>.\nСобытие: {r_info}."
                )

        elif mode_g in ("trailing_ratchet", "classic", "iter_net_classic"):
            scale_money_g = float(pos_cfg.get("scale_money", auto_cfg.get("scale_money", 1.0)))
            if scale_money_g <= 0:
                scale_money_g = 1.0
            import copy
            r_cfg_g_scaled = copy.deepcopy(r_cfg_g or {})
            r_cfg_g_scaled["scale_money"] = scale_money_g
            r_cfg_g_scaled["scale_factor"] = scale_money_g
            should_close, trig_floor, r_info, is_stagnation = self.evaluate_trailing_ratchet(
                iter_net=iter_net_g, iter_age_hours=iter_age_h_g, ratchet_cfg=r_cfg_g_scaled, state=r_state_g
            )
            if should_close and trig_floor is not None:
                self._is_closing = True
                triggered = True
                logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} {r_info}. Auto-closing portfolio!")
                action_msg = self._build_close_msg(
                    name="ПОРТФЕЛЯ", symbols_str="", iter_idx=iter_idx_g, next_iter_idx=next_iter_idx_g,
                    iter_net=iter_net_g, strat_net=net_usdt, new_iter_start_net=new_iter_start_net_g,
                    close_type="TRAILING RATCHET",
                    detail_info=f"Зафиксирована прибыль: <b>+{iter_net_g:.2f} USDT</b> (Floor: <b>{trig_floor:.2f} USDT</b>).\nСобытие: {r_info}."
                )

        elif mode_g == "fixed_net":
            scale_money_g = float(pos_cfg.get("scale_money", auto_cfg.get("scale_money", 1.0)))
            if scale_money_g <= 0:
                scale_money_g = 1.0
            fixed_cfg_g = pos_cfg.get("fixed_net", {})
            is_fixed, trig_target, f_info = ToxicityEngine.evaluate_fixed_net(
                iter_net=iter_net_g, fixed_cfg=fixed_cfg_g, scale_money=scale_money_g
            )
            if is_fixed and trig_target is not None:
                self._is_closing = True
                triggered = True
                logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} {f_info}. Auto-closing portfolio!")
                action_msg = self._build_close_msg(
                    name="ПОРТФЕЛЯ", symbols_str="", iter_idx=iter_idx_g, next_iter_idx=next_iter_idx_g,
                    iter_net=iter_net_g, strat_net=net_usdt, new_iter_start_net=new_iter_start_net_g,
                    close_type="FIXED NET PROFIT",
                    detail_info=f"Зафиксирована прибыль: <b>+{iter_net_g:.2f} USDT</b> (Цель: <b>+{trig_target:.2f} USDT</b>).\nСобытие: {f_info}."
                )

        else:
            scale_money_g = float(pos_cfg.get("scale_money", auto_cfg.get("scale_money", 1.0)))
            if scale_money_g <= 0:
                scale_money_g = 1.0
            th_pos, is_stagnation, stag_info, ref_top = self.evaluate_stagnation_target(analytics_data, pos_cfg)
            if th_pos is not None:
                th_pos = round(th_pos * scale_money_g, 2)
            if th_pos is not None and iter_net_g >= th_pos:
                self._is_closing = True
                triggered = True
                logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} Net ({iter_net_g:+.2f}) >= {th_pos}. Auto-closing portfolio!")
                close_tag = "PORTFOLIO STAGNATION RATCHET" if is_stagnation else "PORTFOLIO TARGET REACHED"
                detail = f"Сработал затухающий порог {stag_info}." if is_stagnation else f"Достигнута цель {th_pos} USDT."
                action_msg = self._build_close_msg(
                    name="ПОРТФЕЛЯ", symbols_str="", iter_idx=iter_idx_g, next_iter_idx=next_iter_idx_g,
                    iter_net=iter_net_g, strat_net=net_usdt, new_iter_start_net=new_iter_start_net_g,
                    close_type=close_tag, detail_info=detail
                )

        # Check Negative Portfolio
        scale_money_neg = float(pos_cfg.get("scale_money", auto_cfg.get("scale_money", 1.0)))
        if scale_money_neg <= 0:
            scale_money_neg = 1.0
        th_neg = neg_cfg.get("threshold")
        if th_neg == 0 or th_neg == 0.0:
            th_neg = None
        if th_neg is not None:
            th_neg = round(float(th_neg) * scale_money_neg, 2)

        if not triggered and th_neg is not None and iter_net_g <= th_neg:
            self._is_closing = True
            triggered = True
            is_stagnation = False
            logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} Net ({iter_net_g:+.2f}) <= {th_neg}. Auto-closing portfolio!")
            action_msg = self._build_close_msg(
                name="ПОРТФЕЛЯ", symbols_str="", iter_idx=iter_idx_g, next_iter_idx=next_iter_idx_g,
                iter_net=iter_net_g, strat_net=net_usdt, new_iter_start_net=new_iter_start_net_g,
                close_type="NEGATIVE STOP",
                detail_info=f"Просадка цикла: <b>{iter_net_g:.2f} USDT</b> (Порог стопа: <b>{th_neg:.2f} USDT</b>)."
            )

        # ЖЕЛЕЗНЫЙ ИНВАРИАНТ: Секция positive ни при каких обстоятельствах не закрывается в минус или ноль!
        is_negative_stop_g = ("NEGATIVE STOP" in action_msg)
        if triggered and not is_negative_stop_g and iter_net_g <= 0.0:
            logger.critical(
                f"[AUTO_CLOSER] 🛡️ [POSITIVE PROFIT GUARD] Blocked global positive auto-close! "
                f"iter_net_g={iter_net_g:+.2f} <= 0.00 USDT. Section 'positive' is forbidden from closing in loss/zero!"
            )
            triggered = False
            self._is_closing = False

        if triggered:
            try:
                self._last_close_ts["__global__"] = time.time()
                await self.bot_core.close_all_positions()
                self._trailing_ratchet_state["__global__"] = {}

                now_ms = int(time.time() * 1000)
                analytics_data["iteration_index"] = next_iter_idx_g
                analytics_data["iteration_start_net_profit_usdt"] = new_iter_start_net_g
                analytics_data["iteration_net_profit_usdt"] = 0.0
                analytics_data["iteration_start_ts"] = now_ms
                analytics_data["iteration_peak_net_profit_usdt"] = 0.0
                analytics_data["iteration_peak_ts"] = now_ms
                analytics_data["iteration_peak_age_hours"] = 0.0
                analytics_data["iteration_net_velocity"] = 0.0
                Utils.atomic_write_json(ANALYTICS_DIR / "analytics.json", analytics_data, indent=4)

                try:
                    c_file = CACHE_DIR / "iteration_state.json"
                    c_dict = json.loads(c_file.read_text(encoding="utf-8")) if c_file.exists() else {}
                    c_dict["__global__"] = {
                        "iteration_index": next_iter_idx_g,
                        "iteration_start_net_profit_usdt": new_iter_start_net_g,
                        "iteration_start_ts": now_ms,
                        "iteration_peak_net_profit_usdt": 0.0,
                        "iteration_peak_ts": now_ms,
                        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    }
                    Utils.atomic_write_json(c_file, c_dict, indent=4)
                except Exception as ce:
                    logger.error(f"[AUTO_CLOSER] Error saving global iteration cache: {ce}")

                if fsm:
                    fsm.mark_iteration_closed(
                        strat_id="__global__",
                        iter_idx=iter_idx_g,
                        details={
                            "close_type": "PORTFOLIO AUTO-CLOSE",
                            "iter_net": iter_net_g,
                            "strat_net": new_iter_start_net_g,
                        }
                    )

                if hasattr(self.bot_core, "fsm_states"):
                    for sym, sides in self.bot_core.fsm_states.items():
                        for s_side, s_obj in sides.items():
                            if hasattr(s_obj, "recent_closes"):
                                s_obj.recent_closes = []

                Utils.write_json_file(app_json_path, app_data)
                if action_msg and getattr(self.bot_core, 'notifier', None):
                    await self.bot_core.notifier.send_alert(action_msg)
            except Exception as e:
                logger.error(f"[AUTO_CLOSER] Error during portfolio auto-close: {e}")
            finally:
                self._is_closing = False
