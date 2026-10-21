# ANALYTICS/drawdown_tracker.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.drawdown_tracker
ROLE: Monitoring portfolio equity, drawdowns, open positions, and margin loads
==============================================================================
"""

import asyncio
import json
import time
import time as _time_module
import traceback
from typing import Optional

from consts import (
    ANALYTICS_START_BALANCE_USDT,
    BG_UNREALIZED_POLL_FREQ_SEC,
    DATA_DIR,
)
from c_log import UnifiedLogger
from c_utils import Utils
from ANALYTICS.circuit_breaker import is_banned, update_ban_from_error, wait_ban_lifted

logger = UnifiedLogger("DrawdownTracker")


class DrawdownTracker:
    """Tracks floating unrealized PnL, drawdowns, capacity, margins, and account equity in realtime."""

    def __init__(self, manager):
        self.manager = manager
        self._is_tracker_running = False
        self._tracker_task: Optional[asyncio.Task] = None
        self._floating_synced = False
        if hasattr(manager, "_floating_synced"):
            manager._floating_synced = False

    async def get_bnb_usdt_rate(self, client, cached_prices: Optional[dict] = None) -> float:
        """Retrieves BNB price in USDT for commission calculation."""
        if cached_prices:
            val = cached_prices.get("BNBUSDT") or cached_prices.get("bnbusdt")
            if val is not None:
                raw_price = val[0] if isinstance(val, (tuple, list)) else val
                parsed = Utils.safe_float(raw_price, default=0.0)
                if parsed > 0:
                    return parsed

        try:
            from API.BINANCE.public import BinancePublic
            prices = await BinancePublic.get_prices_bulk(["BNBUSDT"])
            if prices and "BNBUSDT" in prices:
                val = prices["BNBUSDT"]
                raw_price = val[0] if isinstance(val, (tuple, list)) else val
                parsed = Utils.safe_float(raw_price, default=0.0)
                if parsed > 0:
                    return parsed
        except Exception as e:
            logger.warning(f"[ANALYTICS] Failed to fetch BNB rate via bulk: {e}")
        return 0.0

    async def update_drawdowns(self, client, data: dict, is_lightweight: bool = False) -> bool:
        """Fetches account info to update current unrealized drawdowns globally and per-coin."""
        try:
            if hasattr(self.manager, "fsm") and self.manager.fsm:
                self.manager.fsm.transition_to_awaiting_api()

            await wait_ban_lifted()

            res = await client.fetch_account_info()

            if is_lightweight and getattr(self.manager, "_sync_locks", None):
                logger.info("[ANALYTICS] Deep Sync lock acquired during REST fetch. Aborting lightweight PnL sync.")
                return False

            if not res.success or not isinstance(res.data, dict):
                err = getattr(res, "error_msg", "Unknown Error") or ""
                if "Connector is closed" in err or not getattr(self, "_is_tracker_running", True):
                    logger.debug(f"[ANALYTICS] fetch_account_info skipped during shutdown: {err}")
                    return False
                update_ban_from_error(err)
                logger.warning(f"[ANALYTICS] fetch_account_info failed: {err}. Skipping recalculation until valid API data.")
                if hasattr(self.manager, "fsm") and self.manager.fsm:
                    self.manager.fsm.on_api_error(err)
                return False

            acc_data = res.data
            if "positions" not in acc_data or "totalMarginBalance" not in acc_data or not isinstance(acc_data.get("positions"), list):
                logger.error("[ANALYTICS] fetch_account_info returned empty or invalid data! Skipping recalculation until valid API data.")
                if hasattr(self.manager, "fsm") and self.manager.fsm:
                    self.manager.fsm.on_api_error("Empty or invalid account data")
                return False

            positions = acc_data.get("positions", [])
            coin_drawdowns = {}
            coin_details = {}
            for p in positions:
                amt = float(p.get("positionAmt", 0.0))
                if amt == 0:
                    continue
                sym = p.get("symbol", "")
                unrealized = float(p.get("unrealizedProfit", 0.0))
                side = p.get("positionSide", "BOTH")
                entry_p = float(p.get("entryPrice", 0.0))
                notional_val = abs(float(p.get("notional", 0.0)))
                if notional_val == 0.0 and entry_p > 0:
                    notional_val = abs(amt) * entry_p

                coin_drawdowns[sym] = coin_drawdowns.get(sym, 0.0) + unrealized

                if sym not in coin_details:
                    coin_details[sym] = {
                        "long_unrealized": 0.0, "short_unrealized": 0.0,
                        "long_amt": 0.0, "short_amt": 0.0,
                        "long_notional": 0.0, "short_notional": 0.0,
                        "long_entry": 0.0, "short_entry": 0.0,
                    }

                if side == "LONG" or (side == "BOTH" and amt > 0):
                    coin_details[sym]["long_unrealized"] = round(unrealized, 4)
                    coin_details[sym]["long_amt"] = amt
                    coin_details[sym]["long_notional"] = notional_val
                    coin_details[sym]["long_entry"] = entry_p
                elif side == "SHORT" or (side == "BOTH" and amt < 0):
                    coin_details[sym]["short_unrealized"] = round(unrealized, 4)
                    coin_details[sym]["short_amt"] = amt
                    coin_details[sym]["short_notional"] = notional_val
                    coin_details[sym]["short_entry"] = entry_p

            bot_unrealized = 0.0

            config_symbols = []
            if getattr(self.manager, "strategy_registry", None) and hasattr(self.manager.strategy_registry, "get_all_active_symbols"):
                try:
                    config_symbols = list(self.manager.strategy_registry.get_all_active_symbols())
                except Exception:
                    config_symbols = []

            if not config_symbols:
                try:
                    with open("CFG/app.json", "r", encoding="utf-8") as _f:
                        _app = json.load(_f)
                        _active_strats = _app.get("active_strategies", {})
                        if _active_strats:
                            config_symbols = []
                            for _sdata in _active_strats.values():
                                if _sdata.get("enabled", False):
                                    config_symbols.extend(Utils.extract_strategy_symbols(_sdata))
                        else:
                            _syms = _app.get("symbols", {})
                            config_symbols = list(_syms.keys()) if isinstance(_syms, dict) else list(_syms)
                except Exception:
                    config_symbols = []

            if not config_symbols:
                config_symbols = list(data.get("per_coin", {}).keys())
            if not config_symbols:
                config_symbols = list(coin_details.keys())

            tracked_symbols = list(dict.fromkeys(config_symbols + list(data.get("per_coin", {}).keys()) + list(data.get("historical_symbols", []))))

            # FSM Position Transition Detector for tracked symbols
            for sym in tracked_symbols:
                details = coin_details.get(sym, {})
                for side in ("LONG", "SHORT"):
                    amt_key = "long_amt" if side == "LONG" else "short_amt"
                    curr_amt = float(details.get(amt_key, 0.0))
                    fsm_key = (sym, side)

                    if fsm_key not in self.manager._symbol_fsm_positions:
                        self.manager._symbol_fsm_positions[fsm_key] = curr_amt
                    else:
                        prev_amt = self.manager._symbol_fsm_positions[fsm_key]
                        if abs(curr_amt - prev_amt) > 1e-4:
                            self.manager._symbol_fsm_positions[fsm_key] = curr_amt
                            if abs(curr_amt) < abs(prev_amt) and fsm_key not in self.manager._sync_locks:
                                logger.info(
                                    f"[ANALYTICS][FSM] Position reduction detected on {sym} {side} "
                                    f"({prev_amt} -> {curr_amt}). Scheduling income sync."
                                )
                                self.manager.record_finished_position(client, sym, side, 0, int(time.time() * 1000))

            if "per_coin" not in data:
                data["per_coin"] = {}

            for sym in config_symbols:
                if sym not in data["per_coin"]:
                    data["per_coin"][sym] = {
                        "current_drawdown": 0.0,
                        "realized_pnl_usdt": 0.0,
                        "realized_pnl_net_usdt": 0.0,
                        "commission_usdt": 0.0,
                        "funding_usdt": 0.0,
                        "net_profit_usdt": 0.0,
                        "win_count": 0,
                        "loss_count": 0,
                        "max_drawdown": 0.0,
                        "min_drawdown": 0.0,
                    }

            for sym, cdata in data.get("per_coin", {}).items():
                details = coin_details.get(sym, {})
                long_unreal = details.get("long_unrealized", 0.0)
                short_unreal = details.get("short_unrealized", 0.0)
                # Источник правды для плавающего PnL открытой позиции — данные биржи (coin_details),
                # искусственное обнуление просадки во время блокировок синхронизации запрещено!
                drawdown = long_unreal + short_unreal
                cdata["current_drawdown"] = round(drawdown, 4)
                cdata["long_unrealized"] = long_unreal
                cdata["short_unrealized"] = short_unreal
                cdata["long_amt"] = details.get("long_amt", 0.0)
                cdata["short_amt"] = details.get("short_amt", 0.0)
                cdata["max_drawdown"] = round(min(cdata.get("max_drawdown", 0.0), drawdown), 4)
                cdata["min_drawdown"] = round(max(cdata.get("min_drawdown", drawdown), drawdown), 4)

                rt_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
                rt_data = {}
                if rt_path.exists():
                    try:
                        rt_data = json.loads(rt_path.read_text(encoding="utf-8"))
                    except Exception:
                        rt_data = {}

                long_cfg = rt_data.get("LONG", {})
                short_cfg = rt_data.get("SHORT", {})

                long_size = float(long_cfg.get("invest_size", 0.0) or 0.0) if long_cfg.get("enable", True) else 0.0
                short_size = float(short_cfg.get("invest_size", 0.0) or 0.0) if short_cfg.get("enable", True) else 0.0
                long_lev = float(long_cfg.get("leverage", 20.0)) or 20.0
                short_lev = float(short_cfg.get("leverage", 20.0)) or 20.0

                grid_capacity = long_size + short_size

                long_notional = float(details.get("long_notional", 0.0))
                if long_notional <= 0 and details.get("long_amt", 0.0) != 0:
                    l_entry = float(details.get("long_entry", 0.0)) or float(rt_data.get("LONG", {}).get("avg_entry_price", 0.0))
                    long_notional = abs(float(details["long_amt"])) * l_entry

                short_notional = float(details.get("short_notional", 0.0))
                if short_notional <= 0 and details.get("short_amt", 0.0) != 0:
                    s_entry = float(details.get("short_entry", 0.0)) or float(rt_data.get("SHORT", {}).get("avg_entry_price", 0.0))
                    short_notional = abs(float(details["short_amt"])) * s_entry

                coin_load_usdt = round(long_notional + short_notional, 4)
                coin_load_pct = round((coin_load_usdt / grid_capacity) * 100.0, 2) if grid_capacity > 0 else 0.0

                cdata["grid_capacity_usdt"] = round(grid_capacity, 2)
                cdata["current_grid_load_usdt"] = coin_load_usdt
                cdata["current_grid_load_pct"] = coin_load_pct
                cdata["max_grid_load_usdt"] = round(max(float(cdata.get("max_grid_load_usdt", 0.0)), coin_load_usdt), 4)
                cdata["max_grid_load_pct"] = round((cdata["max_grid_load_usdt"] / grid_capacity) * 100.0, 2) if grid_capacity > 0 else 0.0
                cdata["load_ratio"] = round(coin_load_usdt / grid_capacity, 4) if grid_capacity > 0 else 0.0

                coin_margin_used = round((long_notional / long_lev) + (short_notional / short_lev), 4)
                cdata["current_margin_usdt"] = coin_margin_used
                cdata["max_margin_usdt"] = round(max(float(cdata.get("max_margin_usdt", 0.0)), coin_margin_used), 4)

                cdata.pop("epoch_state", None)
                cdata.pop("DRME", None)
                cdata.pop("MDME", None)

                bot_unrealized += drawdown

            data["unrealized_pnl_usdt"] = round(bot_unrealized, 4)

            bot_gross_profit, bot_total_comm, bot_total_fund = 0.0, 0.0, 0.0
            if "per_coin" in data:
                for sym, cdata in data["per_coin"].items():
                    c_gross = cdata.get("realized_pnl_usdt", 0.0)
                    c_comm = cdata.get("commission_usdt", 0.0)
                    c_fund = cdata.get("funding_usdt", 0.0)

                    cdata["realized_pnl_usdt"] = round(c_gross, 4)
                    c_net = round(c_gross + c_comm + c_fund, 4)
                    cdata["realized_pnl_net_usdt"] = c_net

                    c_drawdown = cdata.get("current_drawdown", 0.0)
                    cdata["net_profit_usdt"] = round(c_net + c_drawdown, 4)
                    global_init = float(data.get("start_balance_usdt", ANALYTICS_START_BALANCE_USDT))
                    cdata["roi_pct"] = round((cdata["net_profit_usdt"] / global_init) * 100.0, 2) if global_init > 0 else 0.0

                    bot_gross_profit += c_gross
                    bot_total_comm += c_comm
                    bot_total_fund += c_fund

            if data.get("total_trades", 0) == 0:
                bot_gross_profit, bot_total_comm, bot_total_fund = 0.0, 0.0, 0.0

            data["total_commission_usdt"] = round(bot_total_comm, 4)
            data["total_funding_usdt"] = round(bot_total_fund, 4)
            data["realized_pnl_usdt"] = round(bot_gross_profit, 4)

            bot_realized_net = round(bot_gross_profit + bot_total_comm + bot_total_fund, 4)
            data["realized_pnl_net_usdt"] = bot_realized_net
            data["net_profit_usdt"] = round(bot_realized_net + bot_unrealized, 4)

            initial = float(data.get("start_balance_usdt", 0.0))
            last_rebase = float(data.get("_rebased_start_balance_usdt", initial))
            if abs(initial - last_rebase) > 0.0001:
                delta = initial - last_rebase
                peak_cur = float(data["peak_balance_usdt"]) if data.get("peak_balance_usdt") is not None else initial
                data["peak_balance_usdt"] = round(peak_cur + delta, 4)
                trough_cur = float(data["_current_trough_usdt"]) if data.get("_current_trough_usdt") is not None else initial
                data["_current_trough_usdt"] = round(trough_cur + delta, 4)
                min_cur = float(data["min_balance_usdt"]) if data.get("min_balance_usdt") is not None else initial
                data["min_balance_usdt"] = round(min_cur + delta, 4)
                data["_rebased_start_balance_usdt"] = initial

            bot_cur_balance = round(initial + data["net_profit_usdt"], 4)
            data["cur_balance_usdt"] = bot_cur_balance
            data["roi_pct"] = round(((bot_cur_balance - initial) / initial) * 100, 2) if initial > 0 else 0.0

            # Global Grid Load & Margin Load
            total_grid_cap = sum(float(c.get("grid_capacity_usdt", 0.0)) for c in data.get("per_coin", {}).values())
            total_grid_load = sum(float(c.get("current_grid_load_usdt", 0.0)) for c in data.get("per_coin", {}).values())
            total_margin_used = sum(float(c.get("current_margin_usdt", 0.0)) for c in data.get("per_coin", {}).values())

            data["total_grid_size_usdt"] = round(total_grid_cap, 2)
            data["current_grid_load_usdt"] = round(total_grid_load, 4)
            data["grid_load_pct"] = round((total_grid_load / total_grid_cap) * 100.0, 2) if total_grid_cap > 0 else 0.0
            data["max_grid_load_usdt"] = round(max(float(data.get("max_grid_load_usdt", 0.0)), total_grid_load), 4)
            data["max_grid_load_pct"] = round((data["max_grid_load_usdt"] / total_grid_cap) * 100.0, 2) if total_grid_cap > 0 else 0.0
            data["load_ratio"] = round(total_grid_load / total_grid_cap, 4) if total_grid_cap > 0 else 0.0

            data["total_margin_used_usdt"] = round(total_margin_used, 4)
            data["max_margin_used_usdt"] = round(max(float(data.get("max_margin_used_usdt", 0.0)), total_margin_used), 4)

            if bot_cur_balance > 0:
                data["margin_load_pct"] = round((total_margin_used / bot_cur_balance) * 100.0, 2)
                data["max_margin_load_pct"] = round((data["max_margin_used_usdt"] / bot_cur_balance) * 100.0, 2)
            else:
                data["margin_load_pct"] = 0.0
                data["max_margin_load_pct"] = 0.0

            free_margin = max(0.0, bot_cur_balance - total_margin_used)
            data["free_margin_usdt"] = round(free_margin, 4)
            data["free_margin_pct"] = round((free_margin / bot_cur_balance) * 100.0, 2) if bot_cur_balance > 0 else 0.0

            prev_min_free = data.get("min_free_margin_usdt")
            if prev_min_free is None:
                data["min_free_margin_usdt"] = round(free_margin, 4)
            else:
                data["min_free_margin_usdt"] = round(min(float(prev_min_free), free_margin), 4)
            data["min_free_margin_pct"] = round((data["min_free_margin_usdt"] / bot_cur_balance) * 100.0, 2) if bot_cur_balance > 0 else 0.0

            for c in data.get("per_coin", {}).values():
                c_m = float(c.get("current_margin_usdt", 0.0))
                c["margin_load_pct"] = round((c_m / bot_cur_balance) * 100.0, 2) if bot_cur_balance > 0 else 0.0
                c_m_max = float(c.get("max_margin_usdt", 0.0))
                c["max_margin_load_pct"] = round((c_m_max / bot_cur_balance) * 100.0, 2) if bot_cur_balance > 0 else 0.0

            peak = float(data.get("peak_balance_usdt") if data.get("peak_balance_usdt") is not None else initial)
            trough = float(data.get("_current_trough_usdt") if data.get("_current_trough_usdt") is not None else initial)
            min_bal = float(data.get("min_balance_usdt") if data.get("min_balance_usdt") is not None else initial)

            can_update_peak = True
            if not getattr(self, "_floating_synced", False):
                can_update_peak = False
                logger.info("[ANALYTICS] First API sync verified. Marking floating stats as synced.")
                self._floating_synced = True
                self.manager._floating_synced = True
                data["_floating_synced"] = True
                if hasattr(self.manager, "fsm") and self.manager.fsm:
                    self.manager.fsm.on_api_sync_success(data)
                if hasattr(self.manager, "_persisted_global_cache"):
                    self.manager._persisted_global_cache = data

            if len(coin_details) > 0 and abs(bot_unrealized) < 1e-5:
                can_update_peak = False
                logger.warning(
                    f"[ANALYTICS] Peak guard triggered: {len(coin_details)} active position(s) exist, "
                    f"but bot_unrealized is 0.0. Blocking peak balance update."
                )

            now_ms = int(time.time() * 1000)
            if bot_cur_balance > peak and can_update_peak:
                peak = bot_cur_balance
                trough = bot_cur_balance
                data["peak_net_ts"] = now_ms
            elif "peak_net_ts" not in data:
                data["peak_net_ts"] = data.get("first_trade_ts", now_ms)

            if bot_cur_balance < trough:
                trough = bot_cur_balance
            if bot_cur_balance < min_bal:
                min_bal = bot_cur_balance

            data["peak_balance_usdt"] = round(peak, 4)
            data["_current_trough_usdt"] = round(trough, 4)
            data["min_balance_usdt"] = round(min_bal, 4)
            max_drawdown = min(0.0, round(min_bal - initial, 4))
            data["max_drawdown_usdt"] = round(max_drawdown, 4)

            max_perf = peak - initial
            data["performance_usdt"] = round(max(data.get("performance_usdt", 0.0), max_perf), 4)

            peak_ts_g = data.get("peak_net_ts", now_ms)
            data["peak_age_hours"] = round(max(0.0, (now_ms - peak_ts_g) / 3600000.0), 2)
            first_ts_g = data.get("first_trade_ts", now_ms)
            active_h_g = max(0.1, (now_ms - first_ts_g) / 3600000.0)
            net_profit_usdt = float(data.get("net_profit_usdt", 0.0))
            data["net_velocity"] = round(net_profit_usdt / active_h_g, 2)

            iter_idx_g = int(data.get("iteration_index", 1))
            if hasattr(self.manager, "fsm") and self.manager.fsm:
                try:
                    fsm_g = int(self.manager.fsm.get_iteration_index("__global__"))
                    if fsm_g > iter_idx_g:
                        iter_idx_g = fsm_g
                except (TypeError, ValueError):
                    pass
            iter_start_net_g = float(data.get("iteration_start_net_profit_usdt", 0.0))
            iter_net_g = round(net_profit_usdt - iter_start_net_g, 4)
            iter_start_ts_g = data.get("iteration_start_ts", first_ts_g)

            iter_peak_net_g = float(data.get("iteration_peak_net_profit_usdt", iter_net_g))
            iter_peak_ts_g = data.get("iteration_peak_ts", iter_start_ts_g)
            if iter_net_g > iter_peak_net_g and can_update_peak:
                iter_peak_net_g = iter_net_g
                iter_peak_ts_g = now_ms

            data["iteration_index"] = iter_idx_g
            data["iteration_start_net_profit_usdt"] = round(iter_start_net_g, 4)
            data["iteration_net_profit_usdt"] = iter_net_g
            data["iteration_start_ts"] = iter_start_ts_g
            data["iteration_peak_net_profit_usdt"] = round(iter_peak_net_g, 4)
            data["iteration_peak_ts"] = iter_peak_ts_g
            data["iteration_peak_age_hours"] = round(max(0.0, (now_ms - iter_peak_ts_g) / 3600000.0), 2)

            iter_active_h_g = max(0.05, (now_ms - iter_start_ts_g) / 3600000.0)
            data["iteration_net_velocity"] = round(iter_net_g / iter_active_h_g, 2)

            if data["max_drawdown_usdt"] < 0:
                data["recovery_factor"] = round(net_profit_usdt / abs(data["max_drawdown_usdt"]), 2)
            else:
                data["recovery_factor"] = 0.0

            self.manager._update_strategies_stats(data)
            if hasattr(self.manager, "fsm") and self.manager.fsm:
                self.manager.fsm.transition_to_autocloser_ready()
            return True

        except Exception as e:
            logger.error(f"[ANALYTICS] Error updating drawdowns: {e}\n{traceback.format_exc()}")
            if hasattr(self.manager, "fsm") and self.manager.fsm:
                self.manager.fsm.on_api_error(str(e))
            return False

    async def sync_lightweight_unrealized_pnl(self, client) -> bool:
        """Lightweight live sync for fast periodic UI / leaderboard refresh."""
        async with self.manager._lock:
            data = self.manager._read_data()
            if not data:
                return False

            success = await self.update_drawdowns(client, data, is_lightweight=True)
            if success:
                data["last_updated_ts"] = int(time.time() * 1000)
                self.manager._write_data(data, mark_backup=False)
                if hasattr(self.manager, "fsm") and self.manager.fsm:
                    self.manager.fsm.transition_to_autocloser_ready()
                try:
                    self.manager._update_strategies_stats(data)
                    if getattr(self.manager, "strategy_registry", None):
                        universes_meta = {
                            sid: {
                                **getattr(scfg, "raw_meta", {}),
                                "name": scfg.name,
                                "enabled": True,
                                "sizing_mode": getattr(scfg, "sizing_mode", "static"),
                                "auto_closing": getattr(
                                    scfg, "auto_closing_cfg", getattr(scfg, "raw_meta", {}).get("auto_closing", {})
                                ),
                            }
                            for sid, scfg in self.manager.strategy_registry.get_active_strategies().items()
                        }
                        entries = self.manager.leaderboard_engine.generate_leaderboard(universes_meta)
                        evaluated_coins = len(self.manager.strategy_registry.get_all_active_symbols())
                        self.manager.leaderboard_engine.save_portfolio(entries, evaluated_coins_count=evaluated_coins)
                except Exception as lbe:
                    logger.debug(f"[ANALYTICS] Lightweight leaderboard refresh: {lbe}")
            return success

    def start_realtime_tracker(self, client):
        """Starts background task polling unrealized PnL at regular intervals."""
        if hasattr(self, "_tracker_task") and self._tracker_task:
            return
        self._is_tracker_running = True
        self._tracker_task = asyncio.create_task(self._realtime_tracker_loop(client))
        self.manager._background_tasks.add(self._tracker_task)
        self._tracker_task.add_done_callback(self.manager._background_tasks.discard)

    def stop_realtime_tracker(self):
        """Stops background real-time polling task."""
        self._is_tracker_running = False
        if hasattr(self, "_tracker_task") and self._tracker_task:
            self._tracker_task.cancel()
            self._tracker_task = None

    async def _realtime_tracker_loop(self, client):
        logger.info(f"[ANALYTICS] Started real-time absolute drawdown tracker (polls every {BG_UNREALIZED_POLL_FREQ_SEC}s)")
        while getattr(self, "_is_tracker_running", True):
            await asyncio.sleep(BG_UNREALIZED_POLL_FREQ_SEC)
            try:
                if getattr(self.manager, "_sync_locks", None):
                    continue

                if is_banned():
                    continue

                await self.sync_lightweight_unrealized_pnl(client)
            except Exception as e:
                logger.error(f"Realtime tracker error: {e}")
