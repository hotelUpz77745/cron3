# ==============================================================================
# Path: FSM/pos_stream_monitor.py
# Role: Тупой бухгалтер: только фиксирует total_volume и avg_entry_price, и на их основе переключает флаги in_position и is_finished. На этом его роль заканчивается.
# ==============================================================================

from typing import Dict, Tuple
from POS_FSM.models import PositionState
from c_log import UnifiedLogger

logger = UnifiedLogger("FSM_Monitor")
IS_SHOW_SIGNAL = False

class PositionMonitor:
    def __init__(self, states_cache: Dict[str, Dict[str, PositionState]], target_symbols: list = None):
        """
        states_cache - ссылка на кеш состояний из торгового ядра (например, BotCore.fsm_states).
        Формат: {symbol: {"LONG": PositionState, "SHORT": PositionState}}
        """
        self.states = states_cache
        if target_symbols:
            for symbol in target_symbols:
                if symbol not in self.states:
                    self.states[symbol] = {
                        "LONG": PositionState(symbol=symbol, side="LONG"),
                        "SHORT": PositionState(symbol=symbol, side="SHORT")
                    }

    async def sync_from_rest(self, client, symbols: list):
        """Запрашивает актуальные позиции по REST и инициализирует/синхронизирует FSM стейт."""
        # logger.debug("[REST] Syncing active positions as failsafe...")
        positions = await client.fetch_positions()
        for pos in positions:
            sym = pos.get("symbol")
            if sym in symbols:
                side = pos.get("positionSide")
                pos_amt = float(pos.get("positionAmt", 0))
                entry_price = float(pos.get("entryPrice", 0))
                self.update_from_stream(sym, side, pos_amt, entry_price)

    def update_from_stream(self, symbol: str, side: str, pos_amt: float, entry_price: float):
        if symbol not in self.states:
            self.states[symbol] = {
                "LONG": PositionState(symbol=symbol, side="LONG"),
                "SHORT": PositionState(symbol=symbol, side="SHORT")
            }
            
        state = self.states[symbol].get(side)
        if not state:
            return

        # Фиксируем количество и среднюю цену (разрешаем отрицательный total_volume)
        state.total_volume = pos_amt
        state.avg_entry_price = entry_price
        
        # logger.debug(f"[MONITOR] update_from_stream called for {symbol} {side} - qty: {pos_amt}, avg: {entry_price}")

        # Проверка наличия позиции (по модулю объема)
        if abs(pos_amt) == 0:
            if state.in_position:
                if IS_SHOW_SIGNAL:
                    logger.debug(f"[MONITOR] POSITION CLOSED {symbol} {side}")
                # Позиция закрыта
                state.is_finished = True
                state.set_in_position(False)
        else:
            if not state.in_position:
                if IS_SHOW_SIGNAL:
                    logger.debug(f"[MONITOR] NEW POSITION {symbol} {side} at {entry_price}")
                # Сбрасываем кэши от прошлых сделок, если позиция была открыта вручную или из-за рассинхрона
                state.fallback_price = None
                state.next_avg_price = None
                state.initial_entry_price = 0.0
            
            # Позиция активна: set_in_position(True) автоматически сбросит in_position_papper в False
            state.set_in_position(True)
            state.is_finished = False

        if IS_SHOW_SIGNAL:
            logger.debug(
                f"[MONITOR] UPDATE {symbol} {side} - qty: {pos_amt}, avg_price: {entry_price}",
                throttle_sec=60,
                throttle_key=f"monitor_update_{symbol}_{side}"
            )
