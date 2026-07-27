# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\RUNTIME_FSM\runtime_manager.py
# Role: runtime_manager.py module

# ==============================================================================
# Path: RUNTIME_FSM/runtime_manager.py
# Role: Менеджер рантайм-кешей и их синхронизации с состоянием позиций
# ==============================================================================

import json
import asyncio
from pathlib import Path
from typing import Dict, Any, List
from consts import DATA_DIR
from c_log import UnifiedLogger
from c_utils import Utils

import copy
logger = UnifiedLogger("RuntimeManager")
RUNTIME_DIR = DATA_DIR / "runtime"

class RuntimeFsmManager:
    """
    Менеджер для управления рантайм-кешами.
    """
    def __init__(self):
        self.caches: Dict[str, Dict[str, Any]] = {}
        self.locks: Dict[str, asyncio.Lock] = {}
        self.runtime_dir = RUNTIME_DIR
        self.backup_manager = None

    def load_initial_caches(self, symbols: List[str]):
        """
        Изначально грузим и фиксируем кеш рантайма "как есть" (as is).
        Здесь же проводим базовую валидацию конфигурации.
        """
        for sym in symbols:
            sym_lower = sym.lower()
            path = RUNTIME_DIR / f"{sym_lower}.json"
            if path.exists():
                data = Utils.read_json_file(path)
                
                # Валидация на совпадение количества усреднений и тейков
                for side in ["LONG", "SHORT"]:
                    if side in data:
                        grid = data[side].get("grid", {})
                        tp_map = data[side].get("tp_map", {})
                        if grid and tp_map and len(grid) != len(tp_map):
                            raise ValueError(f"[{sym}] {side} ОШИБКА КОНФИГУРАЦИИ: Количество уровней grid ({len(grid)}) не совпадает с количеством уровней tp_map ({len(tp_map)}). Проверьте настройки!")
                            
                self.caches[sym] = data
            else:
                self.caches[sym] = {}
            
            if sym not in self.locks:
                self.locks[sym] = asyncio.Lock()

    def populate_fsm_from_cache(self, fsm_states: dict):
        """
        Инициализируем PositionState из рантайм-кеша при старте (до REST-синхронизации).
        Копируем все переменные стейта из JSON.
        """
        for symbol, states in fsm_states.items():
            cache = self.caches.get(symbol, {})
            for side in ("LONG", "SHORT"):
                state = states.get(side)
                if not state:
                    continue
                side_cache = cache.get(side, {})
                if not side_cache:
                    continue
                    
                # Загружаем простые переменные
                state.total_volume = side_cache.get("total_volume", 0.0)
                state.avg_entry_price = side_cache.get("avg_entry_price", 0.0)
                state.pre_avg_price = side_cache.get("pre_avg_price", 0.0)
                state.initial_entry_price = side_cache.get("initial_entry_price", 0.0)
                state.open_time = side_cache.get("open_time", 0)
                state.next_avg_price = side_cache.get("next_avg_price")
                state.fallback_price = side_cache.get("fallback_price")
                
                state.in_position = side_cache.get("in_position", False)
                state.in_position_papper = side_cache.get("in_position_papper", False)
                state.is_finished = side_cache.get("is_finished", False)
                state.pending_avg = side_cache.get("pending_avg", False)
                state.pending_rolling_tp = side_cache.get("pending_rolling_tp", False)
                
                # Копируем словари
                state.grid = copy.deepcopy(side_cache.get("grid", {}))
                state.tp_map = copy.deepcopy(side_cache.get("tp_map", {}))
                
    # =========================================================================
    # СИНХРОНИЗАЦИЯ ПОСЛЕ ЗАПУСКА СТРИМА И СНЕПШОТОВ
    # =========================================================================

    async def sync_with_fsm(self, fsm_states: dict, force_save: bool = False):
        """
        Метод-синхронизатор. Сериализует актуальный PositionState обратно в json-кеш.
        Работает как "дамп" оперативной памяти на HDD.
        """
        for symbol, cache in self.caches.items():
            sym_states = fsm_states.get(symbol, {})
            needs_save = force_save
            
            for side in ("LONG", "SHORT"):
                state = sym_states.get(side)
                if not state:
                    continue
                
                if side not in cache:
                    cache[side] = {}
                side_cache = cache[side]
                
                # Формируем слепок текущего стейта
                new_state_snapshot = {
                    "total_volume": state.total_volume,
                    "avg_entry_price": state.avg_entry_price,
                    "pre_avg_price": state.pre_avg_price,
                    "initial_entry_price": state.initial_entry_price,
                    "open_time": state.open_time,
                    "next_avg_price": state.next_avg_price,
                    "fallback_price": state.fallback_price,
                    "in_position": state.in_position,
                    "in_position_papper": state.in_position_papper,
                    "is_finished": state.is_finished,
                    "pending_avg": state.pending_avg,
                    "pending_rolling_tp": state.pending_rolling_tp,
                    "grid": copy.deepcopy(state.grid),
                    "tp_map": copy.deepcopy(state.tp_map),
                }
                
                changed = False
                for k, v in new_state_snapshot.items():
                    if side_cache.get(k) != v:
                        changed = True
                        side_cache[k] = v
                
                if changed:
                    needs_save = True

            if needs_save:
                await self.save_cache(symbol)

    def reset_state_to_default(self, symbol: str, side: str, state):
        """
        Сбрасывает рантайм стейт позиции. Пользовательская конфигурация (инденты, объемы) 
        в grid и tp_map сохраняется, обнуляются только оперативные переменные (is_active, price и тд).
        """
        state.reset()
        logger.debug(f"[{symbol}] {side} FSM state reset to default (kept custom config).")

    async def save_cache(self, symbol: str):
        """Конкурентно-безопасное сохранение in-memory кеша на диск."""
        if symbol not in self.locks:
            self.locks[symbol] = asyncio.Lock()
            
        async with self.locks[symbol]:
            if symbol in self.caches:
                sym_lower = symbol.lower()
                path = RUNTIME_DIR / f"{sym_lower}.json"
                # Используем Utils для записи
                Utils.write_json_file(path, self.caches[symbol])
                if self.backup_manager:
                    self.backup_manager.mark_changed()
