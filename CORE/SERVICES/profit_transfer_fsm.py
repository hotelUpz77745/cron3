# ==============================================================================
# Path: CORE/SERVICES/profit_transfer_fsm.py
# Role: Конечный автомат (FSM) отложенного 1-кратного фонового перевода профита trailing_ratchet на Spot
# ==============================================================================

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ANALYTICS.iteration_manager import StrategyIterationManager
from consts import (
    ANALYTICS_STATS_DIR,
    CACHE_DIR,
    POST_CLOSE_SYNC_DEBOUNCE_SEC,
    QTY_ZERO_EPSILON,
)
from CORE.SERVICES.profit_transfer_service import ProfitTransferService
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("ProfitTransferFSM")

UNREALIZED_ZERO_EPSILON: float = 1e-4


class ProfitTransferState(str, Enum):
    """Состояния конечного автомата автоперевода профита с Futures на Spot."""

    IDLE = "IDLE"
    CLOSING_POSITIONS = "CLOSING_POSITIONS"
    AWAITING_SETTLED_ANALYTICS = "AWAITING_SETTLED_ANALYTICS"
    AWAITING_HEARTBEAT_START = "AWAITING_HEARTBEAT_START"
    DISPATCHED = "DISPATCHED"


@dataclass
class PendingProfitTransfer:
    """Контекст ожидаемого перевода профита по завершенной итерации купола."""

    strat_id: str
    close_type: str
    closed_iter_idx: int
    next_iter_idx: int
    prev_baseline_net: float
    ready_at_ts: float
    closed_at_ts: float = 0.0
    state: ProfitTransferState = ProfitTransferState.CLOSING_POSITIONS
    settled_net: Optional[float] = None
    settled_realized: Optional[float] = None
    actual_iter_profit: Optional[float] = None
    last_forced_sync_try_ts: float = 0.0
    is_global: bool = False
    stats_path: Optional[Path] = None
    strat_cfg: Optional[Any] = None


