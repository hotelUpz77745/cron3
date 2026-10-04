# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\RUNTIME_FSM\runtime_builder.py
# Role: runtime_builder.py module

# ==============================================================================
# Path: RUNTIME_FSM/runtime_builder.py
# Role: Сборка пер-символьных рантайм конфигураций при старте
# ==============================================================================

import os
import sys
import json
from pathlib import Path
from consts import _CFG, DATA_DIR, AVOID_CHECK_RUNTIME_CFG
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.STRATEGY.strategy_registry import StrategyRegistry
from CORE.STRATEGY.strategy_applicator import StrategyApplicator

from typing import Optional, List
logger = UnifiedLogger("RuntimeBuilder")

RUNTIME_DIR = DATA_DIR / "runtime"

def _ensure_dirs():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

def build_runtime_caches(
    symbols: Optional[List[str]] = None,
    registry: Optional[StrategyRegistry] = None
) -> bool:
    _ensure_dirs()
    created_new = False

    if registry is None:
        try:
            registry = StrategyRegistry()
        except Exception as e:
            logger.warning(f"Could not load StrategyRegistry: {e}. Falling back to _base.json.")
            registry = None

    if symbols is None:
        symbols = registry.get_all_active_symbols() if registry else []

    applicator = StrategyApplicator()

    base_file = DATA_DIR / "_base.json"
    base_data = Utils.read_json_file(base_file) or {}

    for side in ("LONG", "SHORT"):
        if side in base_data:
            tp_purpose = base_data[side].get("tp_purpose", "self")
            grid = base_data[side].get("grid", {})
            tp_map = base_data[side].get("tp_map", {})
            if tp_purpose != "neutral" and grid and tp_map and len(grid) != len(tp_map):
                raise ValueError(f"[_base.json] {side} ОШИБКА КОНФИГУРАЦИИ БАЗОВОГО ШАБЛОНА: Количество уровней grid ({len(grid)}) не совпадает с количеством уровней tp_map ({len(tp_map)}). Проверьте файл _base.json!")

    for symbol in symbols:
        sym_lower = symbol.lower()
        strat = registry.get_strategy_for_symbol(symbol) if registry else None

        if strat:
            target_dir = RUNTIME_DIR / strat.strategy_id
            target_file = target_dir / f"{sym_lower}.json"
            was_existing = target_file.exists()
            applicator.build_runtime_for_symbol(symbol, strat, target_dir=target_dir)
            if not was_existing and target_file.exists():
                created_new = True
        else:
            target_file = RUNTIME_DIR / f"{sym_lower}.json"
            if not target_file.exists():
                if not base_data:
                    logger.error(f"[CRITICAL] Neither strategy nor base template found for symbol {symbol}!")
                    continue
                # Create from base template and add is_active / price
                new_data = json.loads(json.dumps(base_data))  # deep copy
                
                for side in ("LONG", "SHORT"):
                    if side in new_data:
                        new_data[side].update({
                            "total_volume": 0.0,
                            "avg_entry_price": 0.0,
                            "pre_avg_price": 0.0,
                            "initial_entry_price": 0.0,
                            "open_time": 0,
                            "next_avg_price": None,
                            "fallback_price": None,
                            "in_position": False,
                            "in_position_papper": False,
                            "is_finished": False,
                            "pending_avg": False,
                            "pending_rolling_tp": False
                        })
                        if "grid" in new_data[side]:
                            for grid_id, grid_cfg in new_data[side]["grid"].items():
                                grid_cfg["is_active"] = False
                                grid_cfg["price"] = None
                                grid_cfg["activated_at"] = None
                                grid_cfg["timestamp"] = None
                        else:
                            logger.error(f"[CRITICAL] Секция 'grid' ОТСУТСТВУЕТ для {side} в _base.json!")
                        
                        if "tp_map" in new_data[side]:
                            for tp_id, tp_cfg in new_data[side]["tp_map"].items():
                                tp_cfg["is_active"] = False
                        else:
                            logger.error(f"[CRITICAL] Секция 'tp_map' ОТСУТСТВУЕТ для {side} в _base.json!")

                Utils.write_json_file(target_file, new_data)
                logger.info(f"Created runtime cache for {symbol} at {target_file.name}")
                created_new = True

    return created_new

def prompt_runtime_check():
    if not AVOID_CHECK_RUNTIME_CFG:
        print("\n" + "="*60)
        print("ВНИМАНИЕ! Проверьте настройки рантайма в CFG/runtime/")
        print("Если все верно, нажмите Enter для продолжения...")
        print("="*60 + "\n")
        try:
            input()
        except EOFError:
            logger.warning("No interactive stdin found, skipping runtime check pause.")
        except KeyboardInterrupt:
            print("Прервано пользователем.")
            sys.exit(0)
