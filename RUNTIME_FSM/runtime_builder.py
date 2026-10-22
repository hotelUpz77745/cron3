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
    registry: Optional[StrategyRegistry] = None,
    target_runtime_dir: Optional[Path] = None
) -> bool:
    rt_dir = target_runtime_dir or RUNTIME_DIR
    rt_dir.mkdir(parents=True, exist_ok=True)
    created_new = False

    if registry is None:
        try:
            registry = StrategyRegistry()
        except Exception as e:
            logger.warning(f"Could not load StrategyRegistry: {e}.")
            registry = None

    if symbols is None:
        symbols = registry.get_all_active_symbols() if registry else []

    applicator = StrategyApplicator()

    for symbol in symbols:
        sym_lower = symbol.lower()
        strat = registry.get_strategy_for_symbol(symbol) if registry else None

        if strat:
            target_dir = rt_dir / strat.strategy_id
            target_file = target_dir / f"{sym_lower}.json"
            was_existing = target_file.exists()
            applicator.build_runtime_for_symbol(symbol, strat, target_dir=target_dir)
            if not was_existing and target_file.exists():
                created_new = True
        else:
            target_file = rt_dir / f"{sym_lower}.json"
            target_pkl = rt_dir / f"{sym_lower}.pkl"
            if not target_file.exists():
                if target_pkl.exists():
                    import pickle
                    try:
                        with open(target_pkl, "rb") as fp:
                            data = pickle.load(fp)
                    except Exception:
                        data = {}
                    Utils.atomic_write_json(target_file, data, indent=4)
                    try:
                        target_pkl.unlink()
                    except Exception:
                        pass
                    created_new = True
                else:
                    logger.error(f"[RUNTIME_BUILDER] No strategy assigned for symbol {symbol}! Skipping runtime creation.")
                    continue

    return created_new

def prompt_runtime_check():
    if not AVOID_CHECK_RUNTIME_CFG:
        print("\n" + "="*60)
        print("ВНИМАНИЕ! Проверьте настройки рантайма (.json) в CFG/runtime/")
        print("Если все верно, нажмите Enter для продолжения...")
        print("="*60 + "\n")
        try:
            input()
        except EOFError:
            logger.warning("No interactive stdin found, skipping runtime check pause.")
        except KeyboardInterrupt:
            print("Прервано пользователем.")
            sys.exit(0)
