import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional, Dict
from consts import DATA_DIR, ANALYTICS_DIR, CACHE_DIR
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("AutoCloser")

class AutoCloser:
    def __init__(self, bot_core):
        self.bot_core = bot_core
        self._is_closing = False
        self._last_close_ts: dict[str, float] = {}

    @classmethod
    def match_rule_adaptive(cls, rule: str, eval_val: float, ref_target: float) -> tuple[bool, float]:
        """
        Проверяет соответствие значения правилу строки с поддержкой процентов от целевого ориентира.
        Поддерживает:
          - Проценты: '>5.0%', '<=2%', '>=1.5%' -> порог = (pct / 100.0) * ref_target
          - Абсолютные числа: '>1.0', '<=0.4' -> порог = float(num)
        Возвращает (is_matched: bool, calculated_threshold: float).
        """
        rule = rule.strip()
        is_pct = rule.endswith("%")
        clean_rule = rule[:-1].strip() if is_pct else rule

        op = ""
        for candidate_op in ("<=", ">=", "==", "<", ">"):
            if clean_rule.startswith(candidate_op):
                op = candidate_op
                raw_num = clean_rule[len(candidate_op):].strip()
                break
        else:
            op = "=="
            raw_num = clean_rule

        try:
            val_num = float(raw_num)
            threshold_val = (val_num / 100.0 * ref_target) if is_pct else val_num
        except ValueError:
            logger.warning(f"[AUTO_CLOSER] Invalid rule format: '{rule}'")
            return False, 0.0

        if op == "<=":
            return eval_val <= threshold_val, threshold_val
        elif op == ">=":
            return eval_val >= threshold_val, threshold_val
        elif op == "==":
            return abs(eval_val - threshold_val) < 1e-4, threshold_val
        elif op == "<":
            return eval_val < threshold_val, threshold_val
        elif op == ">":
            return eval_val > threshold_val, threshold_val

        return False, threshold_val

    @classmethod
    def match_rule_float(cls, rule: str, val: float, ref_target: float = 1.0) -> bool:
        """
        Проверяет соответствие вещественного значения (скорости или часов) правилу строки.
        Поддерживает операторы: '<=', '>=', '==', '<', '>'.
        Обеспечивает обратную совместимость для абсолютных и процентных правил.
        """
        matched, _ = cls.match_rule_adaptive(rule, val, ref_target)
        return matched

    @staticmethod
    def parse_card_target(raw_target: Any) -> tuple[Optional[float], bool]:
        """
        Разбирает значение правила в stagnation_card.
        Возвращает (target_float, is_enabled).
        Поддерживает:
          - Число: 21.0 -> (21.0, True)
          - null / None / False: -> (None, False)
          - Словарь: {"target": 21.0, "enabled": True} -> (21.0, True)
          - Словарь: {"target": 21.0, "enabled": False} -> (None, False)
        """
        if raw_target is None or raw_target is False:
            return None, False
        if isinstance(raw_target, (int, float)):
            return float(raw_target), True
        if isinstance(raw_target, dict):
            if not raw_target.get("enabled", True):
                return None, False
            val = None
            for k in ("target", "threshold", "val", "value"):
                if k in raw_target:
                    val = raw_target[k]
                    break
            if val is not None:
                return float(val), True
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
            # Поддержка плоской структуры: stagnation_card прямо внутри pos_cfg
            stag_cfg = pos_cfg if ("stagnation_card" in pos_cfg or "card" in pos_cfg) else {}

        if not stag_cfg or not stag_cfg.get("enabled", True):
            return base_th, False, "", (base_th or 0.0)

        card = stag_cfg.get("stagnation_card") or stag_cfg.get("card", {})
        if not card:
            return base_th, False, "", (base_th or 0.0)

        min_floor = float(stag_cfg.get("min_floor", 7.0))
        mode = stag_cfg.get("mode", "auto")
        velocity_pct_of = stag_cfg.get("velocity_pct_of", "top_target")

        now_ms = int(time.time() * 1000)
        peak_age_h = float(analytics_dict.get("iteration_peak_age_hours", analytics_dict.get("peak_age_hours", 0.0)))
        
        velocity = float(analytics_dict.get("iteration_net_velocity", analytics_dict.get("net_velocity", 0.0)))
        if "iteration_net_velocity" not in analytics_dict and "net_velocity" not in analytics_dict:
            first_ts = analytics_dict.get("iteration_start_ts", analytics_dict.get("first_trade_ts", now_ms))
            active_h = max(0.05, (now_ms - first_ts) / 3600000.0)
            iter_profit = float(analytics_dict.get("iteration_net_profit_usdt", analytics_dict.get("net_profit_usdt", 0.0)))
            velocity = round(iter_profit / active_h, 2)

        if mode == "auto":
            first_key = next(iter(card.keys()), "")
            if "<" in first_key and any(c.isdigit() and int(c) >= 3 for c in first_key) and "%" not in first_key:
                mode = "time"
            else:
                mode = "velocity"

        eval_val = velocity if mode == "velocity" else peak_age_h

        # Собираем активные цели карты для определения reference_top
        active_targets = []
        for rule, raw_target in card.items():
            target_float, is_en = cls.parse_card_target(raw_target)
            if is_en and target_float is not None:
                active_targets.append(target_float)

        if not active_targets:
            # Все уровни карты отключены
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
                # HTML-экранируем rule: '<=' -> '&lt;=' чтобы Telegram HTML-парсер не падал
                safe_rule = rule.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                if "%" in rule:
                    info = f"[{mode.upper()} {eval_val:.2f}{unit} matched '{safe_rule}' ({calc_threshold:.2f}{unit}) -> target {effective_target} USDT]"
                else:
                    info = f"[{mode.upper()} {eval_val:.2f}{unit} matched '{safe_rule}' -> target {effective_target} USDT]"
                is_stag = effective_target < reference_top
                return effective_target, is_stag, info, reference_top

        cls._last_matched_rule = ""
        fallback_target = base_th if base_th is not None else reference_top
        return fallback_target, False, "", reference_top

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
            
        # RISK SYSTEM CHECK (если задана)
        risk_cfg = app_data.get("stop_bot_rirk_system")
        if risk_cfg and risk_cfg.get("enabled"):
            roi_pct = analytics_data.get("roi_pct", 0.0)
            
            try:
                raw_critical_roi = float(risk_cfg["roi_critical_level_pct"])
                critical_roi = -abs(raw_critical_roi)
            except (ValueError, TypeError):
                logger.error(f"[RISK_SYSTEM] Invalid roi_critical_level_pct in config: {risk_cfg['roi_critical_level_pct']}")
                critical_roi = None
            
            if critical_roi is not None and roi_pct <= critical_roi:
                if not getattr(self.bot_core, "is_paused", False):
                    self._is_closing = True
                    logger.critical(f"[RISK_SYSTEM] ROI {roi_pct}% drops below critical {critical_roi}%! Stopping bot.")
                    
                    # 1. Block new entries
                    self.bot_core.entry_blocked = True
                    
                    try:
                        # 2. Close all positions
                        await self.bot_core.close_all_positions()
                        
                        # 3. Wait for analytics to sync (debounce timeout in AnalyticsManager is 10 sec)
                        logger.info("[RISK_SYSTEM] Waiting for analytics sync (12s)...")
                        await asyncio.sleep(12.0)
                        
                        # 4. Turn off bot
                        self.bot_core.is_paused = True
                        
                        # Turn off the risk system in config so it doesn't loop
                        app_data["stop_bot_rirk_system"]["enabled"] = False
                        Utils.write_json_file(app_json_path, app_data)
                        
                        msg = f"⛔ <b>CRITICAL RISK STOP</b> ⛔\n\nROI dropped to <b>{roi_pct}%</b> (Threshold: {critical_roi}%).\nAll positions closed. Bot is PAUSED."
                        if getattr(self.bot_core, 'notifier', None):
                            await self.bot_core.notifier.send_alert(msg)
                            
                    except Exception as e:
                        logger.error(f"[RISK_SYSTEM] Error during stop sequence: {e}")
                    finally:
                        self.bot_core.entry_blocked = False
                        self._is_closing = False
                    
                    return # Exit check early

        CLOSE_COOLDOWN_SEC = 45.0
        now_sec = time.time()

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
            if strat_stats_path.exists():
                strat_analytics = Utils.read_json_file(strat_stats_path) or {}
            else:
                strat_analytics = analytics_data

            strat_net = float(strat_analytics.get("net_profit_usdt", 0.0))
            iter_idx = int(strat_analytics.get("iteration_index", 1))
            iter_start_net = float(strat_analytics.get("iteration_start_net_profit_usdt", 0.0))
            iter_net = float(strat_analytics.get("iteration_net_profit_usdt", round(strat_net - iter_start_net, 4)))

            pos_cfg = strat_auto_cfg.get("positive", {})
            neg_cfg = strat_auto_cfg.get("negative", {})

            strat_triggered = False
            strat_msg = ""
            next_iter_idx = iter_idx + 1
            new_iter_start_net = round(strat_net, 4)

            # Check Positive for Strategy Dome (ориентируемся строго на Net текущей итерации)
            th_pos, is_stagnation, stag_info, ref_top = self.evaluate_stagnation_target(strat_analytics, pos_cfg)

            if th_pos is not None and iter_net >= th_pos:
                strat_triggered = True
                logger.info(
                    f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} Net ({iter_net:+.2f}) >= {th_pos} "
                    f"(Global Net: {strat_net:+.2f}) "
                    f"({'STAGNATION ' + stag_info if is_stagnation else 'TARGET'}). Auto-closing strategy dome!"
                )
                if is_stagnation:
                    strat_msg = (
                        f"🟢 <b>AUTO-CLOSE КУПОЛА [{strat_cfg.name}] (STAGNATION RATCHET)</b>\n"
                        f"Завершен <b>Цикл #{iter_idx}</b>!\n"
                        f"Прибыль цикла: <b>+{iter_net:.2f} USDT</b> (Порог: <b>{th_pos} USDT</b>).\n"
                        f"Глобальный Net Profit: <b>{strat_net:+.2f} USDT</b>.\n"
                        f"Сработал затухающий порог {stag_info}.\n"
                        f"Закрыты позиции монет: {strat_cfg.symbols}.\n"
                        f"Старт <b>Цикла #{next_iter_idx}</b> (базовая точка Net: <b>{new_iter_start_net:+.2f} USDT</b>)."
                    )
                else:
                    strat_msg = (
                        f"🟢 <b>AUTO-CLOSE КУПОЛА [{strat_cfg.name}] (TARGET REACHED)</b>\n"
                        f"Завершен <b>Цикл #{iter_idx}</b>!\n"
                        f"Прибыль цикла: <b>+{iter_net:.2f} USDT</b> (Порог: <b>{th_pos} USDT</b>).\n"
                        f"Глобальный Net Profit: <b>{strat_net:+.2f} USDT</b>.\n"
                        f"Закрыты позиции монет: {strat_cfg.symbols}.\n"
                        f"Старт <b>Цикла #{next_iter_idx}</b> (базовая точка Net: <b>{new_iter_start_net:+.2f} USDT</b>)."
                    )

            # Check Negative for Strategy Dome
            th_neg = neg_cfg.get("threshold")
            if th_neg == 0 or th_neg == 0.0:
                th_neg = None

            if not strat_triggered and th_neg is not None and iter_net <= th_neg:
                strat_triggered = True
                logger.info(
                    f"[AUTO_CLOSER][{strat_id}] Iteration #{iter_idx} Net ({iter_net:+.2f}) <= {th_neg} "
                    f"(Global Net: {strat_net:+.2f}). Auto-closing strategy dome!"
                )
                strat_msg = (
                    f"🔴 <b>AUTO-CLOSE КУПОЛА [{strat_cfg.name}] (NEGATIVE STOP)</b>\n"
                    f"Завершен <b>Цикл #{iter_idx}</b> по защитному стопу!\n"
                    f"Просадка цикла: <b>{iter_net:.2f} USDT</b> (Порог: <b>{th_neg} USDT</b>).\n"
                    f"Глобальный Net Profit: <b>{strat_net:+.2f} USDT</b>.\n"
                    f"Закрыты позиции монет: {strat_cfg.symbols}.\n"
                    f"Старт <b>Цикла #{next_iter_idx}</b> (базовая точка Net: <b>{new_iter_start_net:+.2f} USDT</b>)."
                )

            if strat_triggered:
                try:
                    self._is_closing = True
                    self._last_close_ts[strat_id] = time.time()

                    # 1. Закрываем позиции и отменяем ордера (в bot.py также блокируется вход до следующей свечи)
                    await self.bot_core.close_strategy_positions(strat_id)

                    # 2. Продвигаем и сохраняем новую итерацию (после закрытия всех позиций Unrealized = 0, поэтому Realized равен Net)
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

                    # Синхронизируем прогресс итерации в CACHE/iteration_state.json
                    try:
                        c_file = CACHE_DIR / "iteration_state.json"
                        c_dict = {}
                        if c_file.exists():
                            try:
                                c_dict = json.loads(c_file.read_text(encoding="utf-8"))
                            except Exception:
                                c_dict = {}
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

                    # 3. Сбрасываем recent_closes у монет стратегии, чтобы Knife Blocker не блокировал входы в новом цикле
                    if hasattr(self.bot_core, "fsm_states"):
                        for sym in strat_cfg.symbols:
                            if sym in self.bot_core.fsm_states:
                                for s_side in ("LONG", "SHORT"):
                                    if s_side in self.bot_core.fsm_states[sym]:
                                        self.bot_core.fsm_states[sym][s_side].recent_closes = []

                    Utils.write_json_file(app_json_path, app_data)
                    if strat_msg and getattr(self.bot_core, 'notifier', None):
                        await self.bot_core.notifier.send_alert(strat_msg)

                    # 4. ХУК ШТУРМАНА: Оценка ротации обоймы по храповику стагнации
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
        next_iter_idx_g = iter_idx_g + 1
        new_iter_start_net_g = round(net_usdt, 4)
        
        # Check Positive Portfolio (с поддержкой карты стагнации по Net итерации)
        th_pos, is_stagnation, stag_info, ref_top = self.evaluate_stagnation_target(analytics_data, pos_cfg)
        
        if th_pos is not None and iter_net_g >= th_pos:
            self._is_closing = True
            triggered = True
            logger.info(
                f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} Net ({iter_net_g:+.2f}) >= {th_pos} "
                f"(Global Net: {net_usdt:+.2f}) "
                f"({'STAGNATION ' + stag_info if is_stagnation else 'TARGET'}). Auto-closing portfolio!"
            )
            if is_stagnation:
                action_msg = (
                    f"🟢 <b>AUTO-CLOSE (PORTFOLIO STAGNATION RATCHET)</b>\n"
                    f"Завершен <b>Цикл #{iter_idx_g}</b> портфеля!\n"
                    f"Прибыль цикла: <b>+{iter_net_g:.2f} USDT</b> (Порог: <b>{th_pos} USDT</b>).\n"
                    f"Глобальный Net Profit: <b>{net_usdt:+.2f} USDT</b>.\n"
                    f"Сработал затухающий порог {stag_info}.\n"
                    f"Старт <b>Цикла #{next_iter_idx_g}</b> (базовая точка Net: <b>{new_iter_start_net_g:+.2f} USDT</b>)."
                )
            else:
                action_msg = (
                    f"🟢 <b>AUTO-CLOSE (PORTFOLIO TARGET REACHED)</b>\n"
                    f"Завершен <b>Цикл #{iter_idx_g}</b> портфеля!\n"
                    f"Прибыль цикла: <b>+{iter_net_g:.2f} USDT</b> (Порог: <b>{th_pos} USDT</b>).\n"
                    f"Глобальный Net Profit: <b>{net_usdt:+.2f} USDT</b>.\n"
                    f"Старт <b>Цикла #{next_iter_idx_g}</b> (базовая точка Net: <b>{new_iter_start_net_g:+.2f} USDT</b>)."
                )
                
        # Check Negative Portfolio
        th_neg = neg_cfg.get("threshold")
        if th_neg == 0 or th_neg == 0.0:
            th_neg = None
            
        if not triggered and th_neg is not None and iter_net_g <= th_neg:
            self._is_closing = True
            triggered = True
            logger.info(
                f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} Net ({iter_net_g:+.2f}) <= {th_neg} "
                f"(Global Net: {net_usdt:+.2f}). Auto-closing portfolio!"
            )
            action_msg = (
                f"🔴 <b>AUTO-CLOSE (PORTFOLIO NEGATIVE STOP)</b>\n"
                f"Завершен <b>Цикл #{iter_idx_g}</b> портфеля по стопу!\n"
                f"Просадка цикла: <b>{iter_net_g:.2f} USDT</b> (Порог: <b>{th_neg} USDT</b>).\n"
                f"Глобальный Net Profit: <b>{net_usdt:+.2f} USDT</b>.\n"
                f"Старт <b>Цикла #{next_iter_idx_g}</b> (базовая точка Net: <b>{new_iter_start_net_g:+.2f} USDT</b>)."
            )

        if triggered:
            try:
                self._last_close_ts["__global__"] = time.time()
                await self.bot_core.close_all_positions()

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
                    c_dict = {}
                    if c_file.exists():
                        try:
                            c_dict = json.loads(c_file.read_text(encoding="utf-8"))
                        except Exception:
                            c_dict = {}
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
