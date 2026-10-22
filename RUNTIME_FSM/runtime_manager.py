# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\RUNTIME_FSM\runtime_manager.py
# Role: runtime_manager.py module

# ==============================================================================
# Path: RUNTIME_FSM/runtime_manager.py
# Role: Менеджер рантайм-кешей и их синхронизации с состоянием позиций
# ==============================================================================

import json
import pickle
import asyncio
import copy
from pathlib import Path
from typing import Dict, Any, List, Optional
from consts import DATA_DIR
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("RuntimeManager")
RUNTIME_DIR = DATA_DIR / "runtime"

class RuntimeFsmManager:
    """
    Менеджер для управления рантайм-кешами под куполами стратегий (вселенных).
    """
    def __init__(self, strategy_registry: Optional[Any] = None):
        self.caches: Dict[str, Dict[str, Any]] = {}
        self.locks: Dict[str, asyncio.Lock] = {}
        self.runtime_dir = RUNTIME_DIR
        self.backup_manager = None
        self.strategy_registry = strategy_registry

    def get_runtime_path(self, symbol: str, default_ext: str = ".json") -> Path:
        """
        Возвращает путь к рантайм-файлу позиции под куполом назначенной стратегии.
        Приоритет: .json (прозрачный читаемый рантайм на жестком диске), fallback: .pkl (миграция).
        Пример: CFG/runtime/u16_chita__classic/strkusdt.json
        """
        sym_lower = symbol.lower()
        if self.strategy_registry:
            strat_id = self.strategy_registry.get_strategy_id_for_symbol(symbol)
            if strat_id:
                cand_json = self.runtime_dir / strat_id / f"{sym_lower}.json"
                cand_pkl = self.runtime_dir / strat_id / f"{sym_lower}.pkl"
                if cand_json.exists():
                    return cand_json
                if cand_pkl.exists():
                    return cand_pkl
                return self.runtime_dir / strat_id / f"{sym_lower}{default_ext}"

        # Поиск по подпапкам стратегий
        if self.runtime_dir.exists():
            for sub in self.runtime_dir.iterdir():
                if sub.is_dir():
                    cand_json = sub / f"{sym_lower}.json"
                    cand_pkl = sub / f"{sym_lower}.pkl"
                    if cand_json.exists():
                        return cand_json
                    if cand_pkl.exists():
                        return cand_pkl

        cand_root_json = self.runtime_dir / f"{sym_lower}.json"
        cand_root_pkl = self.runtime_dir / f"{sym_lower}.pkl"
        if cand_root_json.exists():
            return cand_root_json
        if cand_root_pkl.exists():
            return cand_root_pkl

        return self.runtime_dir / f"{sym_lower}{default_ext}"

    def load_initial_caches(self, symbols: List[str]):
        """
        Изначально грузим и фиксируем кеш рантайма из папки стратегии (.json или .pkl).
        Здесь же проводим базовую валидацию конфигурации.
        """
        for sym in symbols:
            path = self.get_runtime_path(sym, default_ext=".json")
            if path.exists():
                if str(path).endswith(".json"):
                    data = Utils.read_json_file(path)
                    # Если остался старый .pkl, удаляем его
                    pkl_target = path.with_suffix(".pkl")
                    if pkl_target.exists():
                        try:
                            pkl_target.unlink()
                        except Exception:
                            pass
                elif str(path).endswith(".pkl"):
                    try:
                        with open(path, "rb") as fp:
                            data = pickle.load(fp)
                    except Exception as e:
                        logger.error(f"[{sym}] Failed to load .pkl runtime: {e}, attempting json fallback")
                        json_path = path.with_suffix(".json")
                        data = Utils.read_json_file(json_path) if json_path.exists() else {}
                    # Миграция: сохраняем в прозрачный .json и удаляем старый .pkl
                    json_target = path.with_suffix(".json")
                    Utils.atomic_write_json(json_target, data, indent=4)
                    try:
                        path.unlink()
                    except Exception:
                        pass
                else:
                    data = Utils.read_json_file(path)
                
                self.caches[sym] = data
            else:
                self.caches[sym] = {}
            
            if sym not in self.locks:
                self.locks[sym] = asyncio.Lock()

    def populate_fsm_from_cache(self, fsm_states: dict):
        """
        Инициализируем PositionState из рантайм-кеша при старте (до REST-синхронизации).
        Использует нативный метод десериализации PositionState.update_from_dict.
        """
        for symbol, states in fsm_states.items():
            cache = self.caches.get(symbol, {})
            for side in ("LONG", "SHORT"):
                state = states.get(side)
                if not state:
                    continue
                side_cache = cache.get(side)
                if side_cache and isinstance(side_cache, dict):
                    state.update_from_dict(side_cache)

    # =========================================================================
    # СИНХРОНИЗАЦИЯ ПОСЛЕ ЗАПУСКА СТРИМА И СНЕПШОТОВ
    # =========================================================================

    async def sync_with_fsm(self, fsm_states: dict, force_save: bool = False):
        """
        Метод-синхронизатор. Сериализует актуальный PositionState обратно в json-кеш.
        Работает как "дамп" оперативной памяти на HDD, используя канонический state.to_dict().
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
                
                new_state_snapshot = state.to_dict()
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
        """Конкурентно-безопасное сохранение in-memory кеша на диск под куполом стратегии (в .json)."""
        if symbol not in self.locks:
            self.locks[symbol] = asyncio.Lock()
            
        async with self.locks[symbol]:
            if symbol in self.caches:
                path = self.get_runtime_path(symbol, default_ext=".json").with_suffix(".json")
                path.parent.mkdir(parents=True, exist_ok=True)
                Utils.atomic_write_json(path, self.caches[symbol], indent=4)
                # Удаляем устаревший .pkl, если остался
                pkl_path = path.with_suffix(".pkl")
                if pkl_path.exists():
                    try:
                        pkl_path.unlink()
                    except Exception:
                        pass
                if self.backup_manager:
                    self.backup_manager.mark_changed()

