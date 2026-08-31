# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\ANALYTICS\analytics.py
# Role: analytics.py module

import asyncio
import json
import logging
import csv
import re
import time as _time_module
from datetime import datetime, timezone
from pathlib import Path
from consts import ANALYTICS_DIR, DATA_DIR, INCOME_PAGINATION_DELAY_SEC, POST_CLOSE_SYNC_DEBOUNCE_SEC, BG_UNREALIZED_POLL_FREQ_SEC
from c_log import UnifiedLogger

import time
from consts import DATA_DIR
import math
import os
import json, csv
import csv
from datetime import datetime, timezone
import json as _json
import traceback
from ANALYTICS.metrics import AnalyticsMathEngine
logger = UnifiedLogger("Analytics")

# ============================================================
# CIRCUIT BREAKER: Binance IP ban tracker
# ============================================================
_ban_until_ms: int = 0  # Global: timestamp (ms) until which the IP is banned

_BAN_PATTERN = re.compile(r"banned until (\d{10,13})")  # matches e.g. "banned until 1785078419200"


def _update_ban_from_error(error_msg: str) -> None:
    """Parse ban timestamp from Binance error message and update global ban tracker."""
    global _ban_until_ms
    if not error_msg:
        return
    m = _BAN_PATTERN.search(error_msg)
    if m:
        ts = int(m.group(1))
        # Binance returns 10-digit (seconds) or 13-digit (ms) timestamps
        if ts < 1_000_000_000_000:  # 10-digit → convert to ms
            ts *= 1000
        if ts > _ban_until_ms:
            _ban_until_ms = ts
            ban_dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%H:%M:%S UTC")
            logger.warning(f"[CIRCUIT_BREAKER] IP ban registered. Sleeping until {ban_dt} before next analytics REST call.")


