# ANALYTICS/strategy_stats.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.strategy_stats
ROLE: Strategy-level analytics calculations, iteration tracking, and leaderboard
==============================================================================
"""

import csv
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from consts import (
    ANALYTICS_START_BALANCE_USDT,
    CACHE_DIR,
)
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("StrategyStats")


class StrategyStatsEngine:
    """Manages strategy-level analytics, iteration cycles, and leaderboard updates."""

    def __init__(self, manager):
        self.manager = manager

    @property
    def cache_dir(self):
        return getattr(self.manager, "cache_dir", CACHE_DIR)

    def get_iteration_cache(self, strat_id: str) -> dict:
        """Retrieves iteration telemetry cache for a specific strategy."""
        try:
            cache_file = self.cache_dir / "iteration_state.json"
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                return data.get(strat_id, {})
        except Exception:
            pass
        return {}

    def save_iteration_cache(self, strat_id: str, state: dict) -> None:
        """Saves iteration telemetry state to persistent cache."""
        try:
            cache_file = self.cache_dir / "iteration_state.json"
            data = {}
            if cache_file.exists():
                try:
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data[strat_id] = state
            Utils.atomic_write_json(cache_file, data, indent=4)
        except Exception as e:
            logger.error(f"[ANALYTICS] Error saving iteration cache for {strat_id}: {e}")

    def update_strategies_stats(self, global_data: dict) -> None:
        """Рассчитывает и сохраняет изолированную аналитику по стратегиям и лидерборд."""
        is_synced = bool(getattr(self.manager, "_floating_synced", False) or global_data.get("_floating_synced", False))
        if not is_synced:
            logger.info("[STRATEGY_STATS] ❄️ Hands off analytics! REST API floating PnL not confirmed yet. Skipping recalculation to preserve disk state.")
            return

        if not getattr(self.manager, "strategy_registry", None):
            try:
                from CORE.STRATEGY.strategy_registry import StrategyRegistry
                self.manager.strategy_registry = StrategyRegistry()
            except Exception:
                return

        now_ms = int(time.time() * 1000)
        per_coin_global = global_data.get("per_coin", {})

        # Aggregation of Global Toxic Defense from Source of Truth Ledgers
        g_peel_cnt, g_peel_vol, g_peel_pnl = 0, 0.0, 0.0
        if self.manager.peels_ledger.exists():
            try:
                with open(self.manager.peels_ledger, "r", encoding="utf-8") as pf:
                    r = csv.reader(pf, delimiter=';')
                    next(r, None)
                    for row in r:
                        if len(row) >= 12:
                            g_peel_cnt += 1
                            g_peel_vol += float(row[10])
                            g_peel_pnl += float(row[11])
            except Exception:
                pass

        g_rec_cnt, g_rec_vol, g_rec_spent, g_rec_absorbed = 0, 0.0, 0.0, 0.0
        if self.manager.recycling_ledger.exists():
            try:
                with open(self.manager.recycling_ledger, "r", encoding="utf-8") as rf:
                    r = csv.reader(rf, delimiter=';')
                    next(r, None)
                    for row in r:
                        if len(row) >= 13:
                            g_rec_cnt += 1
                            g_rec_vol += float(row[10])
                            g_rec_absorbed += float(row[11])
                            g_rec_spent += float(row[12])
            except Exception:
                pass

        global_data["toxic_defense"] = {
            "subgrid_peeling": {"events_count": g_peel_cnt, "total_peeled_usdt": round(g_peel_vol, 4), "locked_micro_pnl_usdt": round(g_peel_pnl, 4)},
            "cross_recycling": {"events_count": g_rec_cnt, "burned_volume_usdt": round(g_rec_vol, 4), "profit_spent_usdt": round(g_rec_spent, 4), "loss_absorbed_usdt": round(g_rec_absorbed, 4)},
        }

        for strat_id, strat_cfg in self.manager.strategy_registry.active_strategies.items():
            strat_file = self.manager.stats_dir / f"analytics_{strat_id}.json"
            strat_data = {}
            if strat_file.exists():
                try:
                    strat_data = json.loads(strat_file.read_text(encoding="utf-8"))
                except Exception:
                    strat_data = {}
            strat_init_bal = float(strat_cfg.analytics_cfg.get("start_balance_usdt", strat_cfg.start_balance_usdt))
            if not strat_data:
                strat_data = {
                    "universe_id": strat_id,
                    "name": strat_cfg.name,
                    "start_balance_usdt": strat_init_bal,
                    "_rebased_start_balance_usdt": strat_init_bal,
                    "first_trade_ts": now_ms,
                    "cur_balance_usdt": strat_init_bal,
                    "peak_balance_usdt": strat_init_bal,
                    "min_balance_usdt": strat_init_bal,
                    "_current_trough_usdt": strat_init_bal,
                    "total_trades": 0,
                    "winning_trades": 0,
                    "winrate_pct": 0.0,
                    "realized_pnl_usdt": 0.0,
                    "net_profit_usdt": 0.0,
                    "unrealized_pnl_usdt": 0.0,
                    "max_drawdown_usdt": 0.0,
                    "max_drawdown_pct": 0.0,
                    "recovery_factor": 0.0,
                    "iteration_index": 1,
                    "iteration_start_net_profit_usdt": 0.0,
                    "iteration_net_profit_usdt": 0.0,
                    "iteration_start_ts": now_ms,
                    "iteration_peak_net_profit_usdt": 0.0,
                    "iteration_peak_ts": now_ms,
                    "iteration_peak_age_hours": 0.0,
                    "iteration_net_velocity": 0.0,
                    "per_coin": {},
                }

            strat_symbols = list(strat_cfg.symbols)
            existing_per_coin: dict = strat_data.get("per_coin") if isinstance(strat_data.get("per_coin"), dict) else {}
            # Собираем список всех символов стратегии: текущие активные + исторические с историей торговли
            all_strat_symbols = set(strat_symbols)
            for sym, c_info in existing_per_coin.items():
                c_trades = int(c_info.get("total_trades", c_info.get("trades", 0)))
                c_pnl = abs(float(c_info.get("realized_pnl_net_usdt", c_info.get("realized_pnl_usdt", 0.0))))
                if c_trades > 0 or c_pnl > 1e-6:
                    all_strat_symbols.add(sym)

            # Сохраняем персистентный список historical_symbols стратегии
            saved_hist: list = strat_data.get("historical_symbols") if isinstance(strat_data.get("historical_symbols"), list) else []
            for sym in saved_hist:
                if sym in per_coin_global or sym in existing_per_coin:
                    all_strat_symbols.add(sym)

            # Выбывшие монеты из глобальной аналитики должны сохраняться в отчетах как неактивные
            active_strats = self.manager.strategy_registry.get_active_strategies()
            for sym, c_info in per_coin_global.items():
                c_trades = int(c_info.get("total_trades", c_info.get("trades", 0)))
                c_pnl = abs(float(c_info.get("realized_pnl_net_usdt", c_info.get("realized_pnl_usdt", 0.0))))
                if c_trades > 0 or c_pnl > 1e-6:
                    # Если на счете 1 активная стратегия, либо монета не принадлежит ни одной другой активной стратегии
                    if len(active_strats) <= 1:
                        all_strat_symbols.add(sym)
                    else:
                        other_has = any(
                            sym in other_cfg.symbols
                            for other_id, other_cfg in active_strats.items()
                            if other_id != strat_id
                        )
                        if not other_has:
                            all_strat_symbols.add(sym)

            strat_data["historical_symbols"] = sorted(list(all_strat_symbols))

            strat_per_coin = {}
            for sym in sorted(all_strat_symbols):
                if sym in per_coin_global:
                    c_data = dict(per_coin_global[sym])
                elif sym in existing_per_coin:
                    c_data = dict(existing_per_coin[sym])
                else:
                    continue

                is_active = (sym in strat_symbols)
                c_data["is_active_symbol"] = is_active
                c_data["is_active"] = is_active
                if not is_active:
                    # Если монета выведена из стратегии и позиция закрыта — обнуляем текущую нагрузку и нереализованную просадку
                    if abs(float(c_data.get("long_amt", 0.0))) <= 1e-8 and abs(float(c_data.get("short_amt", 0.0))) <= 1e-8:
                        c_data["current_drawdown"] = 0.0
                        c_data["current_grid_load_usdt"] = 0.0
                        c_data["current_grid_load_pct"] = 0.0
                        c_data["current_margin_usdt"] = 0.0
                        c_data["margin_load_pct"] = 0.0
                s_init_coin = float(strat_data.get("start_balance_usdt", ANALYTICS_START_BALANCE_USDT))
                c_np = float(c_data.get("net_profit_usdt", 0.0))
                c_data["roi_pct"] = round((c_np / s_init_coin) * 100.0, 2) if s_init_coin > 0 else 0.0
                strat_per_coin[sym] = c_data

            strat_data["per_coin"] = strat_per_coin

            s_unreal = sum(float(c.get("current_drawdown", 0.0)) for c in strat_per_coin.values())
            s_real = sum(float(c.get("realized_pnl_net_usdt", c.get("realized_pnl_usdt", 0.0))) for c in strat_per_coin.values())
            s_real_gross = sum(float(c.get("realized_pnl_usdt", c.get("realized_pnl_net_usdt", 0.0))) for c in strat_per_coin.values())
            s_comm = sum(float(c.get("commission_usdt", 0.0)) for c in strat_per_coin.values())
            s_fund = sum(float(c.get("funding_usdt", 0.0)) for c in strat_per_coin.values())
            s_trades = sum(int(c.get("total_trades", c.get("trades", 0))) for c in strat_per_coin.values())
            s_wins = sum(int(c.get("winning_trades", c.get("win_count", 0))) for c in strat_per_coin.values())

            strat_data["unrealized_pnl_usdt"] = round(s_unreal, 4)
            strat_data["realized_pnl_usdt"] = round(s_real_gross if s_real_gross != 0.0 else s_real, 4)
            strat_data["realized_pnl_net_usdt"] = round(s_real, 4)
            strat_data["total_commission_usdt"] = round(s_comm, 4)
            strat_data["total_funding_usdt"] = round(s_fund, 4)
            strat_data["net_profit_usdt"] = round(s_real + s_unreal, 4)
            if s_trades > 0:
                strat_data["total_trades"] = s_trades
                strat_data["winning_trades"] = s_wins
                strat_data["winrate_pct"] = round(s_wins / s_trades * 100.0, 2)

            # Расчет Grid Load и Margin Load под куполом стратегии
            s_grid_cap = sum(float(c.get("grid_capacity_usdt", 0.0)) for c in strat_per_coin.values())
            s_grid_load = sum(float(c.get("current_grid_load_usdt", 0.0)) for c in strat_per_coin.values())
            s_margin_used = sum(float(c.get("current_margin_usdt", 0.0)) for c in strat_per_coin.values())

            strat_data["total_grid_size_usdt"] = round(s_grid_cap, 2)
            strat_data["current_grid_load_usdt"] = round(s_grid_load, 4)
            strat_data["grid_load_pct"] = round((s_grid_load / s_grid_cap) * 100.0, 2) if s_grid_cap > 0 else 0.0
            strat_data["max_grid_load_usdt"] = round(max(float(strat_data.get("max_grid_load_usdt", 0.0)), s_grid_load), 4)
            strat_data["max_grid_load_pct"] = round((strat_data["max_grid_load_usdt"] / s_grid_cap) * 100.0, 2) if s_grid_cap > 0 else 0.0
            strat_data["total_margin_used_usdt"] = round(s_margin_used, 4)
            strat_data["max_margin_used_usdt"] = round(max(float(strat_data.get("max_margin_used_usdt", 0.0)), s_margin_used), 4)

            # Rebase Guard for Strategy
            initial = float(strat_data.get("start_balance_usdt", ANALYTICS_START_BALANCE_USDT))
            last_rebase = float(strat_data.get("_rebased_start_balance_usdt", initial))
            if abs(initial - last_rebase) > 0.0001:
                delta = initial - last_rebase
                peak_cur = float(strat_data["peak_balance_usdt"]) if strat_data.get("peak_balance_usdt") is not None else initial
                strat_data["peak_balance_usdt"] = round(peak_cur + delta, 4)
                trough_cur = float(strat_data["_current_trough_usdt"]) if strat_data.get("_current_trough_usdt") is not None else initial
                strat_data["_current_trough_usdt"] = round(trough_cur + delta, 4)
                min_cur = float(strat_data["min_balance_usdt"]) if strat_data.get("min_balance_usdt") is not None else initial
                strat_data["min_balance_usdt"] = round(min_cur + delta, 4)
                strat_data["_rebased_start_balance_usdt"] = initial

            s_init = float(strat_data.get("start_balance_usdt", ANALYTICS_START_BALANCE_USDT))
            s_cur = round(s_init + strat_data["net_profit_usdt"], 4)
            strat_data["cur_balance_usdt"] = s_cur
            strat_data["roi_pct"] = round(((s_cur - s_init) / s_init) * 100.0, 2) if s_init > 0 else 0.0

            free_margin = max(0.0, s_cur - s_margin_used)
            strat_data["free_margin_usdt"] = round(free_margin, 4)
            strat_data["free_margin_pct"] = round((free_margin / s_cur) * 100.0, 2) if s_cur > 0 else 0.0
            strat_data["margin_load_pct"] = round((s_margin_used / s_cur) * 100.0, 2) if s_cur > 0 else 0.0

            s_peak = float(strat_data.get("peak_balance_usdt", s_init))
            s_trough = float(strat_data.get("_current_trough_usdt", s_init))
            s_min = float(strat_data.get("min_balance_usdt", s_init))

            strat_data["_floating_synced"] = True
            has_active_positions = any(
                abs(float(c.get("long_amt", 0.0))) > 1e-8 or abs(float(c.get("short_amt", 0.0))) > 1e-8
                for c in strat_per_coin.values()
            )
            can_update_strat_peak = True
            if has_active_positions and abs(s_unreal) < 1e-5:
                can_update_strat_peak = False
                logger.warning(
                    f"[STRATEGY_STATS][{strat_id}] Peak guard triggered: active position(s) exist, "
                    f"but s_unreal is 0.0. Blocking peak balance update."
                )

            if s_cur > s_peak and can_update_strat_peak:
                s_peak = s_cur
                s_trough = s_cur
                strat_data["peak_net_ts"] = now_ms
            elif "peak_net_ts" not in strat_data:
                strat_data["peak_net_ts"] = strat_data.get("first_trade_ts", now_ms)

            if s_cur < s_trough:
                s_trough = s_cur
            if s_cur < s_min:
                s_min = s_cur
                strat_data["min_balance_ts"] = now_ms

            strat_data["peak_balance_usdt"] = round(s_peak, 4)
            strat_data["peak_net_profit_usdt"] = round(s_peak - s_init, 4)
            strat_data["_current_trough_usdt"] = round(s_trough, 4)
            strat_data["min_balance_usdt"] = round(s_min, 4)

            # Strategy Iteration Telemetry & Historical Records
            iter_cache = self.get_iteration_cache(strat_id)
            hist_peak = float(iter_cache.get("historical_peak_net_profit_usdt", 0.0))
            hist_peak_ts = iter_cache.get("historical_peak_net_ts")
            hist_max_dd = float(iter_cache.get("historical_max_drawdown_usdt", 0.0))

            cur_peak_net = float(strat_data.get("peak_net_profit_usdt", 0.0))
            if hist_peak > cur_peak_net and can_update_strat_peak:
                strat_data["peak_net_profit_usdt"] = hist_peak
                s_peak = s_init + hist_peak
                strat_data["peak_balance_usdt"] = round(s_peak, 4)
                if hist_peak_ts:
                    strat_data["peak_net_ts"] = hist_peak_ts

            # Telemetry: Peak Age & Net Velocity
            peak_ts_s = strat_data.get("peak_net_ts") or now_ms
            strat_data["peak_age_hours"] = round(max(0.0, (now_ms - peak_ts_s) / 3600000.0), 2)
            first_ts_s = strat_data.get("first_trade_ts") or now_ms
            active_h_s = max(0.1, (now_ms - first_ts_s) / 3600000.0)
            strat_data["net_velocity"] = round(float(strat_data.get("net_profit_usdt", 0.0)) / active_h_s, 2)

            s_net = float(strat_data.get("net_profit_usdt", 0.0))
            iter_idx = int(strat_data.get("iteration_index") or iter_cache.get("iteration_index") or 1)

            if iter_idx == 1:
                iter_start_net = 0.0
            elif "iteration_start_net_profit_usdt" in strat_data:
                iter_start_net = float(strat_data["iteration_start_net_profit_usdt"])
            elif "iteration_start_net_profit_usdt" in iter_cache:
                iter_start_net = float(iter_cache["iteration_start_net_profit_usdt"])
            else:
                iter_start_net = 0.0

            iter_net = round(s_net - iter_start_net, 4)
            iter_start_ts = strat_data.get("iteration_start_ts") or iter_cache.get("iteration_start_ts") or first_ts_s

            iter_peak_net = float(
                strat_data.get("iteration_peak_net_profit_usdt")
                if strat_data.get("iteration_peak_net_profit_usdt") is not None
                else iter_cache.get("iteration_peak_net_profit_usdt", iter_net)
            )
            iter_peak_ts = strat_data.get("iteration_peak_ts") or iter_cache.get("iteration_peak_ts") or iter_start_ts
            if iter_net > iter_peak_net and can_update_strat_peak:
                iter_peak_net = iter_net
                iter_peak_ts = now_ms

            strat_data["iteration_index"] = iter_idx
            strat_data["iteration_start_net_profit_usdt"] = round(iter_start_net, 4)
            strat_data["iteration_net_profit_usdt"] = iter_net
            strat_data["iteration_start_ts"] = iter_start_ts
            strat_data["iteration_peak_net_profit_usdt"] = round(iter_peak_net, 4)
            strat_data["iteration_peak_ts"] = iter_peak_ts
            strat_data["iteration_peak_age_hours"] = round(max(0.0, (now_ms - iter_peak_ts) / 3600000.0), 2)

            iter_active_h = max(0.05, (now_ms - iter_start_ts) / 3600000.0)
            strat_data["iteration_net_velocity"] = round(iter_net / iter_active_h, 2)

            # Iteration Realized PnL
            if iter_idx == 1:
                iter_start_real = 0.0
            elif "iteration_start_realized_pnl_usdt" in strat_data:
                iter_start_real = float(strat_data["iteration_start_realized_pnl_usdt"])
            elif "iteration_start_realized_pnl_usdt" in iter_cache:
                iter_start_real = float(iter_cache["iteration_start_realized_pnl_usdt"])
            else:
                iter_start_real = s_real if iter_idx > 1 else 0.0

            iter_real = round(s_real - iter_start_real, 4)
            strat_data["iteration_start_realized_pnl_usdt"] = round(iter_start_real, 4)
            strat_data["iteration_realized_pnl_usdt"] = iter_real

            cur_peak = strat_data.get("peak_net_profit_usdt", 0.0)
            if cur_peak > hist_peak:
                hist_peak = cur_peak
                hist_peak_ts = strat_data.get("peak_net_ts", now_ms)
            net_dd = min(0.0, round(s_min - s_init, 4))
            hist_max_dd = net_dd
            calc_dd = net_dd
            strat_data["max_drawdown_usdt"] = calc_dd

            # Сохраняем состояние итерации и исторические рекорды в кеш
            self.save_iteration_cache(
                strat_id,
                {
                    "iteration_index": iter_idx,
                    "iteration_start_net_profit_usdt": round(iter_start_net, 4),
                    "iteration_start_realized_pnl_usdt": round(iter_start_real, 4),
                    "iteration_realized_pnl_usdt": iter_real,
                    "iteration_start_ts": iter_start_ts,
                    "iteration_peak_net_profit_usdt": round(iter_peak_net, 4),
                    "iteration_peak_ts": iter_peak_ts,
                    "historical_peak_net_profit_usdt": hist_peak,
                    "historical_peak_net_ts": hist_peak_ts,
                    "historical_max_drawdown_usdt": hist_max_dd,
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                },
            )

            strat_data["max_drawdown_usdt"] = round(calc_dd, 4)

            if s_init > 0:
                strat_data["max_drawdown_pct"] = round(abs(strat_data["max_drawdown_usdt"]) / s_init * 100.0, 2)
            if strat_data["max_drawdown_usdt"] < 0:
                strat_data["recovery_factor"] = round(
                    strat_data["net_profit_usdt"] / abs(strat_data["max_drawdown_usdt"]), 2
                )
            else:
                strat_data["recovery_factor"] = (
                    round(strat_data["net_profit_usdt"] / 1.0, 2) if strat_data["net_profit_usdt"] > 0 else 0.0
                )

            # Расчет Trade Velocity для конкретного купола стратегии
            try:
                if not hasattr(self.manager, "_strat_velocity_engines"):
                    self.manager._strat_velocity_engines = {}
                if strat_id not in self.manager._strat_velocity_engines:
                    from ANALYTICS.trade_velocity import TradeVelocityEngine
                    strat_ledger = self.manager.ledgers_dir / f"trades_ledger_{strat_id}.txt"
                    self.manager._strat_velocity_engines[strat_id] = TradeVelocityEngine(
                        universe_id=strat_id, ledger_path=strat_ledger
                    )
                strat_data["trade_velocity"] = self.manager._strat_velocity_engines[strat_id].calculate_velocity(strat_data)
            except Exception as sve:
                logger.warning(f"[ANALYTICS] Error calculating trade velocity for {strat_id}: {sve}")

            # Toxic Defense Aggregation for Strategy (Source of Truth: Strategy Ledgers)
            strat_peel_txt = self.manager.ledgers_dir / f"peels_ledger_{strat_id}.txt"
            strat_rec_txt = self.manager.ledgers_dir / f"recycling_ledger_{strat_id}.txt"

            strat_peel_cnt, strat_peel_vol, strat_peel_pnl = 0, 0.0, 0.0
            if strat_peel_txt.exists():
                try:
                    with open(strat_peel_txt, "r", encoding="utf-8") as pf:
                        r = csv.reader(pf, delimiter=';')
                        next(r, None)
                        for row in r:
                            if len(row) >= 12:
                                strat_peel_cnt += 1
                                strat_peel_vol += float(row[10])
                                strat_peel_pnl += float(row[11])
                except Exception:
                    pass
            if strat_peel_cnt == 0:
                strat_peel_cnt = sum(c.get("peeling", {}).get("events_count", 0) for c in strat_per_coin.values())
                strat_peel_vol = sum(c.get("peeling", {}).get("total_peeled_usdt", 0.0) for c in strat_per_coin.values())
                strat_peel_pnl = sum(c.get("peeling", {}).get("locked_micro_pnl_usdt", 0.0) for c in strat_per_coin.values())

            strat_rec_cnt, strat_rec_vol, strat_rec_spent, strat_rec_absorbed = 0, 0.0, 0.0, 0.0
            if strat_rec_txt.exists():
                try:
                    with open(strat_rec_txt, "r", encoding="utf-8") as rf:
                        r = csv.reader(rf, delimiter=';')
                        next(r, None)
                        for row in r:
                            if len(row) >= 13:
                                strat_rec_cnt += 1
                                strat_rec_vol += float(row[10])
                                strat_rec_absorbed += float(row[11])
                                strat_rec_spent += float(row[12])
                except Exception:
                    pass
            if strat_rec_cnt == 0:
                strat_rec_cnt = sum(c.get("cross_recycling", {}).get("events_count", 0) for c in strat_per_coin.values())
                strat_rec_vol = sum(c.get("cross_recycling", {}).get("burned_volume_usdt", 0.0) for c in strat_per_coin.values())
                strat_rec_spent = sum(c.get("cross_recycling", {}).get("profit_spent_usdt", 0.0) for c in strat_per_coin.values())
                strat_rec_absorbed = sum(c.get("cross_recycling", {}).get("loss_absorbed_usdt", 0.0) for c in strat_per_coin.values())

            strat_data["toxic_defense"] = {
                "subgrid_peeling": {"events_count": strat_peel_cnt, "total_peeled_usdt": round(strat_peel_vol, 4), "locked_micro_pnl_usdt": round(strat_peel_pnl, 4)},
                "cross_recycling": {"events_count": strat_rec_cnt, "burned_volume_usdt": round(strat_rec_vol, 4), "profit_spent_usdt": round(strat_rec_spent, 4), "loss_absorbed_usdt": round(strat_rec_absorbed, 4)},
            }

            Utils.atomic_write_json(strat_file, strat_data, indent=4)

        # Update Leaderboard & portfolio analytics_all.json
        try:
            universes_meta = {
                sid: {**getattr(scfg, "raw_meta", {}), "name": scfg.name, "enabled": True, "sizing_mode": getattr(scfg, "sizing_mode", "static"), "auto_closing": getattr(scfg, "auto_closing_cfg", getattr(scfg, "raw_meta", {}).get("auto_closing", {}))}
                for sid, scfg in self.manager.strategy_registry.get_active_strategies().items()
            }
            entries = self.manager.leaderboard_engine.generate_leaderboard(universes_meta)
            evaluated_coins = len(self.manager.strategy_registry.get_all_active_symbols())
            self.manager.leaderboard_engine.save_portfolio(entries, evaluated_coins_count=evaluated_coins)
        except Exception as e:
            logger.error(f"[ANALYTICS] Error saving leaderboard portfolio: {e}")

    def reset_strategy_iteration(self, strategy_id: str) -> dict:
        """
        Принудительный сброс итерации стратегии и переход на новый цикл.
        Синхронизирует stats, cache и возвращает метаданные сброса.
        """
        now_ms = int(time.time() * 1000)
        strat_stats_path = self.manager.stats_dir / f"analytics_{strategy_id}.json"
        strat_analytics = {}
        if strat_stats_path.exists():
            strat_analytics = Utils.read_json_file(strat_stats_path) or {}
        else:
            strat_analytics = self.manager._read_data() or {}

        strat_net = float(strat_analytics.get("net_profit_usdt", 0.0))
        cur_iter_idx = int(strat_analytics.get("iteration_index", 1))
        next_iter_idx = cur_iter_idx + 1
        new_iter_start_net = round(strat_net, 4)
        new_iter_start_real = round(float(strat_analytics.get("realized_pnl_net_usdt", strat_analytics.get("realized_pnl_usdt", strat_net))), 4)

        strat_analytics.update({
            "iteration_index": next_iter_idx, "iteration_start_net_profit_usdt": new_iter_start_net,
            "iteration_net_profit_usdt": 0.0, "iteration_start_realized_pnl_usdt": new_iter_start_real,
            "iteration_realized_pnl_usdt": 0.0, "iteration_start_ts": now_ms,
            "iteration_peak_net_profit_usdt": 0.0, "iteration_peak_ts": now_ms,
            "iteration_peak_age_hours": 0.0, "iteration_net_velocity": 0.0,
        })

        if strat_stats_path.exists():
            Utils.atomic_write_json(strat_stats_path, strat_analytics, indent=4)

        # Синхронизация CACHE/iteration_state.json
        try:
            c_file = self.cache_dir / "iteration_state.json"
            c_dict = {}
            if c_file.exists():
                try:
                    c_dict = json.loads(c_file.read_text(encoding="utf-8"))
                except Exception:
                    c_dict = {}
            prev_entry = c_dict.get(strategy_id, {})
            c_dict[strategy_id] = {
                "iteration_index": next_iter_idx,
                "iteration_start_net_profit_usdt": new_iter_start_net,
                "iteration_start_realized_pnl_usdt": new_iter_start_real,
                "iteration_realized_pnl_usdt": 0.0,
                "iteration_start_ts": now_ms,
                "iteration_peak_net_profit_usdt": 0.0,
                "iteration_peak_ts": now_ms,
                "historical_peak_net_profit_usdt": prev_entry.get("historical_peak_net_profit_usdt", strat_net),
                "historical_peak_net_ts": prev_entry.get("historical_peak_net_ts", now_ms),
                "historical_max_drawdown_usdt": prev_entry.get("historical_max_drawdown_usdt", 0.0),
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
            Utils.atomic_write_json(c_file, c_dict, indent=4)
        except Exception as ce:
            logger.error(f"[{strategy_id}] Error saving iteration cache on manual reset: {ce}")

        return {
            "strategy_id": strategy_id,
            "prev_iteration": cur_iter_idx,
            "new_iteration": next_iter_idx,
            "base_net": new_iter_start_net,
            "base_realized": new_iter_start_real,
        }

    def reset_global_iteration(self) -> dict:
        """Сброс глобальной итерации портфеля в analytics.json и CACHE/iteration_state.json."""
        try:
            g_path = self.manager.log_file
            g_data = Utils.read_json_file(g_path) or {}
            g_net = float(g_data.get("net_profit_usdt", 0.0))
            g_idx = int(g_data.get("iteration_index", 1))
            next_g_idx = g_idx + 1
            now_ms = int(time.time() * 1000)

            g_data["iteration_index"] = next_g_idx
            g_data["iteration_start_net_profit_usdt"] = round(g_net, 4)
            g_data["iteration_net_profit_usdt"] = 0.0
            g_data["iteration_start_ts"] = now_ms
            g_data["iteration_peak_net_profit_usdt"] = 0.0
            g_data["iteration_peak_ts"] = now_ms
            g_data["iteration_peak_age_hours"] = 0.0
            g_data["iteration_net_velocity"] = 0.0
            Utils.atomic_write_json(g_path, g_data, indent=4)

            c_file = self.cache_dir / "iteration_state.json"
            c_dict = {}
            if c_file.exists():
                try:
                    c_dict = json.loads(c_file.read_text(encoding="utf-8"))
                except Exception:
                    c_dict = {}
            c_dict["__global__"] = {
                "iteration_index": next_g_idx,
                "iteration_start_net_profit_usdt": round(g_net, 4),
                "iteration_start_ts": now_ms,
                "iteration_peak_net_profit_usdt": 0.0,
                "iteration_peak_ts": now_ms,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
            Utils.atomic_write_json(c_file, c_dict, indent=4)
            return {"iteration_index": next_g_idx, "base_net": round(g_net, 4)}
        except Exception as ge:
            logger.error(f"Error resetting global portfolio iteration: {ge}")
            return {}
