# CORE/SERVICES/portfolio_closer.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.portfolio_closer
ROLE: Проверка условий и оркестрация глобального закрытия портфеля
==============================================================================
"""

import copy
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from consts import GLOBAL_ANALYTICS_JSON
from c_log import UnifiedLogger
from CORE.STRATEGY.trailing_system import ToxicityEngine
from CORE.SERVICES.dome_closer_executor import DomeCloserExecutor

logger = UnifiedLogger("PortfolioCloser")


class PortfolioCloser:
    """Глобальный мониторинг портфеля и исполнение автозакрытия."""

    @staticmethod
    async def check(
        auto_closer_inst: Any,
        app_data: dict,
        app_json_path: Any,
        analytics_data: dict,
        now_sec: float,
        now_ms: int,
        fsm: Any
    ):
        CLOSE_COOLDOWN_SEC = 45.0
        # ГЛОБАЛЬНЫЙ ПОРТФЕЛЬНЫЙ AUTO-CLOSING (если задан на верхнем уровне)
        auto_cfg = app_data.get("auto_closing")
        if not auto_cfg:
            return

        if now_sec - auto_closer_inst._last_close_ts.get("__global__", 0.0) < CLOSE_COOLDOWN_SEC:
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
        r_state_g = auto_closer_inst._trailing_ratchet_state.setdefault("__global__", {})
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
                auto_closer_inst._is_closing = True
                triggered = True
                logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} {r_info}. Auto-closing portfolio!")
                action_msg = auto_closer_inst._build_close_msg(
                    name="ПОРТФЕЛЯ", symbols_str="", iter_idx=iter_idx_g, next_iter_idx=next_iter_idx_g,
                    iter_net=iter_net_g, strat_net=net_usdt, new_iter_start_net=new_iter_start_net_g,
                    close_type="TOXICITY ADAPTIVE",
                    detail_info=f"Зафиксирована прибыль: <b>+{iter_net_g:.2f} USDT</b> (Floor: <b>{trig_floor:.2f} USDT</b>).\nИнвентарь: Score: <b>{t_score_g:.2f}</b>, Recovery: <b>{delta_rec_g:.0%}</b>.\nСобытие: {r_info}."
                )

        elif mode_g in ("trailing_ratchet", "classic", "iter_net_classic"):
            scale_money_g = float(pos_cfg.get("scale_money", auto_cfg.get("scale_money", 1.0)))
            if scale_money_g <= 0:
                scale_money_g = 1.0
            r_cfg_g_scaled = copy.deepcopy(r_cfg_g or {})
            r_cfg_g_scaled["scale_money"] = scale_money_g
            r_cfg_g_scaled["scale_factor"] = scale_money_g
            should_close, trig_floor, r_info, is_stagnation = auto_closer_inst.evaluate_trailing_ratchet(
                iter_net=iter_net_g, iter_age_hours=iter_age_h_g, ratchet_cfg=r_cfg_g_scaled, state=r_state_g
            )
            if should_close and trig_floor is not None:
                auto_closer_inst._is_closing = True
                triggered = True
                logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} {r_info}. Auto-closing portfolio!")
                action_msg = auto_closer_inst._build_close_msg(
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
                auto_closer_inst._is_closing = True
                triggered = True
                logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} {f_info}. Auto-closing portfolio!")
                action_msg = auto_closer_inst._build_close_msg(
                    name="ПОРТФЕЛЯ", symbols_str="", iter_idx=iter_idx_g, next_iter_idx=next_iter_idx_g,
                    iter_net=iter_net_g, strat_net=net_usdt, new_iter_start_net=new_iter_start_net_g,
                    close_type="FIXED NET PROFIT",
                    detail_info=f"Зафиксирована прибыль: <b>+{iter_net_g:.2f} USDT</b> (Цель: <b>+{trig_target:.2f} USDT</b>).\nСобытие: {f_info}."
                )

        else:
            scale_money_g = float(pos_cfg.get("scale_money", auto_cfg.get("scale_money", 1.0)))
            if scale_money_g <= 0:
                scale_money_g = 1.0
            th_pos, is_stagnation, stag_info, ref_top = auto_closer_inst.evaluate_stagnation_target(analytics_data, pos_cfg)
            if th_pos is not None:
                th_pos = round(th_pos * scale_money_g, 2)
            if th_pos is not None and iter_net_g >= th_pos:
                auto_closer_inst._is_closing = True
                triggered = True
                logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} Net ({iter_net_g:+.2f}) >= {th_pos}. Auto-closing portfolio!")
                close_tag = "PORTFOLIO STAGNATION RATCHET" if is_stagnation else "PORTFOLIO TARGET REACHED"
                detail = f"Сработал затухающий порог {stag_info}." if is_stagnation else f"Достигнута цель {th_pos} USDT."
                action_msg = auto_closer_inst._build_close_msg(
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
            th_neg = -abs(float(th_neg))
            th_neg = round(th_neg * scale_money_neg, 2)

        if not triggered and th_neg is not None and iter_net_g <= th_neg:
            auto_closer_inst._is_closing = True
            triggered = True
            is_stagnation = False
            logger.info(f"[AUTO_CLOSER] Portfolio Iteration #{iter_idx_g} Net ({iter_net_g:+.2f}) <= {th_neg}. Auto-closing portfolio!")
            action_msg = auto_closer_inst._build_close_msg(
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
            auto_closer_inst._is_closing = False

        if triggered:
            try:
                auto_closer_inst._last_close_ts["__global__"] = time.time()
                await DomeCloserExecutor.execute_close(
                    bot_core=auto_closer_inst.bot_core,
                    strat_id="__global__",
                    is_global=True,
                    strat_cfg=None,
                    iter_idx=iter_idx_g,
                    next_iter_idx=next_iter_idx_g,
                    iter_net=iter_net_g,
                    strat_net=net_usdt,
                    stats_path=GLOBAL_ANALYTICS_JSON,
                    raw_analytics=analytics_data,
                    app_data=app_data,
                    app_json_path=app_json_path,
                    action_msg=action_msg,
                    trailing_ratchet_state=auto_closer_inst._trailing_ratchet_state,
                )
            except Exception as e:
                logger.error(f"[AUTO_CLOSER] Error during portfolio auto-close: {e}")
            finally:
                auto_closer_inst._is_closing = False
