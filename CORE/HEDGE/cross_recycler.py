# ==============================================================================
# Path: CORE/HEDGE/cross_recycler.py
# Role: Модуль рециркуляции прибыли (Cross-Side Profit Recycling) для хедж-позиций
# ==============================================================================

import asyncio
from typing import Optional, Dict, Any
from c_log import UnifiedLogger
from CORE._utils import TradeMath, RiskCalculatingUtils

logger = UnifiedLogger("CrossRecycler")


class CrossSideProfitRecycler:
    """
    Модуль рециркуляции прибыли (Cross-Side Profit Recycling).
    При закрытии прибыльного тейка сильной стороны направляет долю прибыли
    на рыночное сокращение токсичного объема противоположной страдающей стороны.

    Защита от 'переливания из пустого в порожнее':
    1. min_burn_level: рециркуляция работает ТОЛЬКО если страдающая сторона набрала сетку >= min_burn_level (например L2+).
    2. min_drawdown_pct: просадка страдающей стороны должна быть реальной (>= min_drawdown_pct, например >= 3.0%).
    3. max_slice_pct: за один цикл сжигается не более max_slice_pct объема страдающей стороны (например <= 25%).
    4. recycle_ratio: расходуется только часть прибыли (например 50%), гарантируя чистый плюс на балансе.
    """

    @classmethod
    async def maybe_recycle(
        cls,
        universe,
        symbol: str,
        winning_side: str,
        realized_profit: float,
        current_price: float,
        spec_data: dict,
    ) -> Optional[Dict[str, Any]]:
        """
        Проверяет условия и при необходимости исполняет срезку объема страдающей стороны.
        Возвращает словарь с деталями операции или None.
        """
        if realized_profit <= 0 or current_price <= 0:
            return None

        # Разрешение конфигурации для CronUniverse (paper) и BotCore (live)
        recycling_cfg = None
        strat_id = getattr(universe, "universe_id", None)

        if hasattr(universe, "cross_recycling_cfg"):
            raw_cfg = getattr(universe, "cross_recycling_cfg")
            if isinstance(raw_cfg, dict):
                recycling_cfg = raw_cfg

        if not recycling_cfg and hasattr(universe, "runtime_manager") and getattr(universe.runtime_manager, "strategy_registry", None):
            strat = universe.runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
            if strat:
                recycling_cfg = getattr(strat, "cross_recycling_cfg", None) or getattr(strat, "overrides", {}).get("cross_side_recycling")
                if not strat_id:
                    strat_id = getattr(strat, "strategy_id", None)

        if not recycling_cfg and hasattr(universe, "overrides") and isinstance(universe.overrides, dict):
            recycling_cfg = universe.overrides.get("cross_side_recycling")

        if not recycling_cfg or not isinstance(recycling_cfg, dict) or not recycling_cfg.get("enabled", False):
            return None

        if not strat_id:
            strat_id = "BOT"

        # Строгое соблюдение Протокола: читаем напрямую через [''], без скрытых дефолтов!
        recycle_ratio = float(recycling_cfg["recycle_ratio"])
        min_burn_level = int(recycling_cfg["min_burn_level"])
        min_drawdown_pct = float(recycling_cfg["min_drawdown_pct"])
        max_slice_pct = float(recycling_cfg["max_slice_pct"])

        losing_side = "SHORT" if winning_side == "LONG" else "LONG"
        fsm_states = getattr(universe, "fsm_states", {}) or {}
        losing_state = fsm_states.get(symbol, {}).get(losing_side)
        if not losing_state or not losing_state.in_position or losing_state.total_volume <= 0:
            return None

        cur_level = int(RiskCalculatingUtils.get_current_grid_level(losing_state.grid))
        if cur_level < min_burn_level:
            return None

        if losing_side == "LONG":
            drawdown_pct = ((losing_state.avg_entry_price - current_price) / losing_state.avg_entry_price) * 100.0
            loss_per_unit = losing_state.avg_entry_price - current_price
        else:
            drawdown_pct = ((current_price - losing_state.avg_entry_price) / losing_state.avg_entry_price) * 100.0
            loss_per_unit = current_price - losing_state.avg_entry_price

        if drawdown_pct < min_drawdown_pct or loss_per_unit <= 0:
            return None

        burn_budget = realized_profit * recycle_ratio
        raw_qty = burn_budget / loss_per_unit
        max_allowed_qty = losing_state.total_volume * (max_slice_pct / 100.0)
        actual_qty = min(raw_qty, max_allowed_qty)

        rounded_qty = TradeMath.round_qty(actual_qty, spec_data, symbol)
        if rounded_qty <= 0 or rounded_qty > losing_state.total_volume:
            return None

        order_side = "SELL" if losing_side == "LONG" else "BUY"
        actual_burn_loss = rounded_qty * loss_per_unit

        logger.info(
            f"[{strat_id}][{symbol}] [CROSS-SIDE RECYCLING] "
            f"Winning {winning_side} (+${realized_profit:.2f}) sponsors burn of {rounded_qty} {losing_side} "
            f"(Budget: ${burn_budget:.2f}, Slice loss: -${actual_burn_loss:.2f}, Lvl {cur_level}, DD: -{drawdown_pct:.1f}%)"
        )

        res = await universe.client.make_order(
            symbol=symbol,
            qty=rounded_qty,
            side=order_side,
            position_side=losing_side,
            market_type="MARKET",
        )

        if res.success:
            losing_state.total_volume = max(0.0, losing_state.total_volume - rounded_qty)
            if losing_state.total_volume <= 0:
                losing_state.set_in_position(False)
                losing_state.is_finished = True

            # Уведомляем рантайм о сохранении стейта
            if hasattr(universe, "runtime_manager") and hasattr(universe.runtime_manager, "save_cache"):
                try:
                    res_save = universe.runtime_manager.save_cache(symbol)
                    if asyncio.iscoroutine(res_save):
                        await res_save
                except Exception:
                    pass
            elif hasattr(universe, "mark_dirty") and callable(universe.mark_dirty):
                try:
                    universe.mark_dirty()
                except Exception:
                    pass

            # Если позиция осталась активна, актуализируем лимитный TP ордер под новый уменьшенный объем
            if losing_state.in_position and losing_state.total_volume > 0:
                if hasattr(universe, "tp_manager") and hasattr(universe.tp_manager, "place_take_profit"):
                    try:
                        res_tp = universe.tp_manager.place_take_profit(
                            client=universe.client,
                            symbol=symbol,
                            side=losing_side,
                            current_price=current_price,
                            spec_data=spec_data,
                            state=losing_state,
                        )
                        if asyncio.iscoroutine(res_tp):
                            await res_tp
                    except Exception as e:
                        logger.warning(f"[{strat_id}][{symbol}] Failed to refresh TP limit order after burn: {e}")

            return {
                "symbol": symbol,
                "winning_side": winning_side,
                "losing_side": losing_side,
                "burned_qty": rounded_qty,
                "burn_loss": actual_burn_loss,
                "burn_budget": burn_budget,
                "remaining_vol": losing_state.total_volume,
            }
        else:
            logger.warning(
                f"[{strat_id}][{symbol}] Cross-side recycling order failed: {res.error_msg}"
            )
            return None
