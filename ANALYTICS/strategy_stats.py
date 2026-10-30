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
    FLOAT_ZERO_EPSILON,
)
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.strategy_registry import StrategyRegistry
from ANALYTICS.trade_velocity import TradeVelocityEngine
from ANALYTICS.strategy_toxic_defense import StrategyToxicDefenseAggregator
from ANALYTICS.iteration_manager import StrategyIterationManager
from ANALYTICS.equity_trajectory import DEFAULT_SNAPSHOT_STORE

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
        return StrategyIterationManager.get_iteration_cache(self.cache_dir, strat_id)

    def save_iteration_cache(self, strat_id: str, state: dict) -> None:
        """Saves iteration telemetry state to persistent cache."""
        StrategyIterationManager.save_iteration_cache(self.cache_dir, strat_id, state)

    def update_strategies_stats(self, global_data: dict) -> None:
        """Рассчитывает и сохраняет изолированную аналитику по стратегиям и лидерборд."""
        is_synced = bool(getattr(self.manager, "_floating_synced", False) or global_data.get("_floating_synced", False))
        if not is_synced:
            logger.info("[STRATEGY_STATS] ❄️ Hands off analytics! REST API floating PnL not confirmed yet. Skipping recalculation to preserve disk state.")
            return

        if not getattr(self.manager, "strategy_registry", None):
            try:
                self.manager.strategy_registry = StrategyRegistry()
            except Exception:
                return

        now_ms = int(time.time() * 1000)
        per_coin_global = global_data.get("per_coin", {})

        # Aggregation of Global Toxic Defense from Source of Truth Ledgers
        global_data["toxic_defense"] = StrategyToxicDefenseAggregator.aggregate_global_defense(
            self.manager.peels_ledger, self.manager.recycling_ledger
        )

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
                    "universe_id": strat_id, "name": strat_cfg.name, "start_balance_usdt": strat_init_bal,
                    "_rebased_start_balance_usdt": strat_init_bal, "first_trade_ts": now_ms, "cur_balance_usdt": strat_init_bal,
                    "peak_balance_usdt": strat_init_bal, "min_balance_usdt": strat_init_bal, "_current_trough_usdt": strat_init_bal,
                    "total_trades": 0, "winning_trades": 0, "winrate_pct": 0.0, "realized_pnl_usdt": 0.0,
                    "net_profit_usdt": 0.0, "unrealized_pnl_usdt": 0.0, "max_drawdown_usdt": 0.0, "max_drawdown_pct": 0.0,
                    "recovery_factor": 0.0, "iteration_index": 1, "iteration_start_net_profit_usdt": 0.0,
                    "iteration_net_profit_usdt": 0.0, "iteration_start_ts": now_ms, "iteration_peak_net_profit_usdt": 0.0,
                    "iteration_peak_ts": now_ms, "iteration_peak_age_hours": 0.0, "iteration_net_velocity": 0.0, "per_coin": {},
                }

            strat_symbols = list(strat_cfg.symbols)
            existing_per_coin: dict = strat_data.get("per_coin") if isinstance(strat_data.get("per_coin"), dict) else {}
            # Собираем список всех символов стратегии: текущие активные + исторические с историей торговли
            all_strat_symbols = set(strat_symbols)
            for sym, c_info in existing_per_coin.items():
                c_trades = int(c_info.get("total_trades", c_info.get("trades", 0)))
                c_pnl = abs(float(c_info.get("realized_pnl_net_usdt", c_info.get("realized_pnl_usdt", 0.0))))
                if c_trades > 0 or c_pnl > FLOAT_ZERO_EPSILON:
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
                if c_trades > 0 or c_pnl > FLOAT_ZERO_EPSILON:
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
            if can_update_strat_peak or not has_active_positions:
                DEFAULT_SNAPSHOT_STORE.record_snapshot(
                    scope_id=strat_id,
                    current_equity=s_cur,
                    now_ms=now_ms,
                )

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
            fsm_val = getattr(getattr(self.manager, "fsm", None), "get_iteration_index", lambda s: 1)(strat_id)
            fsm_i = int(fsm_val) if isinstance(fsm_val, int) or (isinstance(fsm_val, str) and str(fsm_val).isdigit()) else 1
            iter_idx = max(int(strat_data.get("iteration_index", 1)), int(iter_cache.get("iteration_index", 1)), fsm_i)
            fsm_obj = getattr(self.manager, "fsm", None)
            fsm_base = fsm_obj.get_iteration_baseline_net(strat_id, iter_idx) if hasattr(fsm_obj, "get_iteration_baseline_net") else 0.0
            cached_start = float(strat_data.get("iteration_start_net_profit_usdt", iter_cache.get("iteration_start_net_profit_usdt", 0.0)))
            iter_start_net = 0.0 if iter_idx == 1 else max(cached_start, fsm_base)

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
            else:
                raw_start_real = strat_data.get("iteration_start_realized_pnl_usdt")
                if raw_start_real is None:
                    raw_start_real = iter_cache.get("iteration_start_realized_pnl_usdt")

                # Защита от десинхронизации: если базовая точка realized PnL отсутствует,
                # либо превышает текущий s_real из-за закрытия позиций старой итерации,
                # выравниваем базовую точку по iter_start_net
                if raw_start_real is None or (float(raw_start_real) > s_real and iter_start_net > 0 and float(raw_start_real) > iter_start_net + 1e-4):
                    iter_start_real = iter_start_net
                else:
                    iter_start_real = float(raw_start_real)

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
                if not isinstance(getattr(self.manager, "_strat_velocity_engines", None), dict):
                    self.manager._strat_velocity_engines = {}
                if strat_id not in self.manager._strat_velocity_engines:
                    strat_ledger = self.manager.ledgers_dir / f"trades_ledger_{strat_id}.txt"
                    self.manager._strat_velocity_engines[strat_id] = TradeVelocityEngine(
                        universe_id=strat_id, ledger_path=strat_ledger
                    )
                vel_res = self.manager._strat_velocity_engines[strat_id].calculate_velocity(strat_data)
                strat_data["trade_velocity"] = vel_res if isinstance(vel_res, (int, float, dict, list, str, bool)) or vel_res is None else 0.0
            except Exception as sve:
                logger.warning(f"[ANALYTICS] Error calculating trade velocity for {strat_id}: {sve}")
                strat_data["trade_velocity"] = 0.0

            # Toxic Defense Aggregation for Strategy (Source of Truth: Strategy Ledgers)
            strat_data["toxic_defense"] = StrategyToxicDefenseAggregator.aggregate_strategy_defense(
                self.manager.ledgers_dir, strat_id, strat_per_coin
            )

            Utils.atomic_write_json(strat_file, strat_data, indent=4)

        # Update Leaderboard & portfolio analytics_all.json
        try:
            universes_meta = {
                sid: {**getattr(scfg, "raw_meta", {}), "name": scfg.name, "enabled": True, "auto_closing": getattr(scfg, "auto_closing_cfg", getattr(scfg, "raw_meta", {}).get("auto_closing", {}))}
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
        return StrategyIterationManager.reset_strategy_iteration(self.manager, self.cache_dir, strategy_id)

    def reset_global_iteration(self) -> dict:
        """Сброс глобальной итерации портфеля в analytics.json и CACHE/iteration_state.json."""
        return StrategyIterationManager.reset_global_iteration(self.manager, self.cache_dir)
