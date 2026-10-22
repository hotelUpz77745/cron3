# CORE/CYCLE/finished_position_handler.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.CYCLE.finished_position_handler
ROLE: Отработка завершенных позиций (Profit Recycling, Analytics, Reset Cache)
==============================================================================
"""

import inspect
from typing import Any, Dict, Optional
from c_log import UnifiedLogger
from CORE.STRATEGY.cross_recycler import CrossSideProfitRecycler

logger = UnifiedLogger("FinishedPositionHandler")


class FinishedPositionHandler:
    """
    Изолированный конвейер закрытия отработавших позиций (is_finished=True):
    1. Cross-side profit recycling (сжигание токсичных слоев противоположной стороны).
    2. Фиксация сделки в аналитическом модуле (AnalyticsManager).
    3. Отмена зависших ордеров закрытой стороны на бирже.
    4. Сброс состояния FSM в дефолт и сброс AverageManager.
    5. Атомарное сохранение чистого рантайм-кеша.
    """

    @staticmethod
    async def process_finished_positions(
        bot: Any,
        symbol: str,
        states: Dict[str, Any],
        runtime_cfg: dict,
        recycler_cls: Optional[Any] = None,
    ) -> None:
        sides_to_reset = [side for side in ("LONG", "SHORT") if states[side].is_finished]
        if not sides_to_reset:
            return

        recycler = recycler_cls or CrossSideProfitRecycler

        for idx, side in enumerate(sides_to_reset):
            logger.info(f"[{symbol}] {side} is_finished. Running analytics and resetting runtime cache...")

            # 1. Cross-Side Profit Recycling
            closed_vol = getattr(states[side], "last_closed_volume", 0.0)
            closed_entry = getattr(states[side], "last_closed_avg_price", 0.0)
            price_data = bot.prices.get(symbol)
            cur_price = price_data[0] if isinstance(price_data, tuple) else price_data
            if (not cur_price or cur_price <= 0) and getattr(states[side], "tp_price", None):
                cur_price = states[side].tp_price

            if closed_vol > 0 and closed_entry > 0 and cur_price and cur_price > 0:
                realized_pnl = (cur_price - closed_entry) * closed_vol if side == "LONG" else (closed_entry - cur_price) * closed_vol
                if realized_pnl > 0:
                    try:
                        await recycler.maybe_recycle(
                            universe=bot,
                            symbol=symbol,
                            winning_side=side,
                            realized_profit=realized_pnl,
                            current_price=cur_price,
                            spec_data=bot.spec_data,
                        )
                    except Exception as e:
                        logger.error(f"[{symbol}] Error during cross-side recycling: {e}")

            now = bot.get_current_time() if hasattr(bot, "get_current_time") else 0.0
            if hasattr(states[side], "recent_closes"):
                states[side].recent_closes.append(now)

            open_time = runtime_cfg.get(side, {}).get("open_time", 0)
            close_time = int(now * 1000)
            sym_client = bot.get_client_for_symbol(symbol) if hasattr(bot, "get_client_for_symbol") else getattr(bot, "client", None)
            if hasattr(bot, "analytics") and bot.analytics and hasattr(bot.analytics, "record_finished_position"):
                bot.analytics.record_finished_position(sym_client, symbol, side, open_time, close_time)

            logger.info(f"[{symbol}] {side} Canceling all limit orders for this side")
            if sym_client and hasattr(sym_client, "cancel_orders_for_side"):
                await sym_client.cancel_orders_for_side(symbol, side)

            if hasattr(bot, "runtime_manager") and bot.runtime_manager and hasattr(bot.runtime_manager, "reset_state_to_default"):
                bot.runtime_manager.reset_state_to_default(symbol, side, states[side])
            if hasattr(bot, "avg_manager") and bot.avg_manager and hasattr(bot.avg_manager, "reset"):
                bot.avg_manager.reset(symbol, side)

            if idx < len(sides_to_reset) - 1 and getattr(bot, "current_sim_time", None) is None:
                await asyncio.sleep(0.1)

        if hasattr(bot, "runtime_manager") and bot.runtime_manager and hasattr(bot.runtime_manager, "save_cache"):
            s_res = bot.runtime_manager.save_cache(symbol)
            if inspect.isawaitable(s_res):
                await s_res
