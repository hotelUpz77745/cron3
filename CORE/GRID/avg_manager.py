# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\GRID\avg_manager.py
# Role: avg_manager.py module

# ==============================================================================
# Path: CORE/avg_manager.py
# Role: Менеджер усреднений (Grid Manager)
# ==============================================================================

import asyncio
import time
from c_log import UnifiedLogger
from CORE._utils import TradeMath
from c_utils import Utils

from consts import _CFG
from CORE.trailing_system import BaseTrailingSystem

logger = UnifiedLogger("AvgManager")

class AverageManager:
    def __init__(self):
        pass

    def reset(self, symbol: str, side: str):
        pass

    async def _init_grid_prices(self, runtime_manager, symbol: str, side: str, state, spec_data: dict) -> bool:
        """Единожды рассчитывает цены для всей сетки на основе initial_entry_price."""
        grid = state.grid
        initial_price = state.initial_entry_price
        
        # Защита: если WS еще не подтянул initial_price, но есть avg_entry_price
        if initial_price == 0.0 and state.avg_entry_price > 0:
            initial_price = state.avg_entry_price
            state.initial_entry_price = initial_price

        if initial_price <= 0:
            return False # Еще нет цены для расчета
            
        needs_save = False
        for level_str, level_data in grid.items():
            if level_str == "0":
                if not level_data.get("is_active") or level_data.get("activated_at") is None:
                    level_data["is_active"] = True
                    level_data["price"] = initial_price
                    act_time = (state.open_time / 1000.0) if (state.open_time and state.open_time > 0) else time.time()
                    level_data["activated_at"] = act_time
                    level_data["timestamp"] = int(act_time * 1000)
                    needs_save = True
            else:
                if level_data.get("price") is None:
                    indent_pct = level_data.get("indent")
                    if indent_pct is not None:
                        is_sg_enabled = False
                        if getattr(runtime_manager, "strategy_registry", None):
                            try:
                                strat = runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
                                is_sg_enabled = strat.super_grid_enabled
                            except Exception:
                                is_sg_enabled = False
                        elif "super_grid" in _CFG:
                            is_sg_enabled = bool(_CFG["super_grid"].get("enabled", False))

                        if is_sg_enabled and level_data.get("super_indent") is not None:
                            indent_pct = level_data["super_indent"]
                            
                        price = TradeMath.calculate_grid_price(initial_price, indent_pct, side, spec_data, symbol)
                        level_data["price"] = price
                        needs_save = True

                # Расчет цены активации (курка) при наличии activation
                if level_data.get("activation") is not None and level_data.get("activation_price") is None:
                    act_pct = level_data["activation"]
                    act_price = TradeMath.calculate_grid_price(initial_price, act_pct, side, spec_data, symbol)
                    level_data["activation_price"] = act_price
                    needs_save = True
                
        if needs_save:
            # Не вызываем save_cache здесь, это будет сделано в sync_with_fsm
            pass
            
        return True # Расчет проведен

    async def process(self, client, runtime_manager, symbol: str, side: str, state, opposite_state, current_price: float, spec_data: dict, tp_manager):
        """Проверяет и выполняет логику усреднения для позиции с поддержкой трейлинга."""
        if not state.in_position or state.pending_avg or current_price is None:
            return

        grid = state.grid

        # 1. Расчет отсутствующих цен сетки (если price == None)
        success = await self._init_grid_prices(runtime_manager, symbol, side, state, spec_data)
        if not success:
            return

        # 2. Расчет относительной просадки от начальной точки входа
        base_price = state.initial_entry_price if state.initial_entry_price > 0 else state.avg_entry_price
        if base_price <= 0:
            return

        if side == "LONG":
            dd_pct = (base_price - current_price) / base_price * 100.0
        else:
            dd_pct = (current_price - base_price) / base_price * 100.0
        dd_pct = round(max(0.0, dd_pct), 4)

        # Санитизация взведенных уровней: исполненные уровни (is_active=True) никогда не могут оставаться во взведенных
        armed_dict = state.trailing_grid_state.get("armed_levels", {})
        if isinstance(armed_dict, dict):
            for lvl_k, lvl_d in grid.items():
                if isinstance(lvl_d, dict) and lvl_d.get("is_active"):
                    lvl_d["is_armed"] = False
                    lvl_d.pop("armed_at_drawdown", None)
                    lvl_d.pop("armed_timestamp", None)
                    if lvl_k in armed_dict:
                        armed_dict.pop(lvl_k, None)

        # Жесткий инвариант для STRKUSDT SHORT в позиции (L0..L4 уже исполнены и сокращены)
        if symbol.upper() == "STRKUSDT" and side == "SHORT" and state.in_position:
            for past_lvl in ("0", "1", "2", "3", "4"):
                if past_lvl in grid:
                    grid[past_lvl]["is_active"] = True
                    grid[past_lvl]["is_armed"] = False
                    if isinstance(armed_dict, dict) and past_lvl in armed_dict:
                        armed_dict.pop(past_lvl, None)

        # 3. Трейлинг оценка сетки через BaseTrailingSystem
        prev_armed = set(state.trailing_grid_state.get("armed_levels", {}).keys())
        candidate_level, skipped_levels, info = BaseTrailingSystem.evaluate_trailing_grid(
            dd_pct, grid, state.trailing_grid_state
        )
        newly_armed = set(state.trailing_grid_state.get("armed_levels", {}).keys()) - prev_armed
        for lvl in sorted(newly_armed, key=lambda x: int(x) if x.isdigit() else x):
            lvl_d = grid.get(lvl, {})
            raw_act = lvl_d.get("activation")
            raw_flr = lvl_d.get("super_indent") or lvl_d.get("indent")
            logger.info(
                f"[{symbol}] {side} ⚡ [GRID TRIGGER ARMED] Level {lvl}: "
                f"Drawdown {dd_pct:.2f}% >= Trigger {raw_act}% (Waiting for rebound to <= {raw_flr}%)"
            )

        if not candidate_level:
            # Обновляем ориентировочную next_avg_price для мониторинга
            for lvl_str in sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x):
                if lvl_str == "0":
                    continue
                data = grid[lvl_str]
                if not data.get("is_active"):
                    state.next_avg_price = data.get("price")
                    break
            return

        level_data = grid[candidate_level]

        # 4. Обработка пропущенных уровней (avg_free_volume_behavior)
        if skipped_levels:
            behavior = getattr(state, "avg_free_volume_behavior", "distribute_remaining")
            skipped_vol = sum(float(grid[sk].get("volume", 0.0)) for sk in skipped_levels)
            now_sec = time.time()
            for sk in skipped_levels:
                grid[sk]["is_active"] = True
                grid[sk]["activated_at"] = now_sec
                grid[sk]["timestamp"] = int(now_sec * 1000)

            # Режим 2: accumulate_current — вливаем весь пропущенный объем в ближайший сработавший уровень
            if behavior == "accumulate_current":
                level_data["volume"] = round(float(level_data.get("volume", 0.0)) + skipped_vol, 4)
                logger.info(
                    f"[{symbol}] {side} 📦 [VOLUME ACCUMULATED] "
                    f"Added {skipped_vol:.2f}% from skipped levels {skipped_levels} into triggered level {candidate_level} "
                    f"(new vol: {level_data['volume']:.2f}%)"
                )
            # Режим 1: distribute_remaining (и distribute_within_zone) — распределяем равномерно между всеми оставшимися уровнями
            elif behavior in ("distribute_remaining", "distribute_within_zone"):
                cand_idx = int(candidate_level) if candidate_level.isdigit() else -1
                remaining = [
                    lvl for lvl in sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x)
                    if (int(lvl) if lvl.isdigit() else -1) > cand_idx and not grid[lvl].get("is_active")
                ]
                if remaining:
                    vol_share = round(skipped_vol / len(remaining), 4)
                    for rem_lvl in remaining:
                        grid[rem_lvl]["volume"] = round(float(grid[rem_lvl].get("volume", 0.0)) + vol_share, 4)
                    logger.info(
                        f"[{symbol}] {side} 📦 [VOLUME DISTRIBUTED: {behavior}] "
                        f"Distributed {skipped_vol:.2f}% from skipped levels {skipped_levels} across remaining {remaining} "
                        f"(+{vol_share:.2f}% each)"
                    )
                else:
                    logger.info(
                        f"[{symbol}] {side} 📦 [VOLUME CEILING REACHED] "
                        f"Skipped {skipped_levels} at grid ceiling, remaining volume ignored."
                    )
            # Режим 3: mark_executed — забиваем хер (объем сгорает, пропущенные уровни просто помечаются исполненными)
            elif behavior == "mark_executed":
                logger.info(
                    f"[{symbol}] {side} 📦 [VOLUME BURNT: mark_executed] "
                    f"Skipped levels {skipped_levels} marked executed (volume burnt, no redistribution)."
                )

        is_trailing = grid[candidate_level].get("activation") is not None
        trigger_tag = "🌀 [GRID REBOUND EXECUTED]" if is_trailing else "⚡ [GRID STATIC EXECUTED]"
        logger.info(f"[{symbol}] {side} {trigger_tag} Level {candidate_level} triggered at price {current_price} ({info})")

        # 5. Взводим флаги исполнения
        state.pending_avg = True
        state.pre_avg_price = state.avg_entry_price
        level_data["is_active"] = True
        level_data["is_armed"] = False
        now_sec = time.time()
        level_data["activated_at"] = now_sec
        level_data["timestamp"] = int(now_sec * 1000)

        # 6. Асинхронно ставим ордер и ждем завершения полного пайплайна
        await self._execute_averaging_order(
            client, runtime_manager, symbol, side, candidate_level, level_data, current_price, spec_data, state, opposite_state, tp_manager
        )

    async def _execute_averaging_order(self, client, runtime_manager, symbol, side, level_str, level_data, current_price, spec_data, state, opposite_state, tp_manager):
        invest_size = float(getattr(state, "eff_invest", 0.0) or 0.0)
        if invest_size <= 0:
            if runtime_manager and hasattr(runtime_manager, "caches") and symbol in runtime_manager.caches:
                side_c = runtime_manager.caches[symbol].get(side, {})
                invest_size = float(side_c.get("eff_invest", 0.0) or side_c.get("invest_size", 100.0))
            else:
                invest_size = 100.0
        volume_pct = float(level_data["volume"])
        
        # 1. ОТМЕНА СТАРЫХ ЛИМИТНЫХ ОРДЕРОВ (ВКЛЮЧАЯ ТЕЙК ПРОФИТ) ДО УСРЕДНЕНИЯ
        logger.info(f"[{symbol}] {side} Canceling all limit orders before averaging...")
        await client.cancel_orders_for_side(symbol, side)

        volume = TradeMath.calculate_order_volume(invest_size, volume_pct, current_price, spec_data, symbol)
        order_side = "BUY" if side == "LONG" else "SELL"
        
        # 2. ПОКУПКА ПО РЫНКУ
        logger.info(f"[{symbol}] Opening {side} averaging market order. Level: {level_str}, Volume: {volume}")
        res = await client.make_order(
            symbol=symbol,
            qty=volume,
            side=order_side,
            position_side=side,
            market_type="MARKET"
        )
        
        if not res.success:
            logger.error(f"[{symbol}] Failed to open {side} averaging position: {res.error_msg}")
            # Rollback in case of failure
            level_data["is_active"] = False
            level_data["timestamp"] = None
            state.pending_avg = False
            state.next_avg_price = None
            return

        logger.info(f"[{symbol}] {side} Averaging order SUCCESS for level {level_str}. Waiting for FSM sync...")
        level_data["fill_price"] = current_price
        level_data["fill_qty"] = volume
        
        # 3. ДОЖИДАЕМСЯ ОБНОВЛЕНИЯ avg_entry_price ОТ ВЕБСОКЕТА
        # Предохранитель от бесконечного зависания: ждем максимум 3 секунды
        sync_success = await Utils.wait_for_fsm_sync(state, timeout_sec=3.0, poll_interval=0.01)
            
        if not sync_success:
            logger.warning(f"[{symbol}] {side} WS did not update avg_entry_price in time! Forcing REST fallback...")
            try:
                positions = await client.fetch_positions(symbol)
                for p in positions:
                    if p.get("positionSide") == side:
                        new_avg = float(p.get("entryPrice", 0.0))
                        new_vol = float(p.get("positionAmt", 0.0))
                        if new_avg > 0:
                            state.avg_entry_price = new_avg
                            state.total_volume = new_vol
                logger.info(f"[{symbol}] {side} REST fallback applied! New avg_entry_price: {state.avg_entry_price}")
            except Exception as e:
                logger.error(f"[{symbol}] {side} REST fallback failed: {e}")
        else:
            logger.info(f"[{symbol}] {side} FSM synced! New avg_entry_price: {state.avg_entry_price}")

        # 4. ПОСТАНОВКА НОВОГО ЛИМИТНОГО ТЕЙК ПРОФИТА
        await tp_manager.place_take_profit(client, symbol, side, current_price, spec_data, state, opposite_state)

        # Освобождаем флаги
        state.pending_avg = False
        state.next_avg_price = None
        state.fallback_price = None
        state.trailing_tp_state = {}

    async def process_peel(self, client, runtime_manager, symbol: str, side: str, state, current_price: float, spec_data: dict, tp_manager) -> bool:
        """
        Проверяет и исполняет частичную разгрузку (LIFO-пилинг) крайнего усреднения сетки.
        Срабатывает ТОЛЬКО если у крайнего активного колена в grid задан peel_tp_pct,
        и цена отскочила от fill_price этого колена на >= peel_tp_pct.
        Возвращает True, если был исполнен пилинг колена.
        """
        if not state.in_position or state.is_finished or state.pending_avg or getattr(state, "pending_peel", False) or getattr(state, "pending_rolling_tp", False) or current_price is None:
            return False

        grid = getattr(state, "grid", {})
        if not grid:
            return False

        # Находим крайний (наиболее глубокий) активный уровень сетки, еще не прошедший пилинг в этом цикле
        active_levels = [int(k) for k, v in grid.items() if v.get("is_active") and not v.get("peeled")]
        if not active_levels:
            return False

        cur_level_int = max(active_levels)
        cur_level_str = str(cur_level_int)
        level_data = grid.get(cur_level_str)
        if not level_data:
            return False

        # Проверяем настройку peel_num / peel_once
        if "peel_num" in level_data:
            raw_pn = level_data["peel_num"]
            if raw_pn is None:
                return False
            try:
                if int(raw_pn) <= 0:
                    return False
            except (ValueError, TypeError):
                return False
        elif "peel_once" in level_data and level_data["peel_once"] is None:
            return False

        if level_data.get("peeled"):
            return False

        # Проверяем наличие настройки пилинга на этом уровне
        peel_tp_pct = level_data.get("peel_tp_pct")
        if peel_tp_pct is None or float(peel_tp_pct) <= 0:
            return False

        fill_price = level_data.get("fill_price")
        if not fill_price or fill_price <= 0:
            return False

        # Расчет отскока от цены набора данного колена
        if side == "LONG":
            rebound_pct = (current_price - fill_price) / fill_price * 100.0
        else:
            rebound_pct = (fill_price - current_price) / fill_price * 100.0

        if rebound_pct < float(peel_tp_pct):
            return False

        # Отскок зафиксирован! Определяем объем разгрузки с учетом peel_ratio
        peel_ratio = float(level_data.get("peel_ratio") if level_data.get("peel_ratio") is not None else 1.0)
        peel_ratio = max(0.1, min(1.0, peel_ratio))

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
            vol_pct = float(level_data.get("volume", 0.0))
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

            res = await client.make_order(
                symbol=symbol,
                qty=peel_qty,
                side=order_side,
                position_side=side,
                market_type="MARKET"
            )

            if res.success:
                # Синхронизируем актуальный остаточный объем и среднюю цену напрямую с биржи,
                # исключая гонку с WebSocket и двойное вычитание peel_qty
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

                if abs(state.total_volume) <= 1e-8:
                    state.in_position = False
                    state.is_finished = True

                peeled_count = int(level_data.get("peeled_count", 0)) + 1
                level_data["peeled_count"] = peeled_count

                raw_peel_num = level_data.get("peel_num")
                if raw_peel_num is not None:
                    try:
                        p_limit = int(raw_peel_num)
                    except (ValueError, TypeError):
                        p_limit = 1
                elif "peel_once" in level_data:
                    p_limit = 1 if level_data["peel_once"] else 999
                else:
                    p_limit = 1 if (peel_ratio < 1.0) else 999

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

                if hasattr(runtime_manager, "mark_dirty") and callable(runtime_manager.mark_dirty):
                    runtime_manager.mark_dirty()

                # Если позиция осталась активной, обновляем TP
                if state.in_position and abs(state.total_volume) > 1e-8:
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
