# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\TP\tp_manager.py
# Role: tp_manager.py module

# ==============================================================================
# Path: CORE/tp_manager.py
# Role: Менеджер постановки лимитных тейк-профит ордеров
# ==============================================================================

from c_log import UnifiedLogger
from CORE._utils import TradeMath

from CORE._utils import RiskCalculatingUtils
import asyncio
logger = UnifiedLogger("TakeProfitManager")

class TakeProfitManager:
    def __init__(self, runtime_manager):
        self.runtime_manager = runtime_manager

    async def place_take_profit(self, client, symbol: str, side: str, current_price: float, spec_data: dict, state, volume: float = None):
        """Расчет и постановка лимитного TP ордера с ретраями и предварительной отменой."""
        
        # Предварительно отменяем все лимитки для этой стороны
        logger.info(f"[{symbol}] {side} Canceling old limit orders before placing new TP...")
        await client.cancel_orders_for_side(symbol, side)

        grid = state.grid
        current_level_str = RiskCalculatingUtils.get_current_grid_level(grid)
        
        tp_map = state.tp_map
        current_tp = tp_map.get(current_level_str)
        if not current_tp:
            logger.error(f"[{symbol}] {side} Missing TP config for level {current_level_str}")
            return False
            
        tp_indent = current_tp.get("indent")
        if tp_indent is None:
            logger.info(f"[{symbol}] {side} TP indent is null. Skipping LIMIT order placement to rely on Fallback TP.")
            if current_level_str not in state.tp_map:
                state.tp_map[current_level_str] = {}
            state.tp_map[current_level_str]["is_active"] = True
            return True
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
        for attempt in range(1, max_retries + 1):
            logger.info(f"[{symbol}] Placing {side} take profit limit order at {tp_price}. Vol: {volume} (Attempt {attempt}/{max_retries})")
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
                    if current_level_str not in state.tp_map:
                        state.tp_map[current_level_str] = {}
                        
                    state.tp_map[current_level_str]["is_active"] = True
                    
                    # Не вызываем save_cache здесь, sync_with_fsm сделает это
                    return True
                    
            logger.error(f"[{symbol}] Failed to place TP for {side}: {res.error_msg}")
            if attempt < max_retries:
                await asyncio.sleep(1.0)
                
        return False
