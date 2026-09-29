# ============================================================
# FILE: CORE/STRATEGY/strategy_registry.py
# ROLE: Strategy template registry and symbol validation
# ============================================================

import copy
import json
from pathlib import Path
from typing import Dict, List, Optional, Any

from consts import (
    DATA_DIR,
    _CFG,
    DEFAULT_SMART_GRACE,
    DEFAULT_TIMEFRAME,
    DEFAULT_SHIFT_INTERVAL_SEC,
    DEFAULT_SMART_SIZE,
    DEFAULT_SMART_TP,
)
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("StrategyRegistry")


class StrategyConfig:
    """Инкапсулирует конфигурацию и параметры переопределения одной торговой стратегии."""

    def __init__(self, strategy_id: str, raw_meta: Dict[str, Any], symbols: List[str]):
        self.strategy_id: str = strategy_id
        self.name: str = raw_meta["name"]
        self.description: str = raw_meta.get("description", "")
        self.invest_size: float = float(raw_meta["invest_size"])
        self.leverage: int = int(raw_meta["leverage"])
        self.start_balance_usdt: float = float(raw_meta.get("start_balance_usdt", 3000.0))
        self.overrides: Dict[str, Any] = copy.deepcopy(raw_meta.get("overrides") or {})
        self.symbols: List[str] = [s.upper().strip() for s in symbols]

        # Разрешение режима Super Grid
        sg_override = self.overrides.get("super_grid")
        if sg_override is not None and isinstance(sg_override, dict):
            self.super_grid_enabled: bool = bool(sg_override.get("enabled", True))
        elif "super_grid_enabled" in raw_meta:
            self.super_grid_enabled: bool = bool(raw_meta["super_grid_enabled"])
        else:
            self.super_grid_enabled: bool = False

        # Конфигурация Smart Grace
        base_smart_grace = copy.deepcopy(DEFAULT_SMART_GRACE)
        if "smart_grace" in self.overrides and isinstance(self.overrides["smart_grace"], dict):
            base_smart_grace.update(self.overrides["smart_grace"])
        if "shift_interval_sec" not in base_smart_grace:
            base_smart_grace["shift_interval_sec"] = DEFAULT_SHIFT_INTERVAL_SEC
        self.smart_grace_cfg: Dict[str, Any] = base_smart_grace

        # Конфигурации Smart Size и Smart TP (наследование дефолтных механик + оверрайды)
        base_smart_size = copy.deepcopy(DEFAULT_SMART_SIZE)
        if "smart_size" in self.overrides and isinstance(self.overrides["smart_size"], dict):
            base_smart_size.update(self.overrides["smart_size"])
        self.smart_size_cfg: Dict[str, Any] = base_smart_size

        base_smart_tp = copy.deepcopy(DEFAULT_SMART_TP)
        if "smart_tp" in self.overrides and isinstance(self.overrides["smart_tp"], dict):
            base_smart_tp.update(self.overrides["smart_tp"])
        self.smart_tp_cfg: Dict[str, Any] = base_smart_tp

        # Персональный таймфрейм вселенной (по умолчанию 5m)
        self.timeframe: str = str(self.overrides.get("timeframe", DEFAULT_TIMEFRAME))

    def __repr__(self) -> str:
        return f"<StrategyConfig id='{self.strategy_id}' name='{self.name}' symbols={self.symbols}>"


