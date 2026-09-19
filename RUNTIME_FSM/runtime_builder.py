# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\RUNTIME_FSM\runtime_builder.py
# Role: runtime_builder.py module

# ==============================================================================
# Path: RUNTIME_FSM/runtime_builder.py
# Role: Сборка пер-символьных рантайм конфигураций при старте
# ==============================================================================

import os
import json
from pathlib import Path
from consts import _CFG, DATA_DIR, AVOID_CHECK_RUNTIME_CFG
from c_log import UnifiedLogger
from c_utils import Utils

import sys
logger = UnifiedLogger("RuntimeBuilder")

RUNTIME_DIR = DATA_DIR / "runtime"

def _ensure_dirs():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

def build_runtime_caches():
    _ensure_dirs()
    
    base_file = DATA_DIR / "_base.json"
    base_data = Utils.read_json_file(base_file)
    if not base_data:
        logger.error(f"Base template not found or empty at {base_file}")
        return False
        
    for side in ("LONG", "SHORT"):
        if side in base_data:
            grid = base_data[side].get("grid", {})
            tp_map = base_data[side].get("tp_map", {})
            if grid and tp_map and len(grid) != len(tp_map):
                raise ValueError(f"[_base.json] {side} ОШИБКА КОНФИГУРАЦИИ БАЗОВОГО ШАБЛОНА: Количество уровней grid ({len(grid)}) не совпадает с количеством уровней tp_map ({len(tp_map)}). Проверьте файл _base.json!")

    created_new = False
    symbols = _CFG["symbols"]
    for symbol in symbols:
        sym_lower = symbol.lower()
        target_file = RUNTIME_DIR / f"{sym_lower}.json"
        
        if not target_file.exists():
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
