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

            # 4.1. Адаптация веса первого ордера grid_0 по режиму рынка (Bullish 12/7, Neutral 9/9, Bearish 7/12)
            regime_cfg = (
                getattr(strategy, "active_settings", {}).get("first_order_weight_regime")
                or (getattr(strategy, "overrides", {}) or {}).get("first_order_weight_regime")
                or (getattr(strategy, "raw_meta", {}) or {}).get("first_order_weight_regime")
            )
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

            # 7. Применение tp_purpose и margin_type
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

            # auto_closing и trend_size_depender вынесены на уровень стратегии и не дублируются в рантайм позиций
            tsd_val = getattr(strategy, "trend_size_depender", None) or getattr(strategy, "sizing_mode", None)
            if tsd_val:
                side_data["sizing_mode"] = tsd_val

            side_cfgs[side] = side_data

        return side_cfgs

    @staticmethod
    def _get_fsm_resolution_cfg(strategy: Optional[StrategyConfig] = None) -> dict:
        """
        Получает настройки fsm_level_resolution:
        1. Из конфига стратегии/вселенной (raw_meta), если заданы индивидуально.
        2. Из глобального CFG/app.json ('fsm_level_resolution').
        3. Безопасные дефолты (enabled=True, mode='cumulative_volume_threshold', volume_tolerance_pct=10.0).
        """
        raw_meta = getattr(strategy, "raw_meta", {}) if hasattr(strategy, "raw_meta") and strategy else {}
        cfg = raw_meta.get("fsm_level_resolution")
        if not cfg or not isinstance(cfg, dict):
            try:
                from consts import _CFG
                cfg = _CFG.get("fsm_level_resolution")
            except Exception:
                cfg = None
        if not cfg or not isinstance(cfg, dict):
            cfg = {"enabled": True, "mode": "cumulative_volume_threshold", "volume_tolerance_pct": 10.0}
        return cfg

    @staticmethod
    def resolve_first_order_volume(regime_cfg: Any, side: str, default_vol: float) -> float:
        """
        «Правило кулака Сильной руки» (Strong Hand Fist Rule):
        Разрешает объем капитала стартового удара (grid_0) для стороны side (LONG / SHORT).
        ВНИМАНИЕ: Управляет ИСКЛЮЧИТЕЛЬНО первым ордером (grid_0), уровни усреднения сетки (L1+) не затрагиваются.
        Поддерживает:
        - Литералы: 'bullish' (12% Long / 7% Short), 'neutral' (9% Long / 9% Short), 'bearish' (7% Long / 12% Short).
        - Числа: 1 (bullish), 0 (neutral), -1 (bearish).
        - Авто-режим: 'auto' / 'auto_ema' (динамический выбор по глобальной EMA).
        - Структурированный словарь с 'mode' и 'presets'.
        """
        mode = "bullish"
        presets = {
            "bullish": {"LONG": 12.0, "SHORT": 7.0},
            "neutral": {"LONG": 9.0, "SHORT": 9.0},
            "bearish": {"LONG": 7.0, "SHORT": 12.0},
        }
        if isinstance(regime_cfg, dict):
            mode = regime_cfg.get("mode", "bullish")
            if "presets" in regime_cfg and isinstance(regime_cfg["presets"], dict):
                for k, v in regime_cfg["presets"].items():
                    if isinstance(v, dict):
                        presets[str(k).lower()] = {s.upper(): float(vol) for s, vol in v.items()}
        elif isinstance(regime_cfg, (str, int)):
            mode = regime_cfg

        # Отключение фичи: off / null / none / false / disabled -> возврат базового объема из сетки
        if mode is None or str(mode).lower().strip() in ("off", "null", "none", "false", "disabled"):
            return default_vol

        # Нормализация режима
        if mode in (1, "1", "bull", "bullish", "long_heavy"):
            mode_key = "bullish"
        elif mode in (0, "0", "flat", "neutral", "symmetric"):
            mode_key = "neutral"
        elif mode in (-1, "-1", "bear", "bearish", "short_heavy"):
            mode_key = "bearish"
        else:
            mode_key = str(mode).lower()

        preset = presets.get(mode_key)
        if preset and side.upper() in preset:
            return float(preset[side.upper()])
        return default_vol

    @staticmethod
    def resolve_executed_grid_levels(strategy: StrategyConfig, symbol: str, side: str, new_side: dict, exist_side: dict):
        """
        Универсально определяет исполненные уровни сетки по FSM-состоянию позиции и конфигу стратегии/app.json.
        Полный отказ от хардкодов имен символов и номеров уровней.
        Все правила читаются из 'fsm_level_resolution' (app.json / strategy).
        """
        exist_vol = Utils.safe_float(exist_side.get("total_volume"), 0.0)
        if not (exist_side.get("in_position", False) or abs(exist_vol) > 1e-6):
            return

        resolution_cfg = StrategyApplicator._get_fsm_resolution_cfg(strategy)
        if not resolution_cfg.get("enabled", True):
            return

        tolerance_pct = Utils.safe_float(resolution_cfg.get("volume_tolerance_pct"), 10.0)
        tolerance_factor = 1.0 - (tolerance_pct / 100.0)

        current_vol = abs(exist_vol)
        entry_price = Utils.safe_float(exist_side.get("avg_entry_price"), 0.0)
        current_notional = current_vol * entry_price
        invest_size = Utils.safe_float(new_side.get("invest_size"), 0.0)
        if invest_size <= 0:
            return

        grid = new_side.get("grid", {})
        exist_grid = exist_side.get("grid", {})
        trailing_state = new_side.get("trailing_grid_state", {})
        armed_dict = trailing_state.get("armed_levels", {}) if isinstance(trailing_state, dict) else {}

        # Сортируем уровни по числовому индексу (0, 1, 2, ...)
        sorted_level_keys = sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else 999)

        cumulative_planned_usdt = 0.0
        for lvl_key in sorted_level_keys:
            lvl_cfg = grid[lvl_key]
            if not isinstance(lvl_cfg, dict):
                continue
            raw_vol = lvl_cfg.get("volume")
            if raw_vol is None and lvl_key == "0":
                regime_cfg = (
                    getattr(strategy, "active_settings", {}).get("first_order_weight_regime")
                    or (getattr(strategy, "overrides", {}) or {}).get("first_order_weight_regime")
                    or (getattr(strategy, "raw_meta", {}) or {}).get("first_order_weight_regime")
                )
                from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager
                vol_pct = FirstOrderRegimeManager.resolve_static_volume(
                    regime_cfg=regime_cfg,
                    side=side,
                    default_vol=12.0 if side.upper() == "LONG" else 7.0
                )
            else:
                vol_pct = Utils.safe_float(raw_vol, 0.0)

            planned_lvl_usdt = invest_size * (vol_pct / 100.0)
            cumulative_planned_usdt += planned_lvl_usdt

            # Уровень считается исполненным, если:
            # 1. Он уже был помечен is_active=True в предыдущем FSM
            # 2. Либо накопленный объем позиции покрывает этот уровень по сетке с учетом допуска
            was_active = bool(exist_grid.get(lvl_key, {}).get("is_active", False)) or bool(lvl_cfg.get("is_active", False))
            covered_by_volume = current_notional >= (cumulative_planned_usdt * tolerance_factor)

            if was_active or covered_by_volume:
                lvl_cfg["is_active"] = True
                lvl_cfg["is_armed"] = False
                lvl_cfg.pop("armed_at_drawdown", None)
                lvl_cfg.pop("armed_timestamp", None)
                if isinstance(armed_dict, dict) and lvl_key in armed_dict:
                    armed_dict.pop(lvl_key, None)
            else:
                # Последующие неисполненные ступени
                lvl_cfg["is_active"] = False
                lvl_cfg["is_armed"] = False

    @classmethod
    def resolve_position_state_grid(
        cls,
        state: Any,
        strategy: Optional[Any] = None,
        invest_size: Optional[float] = None,
        tolerance_pct: Optional[float] = None,
        current_price: Optional[float] = None,
    ) -> bool:
        """
        Универсально синхронизирует сетку PositionState с реальной позицией на бирже.
        Помечает исполненными (is_active=True) все ступени сетки, которые покрыты
        текущим объемом позиции с учетом допустимого отклонения (tolerance_pct).
        Предотвращает повторные и ложные срабатывания усреднений при перезапусках,
        восстановлениях и сбоях стрима.
        Возвращает True, если хотя бы один уровень был обновлен.
        """
        if not getattr(state, "in_position", False) and abs(float(getattr(state, "total_volume", 0.0) or 0.0)) <= 1e-6:
            return False

        grid = getattr(state, "grid", None)
        if not grid or not isinstance(grid, dict):
            return False

        resolution_cfg = StrategyApplicator._get_fsm_resolution_cfg(strategy)
        if resolution_cfg.get("enabled", True) is False:
            return False

        tol_pct = tolerance_pct
        if tol_pct is None:
            tol_pct = float(resolution_cfg.get("volume_tolerance_pct", 10.0))
        tolerance_factor = 1.0 - (tol_pct / 100.0)

        eff_invest = invest_size
        if eff_invest is None or eff_invest <= 0:
            eff_invest = float(getattr(state, "eff_invest", 0.0) or 0.0)
        if (eff_invest is None or eff_invest <= 0) and strategy and hasattr(strategy, "sides"):
            side_cfg = strategy.sides.get(getattr(state, "side", ""), {})
            eff_invest = float(side_cfg.get("invest_size", 0.0) or 0.0)
        if eff_invest is None or eff_invest <= 0:
            return False

        current_vol = abs(float(getattr(state, "total_volume", 0.0) or 0.0))
        entry_price = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
        if entry_price <= 0:
            entry_price = float(getattr(state, "initial_entry_price", 0.0) or 0.0)
        if entry_price <= 0 and current_price and current_price > 0:
            entry_price = float(current_price)

        if entry_price <= 0:
            return False

        current_notional = current_vol * entry_price
        trailing_state = getattr(state, "trailing_grid_state", {})
        armed_dict = trailing_state.get("armed_levels", {}) if isinstance(trailing_state, dict) else {}

        sorted_keys = sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else 999)
        cumulative_planned_usdt = 0.0
        updated = False

        for lvl_key in sorted_keys:
            lvl_cfg = grid[lvl_key]
            if not isinstance(lvl_cfg, dict):
                continue
            raw_vol = lvl_cfg.get("volume")
            if raw_vol is None and lvl_key == "0":
                regime_cfg = (
                    getattr(strategy, "active_settings", {}).get("first_order_weight_regime")
                    or getattr(strategy, "raw_meta", {}).get("first_order_weight_regime")
                ) if strategy else None
                from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager
                vol_pct = FirstOrderRegimeManager.resolve_static_volume(
                    regime_cfg=regime_cfg,
                    side=getattr(state, "side", "LONG"),
                    default_vol=12.0 if getattr(state, "side", "").upper() == "LONG" else 7.0
                )
            else:
                vol_pct = Utils.safe_float(raw_vol, 0.0)
            planned_lvl_usdt = eff_invest * (vol_pct / 100.0)
            cumulative_planned_usdt += planned_lvl_usdt

            was_active = bool(lvl_cfg.get("is_active", False))
            covered_by_volume = current_notional >= (cumulative_planned_usdt * tolerance_factor)

            if was_active or covered_by_volume:
                if not was_active:
                    lvl_cfg["is_active"] = True
                    updated = True
                lvl_cfg["is_armed"] = False
                lvl_cfg.pop("armed_at_drawdown", None)
                lvl_cfg.pop("armed_timestamp", None)
                if isinstance(armed_dict, dict) and lvl_key in armed_dict:
                    armed_dict.pop(lvl_key, None)
                    updated = True

        return updated

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
                                isinstance(s_val, dict) and (s_val.get("in_position") or abs(Utils.safe_float(s_val.get("total_volume"), 0.0)) > 1e-6)
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
                                    isinstance(s_val, dict) and (s_val.get("in_position") or abs(Utils.safe_float(s_val.get("total_volume"), 0.0)) > 1e-6)
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
