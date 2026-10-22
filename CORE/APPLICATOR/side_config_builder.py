# CORE/APPLICATOR/side_config_builder.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.APPLICATOR.side_config_builder
ROLE: Построение конфигурации сторон (LONG / SHORT) из спецификаций стратегии
==============================================================================
"""

import copy
from typing import Dict, Any, Optional

from c_utils import Utils
from CORE.GRID.fsm_grid_resolver import FSMGridResolver


class SideConfigBuilder:
    """
    Создает стороны LONG и SHORT для символа напрямую из спецификации стратегии
    с соблюдением правила TP >= min_tp_indent, поддержкой асимметричных настроек
    per-side (LONG / SHORT) и per-symbol invest_size.
    """

    @staticmethod
    def build_side_configs(
        strategy: Any,
        symbol: Optional[str] = None,
        default_fallback_base: Optional[Dict[str, Any]] = None,
        base_cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        if default_fallback_base is None:
            default_fallback_base = {
                "LONG": {
                    "enable": True, "invest_size": None, "leverage": 20,
                    "tp_purpose": "self", "margin_type": "CROSSED", "grid": {}, "tp_map": {}
                },
                "SHORT": {
                    "enable": True, "invest_size": None, "leverage": 20,
                    "tp_purpose": "self", "margin_type": "CROSSED", "grid": {}, "tp_map": {}
                }
            }
        base_cfg = base_cfg or {}
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

            side_data = copy.deepcopy(side_dict) if side_dict else copy.deepcopy(default_fallback_base[side])

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
            elif isinstance(getattr(strategy, "leverage", None), dict) and side in strategy.leverage:
                side_data["leverage"] = int(strategy.leverage[side])
            elif isinstance(getattr(strategy, "leverage", None), (int, float)):
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
                    resolved_vol = FSMGridResolver.resolve_first_order_volume(
                        regime_cfg=regime_cfg,
                        side=side,
                        default_vol=curr_default
                    )
                    side_data["grid"]["0"]["volume"] = resolved_vol
                elif side_data["grid"]["0"].get("volume") is None:
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
            elif "min_tp_indent" in base_cfg and base_cfg["min_tp_indent"] is not None:
                min_tp_floor = float(base_cfg["min_tp_indent"])

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
            elif hasattr(strategy, "overrides") and "tp_purpose" in strategy.overrides:
                side_data["tp_purpose"] = strategy.overrides["tp_purpose"]

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
            elif hasattr(strategy, "overrides") and "margin_type" in getattr(strategy, "overrides", {}):
                side_data["margin_type"] = str(strategy.overrides["margin_type"]).upper()
            else:
                side_data["margin_type"] = "CROSSED"

            # 9. Применение avg_free_volume_behavior
            if "avg_free_volume_behavior" in side_dict:
                side_data["avg_free_volume_behavior"] = side_dict["avg_free_volume_behavior"]
            elif hasattr(strategy, "raw_meta") and "avg_free_volume_behavior" in strategy.raw_meta:
                side_data["avg_free_volume_behavior"] = strategy.raw_meta["avg_free_volume_behavior"]
            elif hasattr(strategy, "overrides") and "avg_free_volume_behavior" in strategy.overrides:
                side_data["avg_free_volume_behavior"] = strategy.overrides["avg_free_volume_behavior"]

            side_cfgs[side] = side_data

        return side_cfgs
