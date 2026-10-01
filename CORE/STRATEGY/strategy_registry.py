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
    """Инкапсулирует конфигурацию и параметры переопределения одной торговой стратегии (купола)."""

    def __init__(
        self,
        strategy_id: str,
        raw_meta: Dict[str, Any],
        symbols: List[str],
        active_settings: Optional[Dict[str, Any]] = None
    ):
        self.strategy_id: str = strategy_id
        self.name: str = raw_meta["name"]
        self.description: str = raw_meta.get("description", "")
        self.raw_meta: Dict[str, Any] = copy.deepcopy(raw_meta)

        raw_inv = raw_meta.get("invest_size", 400.0)
        if isinstance(raw_inv, dict):
            self.invest_size = {k.upper(): float(v) for k, v in raw_inv.items()}
        else:
            self.invest_size = float(raw_inv)

        raw_lev = raw_meta.get("leverage", 20)
        if isinstance(raw_lev, dict):
            self.leverage = {k.upper(): int(v) for k, v in raw_lev.items()}
        else:
            self.leverage = int(raw_lev)

        self.overrides: Dict[str, Any] = copy.deepcopy(raw_meta.get("overrides") or {})
        self.symbols: List[str] = [s.upper().strip() for s in symbols]
        self.active_settings: Dict[str, Any] = copy.deepcopy(active_settings or {})

        if self.active_settings:
            if "invest_size" in self.active_settings:
                act_inv = self.active_settings["invest_size"]
                if isinstance(act_inv, dict):
                    self.invest_size = {k.upper(): float(v) for k, v in act_inv.items()}
                else:
                    self.invest_size = float(act_inv)
            if "leverage" in self.active_settings:
                act_lev = self.active_settings["leverage"]
                if isinstance(act_lev, dict):
                    self.leverage = {k.upper(): int(v) for k, v in act_lev.items()}
                else:
                    self.leverage = int(act_lev)
            for side_k in ("LONG", "SHORT"):
                if side_k in self.active_settings and isinstance(self.active_settings[side_k], dict):
                    if side_k not in self.overrides:
                        self.overrides[side_k] = {}
                    self.overrides[side_k].update(self.active_settings[side_k])

        # 1. Купол Аналитики (Analytics Dome)
        strat_analytics = self.active_settings.get("analytics") or self.overrides.get("analytics")
        if strat_analytics and isinstance(strat_analytics, dict) and "start_balance_usdt" in strat_analytics:
            self.start_balance_usdt: float = float(strat_analytics["start_balance_usdt"])
        elif "start_balance_usdt" in self.active_settings:
            self.start_balance_usdt: float = float(self.active_settings["start_balance_usdt"])
        elif "start_balance_usdt" in raw_meta:
            self.start_balance_usdt: float = float(raw_meta["start_balance_usdt"])
        else:
            self.start_balance_usdt: float = 150.0
        self.analytics_cfg: Dict[str, Any] = {"start_balance_usdt": self.start_balance_usdt}

        # 2. Купол Super Grid (Volatility Grid Dome)
        sg_override = (
            self.active_settings.get("super_grid")
            or self.overrides.get("super_grid")
            or raw_meta.get("super_grid")
        )
        if sg_override is not None and isinstance(sg_override, dict):
            self.super_grid_cfg: Dict[str, Any] = copy.deepcopy(sg_override)
            self.super_grid_enabled: bool = bool(sg_override.get("enabled", True))
        elif "super_grid_enabled" in raw_meta:
            self.super_grid_enabled: bool = bool(raw_meta["super_grid_enabled"])
            self.super_grid_cfg = {
                "enabled": self.super_grid_enabled,
                "timeframe": "1w",
                "window": 12,
                "multiplier": 2.0,
                "min_volatility_pct": 15.0,
                "max_volatility_pct": None,
                "update_interval_hours": 24
            }
        else:
            self.super_grid_enabled: bool = False
            self.super_grid_cfg = {
                "enabled": False,
                "timeframe": "1w",
                "window": 12,
                "multiplier": 2.0,
                "min_volatility_pct": 15.0,
                "max_volatility_pct": None,
                "update_interval_hours": 24
            }

        # 3. Купол Уведомлений (Notifications Dome)
        notif_cfg = (
            self.active_settings.get("notifications")
            or self.overrides.get("notifications")
            or raw_meta.get("notifications")
        )
        if notif_cfg and isinstance(notif_cfg, dict):
            self.notifications_cfg: Dict[str, Any] = copy.deepcopy(notif_cfg)
        else:
            self.notifications_cfg: Dict[str, Any] = {
                "enabled": True,
                "negative_threshold": -20.0,
                "positive_threshold": 20.0
            }

        # 4. Купол Авто-закрытия (Auto-closing Dome)
        ac_cfg = (
            self.active_settings.get("auto_closing")
            or self.overrides.get("auto_closing")
            or raw_meta.get("auto_closing")
        )
        if ac_cfg and isinstance(ac_cfg, dict):
            self.auto_closing_cfg: Dict[str, Any] = copy.deepcopy(ac_cfg)
        else:
            self.auto_closing_cfg: Dict[str, Any] = {
                "negative": {"threshold": None, "threshold_increment": None},
                "positive": {"threshold": 100, "threshold_increment": 100}
            }

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
                symbols=strat_symbols,
                active_settings=strat_settings
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

    def get_strategy_id_for_symbol(self, symbol: str) -> Optional[str]:
        """Возвращает идентификатор стратегии для указанного символа или None."""
        sym_clean = symbol.upper().strip()
        if sym_clean in self.symbol_to_strategy:
            return self.symbol_to_strategy[sym_clean].strategy_id
        return None

    def is_symbol_active(self, symbol: str) -> bool:
        """Проверяет, привязан ли символ к какой-либо активной стратегии."""
        return symbol.upper().strip() in self.symbol_to_strategy

    def get_all_active_symbols(self) -> List[str]:
        """Возвращает общий объединенный список символов всех активных стратегий."""
        return list(self.all_active_symbols)

    def get_active_strategies(self) -> Dict[str, StrategyConfig]:
        """Возвращает словарь всех активных объектов StrategyConfig."""
        return dict(self.active_strategies)

    def reload(self, active_strategies_cfg: Optional[Dict[str, Any]] = None) -> None:
        """Перезагружает базу стратегий и пересобирает активные маппинги символов."""
        self.universes_db = self._load_universes_db()
        if active_strategies_cfg is not None:
            self.active_cfg = active_strategies_cfg
        else:
            app_data = Utils.read_json_file(DATA_DIR / "app.json")
            self.active_cfg = app_data.get("active_strategies", {})
        self._resolve_and_validate()
