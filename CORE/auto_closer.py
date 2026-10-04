import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional, Dict
from consts import DATA_DIR, ANALYTICS_DIR, CACHE_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.trailing_system import BaseTrailingSystem

logger = UnifiedLogger("AutoCloser")

class AutoCloser:
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
        active_targets = [cls.parse_card_target(rt)[0] for rt in card.values() if cls.parse_card_target(rt)[1] and cls.parse_card_target(rt)[0] is not None]
        if not active_targets:
            return base_th, False, "", (base_th or 0.0)

        top_target = max(active_targets)
        reference_top = base_th if base_th is not None else top_target

        for rule, raw_target in card.items():
            target_float, is_en = cls.parse_card_target(raw_target)
            if not is_en or target_float is None:
                continue
            ref_target = reference_top if velocity_pct_of == "top_target" else target_float
            is_matched, calc_threshold = cls.match_rule_adaptive(rule, eval_val, ref_target)
            if is_matched:
                cls._last_matched_rule = rule
                effective_target = max(target_float, min_floor)
                unit = "$/h" if mode == "velocity" else "h"
                safe_rule = rule.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                info = f"[{mode.upper()} {eval_val:.2f}{unit} matched '{safe_rule}' ({calc_threshold:.2f}{unit}) -> target {effective_target} USDT]" if "%" in rule else f"[{mode.upper()} {eval_val:.2f}{unit} matched '{safe_rule}' -> target {effective_target} USDT]"
                return effective_target, effective_target < reference_top, info, reference_top

        cls._last_matched_rule = ""
        return (base_th if base_th is not None else reference_top), False, "", reference_top

    async def check(self):
        if self._is_closing:
            return
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        if not app_data:
            return
        analytics_data = Utils.read_json_file(ANALYTICS_DIR / "analytics.json")
        if not analytics_data:
            return

        # RISK SYSTEM CHECK (Глобальный ручник по балансу в $)
        risk_cfg = app_data.get("risk_system") or app_data.get("stop_bot_rirk_system") or {}
        if risk_cfg.get("enabled"):
            thresh = risk_cfg.get("min_balance_threshold_usdt")
            cur_bal = float(analytics_data.get("cur_balance_usdt", 0.0))
            if thresh is not None and cur_bal > 0 and cur_bal < float(thresh):
                if not getattr(self.bot_core, "is_paused", False):
                    self._is_closing = True
                    logger.critical(f"[RISK_SYSTEM] Balance {cur_bal:.2f} < threshold {thresh}! Triggering emergency stop.")
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
                        msg = f"⛔ <b>ГЛОБАЛЬНЫЙ РУЧНИК (RISK STOP)</b> ⛔\n\nБаланс упал ниже порога: <b>{cur_bal:.2f} USDT</b> < <b>{thresh:.2f} USDT</b>.\nВсе позиции закрыты. Бот <b>ОСТАНОВЛЕН</b>."
                        if getattr(self.bot_core, 'notifier', None):
                            await self.bot_core.notifier.send_alert(msg)
                    except Exception as e:
                        logger.error(f"[RISK_SYSTEM] Error during emergency stop: {e}")
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

            strat_app_cfg = app_data.get("active_strategies", {}).get(strat_id, {})
            strat_auto_cfg = strat_app_cfg.get("auto_closing") or strat_cfg.auto_closing_cfg
            if not strat_auto_cfg:
                continue

            strat_stats_path = ANALYTICS_DIR / "STATS" / f"analytics_{strat_id}.json"
            strat_analytics = Utils.read_json_file(strat_stats_path) if strat_stats_path.exists() else analytics_data
            if not strat_analytics:
                strat_analytics = analytics_data

            strat_net = float(strat_analytics.get("net_profit_usdt", 0.0))
            iter_idx = int(strat_analytics.get("iteration_index", 1))
            iter_start_net = float(strat_analytics.get("iteration_start_net_profit_usdt", 0.0))
            iter_net = float(strat_analytics.get("iteration_net_profit_usdt", round(strat_net - iter_start_net, 4)))

            pos_cfg = strat_auto_cfg.get("positive", {})
            neg_cfg = strat_auto_cfg.get("negative", {})

            strat_triggered = False
            strat_msg = ""
            is_stagnation = False
            next_iter_idx = iter_idx + 1
            new_iter_start_net = round(strat_net, 4)

            # Check Positive for Strategy Dome
            mode = pos_cfg.get("mode", "")
            r_cfg = pos_cfg.get("trailing_ratchet")
            if mode == "trailing_ratchet" or (r_cfg and r_cfg.get("enabled")):
                r_state = self._trailing_ratchet_state.setdefault(strat_id, {})
                if r_state.get("iteration_index") != iter_idx:
                    r_state.clear()
                    r_state["iteration_index"] = iter_idx
                first_ts = strat_analytics.get("iteration_start_ts", strat_analytics.get("first_trade_ts", now_ms))
                iter_age_h = max(0.0, (now_ms - first_ts) / 3600000.0)
                should_close, trig_floor, r_info, is_stagnation = self.evaluate_trailing_ratchet(
                    iter_net=iter_net, iter_age_hours=iter_age_h, ratchet_cfg=r_cfg or {}, state=r_state
                )
                if should_close and trig_floor is not None:
                    strat_triggered = True
                    logger.info(f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} {r_info}. Auto-closing dome!")
                    strat_msg = self._build_close_msg(
                        name=f"КУПОЛА [{strat_cfg.name}]",
                        symbols_str=", ".join(strat_cfg.symbols) if hasattr(strat_cfg, "symbols") else "",
                        iter_idx=iter_idx, next_iter_idx=next_iter_idx, iter_net=iter_net, strat_net=strat_net,
                        new_iter_start_net=new_iter_start_net, close_type="TRAILING RATCHET",
                        detail_info=f"Зафиксирована прибыль: <b>+{iter_net:.2f} USDT</b> (Floor: <b>{trig_floor:.2f} USDT</b>).\nСобытие: {r_info}."
                    )
            else:
                th_pos, is_stagnation, stag_info, ref_top = self.evaluate_stagnation_target(strat_analytics, pos_cfg)
                if th_pos is not None and iter_net >= th_pos:
                    strat_triggered = True
                    logger.info(f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} Net ({iter_net:+.2f}) >= {th_pos} (Global: {strat_net:+.2f}). Auto-closing dome!")
                    close_tag = "STAGNATION RATCHET" if is_stagnation else "TARGET REACHED"
                    detail = f"Сработал затухающий порог {stag_info}." if is_stagnation else f"Достигнута цель {th_pos} USDT."
                    strat_msg = self._build_close_msg(
                        name=f"КУПОЛА [{strat_cfg.name}]",
                        symbols_str=", ".join(strat_cfg.symbols) if hasattr(strat_cfg, "symbols") else "",
                        iter_idx=iter_idx, next_iter_idx=next_iter_idx, iter_net=iter_net, strat_net=strat_net,
                        new_iter_start_net=new_iter_start_net, close_type=close_tag, detail_info=detail
                    )

            # Check Negative for Strategy Dome
            th_neg = neg_cfg.get("threshold")
            if th_neg == 0 or th_neg == 0.0:
                th_neg = None

            if not strat_triggered and th_neg is not None and iter_net <= th_neg:
                strat_triggered = True
                is_stagnation = False
                logger.info(f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} Net ({iter_net:+.2f}) <= {th_neg}. Auto-closing dome!")
                strat_msg = self._build_close_msg(
                    name=f"КУПОЛА [{strat_cfg.name}]",
                    symbols_str=", ".join(strat_cfg.symbols) if hasattr(strat_cfg, "symbols") else "",
                    iter_idx=iter_idx, next_iter_idx=next_iter_idx, iter_net=iter_net, strat_net=strat_net,
                    new_iter_start_net=new_iter_start_net, close_type="NEGATIVE STOP",
                    detail_info=f"Просадка цикла: <b>{iter_net:.2f} USDT</b> (Порог стопа: <b>{th_neg} USDT</b>)."
                )

            if strat_triggered:
                try:
                    self._is_closing = True
                    self._last_close_ts[strat_id] = time.time()
                    await self.bot_core.close_strategy_positions(strat_id)
                    self._trailing_ratchet_state[strat_id] = {}

                    now_ms = int(time.time() * 1000)
                    new_iter_start_real = new_iter_start_net
                    strat_analytics["iteration_index"] = next_iter_idx
                    strat_analytics["iteration_start_net_profit_usdt"] = new_iter_start_net
                    strat_analytics["iteration_net_profit_usdt"] = 0.0
                    strat_analytics["iteration_start_realized_pnl_usdt"] = new_iter_start_real
                    strat_analytics["iteration_realized_pnl_usdt"] = 0.0
                    strat_analytics["iteration_start_ts"] = now_ms
                    strat_analytics["iteration_peak_net_profit_usdt"] = 0.0
                    strat_analytics["iteration_peak_ts"] = now_ms
                    strat_analytics["iteration_peak_age_hours"] = 0.0
                    strat_analytics["iteration_net_velocity"] = 0.0
                    if strat_stats_path:
                        Utils.atomic_write_json(strat_stats_path, strat_analytics, indent=4)

                    try:
                        c_file = CACHE_DIR / "iteration_state.json"
                        c_dict = json.loads(c_file.read_text(encoding="utf-8")) if c_file.exists() else {}
                        prev_entry = c_dict.get(strat_id, {})
                        c_dict[strat_id] = {
                            "iteration_index": next_iter_idx,
                            "iteration_start_net_profit_usdt": new_iter_start_net,
                            "iteration_start_realized_pnl_usdt": new_iter_start_real,
                            "iteration_realized_pnl_usdt": 0.0,
                            "iteration_start_ts": now_ms,
                            "iteration_peak_net_profit_usdt": 0.0,
                            "iteration_peak_ts": now_ms,
                            "historical_peak_net_profit_usdt": prev_entry.get("historical_peak_net_profit_usdt", 9.7816),
                            "historical_peak_net_ts": prev_entry.get("historical_peak_net_ts", 1790770078270),
                            "historical_max_drawdown_usdt": prev_entry.get("historical_max_drawdown_usdt", -8.0038),
                            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        }
                        Utils.atomic_write_json(c_file, c_dict, indent=4)
                    except Exception as ce:
                        logger.error(f"[AUTO_CLOSER][{strat_id}] Error saving iteration cache: {ce}")

                    if hasattr(self.bot_core, "fsm_states"):
                        for sym in strat_cfg.symbols:
                            if sym in self.bot_core.fsm_states:
                                for s_side in ("LONG", "SHORT"):
                                    if s_side in self.bot_core.fsm_states[sym]:
                                        self.bot_core.fsm_states[sym][s_side].recent_closes = []

                    Utils.write_json_file(app_json_path, app_data)
                    if strat_msg and getattr(self.bot_core, 'notifier', None):
                        await self.bot_core.notifier.send_alert(strat_msg)

                    if hasattr(self.bot_core, "radar_navigator") and self.bot_core.radar_navigator.is_enabled():
                        matched_rule_str = getattr(self, "_last_matched_rule", "") or getattr(AutoCloser, "_last_matched_rule", "")
                        asyncio.create_task(
                            self.bot_core.radar_navigator.on_ratchet_closed(
                                strat_id=strat_id,
                                matched_rule=matched_rule_str,
                                is_stagnation=is_stagnation
                            )
                        )
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

        net_usdt = float(analytics_data.get("net_profit_usdt", 0.0))
        iter_idx_g = int(analytics_data.get("iteration_index", 1))
        iter_start_net_g = float(analytics_data.get("iteration_start_net_profit_usdt", 0.0))
        iter_net_g = float(analytics_data.get("iteration_net_profit_usdt", round(net_usdt - iter_start_net_g, 4)))

        pos_cfg = auto_cfg.get("positive", {})
        neg_cfg = auto_cfg.get("negative", {})
        
        triggered = False
        action_msg = ""
        is_stagnation = False
        next_iter_idx_g = iter_idx_g + 1
        new_iter_start_net_g = round(net_usdt, 4)
        
        # Check Positive Portfolio
        mode_g = pos_cfg.get("mode", "")
        r_cfg_g = pos_cfg.get("trailing_ratchet")
        if mode_g == "trailing_ratchet" or (r_cfg_g and r_cfg_g.get("enabled")):
            r_state_g = self._trailing_ratchet_state.setdefault("__global__", {})
            if r_state_g.get("iteration_index") != iter_idx_g:
                r_state_g.clear()
                r_state_g["iteration_index"] = iter_idx_g
            first_ts_g = analytics_data.get("iteration_start_ts", analytics_data.get("first_trade_ts", now_ms))
            iter_age_h_g = max(0.0, (now_ms - first_ts_g) / 3600000.0)
            should_close, trig_floor, r_info, is_stagnation = self.evaluate_trailing_ratchet(
                iter_net=iter_net_g, iter_age_hours=iter_age_h_g, ratchet_cfg=r_cfg_g or {}, state=r_state_g
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
        else:
            th_pos, is_stagnation, stag_info, ref_top = self.evaluate_stagnation_target(analytics_data, pos_cfg)
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
        th_neg = neg_cfg.get("threshold")
        if th_neg == 0 or th_neg == 0.0:
            th_neg = None

        if not triggered and th_neg is not None and iter_net_g <= th_neg:
            self._is_closing = True
            triggered = True
            is_stagnation = False
            logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} Net ({iter_net_g:+.2f}) <= {th_neg}. Auto-closing portfolio!")
            action_msg = self._build_close_msg(
                name="ПОРТФЕЛЯ", symbols_str="", iter_idx=iter_idx_g, next_iter_idx=next_iter_idx_g,
                iter_net=iter_net_g, strat_net=net_usdt, new_iter_start_net=new_iter_start_net_g,
                close_type="NEGATIVE STOP",
                detail_info=f"Просадка цикла: <b>{iter_net_g:.2f} USDT</b> (Порог стопа: <b>{th_neg} USDT</b>)."
            )

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