class ProfitTransferFSM:
    """
    Изолированная надстройка над AutoCloser: конечный автомат фонового перевода профита на Spot.
    Не вмешивается в логику храповика trailing_ratchet и не запрашивает физический баланс биржи (fetch_balance).

    Строгий порядок срабатывания перевода:
      1. Подписка исключительно на событие срабатывания trailing_ratchet (CLOSING_POSITIONS).
      2. Ожидание полного закрытия всех позиций купола и перехода аналитики на новую итерацию.
      3. Ожидание подтвержденной биржевой реконструкции (0 позиций, |unrealized| == 0, отсутствие _sync_locks,
         завершенный deep_sync_analytics после closed_at_ts) и уточнение базы итерации с учетом проскальзываний.
      4. Ожидание наступления нового цикла сердцебиения сигнала (now >= ready_at_ts).
      5. Фоновый строго 1-кратный SAPI-перевод при выполнении 2 условий:
         - фактическая прибыльность итерации >= min_profit_guard;
         - расчетный баланс маржи купола строго выше стартового (net_profit_usdt - transferred_to_spot_usdt > 0).
    """

    def __init__(
        self,
        stats_dir: Path = ANALYTICS_STATS_DIR,
        cache_dir: Path = CACHE_DIR,
        sync_debounce_sec: float = POST_CLOSE_SYNC_DEBOUNCE_SEC,
    ):
        self.stats_dir = Path(stats_dir)
        self.cache_dir = Path(cache_dir)
        self.sync_debounce_sec = float(sync_debounce_sec)
        self._pending: Dict[str, PendingProfitTransfer] = {}
        self._claimed_keys: Set[Tuple[str, int]] = set()
        self._transferred_totals: Dict[str, float] = {}
        self._dispatch_lock = asyncio.Lock()
        self._bg_tasks: Set[asyncio.Task] = set()

    @classmethod
    def is_eligible_close_type(cls, close_type: str) -> bool:
        """Profit transfer applies to any positive autoclose mode except _off and emergency/manual stops."""
        return ProfitTransferService.is_eligible_close_type(close_type)

    def get_state(self, strat_id: str) -> ProfitTransferState:
        job = self._pending.get(strat_id)
        return job.state if job else ProfitTransferState.IDLE

    def get_job(self, strat_id: str) -> Optional[PendingProfitTransfer]:
        return self._pending.get(strat_id)

    def is_claimed(self, strat_id: str, iter_idx: int) -> bool:
        return (strat_id, int(iter_idx)) in self._claimed_keys

    def arm_on_ratchet_trigger(
        self,
        strat_id: str,
        strat_cfg: Optional[Any],
        close_type: str,
        closed_iter_idx: int,
        next_iter_idx: int,
        prev_baseline_net: float,
        ready_at_ts: float = 0.0,
        is_global: bool = False,
        stats_path: Optional[Path] = None,
    ) -> bool:
        """Регистрирует задачу автоперевода строго в момент срабатывания храповика."""
        if is_global or not self.is_eligible_close_type(close_type):
            return False

        transfer_cfg = ProfitTransferService.extract_transfer_config(strat_cfg)
        if not transfer_cfg.get("enabled", False):
            return False

        key = (strat_id, int(closed_iter_idx))
        if key in self._claimed_keys or strat_id in self._pending:
            logger.warning(
                f"[PROFIT_TRANSFER_FSM][{strat_id}] Задача для итерации #{closed_iter_idx} уже зарегистрирована или выполнена."
            )
            return False

        self._pending[strat_id] = PendingProfitTransfer(
            strat_id=strat_id,
            close_type=close_type,
            closed_iter_idx=int(closed_iter_idx),
            next_iter_idx=int(next_iter_idx),
            prev_baseline_net=float(prev_baseline_net),
            ready_at_ts=float(ready_at_ts or 0.0),
            state=ProfitTransferState.CLOSING_POSITIONS,
            is_global=is_global,
            stats_path=stats_path,
            strat_cfg=strat_cfg,
        )
        return True

    def mark_positions_closed(
        self,
        strat_id: str,
        now_ts: Optional[float] = None,
        ready_at_ts: Optional[float] = None,
    ) -> bool:
        """Переводит автомат в ожидание подтвержденной биржевой аналитики после закрытия всех позиций и перехода итерации."""
        job = self._pending.get(strat_id)
        if not job or job.state != ProfitTransferState.CLOSING_POSITIONS:
            return False
        job.closed_at_ts = float(now_ts if now_ts is not None else time.time())
        if isinstance(ready_at_ts, (int, float)) and float(ready_at_ts) > job.ready_at_ts:
            job.ready_at_ts = float(ready_at_ts)
        job.state = ProfitTransferState.AWAITING_SETTLED_ANALYTICS
        return True

    def cancel_job(self, strat_id: str, reason: str = "") -> None:
        if strat_id in self._pending:
            logger.info(f"[PROFIT_TRANSFER_FSM][{strat_id}] Сброс ожидающей задачи ({reason}).")
            self._pending.pop(strat_id, None)

    @staticmethod
    def _positions_confirmed_zero(strat_analytics: dict, strat_symbols: List[str]) -> bool:
        per_coin = strat_analytics.get("per_coin", {})
        if not isinstance(per_coin, dict):
            return False
        symbols_to_check = set(strat_symbols) | set(per_coin.keys())
        for sym in symbols_to_check:
            c = per_coin.get(sym, {})
            if not isinstance(c, dict):
                continue
            if abs(float(c.get("long_amt", 0.0) or 0.0)) > QTY_ZERO_EPSILON:
                return False
            if abs(float(c.get("short_amt", 0.0) or 0.0)) > QTY_ZERO_EPSILON:
                return False
        return abs(float(strat_analytics.get("unrealized_pnl_usdt", 0.0) or 0.0)) < UNREALIZED_ZERO_EPSILON

    def _get_transferred_total(self, strat_id: str, sdata: Optional[dict] = None) -> float:
        mem_val = float(self._transferred_totals.get(strat_id, 0.0) or 0.0)
        stat_val = float((sdata or {}).get("transferred_to_spot_usdt", 0.0) or 0.0)
        cache_entry = StrategyIterationManager.get_iteration_cache(self.cache_dir, strat_id) or {}
        cache_val = float(cache_entry.get("transferred_to_spot_usdt", 0.0) or 0.0)
        total = round(max(mem_val, stat_val, cache_val), 4)
        self._transferred_totals[strat_id] = total
        return total

    def _persist_transferred_total(self, bot_core: Any, job: PendingProfitTransfer, new_total: float) -> None:
        rounded_total = round(float(new_total), 4)
        self._transferred_totals[job.strat_id] = rounded_total

        stats_path = job.stats_path or (self.stats_dir / f"analytics_{job.strat_id}.json")
        if stats_path.exists():
            try:
                sdata = Utils.read_json_file(stats_path) or {}
                sdata["transferred_to_spot_usdt"] = rounded_total
                Utils.atomic_write_json(stats_path, sdata, indent=4)
            except Exception as exc:
                logger.warning(f"[PROFIT_TRANSFER_FSM][{job.strat_id}] Не удалось обновить analytics json: {exc}")

        cache_entry = StrategyIterationManager.get_iteration_cache(self.cache_dir, job.strat_id) or {}
        cache_entry["transferred_to_spot_usdt"] = rounded_total
        StrategyIterationManager.save_iteration_cache(self.cache_dir, job.strat_id, cache_entry)

        fsm = getattr(getattr(bot_core, "analytics", None), "fsm", None)
        if fsm and hasattr(fsm, "_iteration_state") and isinstance(fsm._iteration_state, dict):
            fsm._iteration_state.setdefault(job.strat_id, {})["transferred_to_spot_usdt"] = rounded_total

    def _reanchor_settled_iteration(self, bot_core: Any, job: PendingProfitTransfer, sdata: dict) -> None:
        """
        Уточняет базовую точку новой итерации после фактического закрытия на бирже,
        исключая расхождение между до-закрытым плавающим PnL и реальными сделками (проскальзывания/комиссии).
        """
        settled_net = round(float(sdata.get("net_profit_usdt", 0.0) or 0.0), 4)
        settled_real = round(
            float(sdata.get("realized_pnl_net_usdt", sdata.get("realized_pnl_usdt", settled_net)) or 0.0),
            4,
        )
        settled_iter_profit = round(settled_net - job.prev_baseline_net, 4)

        stats_path = job.stats_path or (self.stats_dir / f"analytics_{job.strat_id}.json")
        sdata["iteration_index"] = job.next_iter_idx
        sdata["iteration_start_net_profit_usdt"] = settled_net
        sdata["iteration_net_profit_usdt"] = 0.0
        if not job.is_global:
            sdata["iteration_start_realized_pnl_usdt"] = settled_real
            sdata["iteration_realized_pnl_usdt"] = 0.0
        Utils.atomic_write_json(stats_path, sdata, indent=4)

        cache_entry = StrategyIterationManager.get_iteration_cache(self.cache_dir, job.strat_id) or {}
        if int(cache_entry.get("iteration_index", 1) or 1) <= job.next_iter_idx:
            cache_entry["iteration_index"] = job.next_iter_idx
            cache_entry["iteration_start_net_profit_usdt"] = settled_net
            if not job.is_global:
                cache_entry["iteration_start_realized_pnl_usdt"] = settled_real
                cache_entry["iteration_realized_pnl_usdt"] = 0.0
            StrategyIterationManager.save_iteration_cache(self.cache_dir, job.strat_id, cache_entry)

        fsm = getattr(getattr(bot_core, "analytics", None), "fsm", None)
        if fsm is not None:
            iter_state = getattr(fsm, "_iteration_state", None)
            if isinstance(iter_state, dict):
                fsm_entry = iter_state.setdefault(job.strat_id, {})
                fsm_entry["iteration_index"] = job.next_iter_idx
                fsm_entry["iteration_start_net_profit_usdt"] = settled_net
                fsm_entry["iteration_start_realized_pnl_usdt"] = settled_real
            history = getattr(fsm, "_auto_close_history", None)
            if isinstance(history, dict) and job.strat_id in history and isinstance(history[job.strat_id], list):
                updated_hist = False
                for rec in reversed(history[job.strat_id]):
                    if isinstance(rec, dict) and rec.get("iteration_index") == job.closed_iter_idx:
                        rec["strat_net"] = settled_net
                        rec["settled_realized"] = settled_real
                        rec["iter_net"] = settled_iter_profit
                        updated_hist = True
                        break
                if updated_hist and hasattr(fsm, "history_file"):
                    try:
                        Utils.atomic_write_json(fsm.history_file, history, indent=4)
                    except Exception as exc:
                        logger.debug(f"[PROFIT_TRANSFER_FSM][{job.strat_id}] Could not update history file: {exc}")

    @staticmethod
    def _resolve_min_profit_guard(strat_cfg: Optional[Any], transfer_cfg: Dict[str, Any]) -> float:
        min_transfer = float(transfer_cfg.get("min_transfer_usdt", 1.0) or 1.0)
        ac_cfg = getattr(strat_cfg, "auto_closing_cfg", None) if strat_cfg is not None else None
        if not isinstance(ac_cfg, dict) and isinstance(strat_cfg, dict):
            ac_cfg = strat_cfg.get("auto_closing", strat_cfg)
        pos_cfg = ac_cfg.get("positive", {}) if isinstance(ac_cfg, dict) else {}
        min_guar = float(pos_cfg.get("min_guaranteed_profit_usdt", 0.0) or 0.0) if isinstance(pos_cfg, dict) else 0.0
        return max(min_transfer, min_guar)

    async def process_tick(self, bot_core: Any, active_strats: Optional[Dict[str, Any]] = None) -> None:
        """
        Выполняет шаг конечного автомата для всех ожидающих стратегий.
        Потокобезопасен и строго идемпотентен при частых вызовах из главного цикла.
        """
        if not self._pending:
            return

        active_map = active_strats or {}
        analytics = getattr(bot_core, "analytics", None)
        afsm = getattr(analytics, "fsm", None) if analytics is not None else None
        now = Utils.get_current_time(getattr(bot_core, "current_sim_time", None))

        for strat_id in list(self._pending.keys()):
            job = self._pending.get(strat_id)
            if not job or job.state == ProfitTransferState.IDLE:
                continue

            strat_cfg = active_map.get(strat_id) or job.strat_cfg
            strat_symbols = [str(s).upper() for s in (getattr(strat_cfg, "symbols", None) or [])]

            if job.state == ProfitTransferState.AWAITING_SETTLED_ANALYTICS:
                if analytics is None or afsm is None or not afsm.is_autocloser_ready():
                    continue

                sync_locks = getattr(analytics, "_sync_locks", set())
                if isinstance(sync_locks, (set, list, tuple)):
                    locked_syms = {str(item[0]).upper() for item in sync_locks if isinstance(item, (tuple, list)) and item}
                    if strat_symbols and (locked_syms & set(strat_symbols)):
                        continue
                    elif not strat_symbols and locked_syms:
                        continue

                sync_in_prog = getattr(analytics, "_sync_in_progress", None)
                if sync_in_prog is not None and callable(getattr(sync_in_prog, "locked", None)) and sync_in_prog.locked():
                    continue

                strat_client = (
                    bot_core.get_client_for_strategy(strat_id)
                    if hasattr(bot_core, "get_client_for_strategy")
                    else getattr(bot_core, "client", None)
                )
                ts_by_client = getattr(analytics, "last_deep_sync_ts_by_client", None)
                if isinstance(ts_by_client, dict) and ts_by_client and strat_client is not None:
                    last_deep_ts = float(ts_by_client.get(id(strat_client), 0.0) or 0.0)
                else:
                    last_deep_ts = float(getattr(analytics, "last_deep_sync_ts", 0.0) or 0.0)

                if last_deep_ts < job.closed_at_ts:
                    if (
                        (now - job.closed_at_ts) >= self.sync_debounce_sec
                        and (now - job.last_forced_sync_try_ts) >= self.sync_debounce_sec
                        and hasattr(analytics, "deep_sync_analytics")
                    ):
                        job.last_forced_sync_try_ts = now
                        try:
                            await analytics.deep_sync_analytics(strat_client, getattr(bot_core, "prices", None))
                        except Exception as exc:
                            logger.warning(f"[PROFIT_TRANSFER_FSM][{strat_id}] Fallback deep_sync error: {exc}")
                    continue

                stats_path = job.stats_path or (self.stats_dir / f"analytics_{strat_id}.json")
                sdata = Utils.read_json_file(stats_path) if stats_path.exists() else None
                if not isinstance(sdata, dict) or sdata.get("_floating_synced") is False or sdata.get("net_profit_usdt") is None:
                    continue
                if int(sdata.get("iteration_index", 1) or 1) < job.next_iter_idx:
                    continue
                if not self._positions_confirmed_zero(sdata, strat_symbols):
                    continue

                settled_net = round(float(sdata.get("net_profit_usdt", 0.0) or 0.0), 4)
                settled_real = round(float(sdata.get("realized_pnl_net_usdt", settled_net) or 0.0), 4)
                actual_iter_profit = round(settled_net - job.prev_baseline_net, 4)

                self._reanchor_settled_iteration(bot_core, job, sdata)
                job.settled_net = settled_net
                job.settled_realized = settled_real
                job.actual_iter_profit = actual_iter_profit
                job.state = ProfitTransferState.AWAITING_HEARTBEAT_START

            if job.state == ProfitTransferState.AWAITING_HEARTBEAT_START:
                if now < job.ready_at_ts:
                    continue

                async with self._dispatch_lock:
                    current_job = self._pending.get(strat_id)
                    claim_key = (strat_id, job.closed_iter_idx)
                    if not current_job or current_job.state != ProfitTransferState.AWAITING_HEARTBEAT_START:
                        continue
                    if claim_key in self._claimed_keys:
                        self._pending.pop(strat_id, None)
                        continue

                    self._claimed_keys.add(claim_key)
                    current_job.state = ProfitTransferState.DISPATCHED
                    self._pending.pop(strat_id, None)

                    task = asyncio.create_task(
                        self._run_background_transfer(bot_core, current_job, strat_cfg),
                        name=f"profit_transfer_{strat_id}_{job.closed_iter_idx}",
                    )
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)

    async def _run_background_transfer(
        self,
        bot_core: Any,
        job: PendingProfitTransfer,
        strat_cfg: Optional[Any],
    ) -> Optional[Dict[str, Any]]:
        """Однократный фоновый перевод профита на Spot после подтверждения обоих условий (без fetch_balance)."""
        cfg_source = strat_cfg if strat_cfg is not None else getattr(bot_core, "auto_closing_cfg", None)
        transfer_cfg = ProfitTransferService.extract_transfer_config(cfg_source)
        if not transfer_cfg.get("enabled", False):
            return None

        stats_path = job.stats_path or (self.stats_dir / f"analytics_{job.strat_id}.json")
        sdata = Utils.read_json_file(stats_path) if stats_path.exists() else {}
        transferred_total = self._get_transferred_total(job.strat_id, sdata)
        actual_profit = float(job.actual_iter_profit or 0.0)
        settled_net = float(job.settled_net or 0.0)
        min_guard = self._resolve_min_profit_guard(strat_cfg, transfer_cfg)

        res = await ProfitTransferService.execute_on_ratchet_close(
            bot_core=bot_core,
            strat_id=job.strat_id,
            strat_cfg=strat_cfg,
            iter_net_usdt=actual_profit,
            close_type=job.close_type,
            net_profit_usdt=settled_net,
            transferred_to_spot_usdt=transferred_total,
            min_profit_guard_usdt=min_guard,
        )

        if res and res.get("executed"):
            actual_amt = float(res.get("amount_usdt", 0.0) or 0.0)
            if actual_amt > 0.0:
                self._persist_transferred_total(bot_core, job, transferred_total + actual_amt)

        if res and res.get("alert_line") and getattr(bot_core, "notifier", None):
            try:
                await bot_core.notifier.send_alert(res["alert_line"])
            except Exception as exc:
                logger.warning(f"[PROFIT_TRANSFER_FSM][{job.strat_id}] Не удалось отправить TG-уведомление: {exc}")

        return res

    async def wait_for_pending_tasks(self) -> List[Any]:
        """Ожидает завершения всех активных фоновых задач перевода (для тестов и graceful shutdown)."""
        if not self._bg_tasks:
            return []
        tasks = list(self._bg_tasks)
        return list(await asyncio.gather(*tasks, return_exceptions=True))
