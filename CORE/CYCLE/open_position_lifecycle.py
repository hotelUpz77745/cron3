# CORE/CYCLE/open_position_lifecycle.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.CYCLE.open_position_lifecycle
ROLE: Конвейер обслуживания открытой позиции (Peeling, Averaging, Fallback TP)
==============================================================================
"""

import time
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
            # Directional Trend-Gate: Пассивный режим при диспозиции к тренду (заморозка усреднения)
            freeze_averaging = False
            rm = getattr(bot, "runtime_manager", None)
            strat = None
            if rm and getattr(rm, "strategy_registry", None):
                try:
                    strat = rm.strategy_registry.get_strategy_for_symbol(symbol)
                except Exception:
                    strat = None

            gate_cfg = getattr(strat, "directional_trend_gate", None) if strat else None
            if gate_cfg and isinstance(gate_cfg, dict) and gate_cfg.get("enabled"):
                if gate_cfg.get("freeze_averaging_on_disposition", True):
                    from CORE.STRATEGY.directional_trend_gate import DirectionalTrendGate
                    client_obj = bot.get_client_for_symbol(symbol) if hasattr(bot, "get_client_for_symbol") else getattr(bot, "client", None)
                    strat_id = getattr(strat, "strategy_id", None)
                    is_allowed, reason = await DirectionalTrendGate.is_side_allowed(
                        symbol=symbol,
                        side=side,
                        gate_cfg=gate_cfg,
                        client=client_obj,
                        strategy_id=strat_id,
                    )
                    if not is_allowed:
                        freeze_averaging = True
                        if not getattr(state, "is_passive_mode", False):
                            state.is_passive_mode = True
                            strat_tag = f"[{strat_id}] " if strat_id else ""
                            logger.info(
                                f"[{symbol}] {strat_tag}{side} ⏸️ [PASSIVE HOLD] Trend disposition detected: {reason}. "
                                f"Grid averaging FROZEN. Side waiting for Take-Profit or Recycler burn."
                            )
                    else:
                        freeze_averaging = False
                        if getattr(state, "is_passive_mode", False):
                            state.is_passive_mode = False
                            strat_tag = f"[{strat_id}] " if strat_id else ""
                            logger.info(
                                f"[{symbol}] {strat_tag}{side} ▶️ [RESUME AVERAGING] "
                                f"Trend alignment restored ({side} allowed). Grid averaging unblocked."
                            )
                    state.is_passive_mode = freeze_averaging

            if not freeze_averaging:
                await bot.avg_manager.process(
                    sym_client, bot.runtime_manager, symbol, side, state, opposite_state, current_price, bot.spec_data, bot.tp_manager
                )

        # 3. Fallback TP Manager
        await bot.fallback_tp_manager.process(
            sym_client, bot.runtime_manager, symbol, side, state, current_price, bot.spec_data, opposite_state=opposite_state
        )
