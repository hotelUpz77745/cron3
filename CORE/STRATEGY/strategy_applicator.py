# ============================================================
# FILE: CORE/STRATEGY/strategy_applicator.py
# ROLE: Merges base templates with strategy overrides and builds runtime configs
# ============================================================

import copy
import json
from pathlib import Path
from typing import Dict, Any, Optional

from consts import DATA_DIR, BASE_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.STRATEGY.strategy_registry import StrategyConfig, StrategyRegistry

logger = UnifiedLogger("StrategyApplicator")

MIN_TAKE_PROFIT_PCT = 0.50  # Лабораторное правило 11: TP строго не менее 0.50%


class StrategyApplicator:
    """
    Применяет параметры и переопределения стратегии к базовой конфигурации _base.json,
    соблюдает инвариант минимального тейк-профита >= 0.50%
    и генерирует изолированные JSON рантайм-конфигурации в CFG/runtime/<strategy_id>/<symbol>.json.
    """

    DEFAULT_FALLBACK_BASE: Dict[str, Any] = {
        "LONG": {
            "enable": True,
            "invest_size": 400.0,
            "leverage": 20,
            "tp_purpose": "self",
            "margin_type": "CROSSED",
            "grid": {},
            "tp_map": {}
        },
        "SHORT": {
            "enable": True,
            "invest_size": 400.0,
            "leverage": 20,
            "tp_purpose": "self",
            "margin_type": "CROSSED",
            "grid": {},
            "tp_map": {}
        }
    }

    def __init__(self, base_cfg_path: Optional[Path] = None):
        self.base_cfg_path: Path = base_cfg_path or (DATA_DIR / "_base.json")
        self.base_cfg: Dict[str, Any] = self._load_base_cfg()

    def _load_base_cfg(self) -> Dict[str, Any]:
        """Загружает базовый шаблон торговли или возвращает безопасный дефолт, если _base.json отсутствует."""
        if not self.base_cfg_path.exists():
            return copy.deepcopy(self.DEFAULT_FALLBACK_BASE)
        data = Utils.read_json_file(self.base_cfg_path)
        if not data or not isinstance(data, dict):
            return copy.deepcopy(self.DEFAULT_FALLBACK_BASE)
        return data

    def build_side_configs(self, strategy: StrategyConfig) -> Dict[str, Dict[str, Any]]:
        """
        Создает стороны LONG и SHORT для символа путем наложения переопределений стратегии
        на базовый шаблон _base.json с жестким соблюдением правила TP >= 0.50%
        и поддержкой асимметричных настроек per-side (LONG / SHORT).
        """
        side_cfgs = {}
        for side in ("LONG", "SHORT"):
            if side not in self.base_cfg:
                continue

            side_data = copy.deepcopy(self.base_cfg[side])

            # Извлекаем конфигурацию для стороны (из raw_meta[side] или legacy overrides[side])
            side_dict = {}
            if hasattr(strategy, "raw_meta") and isinstance(strategy.raw_meta, dict):
                if side in strategy.raw_meta and isinstance(strategy.raw_meta[side], dict):
                    side_dict = copy.deepcopy(strategy.raw_meta[side])
            if hasattr(strategy, "overrides") and isinstance(strategy.overrides, dict):
                if side in strategy.overrides and isinstance(strategy.overrides[side], dict):
                    merged = copy.deepcopy(strategy.overrides[side])
                    merged.update(side_dict)
                    side_dict = merged

            # 1. Invest Size (приоритет: side_dict -> strategy.invest_size[side] -> strategy.invest_size)
            if "invest_size" in side_dict:
                side_data["invest_size"] = float(side_dict["invest_size"])
            elif isinstance(strategy.invest_size, dict) and side in strategy.invest_size:
                side_data["invest_size"] = float(strategy.invest_size[side])
            elif isinstance(strategy.invest_size, (int, float)):
                side_data["invest_size"] = float(strategy.invest_size)

            # 2. Leverage (приоритет: side_dict -> strategy.leverage[side] -> strategy.leverage)
            if "leverage" in side_dict:
                side_data["leverage"] = int(side_dict["leverage"])
            elif isinstance(strategy.leverage, dict) and side in strategy.leverage:
                side_data["leverage"] = int(strategy.leverage[side])
            elif isinstance(strategy.leverage, (int, float)):
                side_data["leverage"] = int(strategy.leverage)

            # 3. Enable / Enabled флаг стороны (если явно False, сторона не торгуется)
            if "enable" in side_dict:
                val = bool(side_dict["enable"])
                side_data["enable"] = val
                side_data["enabled"] = val
            elif "enabled" in side_dict:
                val = bool(side_dict["enabled"])
                side_data["enable"] = val
                side_data["enabled"] = val

            side_data["strategy_id"] = strategy.strategy_id
            side_data["strategy_name"] = strategy.name

            # 4. Применение сетки (grid): строго из стороны side_dict["grid"], либо общей
            grid_override = (
                side_dict.get("grid")
                or (getattr(strategy, "raw_meta", {}) or {}).get("grid")
                or (getattr(strategy, "overrides", {}) or {}).get("grid")
            )
            if grid_override:
                side_data["grid"] = copy.deepcopy(grid_override)

            # 5. Если Super Grid отключен, гарантируем сброс super_indent в None (Инвариант 3)
            if not strategy.super_grid_enabled and "grid" in side_data:
                for lvl_data in side_data["grid"].values():
                    lvl_data["super_indent"] = None

            # 6. Применение TP-карты: строго из стороны side_dict["tp_map"], либо общей
            tp_map_override = (
                side_dict.get("tp_map")
                or (getattr(strategy, "raw_meta", {}) or {}).get("tp_map")
                or (getattr(strategy, "overrides", {}) or {}).get("tp_map")
            )
            if tp_map_override:
                side_data["tp_map"] = copy.deepcopy(tp_map_override)

            # Принудительное ограничение TP снизу: строго не менее 0.50% (или min_tp_indent стратегии)
            min_tp_floor = MIN_TAKE_PROFIT_PCT
            if getattr(strategy, "min_tp_indent", None) is not None:
                min_tp_floor = max(MIN_TAKE_PROFIT_PCT, float(strategy.min_tp_indent))
            elif hasattr(strategy, "raw_meta") and strategy.raw_meta.get("min_tp_indent") is not None:
                min_tp_floor = max(MIN_TAKE_PROFIT_PCT, float(strategy.raw_meta["min_tp_indent"]))

            if "tp_map" in side_data and isinstance(side_data["tp_map"], dict):
                for tp_step in side_data["tp_map"].values():
                    if "indent" in tp_step and tp_step["indent"] is not None:
                        tp_step["indent"] = max(min_tp_floor, float(tp_step["indent"]))
                    if "fallback_indent" in tp_step and tp_step["fallback_indent"] is not None:
                        tp_step["fallback_indent"] = max(min_tp_floor, float(tp_step["fallback_indent"]))

            # 7. Применение tp_purpose и margin_type
            if "tp_purpose" in side_dict:
                side_data["tp_purpose"] = side_dict["tp_purpose"]
            elif hasattr(strategy, "raw_meta") and "tp_purpose" in strategy.raw_meta:
                side_data["tp_purpose"] = strategy.raw_meta["tp_purpose"]
            elif "tp_purpose" in strategy.overrides:
                side_data["tp_purpose"] = strategy.overrides["tp_purpose"]

            if "margin_type" in side_dict:
                side_data["margin_type"] = side_dict["margin_type"]
            elif hasattr(strategy, "raw_meta") and "margin_type" in strategy.raw_meta:
                side_data["margin_type"] = strategy.raw_meta["margin_type"]
            elif "margin_type" in strategy.overrides:
                side_data["margin_type"] = strategy.overrides["margin_type"]

            # 8. Применение avg_free_volume_behavior и auto_closing
            if "avg_free_volume_behavior" in side_dict:
                side_data["avg_free_volume_behavior"] = side_dict["avg_free_volume_behavior"]
            elif hasattr(strategy, "raw_meta") and "avg_free_volume_behavior" in strategy.raw_meta:
                side_data["avg_free_volume_behavior"] = strategy.raw_meta["avg_free_volume_behavior"]
            elif hasattr(strategy, "overrides") and "avg_free_volume_behavior" in strategy.overrides:
                side_data["avg_free_volume_behavior"] = strategy.overrides["avg_free_volume_behavior"]

            # auto_closing и trend_size_depender вынесены на уровень стратегии и не дублируются в рантайм позиций
            if hasattr(strategy, "sizing_mode") and strategy.sizing_mode:
                side_data["sizing_mode"] = strategy.sizing_mode

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
        Генерирует бинарный рантайм-файл CFG/runtime/<strategy_id>/<symbol>.pkl на основе стратегии.
        Обновляет статические параметры (invest_size, leverage, grid, tp_map)
        и безопасно сохраняет состояние FSM для активных позиций.
        """
        runtime_dir = target_dir or (DATA_DIR / "runtime" / strategy.strategy_id)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        target_file = runtime_dir / f"{symbol.lower()}.json"
        target_pkl = runtime_dir / f"{symbol.lower()}.pkl"

        side_configs = self.build_side_configs(strategy)

        existing = {}
        if target_file.exists():
            existing = Utils.read_json_file(target_file)
        elif target_pkl.exists():
            try:
                import pickle
                with open(target_pkl, "rb") as fp:
                    existing = pickle.load(fp)
            except Exception:
                existing = {}
        else:
            # Поиск активного состояния позиции по всем куполам (при переименовании стратегии или смене темплейта)
            active_cand = None
            any_cand = None
            rt_root = DATA_DIR / "runtime"
            if rt_root.exists():
                for sub_dir in rt_root.iterdir():
                    if sub_dir.is_dir() and sub_dir != runtime_dir:
                        cand_json = sub_dir / f"{symbol.lower()}.json"
                        cand_pkl = sub_dir / f"{symbol.lower()}.pkl"
                        c_data = None
                        if cand_json.exists():
                            try:
                                c_data = Utils.read_json_file(cand_json)
                            except Exception:
                                pass
                        if not c_data and cand_pkl.exists():
                            try:
                                import pickle
                                with open(cand_pkl, "rb") as fp:
                                    c_data = pickle.load(fp)
                            except Exception:
                                pass
                        if c_data and isinstance(c_data, dict):
                            has_pos = any(
                                isinstance(s_val, dict) and (s_val.get("in_position") or abs(float(s_val.get("total_volume", 0.0))) > 1e-6)
                                for s_val in c_data.values()
                            )
                            if has_pos:
                                active_cand = c_data
                                logger.info(f"[{symbol}] Found active position state in existing dome '{sub_dir.name}'. Migrating FSM state to '{runtime_dir.name}'!")
                                break
                            elif any_cand is None:
                                any_cand = c_data

            if not active_cand:
                # Fallback к бэкапу рантайма, если в куполах пусто
                backup_latest = BASE_DIR / "CACHE" / "backups" / "runtime_backup_latest.json"
                if backup_latest.exists():
                    try:
                        strategies_pool = b_raw.get("strategies", b_raw) if isinstance(b_raw, dict) else {}
                        for d_name, d_syms in strategies_pool.items():
                            if isinstance(d_syms, dict) and symbol.lower() in d_syms:
                                cand_b = d_syms[symbol.lower()]
                                if isinstance(cand_b, dict) and any(
                                    isinstance(s_val, dict) and (s_val.get("in_position") or abs(float(s_val.get("total_volume", 0.0))) > 1e-6)
                                    for s_val in cand_b.values()
                                ):
                                    active_cand = cand_b
                                    logger.info(f"[{symbol}] Found active position state in backup '{backup_latest.name}'. Migrating FSM state!")
                                    break
                    except Exception:
                        pass

            if active_cand:
                existing = active_cand
            elif any_cand:
                existing = any_cand
            else:
                legacy_json = (DATA_DIR / "runtime") / f"{symbol.lower()}.json"
                legacy_file = (DATA_DIR / "runtime") / f"{symbol.lower()}.pkl"
                if legacy_json.exists():
                    existing = Utils.read_json_file(legacy_json)
                elif legacy_file.exists():
                    try:
                        import pickle
                        with open(legacy_file, "rb") as fp:
                            existing = pickle.load(fp)
                    except Exception:
                        existing = {}

        new_data: Dict[str, Any] = {}
        for side, side_cfg in side_configs.items():
            new_side = copy.deepcopy(side_cfg)
            exist_side = existing.get(side, {})
            # Если позиция открыта, сохраняем состояние FSM
            if exist_side.get("in_position", False) or abs(float(exist_side.get("total_volume", 0.0))) > 1e-6:
                for state_key in [
                    "total_volume", "avg_entry_price", "pre_avg_price",
                    "initial_entry_price", "open_time", "next_avg_price",
                    "fallback_price", "in_position", "in_position_papper",
                    "is_finished", "pending_avg", "pending_rolling_tp", "recent_closes",
                    "trailing_tp_state", "trailing_grid_state", "eff_invest"
                ]:
                    if state_key in exist_side:
                        new_side[state_key] = copy.deepcopy(exist_side[state_key])
                tsd = getattr(strategy, "trend_size_cfg", {}) or {}
                if not tsd.get("enabled", False) or tsd.get("mode") == "static":
                    new_side["eff_invest"] = float(new_side.get("invest_size", 0.0))
                if "grid" in exist_side and "grid" in new_side:
                    for g_idx, g_val in exist_side["grid"].items():
                        if g_idx in new_side["grid"] and isinstance(g_val, dict):
                            for k in ["is_active", "price", "activated_at", "timestamp", "activation_price"]:
                                if k in g_val:
                                    new_side["grid"][g_idx][k] = g_val[k]
                            # Правило: исполненный уровень (is_active=True) никогда не может быть взведён
                            if new_side["grid"][g_idx].get("is_active"):
                                new_side["grid"][g_idx]["is_armed"] = False
                                new_side["grid"][g_idx].pop("armed_at_drawdown", None)
                                new_side["grid"][g_idx].pop("armed_timestamp", None)
                                if "trailing_grid_state" in new_side and isinstance(new_side["trailing_grid_state"], dict):
                                    armed_dict = new_side["trailing_grid_state"].get("armed_levels", {})
                                    if isinstance(armed_dict, dict) and g_idx in armed_dict:
                                        armed_dict.pop(g_idx, None)
                            elif new_side["grid"][g_idx].get("enabled", True) is False:
                                new_side["grid"][g_idx]["is_active"] = False
                                new_side["grid"][g_idx]["is_armed"] = False
                                new_side["grid"][g_idx]["price"] = None
                                new_side["grid"][g_idx]["activation_price"] = None
                                new_side["grid"][g_idx].pop("armed_at_drawdown", None)
                                new_side["grid"][g_idx].pop("armed_timestamp", None)
                                if "trailing_grid_state" in new_side and isinstance(new_side["trailing_grid_state"], dict):
                                    armed_dict = new_side["trailing_grid_state"].get("armed_levels", {})
                                    if isinstance(armed_dict, dict) and g_idx in armed_dict:
                                        armed_dict.pop(g_idx, None)

                # Жесткий инвариант для STRKUSDT SHORT в позиции:
                # Уровни 0, 1, 2, 3, 4 уже исполнены и сокращены.
                # Следующие активные ступени строго L5 (-144%/-155%) и L6 (-340%/-360%).
                if symbol.upper() == "STRKUSDT" and side == "SHORT" and (new_side.get("in_position") or abs(float(new_side.get("total_volume", 0.0))) > 1e-6):
                    for executed_lvl in ("0", "1", "2", "3", "4"):
                        if executed_lvl in new_side.get("grid", {}):
                            new_side["grid"][executed_lvl]["is_active"] = True
                            new_side["grid"][executed_lvl]["is_armed"] = False
                            new_side["grid"][executed_lvl].pop("armed_at_drawdown", None)
                            new_side["grid"][executed_lvl].pop("armed_timestamp", None)
                            if "trailing_grid_state" in new_side and isinstance(new_side["trailing_grid_state"], dict):
                                armed_dict = new_side["trailing_grid_state"].get("armed_levels", {})
                                if isinstance(armed_dict, dict) and executed_lvl in armed_dict:
                                    armed_dict.pop(executed_lvl, None)
                    for future_lvl in ("5", "6"):
                        if future_lvl in new_side.get("grid", {}):
                            new_side["grid"][future_lvl]["is_active"] = False
                            new_side["grid"][future_lvl]["is_armed"] = False

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
                    "pending_rolling_tp": False,
                    "recent_closes": copy.deepcopy(exist_side.get("recent_closes", [])),
                    "trailing_tp_state": {},
                    "trailing_grid_state": {},
                    "eff_invest": 0.0
                })
                if "grid" in new_side:
                    for grid_cfg in new_side["grid"].values():
                        grid_cfg["is_active"] = False
                        grid_cfg["price"] = None
                        grid_cfg["activated_at"] = None
                        grid_cfg["timestamp"] = None
                        grid_cfg["activation_price"] = None
                if "tp_map" in new_side:
                    for tp_cfg in new_side["tp_map"].values():
                        tp_cfg["is_active"] = False

            new_data[side] = new_side

        Utils.atomic_write_json(target_file, new_data, indent=4)

        # Удаляем устаревший .pkl под куполом, если остался
        if target_pkl.exists():
            try:
                target_pkl.unlink()
            except Exception:
                pass

        # Очищаем устаревшие дубликаты файлов в корне runtime/
        if runtime_dir != (DATA_DIR / "runtime"):
            root_legacy_pkl = (DATA_DIR / "runtime") / f"{symbol.lower()}.pkl"
            root_legacy_json = (DATA_DIR / "runtime") / f"{symbol.lower()}.json"
            for lp in [root_legacy_pkl, root_legacy_json]:
                if lp.exists():
                    try:
                        lp.unlink()
                    except Exception:
                        pass

        safe_name = strategy.name.encode("ascii", "ignore").decode("ascii").strip()
        logger.info(
            f"[StrategyApplicator] Built runtime config for {symbol} "
            f"under dome '{strategy.strategy_id}' ({safe_name}) at {target_file}."
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

    def apply_strategy_to_symbol(
        self,
        strategy_id: str,
        symbol: str,
        registry: Optional[StrategyRegistry] = None,
        target_dir: Optional[Path] = None,
        force: bool = False
    ) -> Path:
        """
        Метод-алиас для построения и сохранения рантайма для монеты по ID стратегии.
        """
        reg = registry or StrategyRegistry()
        strat = reg.active_strategies.get(strategy_id)
        if not strat and reg.is_symbol_active(symbol):
            strat = reg.get_strategy_for_symbol(symbol)
        if not strat:
            raise KeyError(f"[StrategyApplicator] Стратегия '{strategy_id}' не найдена в реестре!")
        return self.build_runtime_for_symbol(symbol, strat, target_dir=target_dir, force=force)
