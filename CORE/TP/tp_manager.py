# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\TP\tp_manager.py
# Role: tp_manager.py module

# ==============================================================================
# Path: CORE/tp_manager.py
# Role: Менеджер постановки лимитных тейк-профит ордеров
# ==============================================================================

from c_log import UnifiedLogger
from CORE._utils import TradeMath

from CORE._utils import RiskCalculatingUtils
from c_utils import Utils
import asyncio
import time
from CORE.STRATEGY.momentum_engine import MomentumEngine
logger = UnifiedLogger("TakeProfitManager")

class TakeProfitManager:
    def __init__(self, runtime_manager):
        self.runtime_manager = runtime_manager

    async def place_take_profit(self, client, symbol: str, side: str, current_price: float, spec_data: dict, state, opposite_state=None, volume: float = None):
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

        tp_map = state.tp_map
        current_tp = tp_map.get(active_level_str)
        if not current_tp:
            logger.error(f"[{symbol}] {side} Missing TP config for level {active_level_str} (purpose: {state.tp_purpose})")
            return False
            
        tp_indent = current_tp.get("indent")
        if tp_indent is None:
            logger.info(f"[{symbol}] {side} TP indent is null. Skipping LIMIT order placement to rely on Fallback TP.")
            if active_level_str not in state.tp_map:
                state.tp_map[active_level_str] = {}
            state.tp_map[active_level_str]["is_active"] = True
            return True

        # Динамический Smart TP множитель для импульсного рынка (если стратегия задает smart_tp)
        if hasattr(self.runtime_manager, "strategy_registry") and self.runtime_manager.strategy_registry:
            try:
                strat = self.runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
                if strat and getattr(strat, "smart_tp_cfg", None):
                    now = time.time()
                    eval_w = float(strat.smart_tp_cfg.get("eval_window_sec", 300))
                    recent = [t for t in getattr(state, "recent_closes", []) if now - t <= eval_w]
                    closed_cnt = len(recent)
                    if closed_cnt > 0:
                        tp_mult = MomentumEngine.calculate_smart_tp_multiplier(closed_cnt, strat.smart_tp_cfg)
                        if tp_mult > 1.0:
                            orig_tp = tp_indent
                            tp_indent = round(float(tp_indent) * tp_mult, 4)
                            logger.info(
                                f"[{symbol}][{strat.strategy_id}] {side} Smart TP x{tp_mult:.2f} applied: "
                                f"{orig_tp}% -> {tp_indent}% (recent closes: {closed_cnt})"
                            )
            except Exception as e:
                logger.debug(f"[{symbol}] Smart TP calculation error: {e}")

        # Строгое соблюдение Правила 11: минимальный тейк не менее 0.50%
        tp_indent = max(0.50, float(tp_indent))

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
                    
                    # Не вызываем save_cache здесь, sync_with_fsm сделает это
                    return True
                    
            logger.error(f"[{symbol}] Failed to place TP for {side}: {res.error_msg}")
            if attempt < max_retries:
                await asyncio.sleep(1.0)
                
        return False
