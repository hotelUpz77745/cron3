# ============================================================
# FILE: CORE/strategy_applicator.py
# ROLE: Merges base templates with strategy overrides and builds runtime configs
# ============================================================

import copy
import json
from pathlib import Path
from typing import Dict, Any, Optional

from consts import DATA_DIR, BASE_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.strategy_registry import StrategyConfig, StrategyRegistry
from CORE.GRID.fsm_grid_resolver import FSMGridResolver

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
        """
        side_cfgs = {}
        for side in ("LONG", "SHORT"):
            # Извлекаем конфигурацию для стороны (из raw_meta[side] с наложением overrides[side])
            side_dict = {}
            if hasattr(strategy, "raw_meta") and isinstance(strategy.raw_meta, dict):
                if side in strategy.raw_meta and isinstance(strategy.raw_meta[side], dict):
                    side_dict = copy.deepcopy(strategy.raw_meta[side])
            if hasattr(strategy, "overrides") and isinstance(strategy.overrides, dict):
                if side in strategy.overrides and isinstance(strategy.overrides[side], dict):
                    side_dict.update(strategy.overrides[side])

            side_data = copy.deepcopy(side_dict) if side_dict else copy.deepcopy(self.DEFAULT_FALLBACK_BASE[side])

            # 1. Invest Size (приоритет: get_invest_size -> per-side / per-symbol -> side_dict -> ValueError)
            if hasattr(strategy, "get_invest_size"):
                try:
                    side_data["invest_size"] = strategy.get_invest_size(side, symbol=symbol)
                except ValueError:
                    if symbol is None:
                        side_data["invest_size"] = None
                    else:
                        raise
            elif side == "LONG" and getattr(strategy, "invest_size_long", None) is not None:
                val = strategy.invest_size_long
                if isinstance(val, dict):
                    if symbol:
                        if symbol.upper() not in val:
                            raise ValueError(f"Missing required invest_size for symbol '{symbol}' under strategy '{getattr(strategy, 'strategy_id', 'unknown')}'")
                        side_data["invest_size"] = Utils.safe_float(val[symbol.upper()], 0.0)
                    else:
                        side_data["invest_size"] = None
                elif isinstance(val, (int, float)):
                    side_data["invest_size"] = Utils.safe_float(val, 0.0)
                elif symbol is None:
                    side_data["invest_size"] = None
                else:
                    raise ValueError(f"Missing required invest_size for symbol '{symbol}' under strategy '{getattr(strategy, 'strategy_id', 'unknown')}'")
            elif side == "SHORT" and getattr(strategy, "invest_size_short", None) is not None:
                val = strategy.invest_size_short
                if isinstance(val, dict):
                    if symbol:
                        if symbol.upper() not in val:
                            raise ValueError(f"Missing required invest_size for symbol '{symbol}' under strategy '{getattr(strategy, 'strategy_id', 'unknown')}'")
                        side_data["invest_size"] = Utils.safe_float(val[symbol.upper()], 0.0)
                    else:
                        side_data["invest_size"] = None
                elif isinstance(val, (int, float)):
                    side_data["invest_size"] = Utils.safe_float(val, 0.0)
                elif symbol is None:
                    side_data["invest_size"] = None
                else:
                    raise ValueError(f"Missing required invest_size for symbol '{symbol}' under strategy '{getattr(strategy, 'strategy_id', 'unknown')}'")
            elif isinstance(getattr(strategy, "invest_size", None), dict) and side in getattr(strategy, "invest_size"):
                val = getattr(strategy, "invest_size")[side]
                if isinstance(val, dict):
                    if symbol:
                        if symbol.upper() not in val:
                            raise ValueError(f"Missing required invest_size for symbol '{symbol}' under strategy '{getattr(strategy, 'strategy_id', 'unknown')}'")
                        side_data["invest_size"] = Utils.safe_float(val[symbol.upper()], 0.0)
                    else:
                        side_data["invest_size"] = None
                elif isinstance(val, (int, float)):
                    side_data["invest_size"] = Utils.safe_float(val, 0.0)
                elif symbol is None:
                    side_data["invest_size"] = None
                else:
                    raise ValueError(f"Missing required invest_size for symbol '{symbol}' under strategy '{getattr(strategy, 'strategy_id', 'unknown')}'")
            elif isinstance(getattr(strategy, "invest_size", None), (int, float)):
                side_data["invest_size"] = Utils.safe_float(strategy.invest_size, 0.0)
            elif "invest_size" in side_dict and side_dict["invest_size"] is not None:
                side_data["invest_size"] = Utils.safe_float(side_dict["invest_size"], 0.0)
            elif symbol is None:
                side_data["invest_size"] = None
            else:
                raise ValueError(
                    f"Missing required invest_size for symbol '{symbol}' ({side}) under strategy '{getattr(strategy, 'strategy_id', 'unknown')}'"
                )

            # 2. Leverage (приоритет: side_dict -> strategy.leverage[side] -> strategy.leverage)
            if "leverage" in side_dict:
                side_data["leverage"] = int(side_dict["leverage"])
            elif isinstance(strategy.leverage, dict) and side in strategy.leverage:
                side_data["leverage"] = int(strategy.leverage[side])
            elif isinstance(strategy.leverage, (int, float)):
                side_data["leverage"] = int(strategy.leverage)

            # 3. Enable / Enabled флаг стороны (высший приоритет — active_strategies на уровне клиента-оператора)
            act_side = getattr(strategy, "active_settings", {}).get(side)
            if isinstance(act_side, dict) and "enabled" in act_side:
                val = bool(act_side["enabled"])
            elif isinstance(act_side, dict) and "enable" in act_side:
                val = bool(act_side["enable"])
            elif "enabled" in side_dict:
                val = bool(side_dict["enabled"])
            elif "enable" in side_dict:
                val = bool(side_dict["enable"])
            else:
                val = True
            side_data["enable"] = val
            side_data["enabled"] = val

            side_data["strategy_id"] = getattr(strategy, "strategy_id", "unknown")
            side_data["strategy_name"] = getattr(strategy, "name", getattr(strategy, "strategy_id", "unknown"))

            # 4. Применение сетки (grid): строго из стороны side_dict["grid"], либо общей
            grid_override = (
                side_dict.get("grid")
                or (getattr(strategy, "raw_meta", {}) or {}).get("grid")
                or (getattr(strategy, "overrides", {}) or {}).get("grid")
            )
            if grid_override:
                side_data["grid"] = copy.deepcopy(grid_override)

            active_regime = getattr(strategy, "active_settings", {}).get("first_order_weight_regime")
            raw_regime = (
                (getattr(strategy, "raw_meta", {}) or {}).get("first_order_weight_regime")
                or (getattr(strategy, "overrides", {}) or {}).get("first_order_weight_regime")
            )
            if isinstance(raw_regime, dict) and active_regime is not None and not isinstance(active_regime, dict):
                regime_cfg = copy.deepcopy(raw_regime)
                regime_cfg["mode"] = active_regime
            else:
                regime_cfg = active_regime or raw_regime

            if regime_cfg and "grid" in side_data and "0" in side_data["grid"]:
                mode_val = regime_cfg.get("mode") if isinstance(regime_cfg, dict) else regime_cfg
                base_raw_vol = side_data["grid"]["0"].get("volume")
                fallback_vol = 12.0 if side.upper() == "LONG" else 7.0
                curr_default = Utils.safe_float(base_raw_vol, fallback_vol) if base_raw_vol is not None else fallback_vol

                if mode_val not in (
                    "auto", "auto_ema", "ema_trend",
                    "off", "null", "none", False, None
                ):
                    resolved_vol = StrategyApplicator.resolve_first_order_volume(
                        regime_cfg=regime_cfg,
                        side=side,
                        default_vol=curr_default
                    )
                    side_data["grid"]["0"]["volume"] = resolved_vol
                elif side_data["grid"]["0"].get("volume") is None:
                    # Для динамических режимов при генерации рантайма задаем канонический базовый объем (из пресетов или дефолта)
                    from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager
                    side_data["grid"]["0"]["volume"] = FirstOrderRegimeManager.resolve_static_volume(
                        regime_cfg=regime_cfg,
                        side=side,
                        default_vol=curr_default
                    )

            # 5. Если Super Grid отключен, гарантируем сброс super_indent в None (Инвариант 3)
            if not getattr(strategy, "super_grid_enabled", False) and "grid" in side_data:
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

            # Применение ограничения TP снизу: min_tp_indent из стратегии или base_cfg
            min_tp_floor = None
            if getattr(strategy, "min_tp_indent", None) is not None:
                min_tp_floor = float(strategy.min_tp_indent)
            elif hasattr(strategy, "raw_meta") and strategy.raw_meta and strategy.raw_meta.get("min_tp_indent") is not None:
                min_tp_floor = float(strategy.raw_meta["min_tp_indent"])
            elif "min_tp_indent" in self.base_cfg and self.base_cfg["min_tp_indent"] is not None:
                min_tp_floor = float(self.base_cfg["min_tp_indent"])

            if min_tp_floor is not None and "tp_map" in side_data and isinstance(side_data["tp_map"], dict):
                for tp_step in side_data["tp_map"].values():
                    if "indent" in tp_step and tp_step["indent"] is not None:
                        tp_step["indent"] = max(min_tp_floor, float(tp_step["indent"]))
                    if "fallback_indent" in tp_step and tp_step["fallback_indent"] is not None:
                        tp_step["fallback_indent"] = max(min_tp_floor, float(tp_step["fallback_indent"]))

            # 7. Декларация FSM-флагов состояний в структуре сетки и TP-карты
            if "grid" in side_data and isinstance(side_data["grid"], dict):
                for lvl_data in side_data["grid"].values():
                    if isinstance(lvl_data, dict):
                        lvl_data.setdefault("is_active", False)
                        lvl_data.setdefault("is_armed", False)
                        lvl_data.setdefault("price", None)
                        lvl_data.setdefault("activation_price", None)
                        lvl_data.setdefault("fill_price", None)
                        lvl_data.setdefault("fill_qty", None)
                        lvl_data.setdefault("peeled", False)
                        lvl_data.setdefault("peeled_count", 0)

            if "tp_map" in side_data and isinstance(side_data["tp_map"], dict):
                for tp_step in side_data["tp_map"].values():
                    if isinstance(tp_step, dict):
                        tp_step.setdefault("is_active", False)
                        tp_step.setdefault("is_armed", False)

            # 8. Применение tp_purpose и margin_type
            if "tp_purpose" in side_dict:
                side_data["tp_purpose"] = side_dict["tp_purpose"]
            elif hasattr(strategy, "raw_meta") and "tp_purpose" in strategy.raw_meta:
                side_data["tp_purpose"] = strategy.raw_meta["tp_purpose"]
            elif "tp_purpose" in strategy.overrides:
                side_data["tp_purpose"] = strategy.overrides["tp_purpose"]

            # 7. Применение tp_purpose и margin_type (margin_type строго с уровня клиента-оператора)
            act_mt = (
                getattr(strategy, "active_settings", {}).get("margin_type")
                or getattr(strategy, "active_settings", {}).get(side, {}).get("margin_type")
            )
            if act_mt:
                side_data["margin_type"] = str(act_mt).upper()
            elif hasattr(strategy, "margin_type") and strategy.margin_type:
                side_data["margin_type"] = str(strategy.margin_type).upper()
            elif "margin_type" in side_dict:
                side_data["margin_type"] = str(side_dict["margin_type"]).upper()
            elif hasattr(strategy, "raw_meta") and "margin_type" in strategy.raw_meta:
                side_data["margin_type"] = str(strategy.raw_meta["margin_type"]).upper()
            elif "margin_type" in getattr(strategy, "overrides", {}):
                side_data["margin_type"] = str(strategy.overrides["margin_type"]).upper()
            else:
                side_data["margin_type"] = "CROSSED"

            # 8. Применение avg_free_volume_behavior и auto_closing
            if "avg_free_volume_behavior" in side_dict:
                side_data["avg_free_volume_behavior"] = side_dict["avg_free_volume_behavior"]
            elif hasattr(strategy, "raw_meta") and "avg_free_volume_behavior" in strategy.raw_meta:
                side_data["avg_free_volume_behavior"] = strategy.raw_meta["avg_free_volume_behavior"]
            elif hasattr(strategy, "overrides") and "avg_free_volume_behavior" in strategy.overrides:
                side_data["avg_free_volume_behavior"] = strategy.overrides["avg_free_volume_behavior"]

            side_cfgs[side] = side_data

        return side_cfgs

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
                                    isinstance(s_val, dict) and (s_val.get("in_position") or abs(Utils.safe_float(s_val.get("total_volume"), 0.0)) > 0.0)
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
                            b_raw = Utils.read_json_file(backup_latest) or {}
                            strategies_pool = b_raw.get("strategies", b_raw) if isinstance(b_raw, dict) else {}
                            for d_name, d_syms in strategies_pool.items():
                                if isinstance(d_syms, dict) and symbol.lower() in d_syms:
                                    cand_b = d_syms[symbol.lower()]
                                    if isinstance(cand_b, dict) and any(
                                        isinstance(s_val, dict) and (s_val.get("in_position") or abs(Utils.safe_float(s_val.get("total_volume"), 0.0)) > 0.0)
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
        else:
            logger.info(f"[{symbol}] Forced runtime rebuild (force=True). Previous FSM cache ignored.")

        new_data: Dict[str, Any] = {}
        for side, side_cfg in side_configs.items():
            new_side = copy.deepcopy(side_cfg)
            exist_side = existing.get(side, {})
            # Если позиция открыта, сохраняем состояние FSM
            if exist_side.get("in_position", False) or abs(Utils.safe_float(exist_side.get("total_volume"), 0.0)) > 1e-6:
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
                                import pickle
                                with open(rf, "rb") as fp:
                                    c_data = pickle.load(fp)
                            if isinstance(c_data, dict):
                                has_pos = any(
                                    isinstance(s_val, dict) and (
                                        s_val.get("in_position")
                                        or abs(Utils.safe_float(s_val.get("total_volume"), 0.0)) > 1e-6
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
