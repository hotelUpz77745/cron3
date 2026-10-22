# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\TP\fallback_tp_manager.py
# Role: fallback_tp_manager.py module

# ==============================================================================
# Path: CORE/TP/fallback_tp_manager.py
# Role: Менеджер страховочного тейк-профита (Fallback Market Hit)
# ==============================================================================

import time
from c_log import UnifiedLogger
from CORE._utils import TradeMath, RiskCalculatingUtils, TakeProfitConfigResolver
from CORE.STRATEGY.trailing_system import BaseTrailingSystem

logger = UnifiedLogger("FallbackMarketTP")

class FallbackTpManager:
    def __init__(self):
        pass

    async def process(self, client, runtime_manager, symbol: str, side: str, state, current_price: float, spec_data: dict, opposite_state=None):
        """Проверяет пробитие страховочной цены (Fallback TP) или срабатывание Trailing TP и бьет по рынку."""
        if not state.in_position or state.is_finished or current_price is None or state.avg_entry_price <= 0:
            return

        # Предохранитель от гонок: если уже летит маркет-ордер фолбека
        if state.pending_rolling_tp:
            return

        # 1. Трейлинг Тейк-Профит: проверяем наличие условий трейлинга в tp_map (наличие activation/trailing) или взведенный пол
        tp_map_vals = (getattr(state, "tp_map", {}) or {}).values()
        has_trailing_tp = (
            bool(getattr(state, "trailing_tp_state", {}).get("armed_floor"))
            or any(isinstance(v, dict) and ("activation" in v or "trailing" in v) for v in tp_map_vals)
        )
        if has_trailing_tp:
            gain_pct = ((current_price - state.avg_entry_price) / state.avg_entry_price * 100.0) if side == "LONG" else ((state.avg_entry_price - current_price) / state.avg_entry_price * 100.0)
            
            active_grid_lvl = RiskCalculatingUtils.get_current_grid_level(state.grid)
            if state.tp_purpose == "opposite" and opposite_state:
                active_grid_lvl = RiskCalculatingUtils.get_current_grid_level(opposite_state.grid)
            elif state.tp_purpose == "both" and opposite_state:
                opp_lvl = RiskCalculatingUtils.get_current_grid_level(opposite_state.grid)
                active_grid_lvl = str(max(int(active_grid_lvl), int(opp_lvl)))

            prev_tp_floor = state.trailing_tp_state.get("armed_floor")
            tp_hit, tp_floor, tp_info = BaseTrailingSystem.evaluate_trailing_tp(
                gain_pct=gain_pct,
                tp_map=state.tp_map,
                state_trailing=state.trailing_tp_state,
                tp_purpose=state.tp_purpose,
                active_grid_level=active_grid_lvl,
            )
            new_tp_floor = state.trailing_tp_state.get("armed_floor")
            if prev_tp_floor is None and new_tp_floor is not None:
                act_lvl = state.trailing_tp_state.get("active_level", active_grid_lvl)
                raw_act = state.tp_map.get(act_lvl, {}).get("activation")
                logger.warning(
                    f"[{symbol}] {side} ⚡ [TRAILING TP ARMED] "
                    f"Level {act_lvl}: Gain {gain_pct:.2f}% >= Trigger {raw_act}% (Trailing floor: {new_tp_floor:.2f}%)"
                )

            if tp_hit:
                state.pending_rolling_tp = True
                logger.info(f"[{symbol}] {side} Canceling all limit orders before executing Trailing TP...")
                await client.cancel_orders_for_side(symbol, side)

                volume_float = abs(state.total_volume)
                volume = TradeMath.round_qty(volume_float, spec_data, symbol)
                order_side = "SELL" if side == "LONG" else "BUY"

                logger.warning(f"[{symbol}] {side} 🎯 [TRAILING TP TRIGGERED] {tp_info}. Price: {current_price}, Gain: {gain_pct:.2f}%. Closing all.")

                res = await client.make_order(
                    symbol=symbol,
                    qty=volume,
                    side=order_side,
                    position_side=side,
                    market_type="MARKET"
                )

                if res.success:
                    logger.info(f"[{symbol}] Trailing TP Market Order successfully executed.")
                    state.last_closed_volume = volume_float
                    state.last_closed_avg_price = state.avg_entry_price
                    state.last_closed_timestamp = time.time()
                    state.is_finished = True
                    state.trailing_tp_state = {}
                    if isinstance(state.tp_map, dict):
                        for lvl_d in state.tp_map.values():
                            if isinstance(lvl_d, dict):
                                lvl_d["is_armed"] = False
                                lvl_d.pop("armed_at_gain", None)
                                lvl_d.pop("armed_floor", None)
                                lvl_d.pop("armed_timestamp", None)
                    if isinstance(state.grid, dict):
                        for lvl_d in state.grid.values():
                            if isinstance(lvl_d, dict):
                                lvl_d["is_armed"] = False
                                lvl_d.pop("armed_at_drawdown", None)
                                lvl_d.pop("armed_timestamp", None)
                else:
                    if res.error_code == -2022 or "-2022" in str(res.error_msg):
                        logger.warning(f"[{symbol}] Trailing TP Market Order rejected with -2022 (ReduceOnly). Position is already closed by Limit TP. Assuming success.")
                        state.is_finished = True
                        state.trailing_tp_state = {}
                        if isinstance(state.tp_map, dict):
                            for lvl_d in state.tp_map.values():
                                if isinstance(lvl_d, dict):
                                    lvl_d["is_armed"] = False
                                    lvl_d.pop("armed_at_gain", None)
                                    lvl_d.pop("armed_floor", None)
                                    lvl_d.pop("armed_timestamp", None)
                        if isinstance(state.grid, dict):
                            for lvl_d in state.grid.values():
                                if isinstance(lvl_d, dict):
                                    lvl_d["is_armed"] = False
                                    lvl_d.pop("armed_at_drawdown", None)
                                    lvl_d.pop("armed_timestamp", None)
                    else:
                        logger.error(f"[{symbol}] Failed to execute Trailing TP Market Order: {res.error_msg}")

                state.pending_rolling_tp = False
                return

        # 2. Детерминация цены фолбека (кешируем только прайс для горячего пути)
        if state.fallback_price is None:
            if state.avg_entry_price <= 0:
                return # Ждем пока сенсоры синхронизируют цену входа
            
            tp_map = state.tp_map
            level_self = RiskCalculatingUtils.get_current_grid_level(state.grid)
            
            if state.tp_purpose == "opposite" and opposite_state:
                level_opp = RiskCalculatingUtils.get_current_grid_level(opposite_state.grid)
                current_level = level_opp
            elif state.tp_purpose == "both" and opposite_state:
                level_opp = RiskCalculatingUtils.get_current_grid_level(opposite_state.grid)
                current_level = str(max(int(level_self), int(level_opp)))
            elif state.tp_purpose == "neutral":
                current_level = level_self if level_self in tp_map else "0"
            else:
                current_level = level_self
                
            current_level = TakeProfitConfigResolver.get_nearest_level_key(current_level, tp_map)
            if current_level not in tp_map:
                return
                
            fallback_indent_pct = tp_map[current_level].get("fallback_indent")
            
            # Если fallback_indent не задан (null), пропускаем проверку
            if fallback_indent_pct is None:
                return

            fb_price = TradeMath.calculate_take_profit_price(
                state.avg_entry_price, fallback_indent_pct, side, spec_data, symbol
            )
            state.fallback_price = fb_price

        # 2. Горячий путь (O(1) сравнение)
        triggered = False
        if side == "LONG" and current_price >= state.fallback_price:
            triggered = True
        elif side == "SHORT" and current_price <= state.fallback_price:
            triggered = True

        if triggered:
            # Предохранитель от повторного срабатывания (чтоб 100 раз не переебнуться)
            state.pending_rolling_tp = True
            
            # ОТМЕНА ВСЕХ ЛИМИТНЫХ ОРДЕРОВ ЧТОБЫ ОСВОБОДИТЬ КВОТУ ДЛЯ MARKET REDUCE-ONLY
            logger.info(f"[{symbol}] {side} Canceling all limit orders before executing Fallback Market...")
            await client.cancel_orders_for_side(symbol, side)
            
            volume_float = abs(state.total_volume)
            volume = TradeMath.round_qty(volume_float, spec_data, symbol)
            order_side = "SELL" if side == "LONG" else "BUY"

            logger.warning(f"[{symbol}] {side} FALLBACK MARKET TRIGGERED! Price {current_price} crossed {state.fallback_price}. Closing all.")
            
            res = await client.make_order(
                symbol=symbol,
                qty=volume,
                side=order_side,
                position_side=side,
                market_type="MARKET"
            )

            if res.success:
                logger.info(f"[{symbol}] Fallback Market Order successfully executed.")
                state.last_closed_volume = volume_float
                state.last_closed_avg_price = state.avg_entry_price
                state.last_closed_timestamp = time.time()
                state.is_finished = True  # Только теперь говорим оркестратору, что всё
            else:
                if res.error_code == -2022 or "-2022" in str(res.error_msg):
                    logger.warning(f"[{symbol}] Fallback Market Order rejected with -2022 (ReduceOnly). Position is already closed by Limit TP. Assuming success.")
                    state.is_finished = True
                else:
                    logger.error(f"[{symbol}] Failed to execute Fallback Market Order: {res.error_msg}")
            
            state.pending_rolling_tp = False  # Снимаем блокировку