async def _wait_ban_lifted() -> bool:
    """If IP is currently banned, sleep until ban expires. Returns True if had to wait."""
    global _ban_until_ms
    now_ms = int(_time_module.time() * 1000)
    if _ban_until_ms <= now_ms:
        return False
    sleep_sec = (_ban_until_ms - now_ms) / 1000.0 + 5.0  # +5s safety buffer
    logger.warning(f"[CIRCUIT_BREAKER] IP still banned. Waiting {sleep_sec:.0f}s before retrying...")
    await asyncio.sleep(sleep_sec)
    _ban_until_ms = 0
    return True

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
            current_ms = int(time.time() * 1000)
            default_data = {
                "start_balance_usdt": 0.0,
                "first_trade_ts": current_ms,
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
                writer.writerow(["Id", "Symbol", "Side", "Open Time", "Close Time", "PnL (USDT)", "Balance"])

    def _read_data(self) -> dict:
        if not self.log_file.exists():
            self._ensure_files()
            
        try:
            return json.loads(self.log_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error reading analytics file: {e}")
            return {}



    def _write_data(self, data: dict, mark_backup: bool = True):
        try:
            AnalyticsMathEngine.calculate(data)
            temp_file = self.log_file.with_suffix('.tmp')
            temp_file.write_text(json.dumps(data, indent=4), encoding="utf-8")
            os.replace(temp_file, self.log_file)
            
            if mark_backup and getattr(self, 'backup_manager', None):
                self.backup_manager.mark_changed()

        except Exception as e:
            logger.error(f"Error writing analytics file: {e}")

    async def _append_to_csv(self, symbol: str, side: str, open_time: int, close_time: int, pnl: float, balance: float):
        async with self._csv_lock:
            try:
                def ts_to_str(ts_ms):
                    if not ts_ms:
                        return "Unknown"
                    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

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
                    
            except Exception as e:
                logger.error(f"Error appending to CSV: {e}")

    async def reset_analytics_state(self):
        """Полный сброс аналитики (удаление файлов) с соблюдением блокировок."""
        async with self._lock:
            async with self._csv_lock:
                if self.log_file.exists():
                    try:
                        os.remove(self.log_file)
                    except Exception as e:
                        logger.error(f"Error removing {self.log_file}: {e}")
                
                if self.txt_file.exists():
                    try:
                        os.remove(self.txt_file)
                    except Exception as e:
                        logger.error(f"Error removing {self.txt_file}: {e}")

    async def set_initial_balance(self, new_balance: float):
        """Установка начального баланса."""
        async with self._lock:
            data = self._read_data()
            data["start_balance_usdt"] = round(new_balance, 4)
            self._write_data(data)


    def record_finished_position(self, client, symbol: str, side: str, open_time: int, close_time: int):
        """Запускает фоновую задачу для подтягивания PnL и записи в лог."""
        self._sync_locks.add((symbol, side))
        task = asyncio.create_task(self._fetch_and_record(client, symbol, side, open_time, close_time))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def deep_sync_analytics(self, client, cached_prices: dict = None):
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
                start_balance = data["start_balance_usdt"]
                # first_trade_ts — единственный источник правды о точке отсчета.
                # Устанавливается при создании analytics.json или при сбросе баланса.
                # НИКОГДА не перезаписывается из ledger — иначе старые записи снова подсасываются.
                start_ts = data["first_trade_ts"]
                    
            if not start_ts:
                logger.warning("No first_trade_ts in analytics.json, skipping deep sync.")
                return
            
            try:
                
                # Fetch all symbols we care about
                try:
                    with open("CFG/app.json", "r", encoding="utf-8") as f:
                        app_cfg = json.load(f)
                        syms = app_cfg.get("symbols", [])
                        active_symbols = list(syms.keys()) if isinstance(syms, dict) else list(syms)
                except Exception:
                    active_symbols = []
                
                ledger_symbols = []
                try:
                    if self.txt_file.exists():
                        with open(self.txt_file, 'r', encoding='utf-8') as f:
                            reader = csv.reader(f, delimiter=';')
                            for row in reader:
                                if not row or row[0] in ("Id", "Symbol"):
                                    continue
                                sym = row[1].strip() if row[0].isdigit() else row[0].strip()
                                ledger_symbols.append(sym)
                except Exception:
                    pass
                
                tracked_symbols = set(active_symbols + ledger_symbols)
                
                # Fetch income starting strictly from first_trade_ts.
                # No safety lookback — first_trade_ts is set precisely at reset/init time.
                # A lookback would pull in pre-reset trades and corrupt stats.
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
                        inc_res = await client._request(
                            "GET", 
                            "https://fapi.binance.com/fapi/v1/income", 
                            params={"limit": 1000, "startTime": current_start, "endTime": end_time}, 
                            signed=True
                        )
                        if inc_res and inc_res.success and isinstance(inc_res.data, list):
                            success_fetch = True
                            break
                        attempts += 1
                        await asyncio.sleep(1.0)
                        
                    if not success_fetch:
                        err_msg = getattr(inc_res, 'error_msg', 'Unknown error') if inc_res else 'None'
                        logger.error(f"[ANALYTICS] Failed to fetch income history (Msg: {err_msg}) after 3 attempts! Aborting Deep Sync to protect stats.")
                        return # Abort the whole sync to prevent data loss
                        
                    page_records = inc_res.data
                    if page_records:
                        income_records.extend(page_records)
                        # Advance current_start to strictly after the last record's time
                        current_start = int(page_records[-1].get("time", current_start)) + 1
                    else:
                        # No records in this 7-day window, jump to the next window
                        current_start = end_time + 1
                        
                    if current_start > now_ts:
                        is_fetching = False
                        continue
                    
                    await asyncio.sleep(INCOME_PAGINATION_DELAY_SEC)  # rate limit safety
                
                # Reconstruct Ledger and Stats
                total_pnl, total_comm, total_fund = 0.0, 0.0, 0.0
                by_symbol = {sym: {"pnl": 0.0, "comm": 0.0, "fund": 0.0, "trades": 0, "wins": 0, "first_ts": None} for sym in tracked_symbols}
                
                # Sort records chronologically
                income_records.sort(key=lambda x: x.get("time", 0))
                
                # Get current BNB price for commission conversion
                bnb_price = await self._get_bnb_usdt_rate(client, cached_prices)

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
                
                # 8th column logic removed entirely
                # Reconstruct Ledger sequentially
                active_trades = {}  # {sym: {"last_ts": 0, "pnl": 0.0}}
                for (ts, sym, info), g in sorted(grouped.items(), key=lambda x: x[0][0]):
                    dt_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    
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
                        if by_symbol[sym]["first_ts"] is None:
                            by_symbol[sym]["first_ts"] = ts
                            
                        # Считаем реальные сделки, а не филы (группируем филы внутри 5-секундного окна)
                        if sym not in active_trades:
                            active_trades[sym] = {"last_ts": ts, "pnl": g["pnl"]}
                        else:
                            last_ts = active_trades[sym]["last_ts"]
                            if ts - last_ts <= 5000:
                                # Тот же трейд (филы рядом по времени)
                                active_trades[sym]["last_ts"] = ts
                                active_trades[sym]["pnl"] += g["pnl"]
                            else:
                                # Прошло больше 5 секунд -> закрываем предыдущий трейд и считаем его
                                by_symbol[sym]["trades"] += 1
                                if active_trades[sym]["pnl"] > 0:
                                    by_symbol[sym]["wins"] += 1
                                # Начинаем отсчет нового трейда
                                active_trades[sym] = {"last_ts": ts, "pnl": g["pnl"]}
                            
                        current_balance += global_pending_delta
                        ledger_rows.append([
                            trade_id_counter, 
                            sym, 
                            "SYNC", 
                            dt_str, 
                            dt_str, 
                            round(global_pending_delta, 4), 
                            round(current_balance, 4)
                        ])
                        trade_id_counter += 1
                        global_pending_delta = 0.0
                
                # Any remaining global_pending_delta (e.g. recent funding fee or open pos comm) 
                # gets added to final balance internally, but not as a trade row.
                current_balance += global_pending_delta
                
                # Финализируем последние открытые трейды для статистики (после цикла)
                for sym, t_info in active_trades.items():
                    if t_info["last_ts"] > 0:
                        by_symbol[sym]["trades"] += 1
                        if t_info["pnl"] > 0:
                            by_symbol[sym]["wins"] += 1
                        
                # Overwrite CSV completely using atomic write
                async with self._csv_lock:
                    temp_txt = self.txt_file.with_suffix('.tmp')
                    with open(temp_txt, 'w', encoding='utf-8', newline='') as f:
                        writer = csv.writer(f, delimiter=';')
                        writer.writerow(["Id", "Symbol", "Side", "Open Time", "Close Time", "PnL (USDT)", "Balance"])
                        writer.writerows(ledger_rows)
                    os.replace(temp_txt, self.txt_file)
                        
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
                    # Update drawdowns logic calculates net_profit_usdt and cur_balance_usdt based on realized_pnl
                    await self._update_drawdowns(client, data)
                    
                    # Save after drawdown update
                    self._write_data(data)
                
                logger.info(f"Absolute Deep Sync completed. PnL: {total_pnl}, Comm: {total_comm}")
                
            except Exception as e:
                logger.error(f"Absolute Deep Sync error: {e}")

    async def _update_drawdowns(self, client, data: dict, is_lightweight: bool = False):
        """Fetches account info to update current unrealized drawdowns globally and per-coin."""
        try:
            # If IP is currently banned, wait until ban expires before making any REST call
            await _wait_ban_lifted()

            res = await client.fetch_account_info()
            
            # CRITICAL RACE CONDITION FIX: 
            # Check if a deep sync lock was added while we were waiting for the REST API.
            if is_lightweight and self._sync_locks:
                logger.info("[ANALYTICS] Deep Sync lock acquired during REST fetch. Aborting lightweight PnL sync to prevent extremes corruption.")
                return False

            if not res.success or not isinstance(res.data, dict):
                err = getattr(res, 'error_msg', 'Unknown Error') or ''
                _update_ban_from_error(err)
                logger.warning(f"[ANALYTICS] fetch_account_info failed: {err}")
                return False
            
            acc_data = res.data
            
            if "positions" not in acc_data or "totalMarginBalance" not in acc_data:
                logger.error("[ANALYTICS] fetch_account_info returned empty or invalid data from Binance! Skipping update to protect stats.")
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
                
                coin_drawdowns[sym] = coin_drawdowns.get(sym, 0.0) + unrealized
                
                if sym not in coin_details:
                    coin_details[sym] = {"long_unrealized": 0.0, "short_unrealized": 0.0, "long_amt": 0.0, "short_amt": 0.0}
                    
                if side == "LONG":
                    coin_details[sym]["long_unrealized"] = round(unrealized, 4)
                    coin_details[sym]["long_amt"] = amt
                elif side == "SHORT":
                    coin_details[sym]["short_unrealized"] = round(unrealized, 4)
                    coin_details[sym]["short_amt"] = amt
                
            bot_unrealized = 0.0
            
            # Load tracked symbols from app.json (not just per_coin keys)
            # This ensures unrealized PnL is tracked even before any trades close.
            try:
                with open("CFG/app.json", "r", encoding="utf-8") as _f:
                    _app = _json.load(_f)
                    _syms = _app["symbols"]
                    config_symbols = list(_syms.keys()) if isinstance(_syms, dict) else list(_syms)
            except Exception:
                config_symbols = list(data.get("per_coin", {}).keys())
                
            if not config_symbols:
                logger.warning("[ANALYTICS] _update_drawdowns: no symbols in config or per_coin, skipping.")
                return False
                
            # SECONDARY RACE CONDITION FIX: 
            # Deep check for REST vs Local State desync (Partial/Full Closes)
            if is_lightweight:
                for sym in config_symbols:
                    rt_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
                    if rt_path.exists():
                        try:
                            import json as _json_local
                            rt_data = _json_local.loads(rt_path.read_text(encoding="utf-8"))
                            local_long_vol = float(rt_data.get("LONG", {}).get("volume", 0.0))
                            local_short_vol = float(rt_data.get("SHORT", {}).get("volume", 0.0))
                            
                            rest_details = coin_details.get(sym, {})
                            rest_long_amt = abs(rest_details.get("long_amt", 0.0))
                            rest_short_amt = abs(rest_details.get("short_amt", 0.0))
                            
                            if (local_long_vol > 0 and rest_long_amt < local_long_vol * 0.99) or \
                               (local_short_vol > 0 and rest_short_amt < local_short_vol * 0.99):
                                logger.info(f"[ANALYTICS] Sync race condition detected for {sym}! REST amt < Local vol. Aborting lightweight sync.")
                                return False
                        except Exception:
                            pass
                
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
                        "min_drawdown": 0.0
                    }

            for sym, cdata in data.get("per_coin", {}).items():
                details = coin_details.get(sym, {})
                long_unreal = details.get("long_unrealized", 0.0)
                short_unreal = details.get("short_unrealized", 0.0)
                
                # Случай Б (Реализ опережает нереализ): Биржа отдает старый нереализ закрытой позиции
                if (sym, "LONG") in self._sync_locks and details.get("long_amt", 0.0) != 0:
                    long_unreal = 0.0
                if (sym, "SHORT") in self._sync_locks and details.get("short_amt", 0.0) != 0:
                    short_unreal = 0.0
                    
                drawdown = long_unreal + short_unreal
                
                cdata["current_drawdown"] = round(drawdown, 4)
                
                cdata["long_unrealized"] = long_unreal
                cdata["short_unrealized"] = short_unreal
                cdata["long_amt"] = details.get("long_amt", 0.0)
                cdata["short_amt"] = details.get("short_amt", 0.0)
                
                cdata["max_drawdown"] = round(min(cdata.get("max_drawdown", 0.0), drawdown), 4)
                cdata["min_drawdown"] = round(max(cdata.get("min_drawdown", drawdown), drawdown), 4)
                
                # Adaptive MDME calculation
                rt_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
                current_margin = 0.0
                if rt_path.exists():
                    try:
                        rt_data = json.loads(rt_path.read_text(encoding="utf-8"))
                        long_size = float(rt_data.get("LONG", {}).get("invest_size", 0.0)) if rt_data.get("LONG", {}).get("enable") else 0.0
                        short_size = float(rt_data.get("SHORT", {}).get("invest_size", 0.0)) if rt_data.get("SHORT", {}).get("enable") else 0.0
                        current_margin = (long_size + short_size) / 2.0
                    except Exception:
                        pass
                
                safe_margin = current_margin if current_margin > 0 else 1.0
                current_mdme = abs(drawdown) / safe_margin
                cdata["MDME"] = round(max(cdata.get("MDME", 0.0), current_mdme), 4)
                
                now_ms = int(time.time() * 1000)
                
                if "epoch_state" not in cdata:
                    cdata["epoch_state"] = {
                        "size": current_margin,
                        "start_ts": cdata.get("first_trade_ts", now_ms),
                        "pnl_at_start": 0.0,
                        "max_dd": drawdown,
                        "closed_drme_sum": 0.0,
                        "closed_mdme_sum": 0.0,
                        "closed_count": 0
                    }
                else:
                    est = cdata["epoch_state"]
                    est["max_dd"] = min(est.get("max_dd", 0.0), drawdown)
                    
                    if current_margin > 0 and abs(current_margin - est.get("size", current_margin)) > current_margin * 0.1:
                        duration_days = (now_ms - est.get("start_ts", now_ms)) / 86400000
                        if duration_days >= 1.0:
                            epoch_profit = cdata.get("realized_pnl_net_usdt", 0.0) - est.get("pnl_at_start", 0.0)
                            safe_sz = est.get("size", 1.0) if est.get("size", 0.0) > 0 else 1.0
                            
                            epoch_drme = (epoch_profit / duration_days) / safe_sz
                            epoch_mdme = abs(est.get("max_dd", 0.0)) / safe_sz
                            
                            est["closed_drme_sum"] = est.get("closed_drme_sum", 0.0) + epoch_drme
                            est["closed_mdme_sum"] = est.get("closed_mdme_sum", 0.0) + epoch_mdme
                            est["closed_count"] = est.get("closed_count", 0) + 1
                            
                        est["size"] = current_margin
                        est["start_ts"] = now_ms
                        est["pnl_at_start"] = cdata.get("realized_pnl_net_usdt", 0.0)
                        est["max_dd"] = drawdown
                
                bot_unrealized += drawdown
                    
            data["unrealized_pnl_usdt"] = round(bot_unrealized, 4)
            
            bot_gross_profit = 0.0
            bot_total_comm = 0.0
            bot_total_fund = 0.0
            
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
                    bot_total_comm += c_comm
                    bot_total_fund += c_fund
                    
            if data.get("total_trades", 0) == 0:
                bot_gross_profit = 0.0
                bot_total_comm = 0.0
                bot_total_fund = 0.0
                
            data["total_commission_usdt"] = round(bot_total_comm, 4)
            data["total_funding_usdt"] = round(bot_total_fund, 4)
                
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
                
            max_drawdown = trough - peak
            data["max_drawdown_usdt"] = round(min(data.get("max_drawdown_usdt", 0.0), max_drawdown), 4)
            
            max_perf = peak - initial
            data["performance_usdt"] = round(max(data.get("performance_usdt", 0.0), max_perf), 4)
            
            if data["max_drawdown_usdt"] < 0:
                data["recovery_factor"] = round(bot_gross_profit / abs(data["max_drawdown_usdt"]), 2)
            else:
                data["recovery_factor"] = 0.0
                
            return True
                    
        except Exception as e:
            logger.error(f"[ANALYTICS] Error updating drawdowns: {e}\n{traceback.format_exc()}")
            return False

    async def _get_bnb_usdt_rate(self, client, cached_prices: dict = None) -> float:
        if cached_prices and "BNBUSDT" in cached_prices:
            val = cached_prices.get("BNBUSDT")
            if val:
                return float(val)
                
        p_res = await client._request("GET", "https://fapi.binance.com/fapi/v1/ticker/price", params={"symbol": "BNBUSDT"})
        if p_res and p_res.success:
            return float(p_res.data.get("price", 0.0))
        return 0.0

    async def sync_lightweight_unrealized_pnl(self, client):
        async with self._lock:
            data = self._read_data()
            if not data:
                return
                
            success = await self._update_drawdowns(client, data, is_lightweight=True)
            if success:
                data["last_updated_ts"] = int(time.time() * 1000)
                self._write_data(data, mark_backup=False)

    def start_realtime_tracker(self, client):
        if hasattr(self, "_tracker_task") and self._tracker_task:
            return
        self._is_tracker_running = True
        self._tracker_task = asyncio.create_task(self._realtime_tracker_loop(client))
        self._background_tasks.add(self._tracker_task)
        self._tracker_task.add_done_callback(self._background_tasks.discard)
        
    def stop_realtime_tracker(self):
        self._is_tracker_running = False
        if hasattr(self, "_tracker_task") and self._tracker_task:
            self._tracker_task.cancel()
            self._tracker_task = None

    async def _realtime_tracker_loop(self, client):
        logger.info(f"[ANALYTICS] Started real-time absolute drawdown tracker (polls every {BG_UNREALIZED_POLL_FREQ_SEC}s)")
        while getattr(self, '_is_tracker_running', True):
            await asyncio.sleep(BG_UNREALIZED_POLL_FREQ_SEC)
            try:
                # Если хотя бы один символ сейчас ждет подтягивания PnL (5 секунд),
                # мы пропускаем такт трекера. Иначе трекер увидит unrealized=0, но
                # gross_profit еще не обновился, что приведет к "виражу" на графике и искажению peak/trough.
                if self._sync_locks:
                    continue

                # Circuit breaker: skip tick entirely if still banned (ban-wait happens inside _update_drawdowns)
                global _ban_until_ms
                now_ms = int(_time_module.time() * 1000)
                if _ban_until_ms > now_ms:
                    # Don't call REST at all while banned — just skip this tick silently
                    continue

                await self.sync_lightweight_unrealized_pnl(client)
            except Exception as e:
                logger.error(f"Realtime tracker error: {e}")

    async def _do_fetch_and_record(self, client, symbol: str, side: str, open_time: int, close_time: int):
        """
        Waits after a trade closes, then triggers the Absolute Deep Sync engine
        to completely reconstruct analytics and ledger.
        """
        logger.info(f"[{symbol}] Trade closed. Waiting {POST_CLOSE_SYNC_DEBOUNCE_SEC}s before Absolute Deep Sync...")
        
        # The first_trade_ts is now automatically anchored to the exact moment analytics.json is created.
        # This prevents the bot from looking into the past and pulling old trades after a reset.
        if open_time:
            pass
            
        await asyncio.sleep(POST_CLOSE_SYNC_DEBOUNCE_SEC)
        # Pass None for cached_prices here because we are in background, if we want cache we need to pass it from BotCore
        await self.deep_sync_analytics(client)
        logger.info(f"[ANALYTICS] Position synced: {symbol} {side}")

    async def _fetch_and_record(self, client, symbol: str, side: str, open_time: int, close_time: int):
        try:
            await self._do_fetch_and_record(client, symbol, side, open_time, close_time)
        finally:
            self._sync_locks.discard((symbol, side))
