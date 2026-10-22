# ANALYTICS/analytics.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.analytics
ROLE: Main Analytics Orchestrator and Facade Manager
==============================================================================
"""

import asyncio
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from consts import (
    ANALYTICS_DIR,
    ANALYTICS_LEDGERS_DIR,
    ANALYTICS_START_BALANCE_USDT,
    ANALYTICS_STATS_DIR,
    CACHE_DIR,
    DATA_DIR,
)
from c_log import UnifiedLogger
from c_utils import Utils

from ANALYTICS.analytics_fsm import AnalyticsFSM, AnalyticsState
from ANALYTICS.circuit_breaker import CircuitBreaker
from ANALYTICS.drawdown_tracker import DrawdownTracker
from ANALYTICS.leaderboard import LeaderboardEngine
from ANALYTICS.ledger_recorder import LedgerRecorder
from ANALYTICS.metrics import AnalyticsMathEngine
from ANALYTICS.reconstruction import ReconstructionService
from ANALYTICS.strategy_stats import StrategyStatsEngine

logger = UnifiedLogger("Analytics")


class AnalyticsManager:
    """
    Main Analytics Orchestrator facade.
    Coordinates trades ledger recording, drawdown tracking, strategy analytics,
    reconstruction resync, and leaderboards.
    """

    def __init__(self, strategy_registry=None):
        self.strategy_registry = strategy_registry
        self.stats_dir = ANALYTICS_STATS_DIR
        self.ledgers_dir = ANALYTICS_LEDGERS_DIR
        self.log_file = ANALYTICS_STATS_DIR / "analytics.json"
        self.txt_file = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
        self.peels_ledger = ANALYTICS_LEDGERS_DIR / "peels_ledger.txt"
        self.recycling_ledger = ANALYTICS_LEDGERS_DIR / "recycling_ledger.txt"

        self.leaderboard_engine = LeaderboardEngine(
            analytics_dir=ANALYTICS_DIR, stats_dir=ANALYTICS_STATS_DIR
        )

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
        self._symbol_fsm_positions = {}
        self._strat_velocity_engines = {}

        # 4-State Analytics State Machine & Iteration Coordinator
        self.fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=CACHE_DIR,
            log_file=self.log_file
        )

        # Sub-engines decomposition
        self.circuit_breaker = CircuitBreaker.get_default()
        self.ledger_recorder = LedgerRecorder(self)
        self.drawdown_tracker = DrawdownTracker(self, circuit_breaker=self.circuit_breaker)
        self.strategy_stats = StrategyStatsEngine(self)
        self.reconstruction = ReconstructionService(self)

        self._floating_synced = False
        self._persisted_global_cache: dict = {}
        self._persisted_strat_cache: dict = {}

        self._ensure_files()
        self._restore_persisted_state()

        # Sync start balance from config upon restart
        data = self._read_data()
        if data:
            stored_start = float(data.get("start_balance_usdt", 0.0))
            config_start = float(ANALYTICS_START_BALANCE_USDT)
            if config_start > 0 and abs(stored_start - config_start) > 0.0001:
                logger.info(f"Syncing analytics start balance from config: {stored_start} -> {config_start}")
                self.rebase_start_balance(config_start)

    def _restore_persisted_state(self):
        """
        Ресторатор аналитики при рестарте системы (Состояние 1: RESTORED_FROZEN):
        Восстанавливает последнее замороженное состояние из JSON-файлов на диске и CACHE/iteration_state.json.
        Если кэш отсутствует, инициализирует с нуля от стартового баланса.
        Любой пересчет метрик блокируется (_floating_synced = False) до тех пор,
        пока от Binance REST API не получен первый подтвержденный ответ по открытым позициям.
        """
        self._floating_synced = False
        init_start = float(ANALYTICS_START_BALANCE_USDT)
        self.fsm.restore_on_boot(initial_start_balance=init_start)
        self._persisted_global_cache = dict(self.fsm._persisted_global_cache)
        self._persisted_strat_cache = dict(self.fsm._persisted_strat_cache)

    def _ensure_files(self):
        """Ensures directories and baseline JSON / ledger files exist."""
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)

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
                "per_coin": {},
            }
            self.log_file.write_text(json.dumps(default_data, indent=4), encoding="utf-8")

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

        if not self.peels_ledger.exists():
            with open(self.peels_ledger, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow([
                    "Id", "Timestamp (UTC)", "Strategy", "Symbol", "Side", "Level",
                    "Fill Price", "Peel Price", "Rebound %", "Peel Qty",
                    "Peel Vol (USDT)", "Micro PnL (USDT)", "Remaining Pos Qty"
                ])

        if not self.recycling_ledger.exists():
            with open(self.recycling_ledger, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow([
                    "Id", "Timestamp (UTC)", "Strategy", "Symbol", "Winning Side",
                    "Realized Profit (USDT)", "Losing Side", "Losing Level", "Losing DD %",
                    "Burn Qty", "Burn Vol (USDT)", "Slice Loss (USDT)", "Burn Budget (USDT)", "Remaining Pos Qty"
                ])

        self._ensure_strategy_files()

    def _ensure_strategy_files(self):
        """Гарантирует наличие изолированных файлов аналитики и леджеров по каждой стратегии."""
        if not self.strategy_registry:
            try:
                from CORE.strategy_registry import StrategyRegistry
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
                    "per_coin": {},
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
            reconstructed = False
            if (not strat_txt.exists() or strat_txt.stat().st_size <= 60) and self.txt_file.exists() and self.txt_file.stat().st_size > 60:
                try:
                    strat_symbols = set(getattr(strat_cfg, "symbols", []))
                    if strat_symbols:
                        with open(self.txt_file, mode="r", encoding="utf-8") as gf:
                            g_reader = csv.reader(gf, delimiter=';')
                            next(g_reader, None)
                            g_rows = list(g_reader)

                        s_rows = []
                        s_bal = strat_init_bal
                        s_cnt = 1
                        for r in g_rows:
                            if not r or len(r) < 6:
                                continue
                            sym = r[1]
                            if sym in strat_symbols:
                                try:
                                    pnl = float(r[5])
                                except (ValueError, TypeError):
                                    pnl = 0.0
                                s_bal += pnl
                                s_rows.append([s_cnt, sym, r[2], r[3], r[4], round(pnl, 4), round(s_bal, 4)])
                                s_cnt += 1

                        if s_rows:
                            temp_s = strat_txt.with_suffix('.tmp')
                            with open(temp_s, mode="w", newline="", encoding="utf-8") as sf:
                                s_writer = csv.writer(sf, delimiter=';')
                                s_writer.writerow(["Id", "Symbol", "Side", "Open Time", "Close Time", "PnL (USDT)", "Balance"])
                                s_writer.writerows(s_rows)
                            os.replace(temp_s, strat_txt)
                            reconstructed = True
                except Exception as e:
                    logger.debug(f"Failed to backfill {strat_id} ledger from main ledger: {e}")

            if not strat_txt.exists() and not reconstructed:
                with open(strat_txt, mode="w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, delimiter=';')
                    writer.writerow(["Id", "Symbol", "Side", "Open Time", "Close Time", "PnL (USDT)", "Balance"])

    def _read_data(self) -> dict:
        """Reads global analytics JSON file."""
        if not self.log_file.exists():
            self._ensure_files()

        try:
            data = json.loads(self.log_file.read_text(encoding="utf-8"))
            if not getattr(self, "_floating_synced", False):
                data["_floating_synced"] = False
            return data
        except Exception as e:
            logger.error(f"Error reading analytics file: {e}")
            return dict(getattr(self, "_persisted_global_cache", {}))

    def _write_data(self, data: dict, mark_backup: bool = True):
        """Writes global analytics JSON file atomically and recalculates derived metrics."""
        try:
            AnalyticsMathEngine.calculate(data)
            if getattr(self, 'velocity_engine', None):
                try:
                    data["trade_velocity"] = self.velocity_engine.calculate_velocity(data)
                except Exception as ve:
                    logger.error(f"Error calculating trade velocity: {ve}")
            json_str = json.dumps(data, indent=4, ensure_ascii=False)
            Utils.atomic_write_text(self.log_file, json_str)

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

    # ============================================================
    # DELEGATED: Iteration Cache & Strategy Stats
    # ============================================================

    def _get_iteration_cache(self, strat_id: str) -> dict:
        return self.strategy_stats.get_iteration_cache(strat_id)

    def _save_iteration_cache(self, strat_id: str, state: dict):
        return self.strategy_stats.save_iteration_cache(strat_id, state)

    def _update_strategies_stats(self, global_data: dict):
        return self.strategy_stats.update_strategies_stats(global_data)

    def reset_strategy_iteration(self, strategy_id: str) -> dict:
        return self.strategy_stats.reset_strategy_iteration(strategy_id)

    def reset_global_iteration(self) -> dict:
        return self.strategy_stats.reset_global_iteration()

    # ============================================================
    # DELEGATED: Ledgers & Records
    # ============================================================

    async def _append_to_csv(
        self,
        symbol: str,
        side: str,
        open_time: int,
        close_time: int,
        pnl: float,
        balance: float,
    ):
        await self.ledger_recorder.append_to_csv(symbol, side, open_time, close_time, pnl, balance)

    def record_finished_position(
        self, client, symbol: str, side: str, open_time: int, close_time: int
    ):
        self.ledger_recorder.record_finished_position(client, symbol, side, open_time, close_time)

    def record_subgrid_peel(
        self,
        strategy_id: str,
        symbol: str,
        side: str,
        level: str,
        fill_price: float,
        peel_price: float,
        rebound_pct: float,
        peel_qty: float,
        rem_pos_qty: float,
    ):
        self.ledger_recorder.record_subgrid_peel(
            strategy_id,
            symbol,
            side,
            level,
            fill_price,
            peel_price,
            rebound_pct,
            peel_qty,
            rem_pos_qty,
        )

    def record_cross_recycling(
        self,
        strategy_id: str,
        symbol: str,
        winning_side: str,
        realized_profit: float,
        losing_side: str,
        losing_level: int,
        losing_dd_pct: float,
        burn_qty: float,
        current_price: float,
        actual_burn_loss: float,
        burn_budget: float,
        rem_pos_qty: float,
    ):
        self.ledger_recorder.record_cross_recycling(
            strategy_id,
            symbol,
            winning_side,
            realized_profit,
            losing_side,
            losing_level,
            losing_dd_pct,
            burn_qty,
            current_price,
            actual_burn_loss,
            burn_budget,
            rem_pos_qty,
        )

    async def _do_fetch_and_record(
        self, client, symbol: str, side: str, open_time: int, close_time: int
    ):
        await self.ledger_recorder._do_fetch_and_record(client, symbol, side, open_time, close_time)

    async def _fetch_and_record(
        self, client, symbol: str, side: str, open_time: int, close_time: int
    ):
        await self.ledger_recorder._fetch_and_record(client, symbol, side, open_time, close_time)

    # ============================================================
    # DELEGATED: Drawdown Tracker & Realtime Poller
    # ============================================================

    async def _update_drawdowns(self, client, data: dict, is_lightweight: bool = False):
        return await self.drawdown_tracker.update_drawdowns(client, data, is_lightweight)

    async def _get_bnb_usdt_rate(self, client, cached_prices: Optional[dict] = None) -> float:
        return await self.drawdown_tracker.get_bnb_usdt_rate(client, cached_prices)

    async def sync_lightweight_unrealized_pnl(self, client):
        return await self.drawdown_tracker.sync_lightweight_unrealized_pnl(client)

    def start_realtime_tracker(self, client):
        self.drawdown_tracker.start_realtime_tracker(client)

    def stop_realtime_tracker(self):
        self.drawdown_tracker.stop_realtime_tracker()

    async def _realtime_tracker_loop(self, client):
        await self.drawdown_tracker._realtime_tracker_loop(client)

    # ============================================================
    # DELEGATED: Absolute Deep Sync Reconstruction
    # ============================================================

    async def deep_sync_analytics(self, client, cached_prices: Optional[dict] = None):
        await self.reconstruction.deep_sync_analytics(client, cached_prices)

    # ============================================================
    # STATE MANAGEMENT & REBASE
    # ============================================================

    async def reset_analytics_state(self):
        """Полный сброс аналитики (удаление файлов) с соблюдением блокировок."""
        async with self._lock:
            async with self._csv_lock:
                targets = [
                    self.log_file,
                    self.txt_file,
                    self.peels_ledger,
                    self.recycling_ledger,
                ]
                for f in targets:
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
                    for f in self.ledgers_dir.glob("*.txt"):
                        try:
                            f.unlink()
                        except Exception:
                            pass

    def rebase_start_balance(self, new_balance: float) -> dict:
        """
        Корректно обновляет стартовый баланс без создания ложной просадки.
        Сдвигает пики, впадины и текущий баланс на delta.
        Если сделок еще не было (total_trades == 0), жестко сбрасывает net_profit в 0.0
        и устанавливает cur_balance равным new_balance.
        """
        data = self._read_data()
        old_balance = float(data.get("start_balance_usdt", 0.0))
        delta = round(new_balance - old_balance, 4)

        data["start_balance_usdt"] = round(new_balance, 4)
        data["_rebased_start_balance_usdt"] = round(new_balance, 4)

        if int(data.get("total_trades", 0)) == 0:
            data["net_profit_usdt"] = 0.0
            data["realized_pnl_usdt"] = 0.0
            data["unrealized_pnl_usdt"] = 0.0
            data["cur_balance_usdt"] = round(new_balance, 4)
            data["peak_balance_usdt"] = round(new_balance, 4)
            data["_current_trough_usdt"] = round(new_balance, 4)
            data["min_balance_usdt"] = round(new_balance, 4)
        else:
            data["cur_balance_usdt"] = round(float(data.get("cur_balance_usdt", old_balance)) + delta, 4)
            peak_val = data.get("peak_balance_usdt")
            data["peak_balance_usdt"] = round((float(peak_val) if peak_val is not None else old_balance) + delta, 4)
            trough_val = data.get("_current_trough_usdt")
            data["_current_trough_usdt"] = round((float(trough_val) if trough_val is not None else old_balance) + delta, 4)
            min_val = data.get("min_balance_usdt")
            data["min_balance_usdt"] = round((float(min_val) if min_val is not None else old_balance) + delta, 4)

        self._write_data(data)
        return data

    async def set_initial_balance(self, new_balance: float):
        """Установка начального баланса с корректным перерасчетом пиков (rebase)."""
        async with self._lock:
            self.rebase_start_balance(new_balance)