class StrategyRegistry:
    """
    Реестр мультистратегических шаблонов торгового ядра.
    Валидирует базу universes.json, разрешает активные стратегии из app.json
    и строго следит за инвариантом взаимной однозначности 1:1 (монета -> стратегия).
    """

    def __init__(
        self,
        active_strategies_cfg: Optional[Dict[str, Any]] = None,
        universes_path: Optional[Path] = None
    ):
        self.universes_path: Path = universes_path or (DATA_DIR / "universes.json")
        self.universes_db: Dict[str, Dict[str, Any]] = self._load_universes_db()

        # Чтение конфигурации активных стратегий строго напрямую (Правило 2)
        if active_strategies_cfg is not None:
            self.active_cfg: Dict[str, Any] = active_strategies_cfg
        else:
            self.active_cfg: Dict[str, Any] = _CFG["active_strategies"]

        self.active_strategies: Dict[str, StrategyConfig] = {}
        self.symbol_to_strategy: Dict[str, StrategyConfig] = {}
        self.all_active_symbols: List[str] = []

        self._resolve_and_validate()

    def _load_universes_db(self) -> Dict[str, Dict[str, Any]]:
        """Загружает базу шаблонов из universes.json."""
        if not self.universes_path.exists():
            raise FileNotFoundError(
                f"[StrategyRegistry] Критическая ошибка: файл шаблонов не найден по пути: {self.universes_path}"
            )
        data = Utils.read_json_file(self.universes_path)
        if not data or not isinstance(data, dict):
            raise ValueError(
                f"[StrategyRegistry] Критическая ошибка: universes.json пуст или имеет неверный формат словаря!"
            )
        return data

    def _resolve_and_validate(self) -> None:
        """
        Проверяет наличие объявленных стратегий в базе и строго соблюдает Инвариант 1:
        Каждая торгуемая монета принадлежит строго одной активной стратегии (1:1).
        """
        self.active_strategies.clear()
        self.symbol_to_strategy.clear()
        resolved_symbols: List[str] = []

        for strat_id, strat_settings in self.active_cfg.items():
            # Прямое чтение флага активности (Правило 2)
            is_enabled = bool(strat_settings["enabled"])
            if not is_enabled:
                continue

            if strat_id not in self.universes_db:
                raise KeyError(
                    f"[StrategyRegistry] Стратегия '{strat_id}', указанная в active_strategies, "
                    f"отсутствует в базе шаблонов {self.universes_path.name}!"
                )

            raw_meta = self.universes_db[strat_id]
            # Прямое чтение списка монет (Правило 2)
            raw_symbols: List[str] = strat_settings["symbols"]
            strat_symbols = [s.upper().strip() for s in raw_symbols if s and s.strip()]

            strat_config = StrategyConfig(
                strategy_id=strat_id,
                raw_meta=raw_meta,
                symbols=strat_symbols
            )
            self.active_strategies[strat_id] = strat_config

            for sym in strat_symbols:
                # Проверка Инварианта 1 (1:1)
                if sym in self.symbol_to_strategy:
                    conflicting_strat = self.symbol_to_strategy[sym].strategy_id
                    raise ValueError(
                        f"[StrategyRegistry] КОНФЛИКТ ИНВАРИАНТА 1:1! "
                        f"Символ '{sym}' одновременно назначен в две активные стратегии: "
                        f"'{conflicting_strat}' и '{strat_id}'. Каждая монета обязана принадлежать строго одной стратегии!"
                    )
                self.symbol_to_strategy[sym] = strat_config
                resolved_symbols.append(sym)

        self.all_active_symbols = resolved_symbols
        logger.info(
            f"[StrategyRegistry] Loaded {len(self.active_strategies)} active strategies. "
            f"Active trading pool: {len(self.all_active_symbols)} symbols {self.all_active_symbols}."
        )

    def get_strategy_for_symbol(self, symbol: str) -> StrategyConfig:
        """Возвращает конфигурацию стратегии для указанного символа."""
        sym_clean = symbol.upper().strip()
        if sym_clean not in self.symbol_to_strategy:
            raise KeyError(
                f"[StrategyRegistry] Символ '{symbol}' не закреплен ни за одной активной стратегией!"
            )
        return self.symbol_to_strategy[sym_clean]

    def is_symbol_active(self, symbol: str) -> bool:
        """Проверяет, привязан ли символ к какой-либо активной стратегии."""
        return symbol.upper().strip() in self.symbol_to_strategy

    def get_all_active_symbols(self) -> List[str]:
        """Возвращает общий объединенный список символов всех активных стратегий."""
        return list(self.all_active_symbols)

    def get_active_strategies(self) -> Dict[str, StrategyConfig]:
        """Возвращает словарь всех активных объектов StrategyConfig."""
        return dict(self.active_strategies)
