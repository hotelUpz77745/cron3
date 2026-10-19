# ANALYTICS/ledger_recorder.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: ANALYTICS.ledger_recorder
ROLE: Record keeping for trades, subgrid peeling, and cross recycling ledgers
==============================================================================
"""

import asyncio
import csv
import time
from datetime import datetime, timezone
from typing import Optional

from consts import (
    ANALYTICS_DIR,
    ANALYTICS_LEDGERS_DIR,
    POST_CLOSE_SYNC_DEBOUNCE_SEC,
)
from c_log import UnifiedLogger

logger = UnifiedLogger("LedgerRecorder")


class LedgerRecorder:
    """Manages appending trades, subgrid peels, and cross-side recycling events to ledgers."""

    def __init__(self, manager):
        self.manager = manager

    @property
    def ledgers_dir(self):
        return getattr(self.manager, "ledgers_dir", ANALYTICS_LEDGERS_DIR)

    @property
    def txt_file(self):
        return getattr(self.manager, "txt_file", self.ledgers_dir / "trades_ledger.txt")

    @property
    def peels_ledger(self):
        return getattr(self.manager, "peels_ledger", self.ledgers_dir / "peels_ledger.txt")

    @property
    def recycling_ledger(self):
        return getattr(self.manager, "recycling_ledger", self.ledgers_dir / "recycling_ledger.txt")

    async def append_to_csv(
        self,
        symbol: str,
        side: str,
        open_time: int,
        close_time: int,
        pnl: float,
        balance: float,
    ):
        """Appends a closed trade to the global trades ledger and strategy ledger."""
        async with self.manager._csv_lock:
            try:
                def ts_to_str(ts_ms):
                    if not ts_ms:
                        return "Unknown"
                    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

                open_str = ts_to_str(open_time)
                close_str = ts_to_str(close_time)

                # Запись в леджер (с зеркалированием только если в боевой директории)
                targets = [self.txt_file]
                if self.ledgers_dir == ANALYTICS_LEDGERS_DIR:
                    root_txt = ANALYTICS_DIR / "trades_ledger.txt"
                    if root_txt != self.txt_file:
                        targets.append(root_txt)

                for target_txt in targets:
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
                if getattr(self.manager, "strategy_registry", None):
                    strat_id = self.manager.strategy_registry.get_strategy_id_for_symbol(symbol)
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

                if getattr(self.manager, "velocity_engine", None):
                    try:
                        self.manager.velocity_engine.on_trade_closed(symbol, close_time)
                    except Exception as ve:
                        logger.error(f"Error updating velocity engine on trade closed: {ve}")

            except Exception as e:
                logger.error(f"Error appending to CSV: {e}")

    def record_finished_position(
        self, client, symbol: str, side: str, open_time: int, close_time: int
    ):
        """Запускает фоновую задачу для подтягивания PnL и записи в лог."""
        self.manager._sync_locks.add((symbol, side))
        task = asyncio.create_task(
            self._fetch_and_record(client, symbol, side, open_time, close_time)
        )
        self.manager._background_tasks.add(task)
        task.add_done_callback(self.manager._background_tasks.discard)

    async def _do_fetch_and_record(
        self, client, symbol: str, side: str, open_time: int, close_time: int
    ):
        """Waits after a trade closes, then triggers Absolute Deep Sync to reconstruct analytics."""
        logger.info(
            f"[{symbol}] Trade closed. Waiting {POST_CLOSE_SYNC_DEBOUNCE_SEC}s before Absolute Deep Sync..."
        )
        if open_time:
            pass
        await asyncio.sleep(POST_CLOSE_SYNC_DEBOUNCE_SEC)
        await self.manager.deep_sync_analytics(client)
        logger.info(f"[ANALYTICS] Position synced: {symbol} {side}")

    async def _fetch_and_record(
        self, client, symbol: str, side: str, open_time: int, close_time: int
    ):
        try:
            await self._do_fetch_and_record(client, symbol, side, open_time, close_time)
        finally:
            self.manager._sync_locks.discard((symbol, side))

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
        """Записывает операцию Sub-grid Peel в бухгалтерский леджер и обновляет агрегированную аналитику."""
        try:
            dt_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            peel_vol_usdt = round(float(peel_qty) * float(peel_price), 4)
            if str(side).upper() == "LONG":
                micro_pnl = round((float(peel_price) - float(fill_price)) * float(peel_qty), 4)
            else:
                micro_pnl = round((float(fill_price) - float(peel_price)) * float(peel_qty), 4)

            # 1. Запись в общий peels_ledger.txt
            cnt = 1
            if self.peels_ledger.exists():
                try:
                    with open(self.peels_ledger, "r", encoding="utf-8") as f:
                        cnt = sum(1 for line in f if line.strip())
                except Exception:
                    cnt = 1

            row = [
                cnt,
                dt_str,
                strategy_id,
                symbol,
                side,
                f"L{level}" if not str(level).startswith("L") else level,
                round(float(fill_price), 6),
                round(float(peel_price), 6),
                f"+{float(rebound_pct):.2f}%",
                round(float(peel_qty), 4),
                peel_vol_usdt,
                micro_pnl,
                round(float(rem_pos_qty), 4),
            ]

            with open(self.peels_ledger, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(row)

            # 2. Запись в изолированный леджер стратегии peels_ledger_{strategy_id}.txt
            if strategy_id and strategy_id != "default":
                strat_peel_txt = self.ledgers_dir / f"peels_ledger_{strategy_id}.txt"
                if not strat_peel_txt.exists():
                    with open(strat_peel_txt, mode="w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f, delimiter=';')
                        writer.writerow([
                            "Id", "Timestamp (UTC)", "Strategy", "Symbol", "Side", "Level",
                            "Fill Price", "Peel Price", "Rebound %", "Peel Qty",
                            "Peel Vol (USDT)", "Micro PnL (USDT)", "Remaining Pos Qty"
                        ])
                s_cnt = 1
                try:
                    with open(strat_peel_txt, "r", encoding="utf-8") as f:
                        s_cnt = sum(1 for line in f if line.strip())
                except Exception:
                    s_cnt = 1
                s_row = [s_cnt] + row[1:]
                with open(strat_peel_txt, mode="a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, delimiter=';')
                    writer.writerow(s_row)

            # 3. Обновление агрегированного JSON аналитики
            data = self.manager._read_data()
            if data:
                if "toxic_defense" not in data:
                    data["toxic_defense"] = {
                        "subgrid_peeling": {"events_count": 0, "total_peeled_usdt": 0.0, "locked_micro_pnl_usdt": 0.0},
                        "cross_recycling": {"events_count": 0, "burned_volume_usdt": 0.0, "profit_spent_usdt": 0.0, "loss_absorbed_usdt": 0.0}
                    }
                sg = data["toxic_defense"].setdefault("subgrid_peeling", {"events_count": 0, "total_peeled_usdt": 0.0, "locked_micro_pnl_usdt": 0.0})
                sg["events_count"] = int(sg.get("events_count", 0)) + 1
                sg["total_peeled_usdt"] = round(float(sg.get("total_peeled_usdt", 0.0)) + peel_vol_usdt, 4)
                sg["locked_micro_pnl_usdt"] = round(float(sg.get("locked_micro_pnl_usdt", 0.0)) + micro_pnl, 4)

                cdata = data.setdefault("per_coin", {}).setdefault(symbol, {})
                cpeel = cdata.setdefault("peeling", {"events_count": 0, "total_peeled_usdt": 0.0, "locked_micro_pnl_usdt": 0.0})
                cpeel["events_count"] = int(cpeel.get("events_count", 0)) + 1
                cpeel["total_peeled_usdt"] = round(float(cpeel.get("total_peeled_usdt", 0.0)) + peel_vol_usdt, 4)
                cpeel["locked_micro_pnl_usdt"] = round(float(cpeel.get("locked_micro_pnl_usdt", 0.0)) + micro_pnl, 4)

                self.manager._write_data(data)
                self.manager._update_strategies_stats(data)

            logger.info(
                f"[{strategy_id}][{symbol}] 🍊 Subgrid Peel accounted: Vol ${peel_vol_usdt:.2f}, "
                f"Micro PnL ${micro_pnl:+.2f}, Rem: {rem_pos_qty:.4f}"
            )
        except Exception as e:
            logger.error(f"[record_subgrid_peel] Error recording peel: {e}")

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
        """Записывает операцию Cross-Side Profit Recycling в бухгалтерский леджер и обновляет агрегированную аналитику."""
        try:
            dt_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            burn_vol_usdt = round(float(burn_qty) * float(current_price), 4)

            # 1. Запись в общий recycling_ledger.txt
            cnt = 1
            if self.recycling_ledger.exists():
                try:
                    with open(self.recycling_ledger, "r", encoding="utf-8") as f:
                        cnt = sum(1 for line in f if line.strip())
                except Exception:
                    cnt = 1

            row = [
                cnt,
                dt_str,
                strategy_id,
                symbol,
                winning_side,
                round(float(realized_profit), 4),
                losing_side,
                f"L{losing_level}",
                f"-{abs(float(losing_dd_pct)):.2f}%",
                round(float(burn_qty), 4),
                burn_vol_usdt,
                round(float(actual_burn_loss), 4),
                round(float(burn_budget), 4),
                round(float(rem_pos_qty), 4),
            ]

            with open(self.recycling_ledger, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(row)

            # 2. Запись в изолированный леджер стратегии recycling_ledger_{strategy_id}.txt
            if strategy_id and strategy_id != "default":
                strat_rec_txt = self.ledgers_dir / f"recycling_ledger_{strategy_id}.txt"
                if not strat_rec_txt.exists():
                    with open(strat_rec_txt, mode="w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f, delimiter=';')
                        writer.writerow([
                            "Id", "Timestamp (UTC)", "Strategy", "Symbol", "Winning Side",
                            "Realized Profit (USDT)", "Losing Side", "Losing Level", "Losing DD %",
                            "Burn Qty", "Burn Vol (USDT)", "Slice Loss (USDT)", "Burn Budget (USDT)", "Remaining Pos Qty"
                        ])
                s_cnt = 1
                try:
                    with open(strat_rec_txt, "r", encoding="utf-8") as f:
                        s_cnt = sum(1 for line in f if line.strip())
                except Exception:
                    s_cnt = 1
                s_row = [s_cnt] + row[1:]
                with open(strat_rec_txt, mode="a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f, delimiter=';')
                    writer.writerow(s_row)

            # 3. Обновление агрегированного JSON аналитики
            data = self.manager._read_data()
            if data:
                if "toxic_defense" not in data:
                    data["toxic_defense"] = {
                        "subgrid_peeling": {"events_count": 0, "total_peeled_usdt": 0.0, "locked_micro_pnl_usdt": 0.0},
                        "cross_recycling": {"events_count": 0, "burned_volume_usdt": 0.0, "profit_spent_usdt": 0.0, "loss_absorbed_usdt": 0.0}
                    }
                rc = data["toxic_defense"].setdefault("cross_recycling", {"events_count": 0, "burned_volume_usdt": 0.0, "profit_spent_usdt": 0.0, "loss_absorbed_usdt": 0.0})
                rc["events_count"] = int(rc.get("events_count", 0)) + 1
                rc["burned_volume_usdt"] = round(float(rc.get("burned_volume_usdt", 0.0)) + burn_vol_usdt, 4)
                rc["profit_spent_usdt"] = round(float(rc.get("profit_spent_usdt", 0.0)) + float(burn_budget), 4)
                rc["loss_absorbed_usdt"] = round(float(rc.get("loss_absorbed_usdt", 0.0)) + float(actual_burn_loss), 4)

                cdata = data.setdefault("per_coin", {}).setdefault(symbol, {})
                crec = cdata.setdefault("cross_recycling", {"events_count": 0, "burned_volume_usdt": 0.0, "profit_spent_usdt": 0.0, "loss_absorbed_usdt": 0.0})
                crec["events_count"] = int(crec.get("events_count", 0)) + 1
                crec["burned_volume_usdt"] = round(float(crec.get("burned_volume_usdt", 0.0)) + burn_vol_usdt, 4)
                crec["profit_spent_usdt"] = round(float(crec.get("profit_spent_usdt", 0.0)) + float(burn_budget), 4)
                crec["loss_absorbed_usdt"] = round(float(crec.get("loss_absorbed_usdt", 0.0)) + float(actual_burn_loss), 4)

                self.manager._write_data(data)
                self.manager._update_strategies_stats(data)

            logger.info(
                f"[{strategy_id}][{symbol}] ♻️ Cross Recycling accounted: Burned ${burn_vol_usdt:.2f}, "
                f"Loss Absorbed ${actual_burn_loss:.2f}, Rem: {rem_pos_qty:.4f}"
            )
        except Exception as e:
            logger.error(f"[record_cross_recycling] Error recording cross recycling: {e}")
