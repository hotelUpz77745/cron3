# ANALYTICS/reconstruction.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.reconstruction
ROLE: Level 2 Absolute Analytics Reconstruction & Ledger Resync
==============================================================================
"""

import asyncio
import csv
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from consts import (
    ANALYTICS_DIR,
    DATA_DIR,
    INCOME_PAGINATION_DELAY_SEC,
)
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("Reconstruction")


class ReconstructionService:
    """Performs deep synchronization of trade income and ledger reconstruction from Binance."""

    def __init__(self, manager):
        self.manager = manager

    async def deep_sync_analytics(self, client, cached_prices: Optional[dict] = None) -> None:
        """
        Level 2 (Absolute) Analytics Reconstruction.
        Fetches true income from Binance since first_trade_ts and perfectly reconstructs 
        trades_ledger.txt and analytics.json.
        """
        if self.manager._sync_in_progress.locked():
            logger.info("Deep Sync already in progress, waiting...")

        async with self.manager._sync_in_progress:
            async with self.manager._lock:
                data = self.manager._read_data()
                if not data:
                    return
                start_balance = data["start_balance_usdt"]
                start_ts = data.get("first_trade_ts")

            if not start_ts:
                start_ts = int(time.time() * 1000)
                logger.info(f"[ANALYTICS] Initialized fresh first_trade_ts: {start_ts}")
                async with self.manager._lock:
                    data["first_trade_ts"] = start_ts
                    self.manager._write_data(data)

            try:
                active_symbols = []
                if self.manager.strategy_registry and hasattr(self.manager.strategy_registry, "get_all_active_symbols"):
                    try:
                        active_symbols = list(self.manager.strategy_registry.get_all_active_symbols())
                    except Exception:
                        active_symbols = []

                if not active_symbols:
                    try:
                        with open("CFG/app.json", "r", encoding="utf-8") as f:
                            app_cfg = json.load(f)
                            active_strategies = app_cfg.get("active_strategies", {})
                            if isinstance(active_strategies, dict):
                                for sdata in active_strategies.values():
                                    if isinstance(sdata, dict) and sdata.get("enabled", False):
                                        active_symbols.extend(Utils.extract_strategy_symbols(sdata))
                            else:
                                syms = app_cfg.get("symbols", [])
                                active_symbols = list(syms.keys()) if isinstance(syms, dict) else list(syms)
                    except Exception as ce:
                        logger.error(f"[ANALYTICS] Error reading active_symbols from app.json: {ce}")

                ledger_symbols = []
                try:
                    if self.manager.txt_file.exists():
                        with open(self.manager.txt_file, 'r', encoding='utf-8') as f:
                            reader = csv.reader(f, delimiter=';')
                            for row in reader:
                                if not row or row[0] in ("Id", "Symbol"):
                                    continue
                                sym = row[1].strip() if row[0].isdigit() else row[0].strip()
                                ledger_symbols.append(sym)
                except Exception:
                    pass

                tracked_symbols = set(active_symbols + ledger_symbols + list(data.get("per_coin", {}).keys()))

                # Fetch income starting strictly from first_trade_ts
                income_records = []
                current_start = start_ts

                is_fetching = True
                while is_fetching:
                    now_ts = int(time.time() * 1000)
                    # Binance income query window cannot exceed 30 days. We use 7 days to be safe.
                    end_time = current_start + (7 * 24 * 60 * 60 * 1000) - 1
                    if end_time > now_ts:
                        end_time = now_ts

                    attempts = 0
                    success_fetch = False
                    inc_res = None

                    while attempts < 3:
                        inc_res = await client.fetch_income(
                            start_time=current_start,
                            end_time=end_time,
                            limit=1000,
                        )
                        if inc_res and inc_res.success and isinstance(inc_res.data, list):
                            success_fetch = True
                            break
                        attempts += 1
                        await asyncio.sleep(1.0)

                    if not success_fetch:
                        err_msg = getattr(inc_res, 'error_msg', 'Unknown error') if inc_res else 'None'
                        logger.error(f"[ANALYTICS] Failed to fetch income history (Msg: {err_msg}) after 3 attempts! Aborting Deep Sync to protect stats.")
                        return

                    page_records = inc_res.data
                    if page_records:
                        income_records.extend(page_records)
                        current_start = int(page_records[-1].get("time", current_start)) + 1
                    else:
                        current_start = end_time + 1

                    if current_start > now_ts:
                        is_fetching = False
                        continue

                    await asyncio.sleep(INCOME_PAGINATION_DELAY_SEC)

                # Reconstruct Ledger and Stats
                total_pnl, total_comm, total_fund = 0.0, 0.0, 0.0
                by_symbol = {
                    sym: {"pnl": 0.0, "comm": 0.0, "fund": 0.0, "trades": 0, "wins": 0, "first_ts": None}
                    for sym in tracked_symbols
                }

                # Sort records chronologically
                income_records.sort(key=lambda x: x.get("time", 0))

                # Get current BNB price for commission conversion
                bnb_price = await self.manager._get_bnb_usdt_rate(client, cached_prices)

                # Group by exact time, symbol, and info (tradeId) to prevent merging partial fills
                grouped = {}
                for r in income_records:
                    sym = r.get("symbol")
                    if not sym or sym not in tracked_symbols:
                        continue

                    if sym not in by_symbol:
                        by_symbol[sym] = {"pnl": 0.0, "comm": 0.0, "fund": 0.0, "trades": 0, "wins": 0}

                    ts = int(r.get("time", 0))
                    info = r.get("info", "")
                    key = (ts, sym, info)
                    if key not in grouped:
                        grouped[key] = {"pnl": 0.0, "comm": 0.0, "fund": 0.0, "has_trade": False}

                    inc_type = r.get("incomeType")
                    val = float(r.get("income", 0.0))
                    asset = r.get("asset", "")

                    if inc_type == "REALIZED_PNL":
                        grouped[key]["pnl"] += val
                        grouped[key]["has_trade"] = True
                    elif inc_type == "COMMISSION":
                        if asset == "BNB" and bnb_price > 0:
                            grouped[key]["comm"] += val * bnb_price
                        else:
                            grouped[key]["comm"] += val
                    elif inc_type == "FUNDING_FEE":
                        grouped[key]["fund"] += val

                ledger_rows = []
                current_balance = start_balance
                global_pending_delta = 0.0
                trade_id_counter = 1

                # Pre-fetch current margins for completely new trades
                current_margins = {}
                for sym in tracked_symbols:
                    rt_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
                    if not rt_path.exists():
                        sub_matches = list((DATA_DIR / "runtime").glob(f"*/{sym.lower()}.json"))
                        if sub_matches:
                            rt_path = sub_matches[0]
                    vol = 0.0
                    if rt_path.exists():
                        try:
                            rt_data = json.loads(rt_path.read_text(encoding="utf-8"))
                            long_size = float(rt_data.get("LONG", {}).get("invest_size", 0.0)) if rt_data.get("LONG", {}).get("enable") else 0.0
                            short_size = float(rt_data.get("SHORT", {}).get("invest_size", 0.0)) if rt_data.get("SHORT", {}).get("enable") else 0.0
                            vol = (long_size + short_size) / 2.0
                        except Exception:
                            pass
                    current_margins[sym] = vol

                # Reconstruct Ledger sequentially
                active_trades = {}  # {sym: {"last_ts": 0, "pnl": 0.0}}
                for (ts, sym, info), g in sorted(grouped.items(), key=lambda x: x[0][0]):
                    dt_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

                    total_pnl += g["pnl"]
                    total_comm += g["comm"]
                    total_fund += g["fund"]

                    by_symbol[sym]["pnl"] += g["pnl"]
                    by_symbol[sym]["comm"] += g["comm"]
                    by_symbol[sym]["fund"] += g["fund"]

                    net_event = g["pnl"] + g["comm"] + g["fund"]
                    global_pending_delta += net_event

                    if g["has_trade"]:
                        if by_symbol[sym]["first_ts"] is None:
                            by_symbol[sym]["first_ts"] = ts

                        if sym not in active_trades:
                            active_trades[sym] = {"last_ts": ts, "pnl": g["pnl"]}
                        else:
                            last_ts = active_trades[sym]["last_ts"]
                            if ts - last_ts <= 5000:
                                active_trades[sym]["last_ts"] = ts
                                active_trades[sym]["pnl"] += g["pnl"]
                            else:
                                by_symbol[sym]["trades"] += 1
                                if active_trades[sym]["pnl"] > 0:
                                    by_symbol[sym]["wins"] += 1
                                active_trades[sym] = {"last_ts": ts, "pnl": g["pnl"]}

                        current_balance += global_pending_delta
                        ledger_rows.append([
                            trade_id_counter,
                            sym,
                            "SYNC",
                            dt_str,
                            dt_str,
                            round(global_pending_delta, 4),
                            round(current_balance, 4),
                        ])
                        trade_id_counter += 1
                        global_pending_delta = 0.0

                current_balance += global_pending_delta

                for sym, t_info in active_trades.items():
                    if t_info["last_ts"] > 0:
                        by_symbol[sym]["trades"] += 1
                        if t_info["pnl"] > 0:
                            by_symbol[sym]["wins"] += 1

                # Overwrite CSV completely using atomic write
                async with self.manager._csv_lock:
                    temp_txt = self.manager.txt_file.with_suffix('.tmp')
                    with open(temp_txt, 'w', encoding='utf-8', newline='') as f:
                        writer = csv.writer(f, delimiter=';')
                        writer.writerow(["Id", "Symbol", "Side", "Open Time", "Close Time", "PnL (USDT)", "Balance"])
                        writer.writerows(ledger_rows)
                    os.replace(temp_txt, self.manager.txt_file)

                    # Синхронно обновляем изолированные леджеры активных стратегий
                    if self.manager.strategy_registry and getattr(self.manager.strategy_registry, "active_strategies", None):
                        for s_id, s_cfg in self.manager.strategy_registry.active_strategies.items():
                            s_symbols = set(getattr(s_cfg, "symbols", []))
                            if not s_symbols:
                                continue
                            s_init_bal = float(getattr(s_cfg, "start_balance_usdt", 151.0))
                            s_rows = []
                            s_bal = s_init_bal
                            s_cnt = 1
                            for r in ledger_rows:
                                if len(r) > 1 and r[1] in s_symbols:
                                    try:
                                        r_pnl = float(r[5])
                                    except (ValueError, TypeError):
                                        r_pnl = 0.0
                                    s_bal += r_pnl
                                    s_rows.append([s_cnt, r[1], r[2], r[3], r[4], round(r_pnl, 4), round(s_bal, 4)])
                                    s_cnt += 1

                            s_txt = self.manager.ledgers_dir / f"trades_ledger_{s_id}.txt"
                            s_tmp = s_txt.with_suffix('.tmp')
                            with open(s_tmp, 'w', encoding='utf-8', newline='') as sf:
                                s_writer = csv.writer(sf, delimiter=';')
                                s_writer.writerow(["Id", "Symbol", "Side", "Open Time", "Close Time", "PnL (USDT)", "Balance"])
                                s_writer.writerows(s_rows)
                            os.replace(s_tmp, s_txt)

                # Reconstruct JSON
                async with self.manager._lock:
                    data = self.manager._read_data()

                    data["total_commission_usdt"] = round(total_comm, 4)
                    data["total_funding_usdt"] = round(total_fund, 4)
                    data["realized_pnl_usdt"] = round(total_pnl, 4)
                    data["realized_pnl_net_usdt"] = round(total_pnl + total_comm + total_fund, 4)

                    total_trades = sum(stats["trades"] for stats in by_symbol.values())
                    total_wins = sum(stats["wins"] for stats in by_symbol.values())

                    data["total_trades"] = total_trades
                    data["winning_trades"] = total_wins
                    data["winrate_pct"] = round((total_wins / total_trades * 100) if total_trades > 0 else 0, 2)

                    if "per_coin" not in data:
                        data["per_coin"] = {}

                    for sym, stats in by_symbol.items():
                        if sym not in data["per_coin"]:
                            data["per_coin"][sym] = {"current_drawdown": 0.0}

                        c = data["per_coin"][sym]
                        c["total_trades"] = stats["trades"]
                        c["winning_trades"] = stats["wins"]
                        c["winrate_pct"] = round((stats["wins"] / stats["trades"] * 100) if stats["trades"] > 0 else 0, 2)

                        if stats.get("first_ts"):
                            if "first_trade_ts" not in c or c["first_trade_ts"] > stats["first_ts"]:
                                c["first_trade_ts"] = stats["first_ts"]

                        c_gross = round(stats["pnl"], 4)
                        c["realized_pnl_usdt"] = c_gross
                        c_comm = round(stats["comm"], 4)
                        c_fund = round(stats["fund"], 4)
                        c["commission_usdt"] = c_comm
                        c["funding_usdt"] = c_fund
                        c_net = round(c_gross + c_comm + c_fund, 4)
                        c["realized_pnl_net_usdt"] = c_net

                    await self.manager._update_drawdowns(client, data)
                    self.manager._write_data(data)

            except Exception as e:
                logger.error(f"Absolute Deep Sync error: {e}")
