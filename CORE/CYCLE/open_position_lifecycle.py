# CORE/CYCLE/open_position_lifecycle.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.CYCLE.open_position_lifecycle
ROLE: Конвейер обслуживания открытой позиции (Peeling, Averaging, Fallback TP)
==============================================================================
"""

from typing import Any
from c_log import UnifiedLogger

logger = UnifiedLogger("OpenPositionLifecycle")


class OpenPositionLifecycleHandler:
    """
    Линейный конвейер обслуживания открытой позиции на каждом тике:
    1. Проверка защитных условий (is_closing, _closing_symbols).
    2. Subgrid Peeling (частичный сброс токсичных слоев).
    3. Grid Averaging (выставление / исполнение лимитных усреднений).
    4. Fallback Take Profit (контроль аварийного закрытия).
    """

    @staticmethod
    async def process_open_position(
        bot: Any,
        symbol: str,
        side: str,
        state: Any,
        opposite_state: Any,
        current_price: float,
    ) -> None:
        if (
            getattr(state, "is_closing", False) is True
            or getattr(state, "is_finished", False) is True
            or symbol in getattr(bot, "_closing_symbols", set())
        ):
            return

        sym_client = bot.get_client_for_symbol(symbol)

        # 1. Subgrid Peeling
        has_peel = any(isinstance(v, dict) and v.get("peel_tp_pct") for v in getattr(state, "grid", {}).values())
        peeled = False
        peel_mgr = getattr(bot, "peeling_manager", None) or getattr(bot.avg_manager, "peeling_manager", None)
        if has_peel and peel_mgr:
            peeled = await peel_mgr.process_peel(
                sym_client, bot.runtime_manager, symbol, side, state, current_price, bot.spec_data, bot.tp_manager
            )

        # 2. Grid Averaging (если не было пилинга на текущем тике)
        if not peeled:
            await bot.avg_manager.process(
                sym_client, bot.runtime_manager, symbol, side, state, opposite_state, current_price, bot.spec_data, bot.tp_manager
            )

        # 3. Fallback TP Manager
        await bot.fallback_tp_manager.process(
            sym_client, bot.runtime_manager, symbol, side, state, current_price, bot.spec_data, opposite_state=opposite_state
        )
