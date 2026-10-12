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
from consts import (
    ANALYTICS_DIR,
    ANALYTICS_STATS_DIR,
    ANALYTICS_LEDGERS_DIR,
    CACHE_DIR,
    DATA_DIR,
    INCOME_PAGINATION_DELAY_SEC,
    POST_CLOSE_SYNC_DEBOUNCE_SEC,
    BG_UNREALIZED_POLL_FREQ_SEC,
    ANALYTICS_START_BALANCE_USDT
)
from ANALYTICS.leaderboard import LeaderboardEngine
from c_log import UnifiedLogger
from c_utils import Utils

import time
import math
import os
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
    Ведет журнал сделок и статистику закрытых позиций под куполами стратегий (вселенных).
    """
    def __init__(self, strategy_registry=None):
        self.strategy_registry = strategy_registry
        self.stats_dir = ANALYTICS_STATS_DIR
        self.ledgers_dir = ANALYTICS_LEDGERS_DIR
        self.log_file = ANALYTICS_STATS_DIR / "analytics.json"
        self.txt_file = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
        self.leaderboard_engine = LeaderboardEngine(analytics_dir=ANALYTICS_DIR, stats_dir=ANALYTICS_STATS_DIR)
        try:
            from ANALYTICS.trade_velocity import TradeVelocityEngine
            self.velocity_engine = TradeVelocityEngine(universe_id="default", ledger_path=self.txt_file)
        except Exception as ve:
            logger.warning(f"Could not initialize TradeVelocityEngine: {ve}")
            self.velocity_engine = None
        self._lock = asyncio.Lock()
        self._csv_lock = asyncio.Lock()
        self._background_tasks = set()
        self._sync_locks = set()
        self._sync_in_progress = asyncio.Lock()
        self._ensure_files()

        # Sync start balance from config upon restart
        data = self._read_data()
        if data:
            stored_start = float(data.get("start_balance_usdt", 0.0))
            config_start = float(ANALYTICS_START_BALANCE_USDT)
            if config_start > 0 and abs(stored_start - config_start) > 0.0001:
                logger.info(f"Syncing analytics start balance from config: {stored_start} -> {config_start}")
                self.rebase_start_balance(config_start)

    def _ensure_files(self):
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)

        if not self.log_file.exists():
            root_json = ANALYTICS_DIR / "analytics.json"
            if root_json.exists():
                try:
                    self.log_file.write_text(root_json.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass

        if not self.log_file.exists():
            current_ms = int(time.time() * 1000)
            initial_balance = float(ANALYTICS_START_BALANCE_USDT)
            default_data = {
                "start_balance_usdt": initial_balance,
                "_rebased_start_balance_usdt": initial_balance,
                "first_trade_ts": current_ms,
                "cur_balance_usdt": initial_balance,
                "peak_balance_usdt": initial_balance,
                "min_balance_usdt": initial_balance,
                "_current_trough_usdt": initial_balance,
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
                "iteration_start_ts": current_ms,
                "iteration_peak_net_profit_usdt": 0.0,
                "iteration_peak_ts": current_ms,
                "iteration_peak_age_hours": 0.0,
                "iteration_net_velocity": 0.0,
                "per_coin": {}
            }
            self.log_file.write_text(json.dumps(default_data, indent=4), encoding="utf-8")
            (ANALYTICS_DIR / "analytics.json").write_text(json.dumps(default_data, indent=4), encoding="utf-8")
        
        if not self.txt_file.exists():
            root_txt = ANALYTICS_DIR / "trades_ledger.txt"
            if root_txt.exists():
                try:
                    self.txt_file.write_text(root_txt.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass

        if not self.txt_file.exists():
            with open(self.txt_file, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Symbol", "Side", "Open Time", "Close Time", "PnL", "Balance"])

        self._ensure_strategy_files()

    def _ensure_strategy_files(self):
        """Гарантирует наличие изолированных файлов аналитики и леджеров по каждой стратегии."""
        if not self.strategy_registry:
            try:
                from CORE.STRATEGY.strategy_registry import StrategyRegistry
                self.strategy_registry = StrategyRegistry()
            except Exception:
                return

        current_ms = int(time.time() * 1000)
        for strat_id, strat_cfg in self.strategy_registry.active_strategies.items():
            strat_init_bal = float(strat_cfg.analytics_cfg.get("start_balance_usdt", strat_cfg.start_balance_usdt))
            strat_json = self.stats_dir / f"analytics_{strat_id}.json"
            if not strat_json.exists():
                strat_data = {
                    "universe_id": strat_id,
                    "name": strat_cfg.name,
                    "start_balance_usdt": strat_init_bal,
                    "_rebased_start_balance_usdt": strat_init_bal,
                    "first_trade_ts": current_ms,
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
                    "iteration_start_ts": current_ms,
                    "iteration_peak_net_profit_usdt": 0.0,
                    "iteration_peak_ts": current_ms,
                    "iteration_peak_age_hours": 0.0,
                    "iteration_net_velocity": 0.0,
                    "per_coin": {}
                }
                Utils.atomic_write_json(strat_json, strat_data, indent=4)
            else:
                try:
                    existing_data = json.loads(strat_json.read_text(encoding="utf-8"))
                    if int(existing_data.get("total_trades", 0)) == 0 and float(existing_data.get("start_balance_usdt", 0.0)) != strat_init_bal:
                        existing_data["start_balance_usdt"] = strat_init_bal
                        existing_data["_rebased_start_balance_usdt"] = strat_init_bal
                        existing_data["cur_balance_usdt"] = strat_init_bal
                        existing_data["peak_balance_usdt"] = strat_init_bal
                        existing_data["min_balance_usdt"] = strat_init_bal
                        existing_data["_current_trough_usdt"] = strat_init_bal
                        Utils.atomic_write_json(strat_json, existing_data, indent=4)
                except Exception:
                    pass

            strat_txt = self.ledgers_dir / f"trades_ledger_{strat_id}.txt"
            if not strat_txt.exists():
                with open(strat_txt, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, delimiter=';')
                    writer.writerow(["Symbol", "Side", "Open Time", "Close Time", "PnL", "Balance"])

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
            if getattr(self, 'velocity_engine', None):
                try:
                    data["trade_velocity"] = self.velocity_engine.calculate_velocity(data)
                except Exception as ve:
                    logger.error(f"Error calculating trade velocity: {ve}")
            json_str = json.dumps(data, indent=4, ensure_ascii=False)
            Utils.atomic_write_text(self.log_file, json_str)
            # Зеркалирование в корень ANALYTICS для обратной совместимости
            root_legacy = ANALYTICS_DIR / "analytics.json"
            if root_legacy != self.log_file:
                Utils.atomic_write_text(root_legacy, json_str)
            
            if mark_backup and getattr(self, 'backup_manager', None):
                self.backup_manager.mark_changed()

        except Exception as e:
            logger.error(f"Error writing analytics file: {e}")

    def get_trade_velocity(self, universe_id: str = "default") -> Dict[str, Any]:
        """Возвращает метрики Trade Velocity для портфеля или конкретной вселенной."""
        try:
            if universe_id == "default":
                if getattr(self, "velocity_engine", None):
                    return self.velocity_engine.calculate_velocity(self._read_data() or {})
                from ANALYTICS.trade_velocity import TradeVelocityEngine
                return TradeVelocityEngine(universe_id="default", ledger_path=self.txt_file).calculate_velocity(self._read_data() or {})
            else:
                if not hasattr(self, "_strat_velocity_engines"):
                    self._strat_velocity_engines = {}
                if universe_id not in self._strat_velocity_engines:
                    from ANALYTICS.trade_velocity import TradeVelocityEngine
                    strat_ledger = self.ledgers_dir / f"trades_ledger_{universe_id}.txt"
                    self._strat_velocity_engines[universe_id] = TradeVelocityEngine(universe_id=universe_id, ledger_path=strat_ledger)
                strat_file = self.stats_dir / f"analytics_{universe_id}.json"
                sdata = Utils.read_json_file(strat_file) if strat_file.exists() else {}
                return self._strat_velocity_engines[universe_id].calculate_velocity(sdata)
        except Exception as e:
            logger.warning(f"[ANALYTICS] Failed to get trade velocity for {universe_id}: {e}")
            return {}

    def _get_iteration_cache(self, strat_id: str) -> dict:
        try:
            cache_file = CACHE_DIR / "iteration_state.json"
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                return data.get(strat_id, {})
        except Exception:
            pass
        return {}

    def _save_iteration_cache(self, strat_id: str, state: dict):
        try:
            cache_file = CACHE_DIR / "iteration_state.json"
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

    async def _append_to_csv(self, symbol: str, side: str, open_time: int, close_time: int, pnl: float, balance: float):
        async with self._csv_lock:
            try:
                def ts_to_str(ts_ms):
                    if not ts_ms:
                        return "Unknown"
                    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

                open_str = ts_to_str(open_time)
                close_str = ts_to_str(close_time)

                # Запись в общий леджер
                for target_txt in [self.txt_file, ANALYTICS_DIR / "trades_ledger.txt"]:
                    if not target_txt.exists():
                        target_txt.parent.mkdir(parents=True, exist_ok=True)
                        with open(target_txt, mode="w", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f, delimiter=';')
                            writer.writerow(["Symbol", "Side", "Open Time", "Close Time", "PnL", "Balance"])

                    with open(target_txt, mode="a", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f, delimiter=';')
                        writer.writerow([symbol, side, open_str, close_str, round(pnl, 4), round(balance, 4)])

                # Запись в изолированный леджер стратегии
                strat_id = None
                if self.strategy_registry:
                    strat_id = self.strategy_registry.get_strategy_id_for_symbol(symbol)
                if strat_id:
                    strat_txt = self.ledgers_dir / f"trades_ledger_{strat_id}.txt"
                    if not strat_txt.exists():
                        strat_txt.parent.mkdir(parents=True, exist_ok=True)
                        with open(strat_txt, mode="w", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f, delimiter=';')
                            writer.writerow(["Symbol", "Side", "Open Time", "Close Time", "PnL", "Balance"])

                    with open(strat_txt, mode="a", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f, delimiter=';')
                        writer.writerow([symbol, side, open_str, close_str, round(pnl, 4), round(balance, 4)])

                if getattr(self, 'velocity_engine', None):
                    try:
                        self.velocity_engine.on_trade_closed(symbol, close_time)
                    except Exception as ve:
                        logger.error(f"Error updating velocity engine on trade closed: {ve}")

            except Exception as e:
                logger.error(f"Error appending to CSV: {e}")

    async def reset_analytics_state(self):
        """Полный сброс аналитики (удаление файлов) с соблюдением блокировок."""
        async with self._lock:
            async with self._csv_lock:
                for f in [self.log_file, self.txt_file, ANALYTICS_DIR / "analytics.json", ANALYTICS_DIR / "trades_ledger.txt"]:
                    if f.exists():
                        try:
                            os.remove(f)
                        except Exception as e:
                            logger.error(f"Error removing {f}: {e}")
                if self.stats_dir.exists():
                    for f in self.stats_dir.glob("analytics_*.json"):
                        try:
                            f.unlink()
                        except Exception:
                            pass
                if self.ledgers_dir.exists():
                    for f in self.ledgers_dir.glob("trades_ledger_*.txt"):
                        try:
                            f.unlink()
                        except Exception:
                            pass

    def rebase_start_balance(self, new_balance: float) -> dict:
        """
        Корректно обновляет стартовый баланс без создания ложной просадки.
        Сдвигает пики, впадины и текущий баланс на delta.
        """
        data = self._read_data()
        old_balance = float(data.get("start_balance_usdt", 0.0))
        delta = round(new_balance - old_balance, 4)
        
        data["start_balance_usdt"] = round(new_balance, 4)
        data["cur_balance_usdt"] = round(float(data.get("cur_balance_usdt", old_balance)) + delta, 4)
        
        peak_val = data.get("peak_balance_usdt")
        data["peak_balance_usdt"] = round((float(peak_val) if peak_val is not None else old_balance) + delta, 4)
        
        trough_val = data.get("_current_trough_usdt")
        data["_current_trough_usdt"] = round((float(trough_val) if trough_val is not None else old_balance) + delta, 4)
        
        min_val = data.get("min_balance_usdt")
        data["min_balance_usdt"] = round((float(min_val) if min_val is not None else old_balance) + delta, 4)
        
        data["_rebased_start_balance_usdt"] = round(new_balance, 4)
        
        self._write_data(data)
        return data

    async def set_initial_balance(self, new_balance: float):
        """Установка начального баланса с корректным перерасчетом пиков (rebase)."""
        async with self._lock:
            self.rebase_start_balance(new_balance)


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
                # Точка отсчета: текущее время (не подтягивать старые закрытые трейды предыдущей сессии)
                start_ts = int(time.time() * 1000)
                logger.info(f"[ANALYTICS] Initialized fresh first_trade_ts: {start_ts}")
                async with self._lock:
                    data["first_trade_ts"] = start_ts
                    self._write_data(data)
            
            try:
                
                try:
                    with open("CFG/app.json", "r", encoding="utf-8") as f:
                        app_cfg = json.load(f)
                        active_strategies = app_cfg.get("active_strategies", {})
                        if active_strategies:
                            active_symbols = []
                            for sdata in active_strategies.values():
                                if sdata.get("enabled", False):
                                    active_symbols.extend(sdata.get("symbols", []))
                        else:
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
                        "long_entry": 0.0, "short_entry": 0.0
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
            
            # Load tracked symbols from app.json (not just per_coin keys)
            # This ensures unrealized PnL is tracked even before any trades close.
            try:
                with open("CFG/app.json", "r", encoding="utf-8") as _f:
                    _app = json.load(_f)
                    _active_strats = _app.get("active_strategies", {})
                    if _active_strats:
                        config_symbols = []
                        for _sdata in _active_strats.values():
                            if _sdata.get("enabled", False):
                                config_symbols.extend(_sdata.get("symbols", []))
                    else:
                        _syms = _app.get("symbols", {})
                        config_symbols = list(_syms.keys()) if isinstance(_syms, dict) else list(_syms)
            except Exception:
                config_symbols = []
                
            if not config_symbols:
                config_symbols = list(data.get("per_coin", {}).keys())
            if not config_symbols:
                config_symbols = list(coin_details.keys())
                
            # SECONDARY RACE CONDITION FIX: 
            # Deep check for REST vs Local State desync (Partial/Full Closes)
            if is_lightweight:
                for sym in config_symbols:
                    rt_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
                    if rt_path.exists():
                        try:
                            import json as _json_local
                            rt_data = _json_local.loads(rt_path.read_text(encoding="utf-8"))
                            local_long_vol = float(rt_data.get("LONG", {}).get("volume") or 0.0)
                            local_short_vol = float(rt_data.get("SHORT", {}).get("volume") or 0.0)
                            
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
                
                # Grid Capacity and Margin calculation for sym
                rt_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
                rt_data = {}
                if rt_path.exists():
                    try:
                        rt_data = json.loads(rt_path.read_text(encoding="utf-8"))
                    except Exception:
                        rt_data = {}
                
                base_data = Utils.read_json_file(DATA_DIR / "_base.json") or {}
                long_cfg = rt_data.get("LONG", base_data.get("LONG", {}))
                short_cfg = rt_data.get("SHORT", base_data.get("SHORT", {}))
                
                long_size = float(long_cfg.get("invest_size", 400.0)) if long_cfg.get("enable", True) else 0.0
                short_size = float(short_cfg.get("invest_size", 400.0)) if short_cfg.get("enable", True) else 0.0
                long_lev = float(long_cfg.get("leverage", 20.0)) or 20.0
                short_lev = float(short_cfg.get("leverage", 20.0)) or 20.0
                
                grid_capacity = long_size + short_size
                current_margin = grid_capacity / 2.0
                
                # Notional exposure (Load)
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
                
                if "epoch_state" in cdata:
                    del cdata["epoch_state"]
                if "DRME" in cdata:
                    del cdata["DRME"]
                if "MDME" in cdata:
                    del cdata["MDME"]
                
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
                    global_init = float(data.get("start_balance_usdt", ANALYTICS_START_BALANCE_USDT))
                    cdata["roi_pct"] = round((cdata["net_profit_usdt"] / global_init) * 100.0, 2) if global_init > 0 else 0.0
                    
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
            
            if initial > 0:
                data["roi_pct"] = round(((bot_cur_balance - initial) / initial) * 100, 2)
            else:
                data["roi_pct"] = 0.0
                
            # --- Global Grid Load & Margin Load Calculations ---
            total_grid_cap = sum(float(c.get("grid_capacity_usdt", 0.0)) for c in data.get("per_coin", {}).values())
            total_grid_load = sum(float(c.get("current_grid_load_usdt", 0.0)) for c in data.get("per_coin", {}).values())
            total_margin_used = sum(float(c.get("current_margin_usdt", 0.0)) for c in data.get("per_coin", {}).values())

            data["total_grid_size_usdt"] = round(total_grid_cap, 2)
            data["current_grid_load_usdt"] = round(total_grid_load, 4)
            data["grid_load_pct"] = round((total_grid_load / total_grid_cap) * 100.0, 2) if total_grid_cap > 0 else 0.0
            data["max_grid_load_usdt"] = round(max(float(data.get("max_grid_load_usdt", 0.0)), total_grid_load), 4)
            data["max_grid_load_pct"] = round((data["max_grid_load_usdt"] / total_grid_cap) * 100.0, 2) if total_grid_cap > 0 else 0.0
            data["load_ratio"] = round(total_grid_load / total_grid_cap, 4) if total_grid_cap > 0 else 0.0

            # Margin load & free margin relative to floating account balance (bot_cur_balance)
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

            # Per-coin margin percentages relative to current floating balance
            for c in data.get("per_coin", {}).values():
                c_m = float(c.get("current_margin_usdt", 0.0))
                c["margin_load_pct"] = round((c_m / bot_cur_balance) * 100.0, 2) if bot_cur_balance > 0 else 0.0
                c_m_max = float(c.get("max_margin_usdt", 0.0))
                c["max_margin_load_pct"] = round((c_m_max / bot_cur_balance) * 100.0, 2) if bot_cur_balance > 0 else 0.0
                
            peak = float(data.get("peak_balance_usdt") if data.get("peak_balance_usdt") is not None else initial)
            trough = float(data.get("_current_trough_usdt") if data.get("_current_trough_usdt") is not None else initial)
            min_bal = float(data.get("min_balance_usdt") if data.get("min_balance_usdt") is not None else initial)
            
            # Active positions detection & Peak Guard
            can_update_peak = True
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
                
            max_drawdown = trough - peak
            data["max_drawdown_usdt"] = round(min(data.get("max_drawdown_usdt", 0.0), max_drawdown), 4)
            
            max_perf = peak - initial
            data["performance_usdt"] = round(max(data.get("performance_usdt", 0.0), max_perf), 4)

            # Telemetry: Peak Age & Net Velocity
            peak_ts_g = data.get("peak_net_ts", now_ms)
            data["peak_age_hours"] = round(max(0.0, (now_ms - peak_ts_g) / 3600000.0), 2)
            first_ts_g = data.get("first_trade_ts", now_ms)
            active_h_g = max(0.1, (now_ms - first_ts_g) / 3600000.0)
            net_profit_usdt = float(data.get("net_profit_usdt", 0.0))
            data["net_velocity"] = round(net_profit_usdt / active_h_g, 2)

            # Global Iteration Telemetry (учет конкретной итерации / цикла портфеля)
            iter_idx_g = int(data.get("iteration_index", 1))
            iter_start_net_g = float(data.get("iteration_start_net_profit_usdt", 0.0))
            iter_net_g = round(net_profit_usdt - iter_start_net_g, 4)
            iter_start_ts_g = data.get("iteration_start_ts", first_ts_g)

            iter_peak_net_g = float(data.get("iteration_peak_net_profit_usdt", iter_net_g))
            iter_peak_ts_g = data.get("iteration_peak_ts", iter_start_ts_g)
            if iter_net_g > iter_peak_net_g:
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
                
            # Обновление изолированной аналитики стратегий и лидерборда
            self._update_strategies_stats(data)

            return True
                    
        except Exception as e:
            logger.error(f"[ANALYTICS] Error updating drawdowns: {e}\n{traceback.format_exc()}")
            return False

    def _update_strategies_stats(self, global_data: dict):
        """Рассчитывает и сохраняет изолированную аналитику по стратегиям и лидерборд."""
        if not self.strategy_registry:
            try:
                from CORE.STRATEGY.strategy_registry import StrategyRegistry
                self.strategy_registry = StrategyRegistry()
            except Exception:
                return

        now_ms = int(time.time() * 1000)
        per_coin_global = global_data.get("per_coin", {})

        for strat_id, strat_cfg in self.strategy_registry.active_strategies.items():
            strat_file = self.stats_dir / f"analytics_{strat_id}.json"
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
                    "per_coin": {}
                }

            strat_symbols = list(strat_cfg.symbols)
            existing_per_coin = strat_data.get("per_coin", {})
            # Собираем список всех символов стратегии: текущие активные + исторические с историей торговли
            all_strat_symbols = set(strat_symbols)
            for sym, c_info in existing_per_coin.items():
                c_trades = int(c_info.get("total_trades", c_info.get("trades", 0)))
                c_pnl = abs(float(c_info.get("realized_pnl_net_usdt", c_info.get("realized_pnl_usdt", 0.0))))
                if c_trades > 0 or c_pnl > 1e-6:
                    all_strat_symbols.add(sym)

            # Сохраняем персистентный список historical_symbols стратегии
            saved_hist = strat_data.get("historical_symbols", [])
            for sym in saved_hist:
                if sym in per_coin_global or sym in existing_per_coin:
                    all_strat_symbols.add(sym)

            # Выбывшие монеты из глобальной аналитики должны сохраняться в отчетах как неактивные
            active_strats = self.strategy_registry.get_active_strategies()
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
            s_losses = max(0, s_trades - s_wins)

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

            if s_cur > s_peak:
                s_peak = s_cur
                s_trough = s_cur
                strat_data["peak_net_ts"] = now_ms
            elif "peak_net_ts" not in strat_data:
                strat_data["peak_net_ts"] = strat_data.get("first_trade_ts", now_ms)
                
            if s_cur < s_trough:
                s_trough = s_cur
            if s_cur < s_min:
                s_min = s_cur

            strat_data["peak_balance_usdt"] = round(s_peak, 4)
            strat_data["peak_net_profit_usdt"] = round(s_peak - s_init, 4)
            strat_data["_current_trough_usdt"] = round(s_trough, 4)
            strat_data["min_balance_usdt"] = round(s_min, 4)

            # Strategy Iteration Telemetry & Historical Records (учет конкретной итерации купола с персистентным кешем)
            iter_cache = self._get_iteration_cache(strat_id)
            hist_peak = float(iter_cache.get("historical_peak_net_profit_usdt", 0.0))
            hist_peak_ts = iter_cache.get("historical_peak_net_ts")
            hist_max_dd = float(iter_cache.get("historical_max_drawdown_usdt", 0.0))

            cur_peak_net = float(strat_data.get("peak_net_profit_usdt", 0.0))
            if hist_peak > cur_peak_net:
                strat_data["peak_net_profit_usdt"] = hist_peak
                s_peak = s_init + hist_peak
                strat_data["peak_balance_usdt"] = round(s_peak, 4)
                if hist_peak_ts:
                    strat_data["peak_net_ts"] = hist_peak_ts

            # Telemetry: Peak Age & Net Velocity
            peak_ts_s = strat_data.get("peak_net_ts", now_ms)
            strat_data["peak_age_hours"] = round(max(0.0, (now_ms - peak_ts_s) / 3600000.0), 2)
            first_ts_s = strat_data.get("first_trade_ts", now_ms)
            active_h_s = max(0.1, (now_ms - first_ts_s) / 3600000.0)
            strat_data["net_velocity"] = round(float(strat_data.get("net_profit_usdt", 0.0)) / active_h_s, 2)

            s_net = float(strat_data.get("net_profit_usdt", 0.0))
            iter_idx = int(strat_data.get("iteration_index") or iter_cache.get("iteration_index") or 1)
            
            if "iteration_start_net_profit_usdt" in strat_data:
                iter_start_net = float(strat_data["iteration_start_net_profit_usdt"])
            elif "iteration_start_net_profit_usdt" in iter_cache:
                iter_start_net = float(iter_cache["iteration_start_net_profit_usdt"])
            else:
                iter_start_net = 0.0

            iter_net = round(s_net - iter_start_net, 4)
            iter_start_ts = strat_data.get("iteration_start_ts") or iter_cache.get("iteration_start_ts") or first_ts_s

            iter_peak_net = float(strat_data.get("iteration_peak_net_profit_usdt") if strat_data.get("iteration_peak_net_profit_usdt") is not None else iter_cache.get("iteration_peak_net_profit_usdt", iter_net))
            iter_peak_ts = strat_data.get("iteration_peak_ts") or iter_cache.get("iteration_peak_ts") or iter_start_ts
            if iter_net > iter_peak_net:
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

            # Iteration Realized PnL (реализованный PnL конкретно в текущей итерации)
            if "iteration_start_realized_pnl_usdt" in strat_data:
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
            cur_max_dd = strat_data.get("max_drawdown_usdt", 0.0)
            if cur_max_dd < hist_max_dd:
                hist_max_dd = cur_max_dd

            # Сохраняем состояние итерации и исторические рекорды в кеш
            self._save_iteration_cache(strat_id, {
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
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            })

            s_dd = s_trough - s_peak
            calc_dd = min(float(strat_data.get("max_drawdown_usdt", 0.0)), s_dd)
            if hist_max_dd < 0:
                calc_dd = min(calc_dd, hist_max_dd)
            strat_data["max_drawdown_usdt"] = round(calc_dd, 4)

            if s_init > 0:
                strat_data["max_drawdown_pct"] = round(abs(strat_data["max_drawdown_usdt"]) / s_init * 100.0, 2)
            if strat_data["max_drawdown_usdt"] < 0:
                strat_data["recovery_factor"] = round(strat_data["net_profit_usdt"] / abs(strat_data["max_drawdown_usdt"]), 2)
            else:
                strat_data["recovery_factor"] = round(strat_data["net_profit_usdt"] / 1.0, 2) if strat_data["net_profit_usdt"] > 0 else 0.0

            # Расчет Trade Velocity для конкретного купола стратегии
            try:
                if not hasattr(self, "_strat_velocity_engines"):
                    self._strat_velocity_engines = {}
                if strat_id not in self._strat_velocity_engines:
                    from ANALYTICS.trade_velocity import TradeVelocityEngine
                    strat_ledger = self.ledgers_dir / f"trades_ledger_{strat_id}.txt"
                    self._strat_velocity_engines[strat_id] = TradeVelocityEngine(universe_id=strat_id, ledger_path=strat_ledger)
                strat_data["trade_velocity"] = self._strat_velocity_engines[strat_id].calculate_velocity(strat_data)
            except Exception as sve:
                logger.warning(f"[ANALYTICS] Error calculating trade velocity for {strat_id}: {sve}")

            Utils.atomic_write_json(strat_file, strat_data, indent=4)

        # Update Leaderboard & portfolio analytics_all.json
        try:
            universes_meta = {
                sid: {
                    **getattr(scfg, "raw_meta", {}),
                    "name": scfg.name,
                    "enabled": True,
                    "sizing_mode": getattr(scfg, "sizing_mode", "static"),
                    "auto_closing": getattr(scfg, "auto_closing_cfg", getattr(scfg, "raw_meta", {}).get("auto_closing", {}))
                }
                for sid, scfg in self.strategy_registry.get_active_strategies().items()
            }
            entries = self.leaderboard_engine.generate_leaderboard(universes_meta)
            evaluated_coins = len(self.strategy_registry.get_all_active_symbols())
            self.leaderboard_engine.save_portfolio(entries, evaluated_coins_count=evaluated_coins)
        except Exception as e:
            logger.error(f"[ANALYTICS] Error saving leaderboard portfolio: {e}")

    async def _get_bnb_usdt_rate(self, client, cached_prices: dict = None) -> float:
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
