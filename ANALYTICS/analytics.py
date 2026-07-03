# ==============================================================================
# Path: ANALYTICS/analytics.py
# Role: Домен аналитики и ведения журнала сделок
# ==============================================================================

import asyncio
import json
import logging
import csv
from datetime import datetime, timezone
from pathlib import Path
from consts import ANALYTICS_DIR, ANALYTICS_CSV_MAX_ROWS, DATA_DIR

logger = logging.getLogger("Analytics")

class AnalyticsManager:
    """
    Ведет журнал сделок и статистику закрытых позиций.
    """
    def __init__(self):
        self.log_file = ANALYTICS_DIR / "analytics.json"
        self.txt_file = ANALYTICS_DIR / "trades_ledger.txt"
        self._lock = asyncio.Lock()
        self._csv_lock = asyncio.Lock()
        self._background_tasks = set()
        self._sync_locks = set()
        self._sync_in_progress = asyncio.Lock()
        self._ensure_files()

    def _ensure_files(self):
        if not self.log_file.exists():
            default_data = {
                "start_balance_usdt": 0.0,
                "cur_balance_usdt": 0.0,
                "total_trades": 0,
                "winning_trades": 0,
                "winrate_pct": 0.0,
                "realized_pnl_usdt": 0.0,
                "net_profit_usdt": 0.0,
                "unrealized_pnl_usdt": 0.0,
                "per_coin": {}
            }
            self.log_file.write_text(json.dumps(default_data, indent=4), encoding="utf-8")
        
        if not self.txt_file.exists():
            with open(self.txt_file, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Symbol", "Side", "Open Time", "Close Time", "PnL (USDT)", "Balance"])

    def _read_data(self) -> dict:
        if not self.log_file.exists():
            self._ensure_files()
            
        try:
            return json.loads(self.log_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error reading analytics file: {e}")
            return {}

    def _calculate_advanced_metrics(self, data: dict):
        # Auto-inject the _help dictionary so it is always present and updated
        data["_help"] = {
            "roi_pct": "Return on Investment (%). (cur_balance_usdt - start_balance_usdt) / start_balance_usdt * 100",
            "load_ratio": "Grid Load Ratio. abs(unrealized_pnl_usdt) / realized_pnl_usdt. Shows how much floating risk is taken per 1 USDT of closed profit.",
            "recovery_factor": "Recovery Factor. realized_pnl_usdt / abs(max_drawdown_usdt). Shows if the bots profit can cover historical max drawdowns.",
            "realized_pnl_usdt": "Total realized profit including commissions and funding fees from all closed trades.",
            "net_profit_usdt": "realized_pnl_usdt + unrealized_pnl_usdt. The true mathematical growth of the account.",
            "unrealized_pnl_usdt": "Current floating drawdown (unrealized PnL) of all open positions.",
            "start_balance_usdt": "Initial configured account balance.",
            "cur_balance_usdt": "Current mathematical margin balance (start_balance_usdt + net_profit_usdt).",
            "peak_balance_usdt": "Absolute highest margin balance reached.",
            "min_balance_usdt": "Absolute lowest margin balance reached.",
            "max_drawdown_usdt": "Maximum historical drawdown (trough - peak).",
            "performance_usdt": "Maximum historical growth from start balance (peak - start_balance).",
            "avg_daily_profit": "[Per-Coin] Average profit per active day of trading for this coin.",
            "max_position_size": "[Per-Coin] Max historical notional size actually reached (total volume * price).",
            "risk_reward_ratio": "[Per-Coin] abs(max_drawdown) / avg_daily_profit.",
            "DRME": "[Per-Coin] Daily Return on Max Exposure: avg_daily_profit / max_position_size.",
            "MDME": "[Per-Coin] Max Drawdown on Max Exposure: abs(max_drawdown) / max_position_size.",
            "max_net_profit": "[Per-Coin] Historical maximum of the coin's fixed net profit.",
            "min_net_profit": "[Per-Coin] Historical minimum of the coin's fixed net profit.",
            "max_drawdown": "[Per-Coin] Historical maximum floating drawdown for this coin.",
            "min_drawdown": "[Per-Coin] Historical minimum floating drawdown for this coin."
        }
        
        if "per_coin" not in data:
            return
        
        import time
        from consts import DATA_DIR
        import math
        
        current_ts = int(time.time() * 1000)
        
        for sym, cdata in data["per_coin"].items():
            first_trade_ts = cdata.get("first_trade_ts")
            if not first_trade_ts:
                days_active = 1.0
            else:
                days_active = max(1.0, (current_ts - first_trade_ts) / 86400000.0)
            
            # USE realized_pnl_net_usdt as requested to avoid double counting drawdown but include fees
            realized_net = cdata.get("realized_pnl_net_usdt", 0.0)
            net_profit = cdata.get("net_profit_usdt", 0.0)
            
            cdata["max_net_profit"] = round(max(cdata.get("max_net_profit", net_profit), net_profit), 4)
            cdata["min_net_profit"] = round(min(cdata.get("min_net_profit", net_profit), net_profit), 4)
            
            avg_daily_profit = round(realized_net / days_active, 4)
            cdata["avg_daily_profit"] = avg_daily_profit
            
            max_dd = abs(cdata.get("max_drawdown", 0.0))
            if avg_daily_profit > 0:
                cdata["risk_reward_ratio"] = round(max_dd / avg_daily_profit, 2)
            else:
                cdata["risk_reward_ratio"] = 0.0
                
            # If the old key exists, remove it
            if "reward_risk_surplus_pct" in cdata:
                del cdata["reward_risk_surplus_pct"]
                
            # Calculate actual historical Max Position Size from runtime config
            runtime_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
            current_margin = 0.0
            if runtime_path.exists():
                try:
                    rt_data = json.loads(runtime_path.read_text(encoding="utf-8"))
                    for side in ("LONG", "SHORT"):
                        if side in rt_data and rt_data[side].get("enable"):
                            vol = float(rt_data[side].get("total_volume", 0.0))
                            price = float(rt_data[side].get("avg_entry_price", 0.0))
                            current_margin += abs(vol) * price
                except Exception:
                    pass
                    
            cdata["max_position_size"] = round(max(cdata.get("max_position_size", 0.0), current_margin), 4)
            
            cumulative_drme = cdata.get("cumulative_drme", 0.0)
            
            # DRME1 / MDME1 (Incremental On-the-Fly Method)
            cdata["DRME1"] = round(cumulative_drme / days_active, 4)
            cdata["MDME1"] = cdata.get("MDME", 0.0) # From _update_drawdowns
            
            # Legacy fields preservation just in case
            cdata["DRME"] = cdata["DRME1"]
            cdata["MDME"] = cdata["MDME1"]
            
            # DRME2 / MDME2 (Epoch Window Method)
            if "epoch_state" in cdata:
                est = cdata["epoch_state"]
                current_profit = cdata.get("realized_pnl_usdt", 0.0) - est.get("pnl_at_start", 0.0)
                import time
                current_duration = (int(time.time() * 1000) - est.get("start_ts", 0)) / 86400000
                
                active_count = est.get("closed_count", 0)
                cur_drme = 0.0
                cur_mdme = 0.0
                
                # If current epoch is mature (>24h), include it in the average
                if current_duration >= 1.0:
                    safe_sz = est.get("size", 1.0) if est.get("size", 0.0) > 0 else 1.0
                    cur_drme = (current_profit / current_duration) / safe_sz
                    cur_mdme = abs(est.get("max_dd", 0.0)) / safe_sz
                    active_count += 1
                
                if active_count > 0:
                    cdata["DRME2"] = round((est.get("closed_drme_sum", 0.0) + cur_drme) / active_count, 4)
                    cdata["MDME2"] = round((est.get("closed_mdme_sum", 0.0) + cur_mdme) / active_count, 4)
                else:
                    cdata["DRME2"] = cdata["DRME1"]
                    cdata["MDME2"] = cdata["MDME1"]
            else:
                cdata["DRME2"] = cdata["DRME1"]
                cdata["MDME2"] = cdata["MDME1"]

    def _write_data(self, data: dict):
        try:
            self._calculate_advanced_metrics(data)
            self.log_file.write_text(json.dumps(data, indent=4), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error writing analytics file: {e}")

    async def _append_to_csv(self, symbol: str, side: str, open_time: int, close_time: int, pnl: float, balance: float):
        async with self._csv_lock:
            try:
                def ts_to_str(ts_ms):
                    if not ts_ms:
                        return "Unknown"
                    return datetime.fromtimestamp(ts_ms / 1000.0).strftime('%Y-%m-%d %H:%M:%S')

                open_str = ts_to_str(open_time)
                close_str = ts_to_str(close_time)

                # Check if we need to write header
                file_exists = self.txt_file.exists()
                if not file_exists:
                    with open(self.txt_file, mode="w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f, delimiter=';')
                        writer.writerow(["Symbol", "Side", "Open Time", "Close Time", "PnL", "Balance"])

                # Append the new row
                with open(self.txt_file, mode="a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, delimiter=';')
                    writer.writerow([symbol, side, open_str, close_str, round(pnl, 4), round(balance, 4)])
                
                # Truncate if necessary
                with open(self.txt_file, mode="r", encoding="utf-8") as f:
                    lines = f.readlines()
                
                if len(lines) > ANALYTICS_CSV_MAX_ROWS + 1: # +1 for header
                    lines = [lines[0]] + lines[-(ANALYTICS_CSV_MAX_ROWS):]
                    with open(self.txt_file, mode="w", encoding="utf-8", newline="") as f:
                        f.writelines(lines)
            except Exception as e:
                logger.error(f"Error appending to CSV: {e}")



    def record_finished_position(self, client, symbol: str, side: str, open_time: int, close_time: int):
        """Запускает фоновую задачу для подтягивания PnL и записи в лог."""
        self._sync_locks.add(symbol)
        task = asyncio.create_task(self._fetch_and_record(client, symbol, side, open_time, close_time))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def deep_sync_analytics(self, client):
        """
        Level 2 (Absolute) Analytics Reconstruction.
        Fetches true income from Binance since first_trade_ts and perfectly reconstructs 
        trades_ledger.txt and analytics.json.
        """
        if self._sync_in_progress.locked():
            logger.info("Deep Sync already in progress, waiting...")
            
        async with self._sync_in_progress:
            async with self._lock:
                data = self._read_data()
                if not data:
                    return
                start_balance = data.get("start_balance_usdt", 0.0)
                
                # 1. ALWAYS give priority to trades_ledger.txt first row
                csv_ts = None
                try:
                    if self.txt_file.exists():
                        import csv
                        from datetime import datetime
                        with open(self.txt_file, 'r', encoding='utf-8') as f:
                            reader = csv.reader(f, delimiter=';')
                            for row in reader:
                                if len(row) > 3 and "Close Time" not in row[3]:
                                    try:
                                        dt = datetime.strptime(row[3].strip(), "%Y-%m-%d %H:%M:%S")
                                        csv_ts = int(dt.timestamp() * 1000)
                                        break  # First valid row is our definitive start
                                    except Exception:
                                        pass
                except Exception as e:
                    logger.error(f"Failed to read ledger for deep sync: {e}")

                if csv_ts:
                    start_ts = csv_ts
                    data["first_trade_ts"] = start_ts
                    self._write_data(data)
                else:
                    start_ts = data.get("first_trade_ts")
                    
            if not start_ts:
                logger.warning("No first_trade_ts in analytics.json or ledger, skipping deep sync.")
                return
            
            try:
                import json, csv
                from datetime import datetime
                
                # Fetch all symbols we care about
                try:
                    with open("CFG/app.json", "r", encoding="utf-8") as f:
                        app_cfg = json.load(f)
                        syms = app_cfg.get("symbols", [])
                        active_symbols = list(syms.keys()) if isinstance(syms, dict) else list(syms)
                except Exception:
                    active_symbols = []
                
                ledger_symbols = []
                old_volumes = {}
                try:
                    if self.txt_file.exists():
                        import csv
                        with open(self.txt_file, 'r', encoding='utf-8') as f:
                            reader = csv.reader(f, delimiter=';')
                            for row in reader:
                                if len(row) > 1 and row[1] != "Symbol":
                                    ledger_symbols.append(row[1])
                                    if len(row) >= 8:
                                        try:
                                            old_volumes[f"{row[1]}_{row[4]}"] = float(row[7])
                                        except ValueError:
                                            pass
                except Exception:
                    pass
                
                tracked_symbols = set(active_symbols + ledger_symbols)
                
                # Fetch income
                income_records = []
                current_start = start_ts - 600000  # -10m safety
                
                while True:
                    inc_res = await client._request(
                        "GET", 
                        "https://fapi.binance.com/fapi/v1/income", 
                        params={"limit": 1000, "startTime": current_start}, 
                        signed=True
                    )
                    
                    if not inc_res.success or not isinstance(inc_res.data, list) or not inc_res.data:
                        break
                        
                    page_records = inc_res.data
                    income_records.extend(page_records)
                    
                    if len(page_records) < 1000:
                        break
                    
                    current_start = int(page_records[-1].get("time", current_start)) + 1
                    await asyncio.sleep(0.5)  # rate limit safety
                
                # Reconstruct Ledger and Stats
                total_pnl, total_comm, total_fund = 0.0, 0.0, 0.0
                by_symbol = {sym: {"pnl": 0.0, "comm": 0.0, "fund": 0.0, "trades": 0, "wins": 0} for sym in tracked_symbols}
                
                # Sort records chronologically
                income_records.sort(key=lambda x: x.get("time", 0))
                
                # Get current BNB price for commission conversion
                bnb_price = 0.0
                p_res = await client._request("GET", "https://fapi.binance.com/fapi/v1/ticker/price", params={"symbol": "BNBUSDT"})
                if p_res.success:
                    bnb_price = float(p_res.data.get("price", 0.0))

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
                
                # Pre-fetch current margins for fallback
                current_margins = {}
                for sym in tracked_symbols:
                    rt_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
                    vol = 0.0
                    if rt_path.exists():
                        try:
                            rt_data = json.loads(rt_path.read_text(encoding="utf-8"))
                            for side in ("LONG", "SHORT"):
                                if side in rt_data and rt_data[side].get("enable"):
                                    v = float(rt_data[side].get("total_volume", 0.0))
                                    p = float(rt_data[side].get("avg_entry_price", 0.0))
                                    vol += abs(v) * p
                        except Exception:
                            pass
                    current_margins[sym] = vol
                    by_symbol[sym]["cumulative_drme"] = 0.0
                
                # Reconstruct Ledger sequentially
                for (ts, sym, info), g in sorted(grouped.items(), key=lambda x: x[0][0]):
                    from datetime import datetime
                    dt_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
                    
                    # Add to global stats
                    total_pnl += g["pnl"]
                    total_comm += g["comm"]
                    total_fund += g["fund"]
                    
                    # Add to per-coin stats
                    by_symbol[sym]["pnl"] += g["pnl"]
                    by_symbol[sym]["comm"] += g["comm"]
                    by_symbol[sym]["fund"] += g["fund"]
                    
                    net_event = g["pnl"] + g["comm"] + g["fund"]
                    global_pending_delta += net_event
                    
                    if g["has_trade"]:
                        by_symbol[sym]["trades"] += 1
                        if g["pnl"] > 0:
                            by_symbol[sym]["wins"] += 1
                            
                        current_balance += global_pending_delta
                        
                        trade_vol = old_volumes.get(f"{sym}_{round(global_pending_delta, 4)}", current_margins.get(sym, 1.0))
                        if trade_vol <= 0: trade_vol = 1.0
                        
                        by_symbol[sym]["cumulative_drme"] += (global_pending_delta / trade_vol)
                        
                        ledger_rows.append([
                            trade_id_counter, 
                            sym, 
                            "SYNC", 
                            dt_str, 
                            dt_str, 
                            round(global_pending_delta, 4), 
                            round(current_balance, 4),
                            round(trade_vol, 4)
                        ])
                        trade_id_counter += 1
                        global_pending_delta = 0.0
                
                # Any remaining global_pending_delta (e.g. recent funding fee or open pos comm) 
                # gets added to final balance internally, but not as a trade row.
                current_balance += global_pending_delta
                        
                # Overwrite CSV completely
                async with self._csv_lock:
                    with open(self.txt_file, 'w', encoding='utf-8', newline='') as f:
                        writer = csv.writer(f, delimiter=';')
                        writer.writerow(["Id", "Symbol", "Side", "Open Time", "Close Time", "PnL (USDT)", "Balance", "Volume (USDT)"])
                        writer.writerows(ledger_rows)
                        
                # Reconstruct JSON
                async with self._lock:
                    data = self._read_data()
                    
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
                        
                        c_gross = round(stats["pnl"], 4)
                        c["realized_pnl_usdt"] = c_gross
                        c_comm = round(stats["comm"], 4)
                        c_fund = round(stats["fund"], 4)
                        c["commission_usdt"] = c_comm
                        c["funding_usdt"] = c_fund
                        c["realized_pnl_net_usdt"] = round(c_gross + c_comm + c_fund, 4)
                        c["cumulative_drme"] = stats.get("cumulative_drme", 0.0)
                        
                # Update drawdowns logic calculates net_profit_usdt and cur_balance_usdt based on realized_pnl
                await self._update_drawdowns(client, data)
                
                # Save after drawdown update
                self._write_data(data)
                logger.info(f"Absolute Deep Sync completed. PnL: {total_pnl}, Comm: {total_comm}")
                
            except Exception as e:
                logger.error(f"Absolute Deep Sync error: {e}")

    async def _update_drawdowns(self, client, data: dict):
        """Fetches account info to update current unrealized drawdowns globally and per-coin."""
        try:
            res = await client.fetch_account_info()
            if not res.success or not isinstance(res.data, dict):
                return
            
            acc_data = res.data
            
            positions = acc_data.get("positions", [])
            coin_drawdowns = {}
            for p in positions:
                sym = p.get("symbol", "")
                unrealized = float(p.get("unrealizedProfit", 0.0))
                coin_drawdowns[sym] = coin_drawdowns.get(sym, 0.0) + unrealized
                
            bot_unrealized = 0.0
            for sym, cdata in data.get("per_coin", {}).items():
                drawdown = coin_drawdowns.get(sym, 0.0)
                cdata["current_drawdown"] = round(drawdown, 4)
                cdata["max_drawdown"] = round(min(cdata.get("max_drawdown", 0.0), drawdown), 4)
                cdata["min_drawdown"] = round(max(cdata.get("min_drawdown", drawdown), drawdown), 4)
                
                # Adaptive MDME calculation
                rt_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
                current_margin = 0.0
                if rt_path.exists():
                    try:
                        rt_data = json.loads(rt_path.read_text(encoding="utf-8"))
                        for side in ("LONG", "SHORT"):
                            if side in rt_data and rt_data[side].get("enable"):
                                v = float(rt_data[side].get("total_volume", 0.0))
                                p = float(rt_data[side].get("avg_entry_price", 0.0))
                                current_margin += abs(v) * p
                    except Exception:
                        pass
                
                safe_margin = current_margin if current_margin > 0 else 1.0
                current_mdme = abs(drawdown) / safe_margin
                cdata["MDME"] = round(max(cdata.get("MDME", 0.0), current_mdme), 4)
                
                import time
                now_ms = int(time.time() * 1000)
                
                if "epoch_state" not in cdata:
                    cdata["epoch_state"] = {
                        "size": current_margin,
                        "start_ts": now_ms,
                        "pnl_at_start": cdata.get("realized_pnl_usdt", 0.0),
                        "max_dd": drawdown,
                        "closed_drme_sum": cdata.get("DRME1", cdata.get("DRME", 0.0)),
                        "closed_mdme_sum": cdata.get("MDME1", cdata.get("MDME", 0.0)),
                        "closed_count": 1 if cdata.get("DRME1", cdata.get("DRME", 0.0)) > 0 else 0
                    }
                else:
                    est = cdata["epoch_state"]
                    # Update max_dd for current epoch
                    est["max_dd"] = min(est.get("max_dd", 0.0), drawdown)
                    
                    # Detect size change (>10%)
                    if current_margin > 0 and abs(current_margin - est.get("size", current_margin)) > current_margin * 0.1:
                        duration_days = (now_ms - est.get("start_ts", now_ms)) / 86400000
                        if duration_days >= 1.0:
                            epoch_profit = cdata.get("realized_pnl_usdt", 0.0) - est.get("pnl_at_start", 0.0)
                            safe_sz = est.get("size", 1.0) if est.get("size", 0.0) > 0 else 1.0
                            
                            epoch_drme = (epoch_profit / duration_days) / safe_sz
                            epoch_mdme = abs(est.get("max_dd", 0.0)) / safe_sz
                            
                            est["closed_drme_sum"] = est.get("closed_drme_sum", 0.0) + epoch_drme
                            est["closed_mdme_sum"] = est.get("closed_mdme_sum", 0.0) + epoch_mdme
                            est["closed_count"] = est.get("closed_count", 0) + 1
                            
                        # Reset epoch
                        est["size"] = current_margin
                        est["start_ts"] = now_ms
                        est["pnl_at_start"] = cdata.get("realized_pnl_usdt", 0.0)
                        est["max_dd"] = drawdown
                
                bot_unrealized += drawdown
                    
            # unrealized_pnl_usdt = Сум по current_drawdown
            data["unrealized_pnl_usdt"] = round(bot_unrealized, 4)
            
            bot_gross_profit = 0.0
            if "per_coin" in data:
                # Net profit = realized_pnl + commission + funding + current_drawdown
                for sym, cdata in data["per_coin"].items():
                    c_gross = cdata.get("realized_pnl_usdt", 0.0)
                    c_comm = cdata.get("commission_usdt", 0.0)
                    c_fund = cdata.get("funding_usdt", 0.0)
                    
                    cdata["realized_pnl_usdt"] = round(c_gross, 4)
                    c_net = round(c_gross + c_comm + c_fund, 4)
                    cdata["realized_pnl_net_usdt"] = c_net
                    
                    c_drawdown = cdata.get("current_drawdown", 0.0)
                    cdata["net_profit_usdt"] = round(c_net + c_drawdown, 4)
                    
                    bot_gross_profit += c_gross
                    
            bot_total_comm = data.get("total_commission_usdt", 0.0)
            bot_total_fund = data.get("total_funding_usdt", 0.0)
            
            data["realized_pnl_usdt"] = round(bot_gross_profit, 4)
            bot_realized_net = round(bot_gross_profit + bot_total_comm + bot_total_fund, 4)
            data["realized_pnl_net_usdt"] = bot_realized_net
            data["net_profit_usdt"] = round(bot_realized_net + bot_unrealized, 4)
            
            initial = float(data.get("start_balance_usdt", 0.0))
            bot_cur_balance = round(initial + data["net_profit_usdt"], 4)
            data["cur_balance_usdt"] = bot_cur_balance
            
            if initial > 0:
                data["roi_pct"] = round(((bot_cur_balance - initial) / initial) * 100, 2)
            else:
                data["roi_pct"] = 0.0
                
            if bot_gross_profit > 0:
                data["load_ratio"] = round(abs(bot_unrealized) / bot_gross_profit, 2)
            else:
                data["load_ratio"] = 0.0
                    
            # Isolate unrealized PnL to ONLY the coins this bot tracks in analytics
            data["unrealized_pnl_usdt"] = round(bot_unrealized, 4)
                    
        except Exception as e:
            logger.error(f"Error updating drawdowns: {e}")

    def start_realtime_tracker(self, client):
        if hasattr(self, "_tracker_task") and self._tracker_task:
            return
        self._tracker_task = asyncio.create_task(self._realtime_tracker_loop(client))
        self._background_tasks.add(self._tracker_task)
        self._tracker_task.add_done_callback(self._background_tasks.discard)
        
    async def _realtime_tracker_loop(self, client):
        logger.info("[ANALYTICS] Started real-time absolute drawdown tracker (polls every 15s)")
        while True:
            await asyncio.sleep(15.0)
            try:
                # Если хотя бы один символ сейчас ждет подтягивания PnL (5 секунд),
                # мы пропускаем такт трекера. Иначе трекер увидит unrealized=0, но
                # gross_profit еще не обновился, что приведет к "виражу" на графике и искажению peak/trough.
                if self._sync_locks:
                    continue
                    
                async with self._lock:
                    data = self._read_data()
                    if not data:
                        continue
                        
                    res = await client.fetch_account_info()
                    if not res.success or not isinstance(res.data, dict):
                        continue
                        
                    margin_balance = float(res.data.get("totalMarginBalance", 0.0))
                    
                    # Update global unrealized pnl as well
                    positions = res.data.get("positions", [])
                    coin_drawdowns = {}
                    for p in positions:
                        sym = p.get("symbol", "")
                        unrealized = float(p.get("unrealizedProfit", 0.0))
                        coin_drawdowns[sym] = coin_drawdowns.get(sym, 0.0) + unrealized
                    bot_unrealized = 0.0
                    for sym, cdata in data.get("per_coin", {}).items():
                        drawdown = coin_drawdowns.get(sym, 0.0)
                        cdata["current_drawdown"] = round(drawdown, 4)
                        cdata["max_drawdown"] = round(min(cdata.get("max_drawdown", 0.0), drawdown), 4)
                        cdata["min_drawdown"] = round(max(cdata.get("min_drawdown", drawdown), drawdown), 4)
                        bot_unrealized += drawdown
                    data["unrealized_pnl_usdt"] = round(bot_unrealized, 4)
                    
                    bot_gross_profit = 0.0
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
                            bot_gross_profit += c_gross
                            
                    bot_total_comm = data.get("total_commission_usdt", 0.0)
                    bot_total_fund = data.get("total_funding_usdt", 0.0)
                    
                    data["realized_pnl_usdt"] = round(bot_gross_profit, 4)
                    bot_realized_net = round(bot_gross_profit + bot_total_comm + bot_total_fund, 4)
                    data["realized_pnl_net_usdt"] = bot_realized_net
                    data["net_profit_usdt"] = round(bot_realized_net + bot_unrealized, 4)
                    
                    initial = data.get("start_balance_usdt", 0.0)
                    bot_cur_balance = round(initial + data["net_profit_usdt"], 4)
                    data["cur_balance_usdt"] = bot_cur_balance
                    
                    if initial > 0:
                        data["roi_pct"] = round(((bot_cur_balance - initial) / initial) * 100, 2)
                    else:
                        data["roi_pct"] = 0.0
                        
                    if bot_gross_profit > 0:
                        data["load_ratio"] = round(abs(bot_unrealized) / bot_gross_profit, 2)
                    else:
                        data["load_ratio"] = 0.0
                    
                    peak = data.get("peak_balance_usdt", initial)
                    trough = data.get("_current_trough_usdt", peak)
                    min_bal = data.get("min_balance_usdt", initial)
                    
                    if bot_cur_balance > peak:
                        peak = bot_cur_balance
                        trough = bot_cur_balance
                        data["peak_balance_usdt"] = peak
                        
                    if bot_cur_balance < trough:
                        trough = bot_cur_balance
                        
                    if bot_cur_balance < min_bal:
                        min_bal = bot_cur_balance
                        data["min_balance_usdt"] = min_bal
                        
                    data["_current_trough_usdt"] = trough
                        
                    # Always write data to update unrealized PnL, not just on peak/trough change
                    max_drawdown = trough - peak
                    data["max_drawdown_usdt"] = round(min(data.get("max_drawdown_usdt", 0.0), max_drawdown), 4)
                    
                    max_perf = peak - initial
                    data["performance_usdt"] = round(max(data.get("performance_usdt", 0.0), max_perf), 4)
                    
                    if data["max_drawdown_usdt"] < 0:
                        data["recovery_factor"] = round(bot_gross_profit / abs(data["max_drawdown_usdt"]), 2)
                    else:
                        data["recovery_factor"] = 0.0
                    
                    import time
                    data["last_updated_ts"] = int(time.time() * 1000)
                    
                    self._write_data(data)
            except Exception as e:
                logger.error(f"Realtime tracker error: {e}")

    async def _do_fetch_and_record(self, client, symbol: str, side: str, open_time: int, close_time: int):
        """
        Waits 5 seconds after a trade closes, then triggers the Absolute Deep Sync engine
        to completely reconstruct analytics and ledger.
        """
        logger.info(f"[{symbol}] Trade closed. Waiting 5s before Absolute Deep Sync...")
        await asyncio.sleep(5.0)
        await self.deep_sync_analytics(client)
        logger.info(f"[ANALYTICS] Position synced: {symbol} {side}")

    async def _fetch_and_record(self, client, symbol: str, side: str, open_time: int, close_time: int):
        try:
            await self._do_fetch_and_record(client, symbol, side, open_time, close_time)
        finally:
            self._sync_locks.discard(symbol)
