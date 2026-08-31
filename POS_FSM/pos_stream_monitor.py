# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\POS_FSM\pos_stream_monitor.py
# Role: pos_stream_monitor.py module

# ==============================================================================
# Path: FSM/pos_stream_monitor.py
# Role: Тупой бухгалтер: только фиксирует total_volume и avg_entry_price, и на их основе переключает флаги in_position и is_finished. На этом его роль заканчивается.
# ==============================================================================

from typing import Dict, Tuple
from POS_FSM.models import PositionState
from c_log import UnifiedLogger
import time
import asyncio
from typing import Dict, Any, Tuple
from POS_FSM.models import PositionState
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("FSM_Monitor")
IS_SHOW_SIGNAL = False

class BinanceWsInterpreter:
    """
    Интерпретатор событий WebSocket (ACCOUNT_UPDATE).
    Преобразует сырые данные биржи и передает в PositionMonitor.
    """
    def __init__(self, monitor, target_symbols=None):
        self.monitor = monitor
        self.target_symbols = {s.upper() for s in target_symbols} if target_symbols else set()

    @staticmethod
    def _safe_float(val: Any, default: float = 0.0) -> float:
        try: return float(val) if val is not None else default
        except (ValueError, TypeError): return default

    async def process_message(self, event_data: Dict[str, Any]):
        event_type = event_data.get("e")
        if event_type == "ACCOUNT_UPDATE":
            positions = event_data.get("a", {}).get("P", [])
            for pos in positions:
                await self._handle_position_update(pos)

    async def _handle_position_update(self, p: Dict[str, Any]):
        raw_symbol = p.get("s", "")
        symbol = Utils.normalize_symbol(raw_symbol)
        if not symbol: return

        pos_side = str(p.get("ps", "")).upper()
        if not pos_side: return
        
        if pos_side == "BOTH":
            pa_val = self._safe_float(p.get("pa", 0.0))
            if pa_val > 0:
                pos_side = "LONG"
            elif pa_val < 0:
                pos_side = "SHORT"
            else:
                # Если позиция закрыта в BOTH, сбросим обе стороны для надежности,
                # либо ту, что была открыта (но у нас нет session_lock как в ARB боте).
                # Просто вызовем update_from_stream для LONG и SHORT.
                if self.target_symbols and symbol not in self.target_symbols:
                    return
                self.monitor.update_from_stream(symbol, "LONG", 0.0, 0.0)
                self.monitor.update_from_stream(symbol, "SHORT", 0.0, 0.0)
                return

        if pos_side not in ("LONG", "SHORT"):
            return

        if self.target_symbols and symbol not in self.target_symbols:
            return

        pos_amt = abs(self._safe_float(p.get("pa", 0.0)))
        entry = self._safe_float(p.get("ep", 0.0))

        self.monitor.update_from_stream(
            symbol=symbol,
            side=pos_side,
            pos_amt=pos_amt,
            entry_price=entry
        )
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
        self._last_rest_sync_time = 0.0
        self._cached_positions = None
        self._rest_lock = asyncio.Lock()

    async def sync_from_rest(self, client, symbols: list):
        """Запрашивает актуальные позиции по REST и инициализирует/синхронизирует FSM стейт."""
        async with self._rest_lock:
            now = time.monotonic()
            if now - self._last_rest_sync_time > 1.0 or self._cached_positions is None:
                self._cached_positions = await client.fetch_positions()
                self._last_rest_sync_time = time.monotonic()

        positions = self._cached_positions
        if not positions:
            # logger.warning("[REST] No positions returned (or API error), skipping sync_from_rest to prevent accidental wipe.")
            return

        binance_positions = {}
        for pos in positions:
            sym = pos.get("symbol")
            side = pos.get("positionSide")
            binance_positions[(sym, side)] = pos

        for sym in symbols:
            for side in ["LONG", "SHORT"]:
                pos = binance_positions.get((sym, side))
                if pos:
                    pos_amt = float(pos.get("positionAmt", 0))
                    entry_price = float(pos.get("entryPrice", 0))
                else:
                    pos_amt = 0.0
                    entry_price = 0.0
                
                # Check for desync before updating
                state = self.states.get(sym, {}).get(side)
                if state:
                    if abs(state.total_volume - pos_amt) > 1e-8 or abs(state.avg_entry_price - entry_price) > 1e-8:
                        if state.in_position or abs(pos_amt) > 0:
                            logger.warning(f"[REST_FALLBACK] Stream desync detected for {sym} {side}! WS missed an update. Forcing FSM update from REST. Local Qty: {state.total_volume}, Remote Qty: {pos_amt}")

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
            # Инвариант 1: если позиции на бирже нет, мы обязаны стереть ВСЕ следы в кэше
            # Даже если in_position УЖЕ False, но в памяти остались якоря (initial_entry_price, fallback_price)
            if state.in_position or state.initial_entry_price > 0 or state.fallback_price is not None:
                if IS_SHOW_SIGNAL:
                    logger.debug(f"[MONITOR] POSITION CLOSED or DIRTY CACHE WIPED {symbol} {side}")
                # Вызов is_finished = True запустит полную очистку в _check_and_reset_finished_positions (включая state.reset())
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
