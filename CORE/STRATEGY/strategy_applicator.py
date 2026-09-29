# ============================================================
# FILE: CORE/STRATEGY/strategy_applicator.py
# ROLE: Merges base templates with strategy overrides and builds runtime configs
# ============================================================

import copy
import json
from pathlib import Path
from typing import Dict, Any, Optional

from consts import DATA_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.STRATEGY.strategy_registry import StrategyConfig, StrategyRegistry

logger = UnifiedLogger("StrategyApplicator")

MIN_TAKE_PROFIT_PCT = 0.50  # Лабораторное правило 11: TP строго не менее 0.50%


class StrategyApplicator:
    """
    Применяет параметры и переопределения стратегии к базовой конфигурации _base.json,
    соблюдает инвариант минимального тейк-профита >= 0.50%
    и генерирует изолированные рантайм-конфигурации в CFG/runtime/<symbol>.json.
    """

    def __init__(self, base_cfg_path: Optional[Path] = None):
        self.base_cfg_path: Path = base_cfg_path or (DATA_DIR / "_base.json")
        self.base_cfg: Dict[str, Any] = self._load_base_cfg()

    def _load_base_cfg(self) -> Dict[str, Any]:
        """Загружает базовый шаблон торговли."""
        if not self.base_cfg_path.exists():
            raise FileNotFoundError(
                f"[StrategyApplicator] Базовый шаблон не найден по пути: {self.base_cfg_path}"
            )
        data = Utils.read_json_file(self.base_cfg_path)
        if not data or not isinstance(data, dict):
            raise ValueError(
                f"[StrategyApplicator] Базовый шаблон {self.base_cfg_path.name} пуст или некорректен!"
            )
        return data

    def build_side_configs(self, strategy: StrategyConfig) -> Dict[str, Dict[str, Any]]:
        """
        Создает стороны LONG и SHORT для символа путем наложения переопределений стратегии
        на базовый шаблон _base.json с жестким соблюдением правила TP >= 0.50%.
        """
        side_cfgs = {}
        for side in ("LONG", "SHORT"):
            if side not in self.base_cfg:
                continue

            side_data = copy.deepcopy(self.base_cfg[side])
            side_data["invest_size"] = strategy.invest_size
            side_data["leverage"] = strategy.leverage
            side_data["strategy_id"] = strategy.strategy_id
            side_data["strategy_name"] = strategy.name

            # 1. Применение сетки (grid) из overrides
            if "grid" in strategy.overrides and strategy.overrides["grid"]:
                side_data["grid"] = copy.deepcopy(strategy.overrides["grid"])

            # 2. Если Super Grid отключен, гарантируем сброс super_indent в None (Инвариант 3)
            if not strategy.super_grid_enabled and "grid" in side_data:
                for lvl_data in side_data["grid"].values():
                    lvl_data["super_indent"] = None

            # 3. Применение TP-карты из overrides с валидацией Инварианта 4 (Rule 11)
            if "tp_map" in strategy.overrides and strategy.overrides["tp_map"]:
                side_data["tp_map"] = copy.deepcopy(strategy.overrides["tp_map"])

            # Принудительное ограничение TP снизу: строго не менее 0.50%
            if "tp_map" in side_data and isinstance(side_data["tp_map"], dict):
                for tp_step in side_data["tp_map"].values():
                    if "indent" in tp_step and tp_step["indent"] is not None:
                        tp_step["indent"] = max(MIN_TAKE_PROFIT_PCT, float(tp_step["indent"]))
                    if "fallback_indent" in tp_step and tp_step["fallback_indent"] is not None:
                        tp_step["fallback_indent"] = max(MIN_TAKE_PROFIT_PCT, float(tp_step["fallback_indent"]))

            side_cfgs[side] = side_data

        return side_cfgs

    def build_runtime_for_symbol(
        self,
        symbol: str,
        strategy: StrategyConfig,
        target_dir: Optional[Path] = None,
        force: bool = False
    ) -> Path:
        """
        Генерирует рантайм-файл CFG/runtime/<symbol>.json на основе стратегии.
        Если файл уже существует и force=False, проверяет совпадение strategy_id.
        """
        runtime_dir = target_dir or (DATA_DIR / "runtime")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        target_file = runtime_dir / f"{symbol.lower()}.json"

        side_configs = self.build_side_configs(strategy)

        existing = Utils.read_json_file(target_file) if target_file.exists() else {}
        if existing and not force:
            existing_strat = existing.get("LONG", {}).get("strategy_id")
            if existing_strat == strategy.strategy_id:
                # Конфигурация уже соответствует назначенной стратегии
                return target_file

        new_data: Dict[str, Any] = {}
        for side, side_cfg in side_configs.items():
            new_side = copy.deepcopy(side_cfg)
            exist_side = existing.get(side, {})
            # Если позиция открыта, сохраняем состояние FSM
            if exist_side.get("in_position", False):
                for state_key in [
                    "total_volume", "avg_entry_price", "pre_avg_price",
                    "initial_entry_price", "open_time", "next_avg_price",
                    "fallback_price", "in_position", "in_position_papper",
                    "is_finished", "pending_avg", "pending_rolling_tp"
                ]:
                    if state_key in exist_side:
                        new_side[state_key] = exist_side[state_key]
                if "grid" in exist_side and "grid" in new_side:
                    for g_idx, g_val in exist_side["grid"].items():
                        if g_idx in new_side["grid"] and isinstance(g_val, dict):
                            for k in ["is_active", "price", "activated_at", "timestamp"]:
                                if k in g_val:
                                    new_side["grid"][g_idx][k] = g_val[k]
                if "tp_map" in exist_side and "tp_map" in new_side:
                    for tp_idx, tp_val in exist_side["tp_map"].items():
                        if tp_idx in new_side["tp_map"] and isinstance(tp_val, dict):
                            if "is_active" in tp_val:
                                new_side["tp_map"][tp_idx]["is_active"] = tp_val["is_active"]
            else:
                new_side.update({
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
                if "grid" in new_side:
                    for grid_cfg in new_side["grid"].values():
                        grid_cfg["is_active"] = False
                        grid_cfg["price"] = None
                        grid_cfg["activated_at"] = None
                        grid_cfg["timestamp"] = None
                if "tp_map" in new_side:
                    for tp_cfg in new_side["tp_map"].values():
                        tp_cfg["is_active"] = False

            new_data[side] = new_side

        Utils.atomic_write_json(target_file, new_data, indent=4)
        safe_name = strategy.name.encode("ascii", "ignore").decode("ascii").strip()
        logger.info(
            f"[StrategyApplicator] Built runtime config for {symbol} "
            f"from strategy '{strategy.strategy_id}' ({safe_name})."
        )
        return target_file

    def build_all_runtimes(
        self,
        registry: StrategyRegistry,
        target_dir: Optional[Path] = None,
        force: bool = False
    ) -> Dict[str, Path]:
        """Генерирует рантайм-файлы для всех монет из активных стратегий."""
        results = {}
        for symbol in registry.get_all_active_symbols():
            strat = registry.get_strategy_for_symbol(symbol)
            path = self.build_runtime_for_symbol(symbol, strat, target_dir=target_dir, force=force)
            results[symbol] = path
        return results
