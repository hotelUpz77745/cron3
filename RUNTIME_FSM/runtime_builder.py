# ==============================================================================
# Path: RUNTIME_FSM/runtime_builder.py
# Role: Сборка пер-символьных рантайм конфигураций при старте системы (OOP)
# ==============================================================================

import json
import os
import pickle
import sys
from pathlib import Path
from typing import List, Optional

from c_log import UnifiedLogger
from c_utils import Utils
from consts import DATA_DIR, _CFG
from CORE.strategy_applicator import StrategyApplicator
from CORE.strategy_registry import StrategyRegistry

logger = UnifiedLogger("RuntimeBuilder")


class RuntimeBuilder:
    """
    Класс-строитель рантайм-кэшей конфигураций символов и стратегий.
    Отвечает за компиляцию активных стратегий в файлы CFG/runtime/{strategy_id}/{symbol}.json
    при старте торгового ядра и обработку миграций.
    """

    def __init__(
        self,
        runtime_dir: Optional[Path] = None,
        registry: Optional[StrategyRegistry] = None,
        applicator: Optional[StrategyApplicator] = None,
        avoid_check: Optional[bool] = None,
    ):
        self.runtime_dir = runtime_dir or (DATA_DIR / "runtime")
        self.registry = registry
        self.applicator = applicator or StrategyApplicator()
        self.avoid_check = avoid_check
        self.logger = logger
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        """Гарантирует существование директории рантайма."""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def get_registry(self) -> Optional[StrategyRegistry]:
        """Возвращает или лениво инициализирует StrategyRegistry."""
        if self.registry is None:
            try:
                self.registry = StrategyRegistry()
            except Exception as e:
                self.logger.warning(f"Could not load StrategyRegistry: {e}.")
                self.registry = None
        return self.registry

    def build_caches(self, symbols: Optional[List[str]] = None) -> bool:
        """
        Собирает и синхронизирует рантайм-файлы конфигураций для списка символов.
        Возвращает True, если был создан хотя бы один новый файл конфигурации.
        """
        self.ensure_dirs()
        registry = self.get_registry()
        target_symbols = (
            symbols
            if symbols is not None
            else (registry.get_all_active_symbols() if registry else [])
        )
        created_new = False

        for symbol in target_symbols:
            sym_lower = symbol.lower()
            strat = registry.get_strategy_for_symbol(symbol) if registry else None

            if strat:
                target_dir = self.runtime_dir / strat.strategy_id
                target_file = target_dir / f"{sym_lower}.json"
                was_existing = target_file.exists()
                self.applicator.build_runtime_for_symbol(symbol, strat, target_dir=target_dir)
                if not was_existing and target_file.exists():
                    created_new = True
            else:
                target_file = self.runtime_dir / f"{sym_lower}.json"
                target_pkl = self.runtime_dir / f"{sym_lower}.pkl"
                if not target_file.exists():
                    if target_pkl.exists():
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
                        self.logger.error(
                            f"[RUNTIME_BUILDER] No strategy assigned for symbol {symbol}! Skipping runtime creation."
                        )
                        continue

        return created_new

    def prompt_check(self) -> None:
        """Пре-трейд валидация достаточности капитала (invest_size >= min notional) перед боевым стартом."""
        from CORE.SERVICES.notional_validator import NotionalCapacityValidator
        registry = self.get_registry()
        if registry:
            NotionalCapacityValidator.validate_or_abort(registry)


# Синглтон по умолчанию и функции-фасады обратной совместимости
default_runtime_builder = RuntimeBuilder()


def build_runtime_caches(
    symbols: Optional[List[str]] = None,
    registry: Optional[StrategyRegistry] = None,
    target_runtime_dir: Optional[Path] = None,
) -> bool:
    """Функциональный фасад для сборки рантайма через RuntimeBuilder."""
    if target_runtime_dir or registry:
        builder = RuntimeBuilder(runtime_dir=target_runtime_dir, registry=registry)
    else:
        builder = default_runtime_builder
    return builder.build_caches(symbols=symbols)


def prompt_runtime_check() -> None:
    """Функциональный фасад для интерактивной паузы проверки рантайма."""
    default_runtime_builder.prompt_check()
