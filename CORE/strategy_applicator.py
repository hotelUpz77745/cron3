# ============================================================
# FILE: CORE/strategy_applicator.py
# ROLE: Merges base templates with strategy overrides and builds runtime configs
# ============================================================

import copy
import json
import pickle
from pathlib import Path
from typing import Dict, Any, Optional

from consts import DATA_DIR, BASE_DIR, FLOAT_ZERO_EPSILON
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.strategy_registry import StrategyConfig, StrategyRegistry
from CORE.GRID.fsm_grid_resolver import FSMGridResolver
from CORE.APPLICATOR.side_config_builder import SideConfigBuilder

logger = UnifiedLogger("StrategyApplicator")

MIN_TAKE_PROFIT_PCT: float = 0.5


class StrategyApplicator:
    """
    Применяет параметры и переопределения стратегии из паспортов universes/<template>.json и app.json
    и генерирует изолированные JSON рантайм-конфигурации в CFG/runtime/<strategy_id>/<symbol>.json.
    """

    DEFAULT_FALLBACK_BASE: Dict[str, Any] = {
        "LONG": {
            "enable": True,
            "invest_size": None,
            "leverage": 20,
            "tp_purpose": "self",
            "margin_type": "CROSSED",
            "grid": {},
            "tp_map": {}
        },
        "SHORT": {
            "enable": True,
            "invest_size": None,
            "leverage": 20,
            "tp_purpose": "self",
            "margin_type": "CROSSED",
            "grid": {},
            "tp_map": {}
        }
    }

    def __init__(self, base_cfg_path: Optional[Path] = None):
        self.base_cfg_path: Optional[Path] = base_cfg_path
        self.base_cfg: Dict[str, Any] = copy.deepcopy(self.DEFAULT_FALLBACK_BASE)

    def build_side_configs(self, strategy: StrategyConfig, symbol: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        Создает стороны LONG и SHORT для символа напрямую из спецификации стратегии
        с жестким соблюдением правила TP >= min_tp_indent
        и поддержкой асимметричных настроек per-side (LONG / SHORT) и per-symbol invest_size.
        Делегирует исполнение специализированному SideConfigBuilder.
        """
        return SideConfigBuilder.build_side_configs(
            strategy=strategy,
            symbol=symbol,
            default_fallback_base=self.DEFAULT_FALLBACK_BASE,
            base_cfg=self.base_cfg,
        )

    @staticmethod
    def _get_fsm_resolution_cfg(strategy: Optional[StrategyConfig] = None) -> dict:
        return FSMGridResolver.get_fsm_resolution_cfg(strategy)

    @staticmethod
    def resolve_first_order_volume(regime_cfg: Any, side: str, default_vol: float) -> float:
        return FSMGridResolver.resolve_first_order_volume(regime_cfg, side, default_vol)

    @staticmethod
    def resolve_executed_grid_levels(strategy: StrategyConfig, symbol: str, side: str, new_side: dict, exist_side: dict):
        return FSMGridResolver.resolve_executed_grid_levels(strategy, symbol, side, new_side, exist_side)

    @classmethod
    def resolve_position_state_grid(
        cls,
        state: Any,
        strategy: Optional[Any] = None,
        invest_size: Optional[float] = None,
        tolerance_pct: Optional[float] = None,
        current_price: Optional[float] = None,
    ) -> bool:
        return FSMGridResolver.resolve_position_state_grid(
            state=state,
            strategy=strategy,
            invest_size=invest_size,
            tolerance_pct=tolerance_pct,
            current_price=current_price,
        )

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

        side_configs = self.build_side_configs(strategy, symbol=symbol)

        existing = {}
        if not force:
            if target_file.exists():
                existing = Utils.read_json_file(target_file)
            elif target_pkl.exists():
                try:
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
                                    with open(cand_pkl, "rb") as fp:
                                        c_data = pickle.load(fp)
                                except Exception:
                                    pass
                            if c_data and isinstance(c_data, dict):
                                has_pos = any(
                                    isinstance(s_val, dict) and (s_val.get("in_position") or abs(Utils.safe_float(s_val.get("total_volume"), 0.0)) > 0.0)
                                    for s_val in c_data.values()
                                )
                                if has_pos:
                                    active_cand = c_data
                                    logger.info(f"[{symbol}] Found active position state in existing dome '{sub_dir.name}'. Migrating FSM state to '{runtime_dir.name}'!")
                                    break
                                elif any_cand is None:
                                    any_cand = c_data


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
                            with open(legacy_file, "rb") as fp:
                                existing = pickle.load(fp)
                        except Exception:
                            existing = {}
        else:
            logger.info(f"[{symbol}] Forced runtime rebuild (force=True). Previous FSM cache ignored.")

        new_data: Dict[str, Any] = {}
        for side, side_cfg in side_configs.items():
            new_side = copy.deepcopy(side_cfg)
            exist_side = existing.get(side, {})
            # Если позиция открыта, сохраняем состояние FSM
            if exist_side.get("in_position", False) or abs(Utils.safe_float(exist_side.get("total_volume"), 0.0)) > FLOAT_ZERO_EPSILON:
                for state_key in [
                    "total_volume", "avg_entry_price", "pre_avg_price",
                    "initial_entry_price", "open_time", "next_avg_price",
                    "fallback_price", "in_position", "in_position_papper",
                    "is_finished", "pending_avg", "pending_rolling_tp", "recent_closes",
                    "trailing_tp_state", "trailing_grid_state"
                ]:
                    if state_key in exist_side:
                        new_side[state_key] = copy.deepcopy(exist_side[state_key])

                # Актуализация invest_size и eff_invest из свежего конфига стратегии (на лету при перезапуске)
                new_inv = Utils.safe_float(new_side.get("invest_size"), 0.0)
                old_inv = Utils.safe_float(exist_side.get("invest_size"), 0.0)
                old_eff = Utils.safe_float(exist_side.get("eff_invest"), 0.0)

                tsd = getattr(strategy, "trend_size_cfg", {}) or {}
                if not tsd.get("enabled", False) or tsd.get("mode") == "static":
                    new_side["eff_invest"] = new_inv
                elif old_inv > 0 and old_eff > 0 and new_inv != old_inv:
                    ratio = new_inv / old_inv
                    new_side["eff_invest"] = round(old_eff * ratio, 4)
                else:
                    new_side["eff_invest"] = new_inv if new_inv > 0 else old_eff
                if "grid" in exist_side and isinstance(exist_side["grid"], dict) and exist_side["grid"]:
                    # ДЛЯ АКТИВНОЙ ПОЗИЦИИ: Сетка позиции на жестком диске священна!
                    # Сохраняем геометрию сетки (отступы, курки, объемы, флаги) из рантайма позиции,
                    # чтобы перезапуск никогда не подменял сетку открытой сделки дефолтами шаблона.
                    merged_grid = copy.deepcopy(exist_side["grid"])
                    # Если в свежем шаблоне появились новые более глубокие ступени, аккуратно мержим их
                    if "grid" in new_side and isinstance(new_side["grid"], dict):
                        for tmpl_k, tmpl_v in new_side["grid"].items():
                            if tmpl_k not in merged_grid and isinstance(tmpl_v, dict):
                                merged_grid[tmpl_k] = copy.deepcopy(tmpl_v)
                                merged_grid[tmpl_k]["is_active"] = False
                                merged_grid[tmpl_k]["is_armed"] = False
                    new_side["grid"] = merged_grid

                    for g_idx, g_val in new_side["grid"].items():
                        if isinstance(g_val, dict):
                            # Правило: исполненный уровень (is_active=True) никогда не может быть взведён
                            if g_val.get("is_active"):
                                g_val["is_armed"] = False
                                g_val.pop("armed_at_drawdown", None)
                                g_val.pop("armed_timestamp", None)
                                if "trailing_grid_state" in new_side and isinstance(new_side["trailing_grid_state"], dict):
                                    armed_dict = new_side["trailing_grid_state"].get("armed_levels", {})
                                    if isinstance(armed_dict, dict) and g_idx in armed_dict:
                                        armed_dict.pop(g_idx, None)
                            elif g_val.get("enabled", True) is False:
                                g_val["is_active"] = False
                                g_val["is_armed"] = False
                                g_val["price"] = None
                                g_val["activation_price"] = None
                                g_val.pop("armed_at_drawdown", None)
                                g_val.pop("armed_timestamp", None)
                                if "trailing_grid_state" in new_side and isinstance(new_side["trailing_grid_state"], dict):
                                    armed_dict = new_side["trailing_grid_state"].get("armed_levels", {})
                                    if isinstance(armed_dict, dict) and g_idx in armed_dict:
                                        armed_dict.pop(g_idx, None)

                # Универсальное определение исполненных уровней сетки по FSM-состоянию и конфигу (без хардкодов символов)
                self.resolve_executed_grid_levels(strategy, symbol, side, new_side, exist_side)

                if "tp_map" in exist_side and isinstance(exist_side["tp_map"], dict) and exist_side["tp_map"]:
                    # Сохраняем карту TP открытой позиции
                    merged_tp = copy.deepcopy(exist_side["tp_map"])
                    if "tp_map" in new_side and isinstance(new_side["tp_map"], dict):
                        for tmpl_k, tmpl_v in new_side["tp_map"].items():
                            if tmpl_k not in merged_tp and isinstance(tmpl_v, dict):
                                merged_tp[tmpl_k] = copy.deepcopy(tmpl_v)
                    new_side["tp_map"] = merged_tp
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
                if "grid" in new_side and isinstance(new_side["grid"], dict):
                    for grid_cfg in new_side["grid"].values():
                        if isinstance(grid_cfg, dict):
                            grid_cfg["is_active"] = False
                            grid_cfg["is_armed"] = False
                            grid_cfg["price"] = None
                            grid_cfg["activated_at"] = None
                            grid_cfg["timestamp"] = None
                            grid_cfg["activation_price"] = None
                            grid_cfg["fill_price"] = None
                            grid_cfg["fill_qty"] = None
                            grid_cfg["peeled"] = False
                            grid_cfg["peeled_count"] = 0
                if "tp_map" in new_side and isinstance(new_side["tp_map"], dict):
                    for tp_cfg in new_side["tp_map"].values():
                        if isinstance(tp_cfg, dict):
                            tp_cfg["is_active"] = False
                            tp_cfg["is_armed"] = False

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
        """Генерирует рантайм-файлы для всех монет из активных стратегий и безопасно удаляет сиротские файлы неактивных монет без позиций."""
        results = {}
        for symbol in registry.get_all_active_symbols():
            strat = registry.get_strategy_for_symbol(symbol)
            path = self.build_runtime_for_symbol(symbol, strat, target_dir=target_dir, force=force)
            results[symbol] = path

        # Очистка сиротских файлов (orphan runtime files) для неактивных монет, у которых нет позиции
        for strat_id, strat in registry.active_strategies.items():
            strat_dir = target_dir or (DATA_DIR / "runtime" / strat_id)
            if not strat_dir.exists():
                continue
            strat_syms = {s.upper() for s in getattr(strat, "symbols", [])}
            for rf in list(strat_dir.iterdir()):
                if rf.suffix.lower() in (".json", ".pkl"):
                    sym_name = rf.stem.upper()
                    if sym_name not in strat_syms:
                        can_delete = False
                        try:
                            c_data = None
                            if rf.suffix.lower() == ".json":
                                c_data = Utils.read_json_file(rf)
                            elif rf.suffix.lower() == ".pkl":
                                with open(rf, "rb") as fp:
                                    c_data = pickle.load(fp)
                            if isinstance(c_data, dict):
                                has_pos = any(
                                    isinstance(s_val, dict) and (
                                        s_val.get("in_position")
                                        or abs(Utils.safe_float(s_val.get("total_volume"), 0.0)) > FLOAT_ZERO_EPSILON
                                    )
                                    for s_val in c_data.values()
                                )
                                if not has_pos:
                                    can_delete = True
                            else:
                                can_delete = True
                        except Exception:
                            can_delete = False

                        if can_delete:
                            try:
                                rf.unlink()
                                logger.info(f"[StrategyApplicator] Pruned inactive orphan runtime file: {rf}")
                            except Exception as e:
                                logger.warning(f"[StrategyApplicator] Failed to prune orphan runtime file {rf}: {e}")
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
