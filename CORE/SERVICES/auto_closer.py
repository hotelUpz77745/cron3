# CORE/SERVICES/auto_closer.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.auto_closer
ROLE: Сервис автоматического закрытия позиций (PnL куполов, таймауты, трейлинг-рэтчет, убытки)
==============================================================================
"""

import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional, Dict
from consts import DATA_DIR, ANALYTICS_STATS_DIR, GLOBAL_ANALYTICS_JSON, CACHE_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.STRATEGY.trailing_system import BaseTrailingSystem, ToxicityEngine
from CORE.SERVICES.stagnation_evaluator import StagnationEvaluator
from CORE.SERVICES.toxicity_extractor import ToxicityMetricsExtractor
from CORE.SERVICES.dome_closer_executor import DomeCloserExecutor
from CORE.SERVICES.portfolio_closer import PortfolioCloser

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
        matched, thresh = StagnationEvaluator.match_rule_adaptive(rule, eval_val, ref_target)
        if matched:
            cls._last_matched_rule = StagnationEvaluator.last_matched_rule
        return matched, thresh

    @classmethod
    def match_rule_float(cls, rule: str, val: float, ref_target: float = 1.0) -> bool:
        return StagnationEvaluator.match_rule_float(rule, val, ref_target)

    @staticmethod
    def parse_card_target(raw_target: Any) -> tuple[Optional[float], bool]:
        return StagnationEvaluator.parse_card_target(raw_target)

    @classmethod
    def evaluate_stagnation_target(
        cls, analytics_dict: dict, pos_cfg: dict
    ) -> tuple[Optional[float], bool, str, float]:
        target, is_stag, info, ref = StagnationEvaluator.evaluate_stagnation_target(analytics_dict, pos_cfg)
        cls._last_matched_rule = StagnationEvaluator.last_matched_rule
        return target, is_stag, info, ref

    @classmethod
    def extract_toxicity_metrics(cls, strat_cfg, analytics_dict: dict, bot_core) -> tuple[float, float, float, float]:
        return ToxicityMetricsExtractor.extract(strat_cfg, analytics_dict, bot_core)

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
        analytics_data = Utils.read_json_file(GLOBAL_ANALYTICS_JSON)
        if not analytics_data:
            return
        now_sec = time.time()
        now_ms = int(now_sec * 1000)

        # 1. Стратегические купола
        await self._check_strategy_domes(app_data, app_json_path, now_sec, now_ms, fsm)

        # 2. Глобальный портфельный автозакрыватель
        await self._check_portfolio(app_data, app_json_path, analytics_data, now_sec, now_ms, fsm)

    async def _check_strategy_domes(self, app_data: dict, app_json_path: Any, now_sec: float, now_ms: int, fsm: Any):
        CLOSE_COOLDOWN_SEC = 45.0
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

            strat_stats_path = ANALYTICS_STATS_DIR / f"analytics_{strat_id}.json"
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
                    await DomeCloserExecutor.execute_close(
                        bot_core=self.bot_core,
                        strat_id=strat_id,
                        is_global=False,
                        strat_cfg=strat_cfg,
                        iter_idx=iter_idx,
                        next_iter_idx=next_iter_idx,
                        iter_net=iter_net,
                        strat_net=strat_net,
                        stats_path=strat_stats_path,
                        raw_analytics=strat_analytics,
                        app_data=app_data,
                        app_json_path=app_json_path,
                        action_msg=strat_msg,
                        trailing_ratchet_state=self._trailing_ratchet_state,
                    )
                except Exception as e:
                    logger.error(f"[AUTO_CLOSER][{strat_id}] Error in strategy auto-close: {e}")
                finally:
                    self._is_closing = False


    async def _check_portfolio(self, app_data: dict, app_json_path: Any, analytics_data: dict, now_sec: float, now_ms: int, fsm: Any):
        """Делегирует проверку и закрытие глобального портфеля специализированному PortfolioCloser."""
        await PortfolioCloser.check(
            auto_closer_inst=self,
            app_data=app_data,
            app_json_path=app_json_path,
            analytics_data=analytics_data,
            now_sec=now_sec,
            now_ms=now_ms,
            fsm=fsm,
        )
