# CORE/STRATEGY/peeling_manager.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY.peeling_manager
ROLE: Менеджер частичной разгрузки объемов сетки (Subgrid LIFO Peeling)
      Доменная сущность паспорта стратегии (peel_tp_pct, peel_ratio, peel_num)
==============================================================================
"""

import asyncio
from typing import Any, Dict, Optional
from c_log import UnifiedLogger
from CORE._utils import TradeMath

logger = UnifiedLogger("PeelingManager")


class PeelingManager:
    """
    Менеджер частичной разгрузки объемов сетки (Subgrid LIFO Peeling).
    Инкапсулирует логику срезки крайних усредняющих колен сетки при отскоке цены
    на заданный процент peel_tp_pct.
    
    В соответствии с Протоколом Разработки:
    - Высокая абстрактность и модульность.
    - Изолированная тестируемость.
    - Отсутствие скрытых дефолтов («Работай или упади сразу»).
    """

    def __init__(
        self,
        bot_core: Optional[Any] = None,
        runtime_manager: Optional[Any] = None,
        client: Optional[Any] = None,
        tp_manager: Optional[Any] = None,
    ):
        """Инициализация менеджера пилинга с явным инжектированием зависимостей."""
        self.bot_core = bot_core
        self.runtime_manager = runtime_manager
        self.client = client
        self.tp_manager = tp_manager

    async def process(
        self,
        symbol: str,
        side: str,
        state: Any,
        current_price: float,
        spec_data: dict,
        client: Optional[Any] = None,
        runtime_manager: Optional[Any] = None,
        tp_manager: Optional[Any] = None,
    ) -> bool:
        """Экземплярный фасад для исполнения LIFO-пилинга."""
        return await self.process_peel(
            client=client or self.client,
            runtime_manager=runtime_manager or self.runtime_manager,
            symbol=symbol,
            side=side,
            state=state,
            current_price=current_price,
            spec_data=spec_data,
            tp_manager=tp_manager or self.tp_manager,
        )

    @staticmethod
    def has_peel_levels(state: Any) -> bool:
        """
        Проверяет, содержит ли сетка позиции активные настройки LIFO-пилинга.
        """
        grid = getattr(state, "grid", {})
        if not isinstance(grid, dict):
            return False
        return any(
            isinstance(v, dict) and "peel_tp_pct" in v and v["peel_tp_pct"] is not None and float(v["peel_tp_pct"]) > 0
            for v in grid.values()
        )

    @classmethod
    async def process_peel(
        cls,
        client: Any,
        runtime_manager: Any,
        symbol: str,
        side: str,
        state: Any,
        current_price: float,
        spec_data: dict,
        tp_manager: Any,
    ) -> bool:
        """
        Проверяет и исполняет частичную разгрузку (LIFO-пилинг) крайнего усреднения сетки.
        Срабатывает ТОЛЬКО если у крайнего активного колена в grid задан peel_tp_pct,
        и цена отскочила от fill_price этого колена на >= peel_tp_pct.
        Возвращает True, если был исполнен пилинг колена.
        """
        if (
            not getattr(state, "in_position", False)
            or getattr(state, "is_finished", False)
            or getattr(state, "pending_avg", False)
            or getattr(state, "pending_peel", False)
            or getattr(state, "pending_rolling_tp", False)
            or current_price is None
            or current_price <= 0
        ):
            return False

        grid = getattr(state, "grid", {})
        if not grid:
            return False

        # Находим крайний (наиболее глубокий) активный уровень сетки, еще не прошедший пилинг в этом цикле
        active_levels = [
            int(k) for k, v in grid.items()
            if isinstance(v, dict) and v.get("is_active") and not v.get("peeled", False) and v.get("enabled", True) is not False
        ]
        if not active_levels:
            return False

        cur_level_int = max(active_levels)
        cur_level_str = str(cur_level_int)
        level_data = grid.get(cur_level_str)
        if not level_data:
            return False

        # Проверяем настройку peel_num (допустимое количество пилингов на колене)
        if "peel_num" in level_data:
            raw_pn = level_data["peel_num"]
            if raw_pn is None:
                return False
            p_limit = int(raw_pn)
            if p_limit <= 0:
                raise ValueError(f"[PeelingManager] peel_num must be > 0, got {p_limit} on level {cur_level_str}")
        else:
            p_limit = 1

        is_peeled = bool(level_data["peeled"]) if "peeled" in level_data else bool(level_data.get("peeled", False))
        peeled_count = int(level_data["peeled_count"]) if "peeled_count" in level_data else int(level_data.get("peeled_count", 0))
        if is_peeled or peeled_count >= p_limit:
            return False

        # Проверяем наличие настройки пилинга на этом уровне (прямое чтение)
        if "peel_tp_pct" not in level_data or level_data["peel_tp_pct"] is None:
            return False
        peel_tp_pct = float(level_data["peel_tp_pct"])
        if peel_tp_pct <= 0:
            return False

        if "fill_price" not in level_data or not level_data["fill_price"] or float(level_data["fill_price"]) <= 0:
            return False
        fill_price = float(level_data["fill_price"])

        # Расчет отскока от цены набора данного колена
        if side == "LONG":
            rebound_pct = (current_price - fill_price) / fill_price * 100.0
        else:
            rebound_pct = (fill_price - current_price) / fill_price * 100.0

        if rebound_pct < peel_tp_pct:
            return False

        # Отскок зафиксирован! Определяем объем разгрузки с учетом peel_ratio (прямое чтение)
        if "peel_ratio" in level_data and level_data["peel_ratio"] is not None:
            peel_ratio = float(level_data["peel_ratio"])
            if not (0.1 <= peel_ratio <= 1.0):
                raise ValueError(f"[PeelingManager] peel_ratio must be in [0.1..1.0], got {peel_ratio}")
        else:
            peel_ratio = 1.0

        fill_qty = level_data.get("fill_qty")
        if fill_qty and fill_qty > 0:
            raw_qty = min(float(fill_qty) * peel_ratio, abs(state.total_volume))
        else:
            invest_size = float(getattr(state, "eff_invest", 0.0) or 0.0)
            if invest_size <= 0:
                if runtime_manager and hasattr(runtime_manager, "caches") and symbol in runtime_manager.caches:
                    side_c = runtime_manager.caches[symbol].get(side, {})
                    invest_size = float(side_c.get("eff_invest", 0.0) or side_c.get("invest_size", 100.0))
                else:
                    invest_size = 100.0
            vol_pct = float(level_data.get("volume") or 0.0)
            raw_qty = TradeMath.calculate_order_volume(invest_size, vol_pct, current_price, spec_data, symbol)
            raw_qty = min(raw_qty * peel_ratio, abs(state.total_volume))

        peel_qty = TradeMath.round_qty(raw_qty, spec_data, symbol)
        if peel_qty <= 0:
            return False

        state.pending_peel = True
        try:
            # 1. Отменяем старые лимитные ордера перед маркет-закрытием порции
            await client.cancel_orders_for_side(symbol, side)

            order_side = "SELL" if side == "LONG" else "BUY"
            logger.info(
                f"[{symbol}] {side} 🍊 [SUB-GRID PEEL TRIGGERED] Level {cur_level_str}: "
                f"Rebound +{rebound_pct:.2f}% >= {peel_tp_pct}% from fill {fill_price}. Closing peel vol {peel_qty} (ratio {peel_ratio})"
            )

            # Фиксируем среднюю цену входа ДО срезки для точного расчета биржевого убытка
            entry_before_peel = float(state.avg_entry_price or 0.0)
            if entry_before_peel > 0:
                peel_loss = round((current_price - entry_before_peel) * peel_qty, 4) if side == "LONG" else round((entry_before_peel - current_price) * peel_qty, 4)
            else:
                peel_loss = 0.0

            res = await client.make_order(
                symbol=symbol,
                qty=peel_qty,
                side=order_side,
                position_side=side,
                market_type="MARKET",
            )

            if res.success:
                # Синхронизируем актуальный остаточный объем и среднюю цену напрямую с биржи
                synced_from_rest = False
                if client and hasattr(client, "fetch_positions"):
                    try:
                        pos_list = await client.fetch_positions(symbol)
                        for p in pos_list:
                            if p.get("positionSide") == side:
                                rem_amt = float(p.get("positionAmt", 0.0))
                                rem_entry = float(p.get("entryPrice", 0.0))
                                state.total_volume = rem_amt
                                if rem_entry > 0:
                                    state.avg_entry_price = rem_entry
                                synced_from_rest = True
                                break
                    except Exception as e:
                        logger.warning(f"[{symbol}] {side} Failed to fetch post-peel position from REST: {e}")

                if not synced_from_rest:
                    if side == "LONG":
                        state.total_volume = max(0.0, round(state.total_volume - peel_qty, 8))
                    else:
                        state.total_volume = min(0.0, round(state.total_volume + peel_qty, 8))

                if abs(state.total_volume) == 0.0:
                    state.in_position = False
                    state.is_finished = True

                peeled_count = int(level_data.get("peeled_count", 0)) + 1
                level_data["peeled_count"] = peeled_count

                # Лимит допустимого числа пилингов на колене задается peel_num
                raw_peel_num = level_data.get("peel_num")
                if raw_peel_num is not None:
                    try:
                        p_limit = max(1, int(raw_peel_num))
                    except (ValueError, TypeError):
                        p_limit = 1
                else:
                    p_limit = 1

                curr_fill_qty = float(level_data.get("fill_qty") or 0.0)
                rem_level_qty = max(0.0, round(curr_fill_qty - peel_qty, 8))
                level_data["fill_qty"] = rem_level_qty

                if peeled_count >= p_limit:
                    level_data["peeled"] = True
                    if rem_level_qty > 0:
                        level_data["is_active"] = True
                    else:
                        level_data["is_active"] = False
                        level_data["fill_price"] = None
                        level_data["fill_qty"] = None
                    logger.info(
                        f"[{symbol}] {side} 🍊 [SUB-GRID PEEL-LIMIT EXECUTED] Level {cur_level_str} peeled {peel_qty} (ratio {peel_ratio})! "
                        f"Peel limit reached ({peeled_count}/{p_limit}). Level locked against re-entry with rem {rem_level_qty:.4f} vol. Pos vol: {abs(state.total_volume):.4f}"
                    )
                else:
                    level_data["is_active"] = False
                    level_data["is_armed"] = False
                    level_data["activated_at"] = None
                    level_data["timestamp"] = None
                    level_data["fill_price"] = None
                    level_data["fill_qty"] = None
                    level_data["peeled"] = False
                    logger.info(
                        f"[{symbol}] {side} 🍊 [SUB-GRID PEEL-RESET EXECUTED] Level {cur_level_str} peeled {peel_qty} (ratio {peel_ratio})! "
                        f"Peel {peeled_count}/{p_limit} complete. Level reset for re-entry. Remaining vol: {abs(state.total_volume):.4f}"
                    )

                # Сбрасываем кэш основного тейка для пересчета под оставшийся объем
                state.tp_price = None
                state.fallback_price = None
                state.trailing_tp_state = {}

                # Учет срезки Sub-grid Peel в бухгалтерии аналитики
                try:
                    analytics = getattr(runtime_manager, "analytics", None)
                    if not analytics and hasattr(runtime_manager, "bot_core"):
                        analytics = getattr(runtime_manager.bot_core, "analytics", None)
                    strat_id = "default"
                    if hasattr(runtime_manager, "strategy_registry") and runtime_manager.strategy_registry:
                        strat = runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
                        if strat:
                            strat_id = getattr(strat, "strategy_id", "default")
                    if analytics and hasattr(analytics, "record_subgrid_peel"):
                        analytics.record_subgrid_peel(
                            strategy_id=strat_id,
                            symbol=symbol,
                            side=side,
                            level=str(cur_level_str),
                            fill_price=fill_price,
                            peel_price=current_price,
                            rebound_pct=rebound_pct,
                            peel_qty=peel_qty,
                            rem_pos_qty=abs(state.total_volume),
                            avg_entry_price=entry_before_peel,
                            realized_loss=peel_loss,
                        )
                except Exception as e:
                    logger.warning(f"[{symbol}] Failed to record subgrid peel: {e}")

                if hasattr(runtime_manager, "mark_dirty") and callable(runtime_manager.mark_dirty):
                    runtime_manager.mark_dirty()

                # Если позиция осталась активной, обновляем TP
                if state.in_position and state.total_volume != 0.0:
                    if tp_manager and hasattr(tp_manager, "place_take_profit"):
                        try:
                            await tp_manager.place_take_profit(
                                client=client,
                                symbol=symbol,
                                side=side,
                                current_price=current_price,
                                spec_data=spec_data,
                                state=state,
                                opposite_state=None,
                            )
                        except Exception as e:
                            logger.warning(f"[{symbol}] Failed to refresh TP after peel: {e}")
                return True
            else:
                logger.error(f"[{symbol}] Failed to execute peel market order: {res.error_msg}")
                return False
        finally:
            state.pending_peel = False
