# CORE/CYCLE/order_submitter.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.CYCLE.order_submitter
ROLE: Идемпотентная отправка рыночного ордера L0, FSM-синхронизация и Take Profit
==============================================================================
"""

import inspect
from typing import Any, Optional, Callable
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.CYCLE.signal_validator import SignalValidator

logger = UnifiedLogger("OrderSubmitter")


class OrderSubmitter:
    """
    Отвечает за физическое исполнение входа:
    - Повторная валидация блокировок прямо перед ордером под символьным локом
    - Отправка MARKET ордера через sym_client.make_order
    - Идемпотентный переход состояния: state.set_in_position(True)
    - Ожидание FSM sync либо REST fallback
    - Выставление Take Profit ордера через bot.tp_manager
    - Защита инварианта: гарантированный сброс in_position_papper в finally
    """

    @staticmethod
    async def submit_market_entry(
        bot: Any,
        symbol: str,
        side: str,
        side_cfg: dict,
        state: Any,
        volume: float,
        current_price: float,
        concurrent_mode: bool = False,
        wait_for_sync_fn: Optional[Callable] = None,
    ) -> bool:
        sym_client = bot.get_client_for_symbol(symbol)
        order_side = "BUY" if side == "LONG" else "SELL"

        sym_lock = bot._get_symbol_lock(symbol) if hasattr(bot, "_get_symbol_lock") else None

        async def _do_open() -> bool:
            is_blocked, reason, _ = SignalValidator.check_entry_block(bot, symbol)
            if is_blocked or getattr(state, "is_closing", False) is True:
                logger.warning(f"[{symbol}] {side}: Entry aborted right before market order — {reason or 'state is closing'}.")
                state.in_position_papper = False
                return False

            logger.info(f"[{symbol}] Opening {side} market order. Volume: {volume}")
            res = await sym_client.make_order(
                symbol=symbol,
                qty=volume,
                side=order_side,
                position_side=side,
                market_type="MARKET",
                concurrent_mode=concurrent_mode,
            )
            if not res or not res.success:
                err = getattr(res, "error_msg", "Unknown error")
                logger.error(f"[{symbol}] Failed to open {side} position: {err}")
                state.in_position_papper = False
                state.last_failed_entry_ts = Utils.get_current_time(getattr(bot, "current_sim_time", None))
                return False

            state.set_in_position(True)
            return True

        if sym_lock:
            async with sym_lock:
                opened = await _do_open()
        else:
            opened = await _do_open()

        if not opened:
            return False

        # Инициализация сетки и таймстемпов открытой позиции
        current_time_ms = int(Utils.get_current_time(getattr(bot, "current_sim_time", None)) * 1000)
        state.open_time = current_time_ms
        side_cfg["open_time"] = current_time_ms
        if "0" in state.grid:
            state.grid["0"]["is_active"] = True
            state.grid["0"]["activated_at"] = current_time_ms / 1000.0
            state.grid["0"]["timestamp"] = current_time_ms

        state.fallback_price = None
        state.next_avg_price = None
        state.initial_entry_price = 0.0
        state.pre_avg_price = 0.0

        # Ожидание синхронизации WebSocket FSM или REST Fallback
        sync_fn = wait_for_sync_fn or Utils.wait_for_fsm_sync
        sync_success = await sync_fn(state, timeout_sec=3.0, poll_interval=0.01)
        if not sync_success:
            try:
                positions = await sym_client.fetch_positions(symbol)
                for p in positions:
                    if p.get("positionSide") == side:
                        new_avg = float(p.get("entryPrice", 0.0))
                        new_vol = float(p.get("positionAmt", 0.0))
                        if new_avg > 0:
                            state.avg_entry_price = new_avg
                            state.total_volume = new_vol
                            state.set_in_position(True)
            except Exception as e:
                logger.error(f"[{symbol}] {side} REST fallback failed: {e}")

        # Выставление Take Profit ордера
        opposite_state = bot.fsm_states[symbol]["SHORT"] if side == "LONG" else bot.fsm_states[symbol]["LONG"]
        await bot.tp_manager.place_take_profit(
            sym_client, symbol, side, current_price, bot.spec_data, state, opposite_state
        )
        return True
