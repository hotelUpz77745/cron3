# ============================================================
# FILE: CORE/TP/tp_manager.py
# ROLE: Limit take-profit order calculation and placement manager
# ============================================================

import asyncio
import time
from typing import Optional

from consts import _CFG
from c_log import UnifiedLogger
from c_utils import Utils
from CORE._utils import TradeMath, RiskCalculatingUtils, TakeProfitConfigResolver
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE.TP.dynamic_atr_tp_manager import DynamicATRTakeProfitManager

logger = UnifiedLogger("TakeProfitManager")

class TakeProfitManager:
    def __init__(self, runtime_manager):
        self.runtime_manager = runtime_manager

    async def place_take_profit(
        self,
        client,
        symbol: str,
        side: str,
        current_price: float,
        spec_data: dict,
        state,
        opposite_state=None,
        volume: Optional[float] = None,
        override_indent: Optional[float] = None
    ):
        """Расчет и постановка лимитного TP ордера с ретраями и предварительной отменой."""
        
        # Предварительно отменяем все лимитки для этой стороны
        logger.info(f"[{symbol}] {side} Canceling old limit orders before placing new TP...")
        await client.cancel_orders_for_side(symbol, side)

        grid = state.grid
        level_self = RiskCalculatingUtils.get_current_grid_level(grid)
        
        if state.tp_purpose == "opposite" and opposite_state:
            level_opp = RiskCalculatingUtils.get_current_grid_level(opposite_state.grid)
            active_level_str = level_opp
        elif state.tp_purpose == "both" and opposite_state:
            level_opp = RiskCalculatingUtils.get_current_grid_level(opposite_state.grid)
            active_level_str = str(max(int(level_self), int(level_opp)))
        elif state.tp_purpose == "neutral":
            # При нейтральном режиме нет жесткой привязки шага TP к уровню сетки
            active_level_str = level_self if level_self in state.tp_map else "0"
        else:
            active_level_str = level_self

        active_level_str = TakeProfitConfigResolver.get_nearest_level_key(active_level_str, state.tp_map)
        current_tp = state.tp_map.get(active_level_str)
        if not current_tp and override_indent is None:
            logger.error(f"[{symbol}] {side} Missing TP config for level {active_level_str} (purpose: {state.tp_purpose})")
            return False
            
        tp_indent = float(override_indent) if override_indent is not None else current_tp.get("indent")
        if tp_indent is None:
            logger.info(f"[{symbol}] {side} TP indent is null. Skipping LIMIT order placement to rely on Fallback TP.")
            if active_level_str not in state.tp_map:
                state.tp_map[active_level_str] = {}
            state.tp_map[active_level_str]["is_active"] = True
            return True

        # Динамический ATR Take-Profit (только если не задан override_indent и стратегия задает dynamic_atr_tp)
        if override_indent is None and hasattr(self.runtime_manager, "strategy_registry") and self.runtime_manager.strategy_registry:
            try:
                strat = self.runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
                atr_cfg = getattr(strat, "dynamic_atr_tp_cfg", None)
                if isinstance(atr_cfg, dict) and atr_cfg.get("enabled"):
                    base_price_atr = state.avg_entry_price if state.avg_entry_price > 0 else current_price
                    _, atr_tp_pct = await DynamicATRTakeProfitManager.calculate_tp_price(
                        client=client,
                        symbol=symbol,
                        side=side,
                        avg_entry_price=base_price_atr,
                        active_level=active_level_str,
                        cfg=strat.dynamic_atr_tp_cfg,
                        spec_data=spec_data,
                    )
                    if atr_tp_pct > 0:
                        tp_indent = atr_tp_pct
            except Exception as e:
                logger.debug(f"[{symbol}] Dynamic ATR TP error: {e}")

        # Динамический Smart TP множитель для импульсного рынка (только если не задан override_indent)
        if override_indent is None and hasattr(self.runtime_manager, "strategy_registry") and self.runtime_manager.strategy_registry:
            try:
                strat = self.runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
                if strat and getattr(strat, "smart_tp_cfg", None):
                    bot = getattr(self.runtime_manager, "bot_core", None)
                    now = bot.get_current_time() if (bot and hasattr(bot, "get_current_time")) else time.time()
                    interval = None
                    if hasattr(strat, "heartbeat_seconds") and strat.heartbeat_seconds:
                        interval = int(strat.heartbeat_seconds)
                    elif bot and hasattr(bot, "get_strategy_heartbeat_seconds"):
                        interval = bot.get_strategy_heartbeat_seconds(symbol=symbol)
                    elif hasattr(self.runtime_manager, "get_heartbeat_interval_for_symbol"):
                        interval = self.runtime_manager.get_heartbeat_interval_for_symbol(symbol)
                    if not interval or interval <= 0:
                        interval = TimeControl.interval_to_seconds(DEFAULT_HEARTBEAT_TFR)
                    candle_id = int(now // interval)
                    candle_start = candle_id * interval
                    recent = [t for t in getattr(state, "recent_closes", []) if t >= candle_start]
                    closed_cnt = len(recent)
                    if closed_cnt > 0:
                        trend_state = None
                        trend_determ = getattr(strat, "smart_tp_trend_determ", None)
                        if trend_determ:
                            ema_cfg = strat.smart_tp_cfg.get("ema_trend") or (getattr(strat, "smart_size_cfg", None) and strat.smart_size_cfg.get("ema_trend"))
                            if ema_cfg:
                                trend_state = await MomentumEngine.get_ema_trend_state(
                                    symbol=symbol,
                                    ema_cfg=ema_cfg,
                                    client=client
                                )

                        tp_mult = MomentumEngine.calculate_smart_tp_multiplier(
                            closed_count=closed_cnt,
                            smart_tp_cfg=strat.smart_tp_cfg,
                            side=side,
                            trend_state=trend_state
                        )
                        if tp_mult > 1.0:
                            orig_tp = tp_indent
                            tp_indent = round(float(tp_indent) * tp_mult, 4)
                            trend_tag = f" [Trend: {trend_state}]" if trend_state else ""
                            logger.info(
                                f"[{symbol}][{strat.strategy_id}] {side} Smart TP x{tp_mult:.2f}{trend_tag} applied: "
                                f"{orig_tp}% -> {tp_indent}% (recent closes: {closed_cnt})"
                            )
            except Exception as e:
                logger.debug(f"[{symbol}] Smart TP calculation error: {e}")

        # Строгое соблюдение минимального тейка: min_tp_indent (из стратегии или app.json)
        min_tp = None
        if hasattr(self.runtime_manager, "strategy_registry") and self.runtime_manager.strategy_registry:
            try:
                strat = self.runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
                if strat:
                    if getattr(strat, "min_tp_indent", None) is not None:
                        min_tp = float(strat.min_tp_indent)
                    elif hasattr(strat, "raw_meta") and strat.raw_meta.get("min_tp_indent") is not None:
                        min_tp = float(strat.raw_meta["min_tp_indent"])
            except Exception:
                pass
        if min_tp is None:
            base_cfg = getattr(self.runtime_manager, "base_cfg", None) or {}
            if "min_tp_indent" in base_cfg and base_cfg["min_tp_indent"] is not None:
                min_tp = float(base_cfg["min_tp_indent"])
            elif "min_tp_indent" in _CFG and _CFG["min_tp_indent"] is not None:
                min_tp = float(_CFG["min_tp_indent"])

        if min_tp is not None:
            tp_indent = max(min_tp, float(tp_indent))

        # Расчет цены TP (строго от средней цены входа, а не от плавающего тикера)
        base_price = state.avg_entry_price if state.avg_entry_price > 0 else current_price
        tp_price = TradeMath.calculate_take_profit_price(base_price, tp_indent, side, spec_data, symbol)
        
        # Всегда продаем весь накопленный объем (если volume не передан явно)
        if volume is None:
            volume_float = abs(state.total_volume)
            volume = TradeMath.round_qty(volume_float, spec_data, symbol)
            
        if volume <= 0:
            logger.warning(f"[{symbol}] {side} Calculated TP volume is {volume}. Skipping TP placement to prevent zero-quantity error.")
            return False
        
        # Сторона ордера: обратная стороне позиции
        order_side = "SELL" if side == "LONG" else "BUY"
        
        max_retries = 3
        display_price = Utils.to_human_digit(tp_price)
        display_vol = Utils.to_human_digit(volume)
        for attempt in range(1, max_retries + 1):
            logger.info(f"[{symbol}] Placing {side} take profit limit order at {display_price}. Vol: {display_vol} (Attempt {attempt}/{max_retries})")
            res = await client.make_order(
                symbol=symbol,
                qty=volume,
                side=order_side,
                position_side=side,
                market_type="LIMIT",
                price=tp_price
            )
            
            if res.success and res.data:
                order_id = res.data.get("orderId")
                if order_id:
                    logger.info(f"[{symbol}] {side} Take profit LIMIT order placed successfully! ID: {order_id}")
                    # Помечаем tp_map текущий уровень как активный
                    if active_level_str not in state.tp_map:
                        state.tp_map[active_level_str] = {}
                        
                    state.tp_map[active_level_str]["is_active"] = True

                    # Фиксация параметров выставленного TP ордера для телеметрии
                    bot = getattr(self.runtime_manager, "bot_core", None)
                    now = bot.get_current_time() if (bot and hasattr(bot, "get_current_time")) else (getattr(client, "current_sim_time", None) or time.time())
                    state.tp_placed_at = now
                    state.tp_current_indent = tp_indent
                    state.tp_price = tp_price
                    
                    # Не вызываем save_cache здесь, sync_with_fsm сделает это
                    return True
                    
            logger.error(f"[{symbol}] Failed to place TP for {side}: {res.error_msg}")
            if attempt < max_retries:
                await asyncio.sleep(1.0)
                
        return False
