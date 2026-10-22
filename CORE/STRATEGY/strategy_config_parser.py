# CORE/STRATEGY/strategy_config_parser.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY.strategy_config_parser
ROLE: Декомпозированный парсер и резолвер полей конфигурации торговой стратегии (купола)
==============================================================================
"""

import copy
from typing import Dict, List, Optional, Any, Tuple, Union

from consts import (
    _CFG,
    DEFAULT_SMART_GRACE,
    DEFAULT_TIMEFRAME,
    DEFAULT_SHIFT_INTERVAL_SEC,
    DEFAULT_SMART_SIZE,
    DEFAULT_SMART_TP,
)
from c_utils import Utils
from CORE.STRATEGY.brownian_noise import BrownianNoiseController
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE.ENTRY.signal import TimeControl


def deep_merge_dict(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Рекурсивно объединяет два словаря, переопределяя поля overlay над base."""
    merged = copy.deepcopy(base)
    for k, v in overlay.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge_dict(merged[k], v)
        else:
            merged[k] = copy.deepcopy(v)
    return merged


class StrategyConfigParser:
    """Парсер секций StrategyConfig по принципу Single Responsibility (Правило 1)."""

    @staticmethod
    def resolve_symbols(active_settings: Dict[str, Any], symbols: Optional[List[str]]) -> List[str]:
        if symbols:
            return [s.upper().strip() for s in symbols if s and s.strip()]

        found_syms = []
        for src in [
            active_settings.get("symbols"),
            (active_settings.get("LONG") or {}).get("invest_size"),
            (active_settings.get("SHORT") or {}).get("invest_size"),
            active_settings.get("invest_size")
        ]:
            if isinstance(src, dict):
                for k in src.keys():
                    k_clean = str(k).upper().strip()
                    if k_clean and k_clean not in found_syms and k_clean not in ("DEFAULT", "LONG", "SHORT"):
                        found_syms.append(k_clean)
            elif isinstance(src, list):
                for s in src:
                    s_clean = str(s).upper().strip() if s else ""
                    if s_clean and s_clean not in found_syms:
                        found_syms.append(s_clean)
        return found_syms

    @staticmethod
    def resolve_invest_and_leverage(
        active_settings: Dict[str, Any],
        raw_meta: Dict[str, Any],
        meta_overrides: Dict[str, Any],
        overrides: Dict[str, Any]
    ) -> Dict[str, Any]:
        act_long_inv = (active_settings.get("LONG") or {}).get("invest_size")
        act_short_inv = (active_settings.get("SHORT") or {}).get("invest_size")
        act_inv = active_settings.get("invest_size")

        raw_long_cfg = raw_meta.get("LONG") or meta_overrides.get("LONG") or {}
        raw_short_cfg = raw_meta.get("SHORT") or meta_overrides.get("SHORT") or {}
        raw_long_inv = raw_long_cfg.get("invest_size")
        raw_short_inv = raw_short_cfg.get("invest_size")
        raw_inv = raw_meta.get("invest_size")

        act_def = active_settings.get("default_invest_size")
        act_long_def = (active_settings.get("LONG") or {}).get("default_invest_size", act_def)
        act_short_def = (active_settings.get("SHORT") or {}).get("default_invest_size", act_def)

        raw_def = raw_meta.get("default_invest_size")
        raw_long_def = raw_long_cfg.get("default_invest_size", raw_def)
        raw_short_def = raw_short_cfg.get("default_invest_size", raw_def)

        def_long = act_long_def if act_long_def is not None else raw_long_def
        def_short = act_short_def if act_short_def is not None else raw_short_def

        if def_long is None and isinstance(act_long_inv, (int, float)):
            def_long = float(act_long_inv)
        elif def_long is None and isinstance(raw_long_inv, (int, float)):
            def_long = float(raw_long_inv)
        elif def_long is None and isinstance(act_inv, (int, float)):
            def_long = float(act_inv)
        elif def_long is None and isinstance(raw_inv, (int, float)):
            def_long = float(raw_inv)

        if def_short is None and isinstance(act_short_inv, (int, float)):
            def_short = float(act_short_inv)
        elif def_short is None and isinstance(raw_short_inv, (int, float)):
            def_short = float(raw_short_inv)
        elif def_short is None and isinstance(act_inv, (int, float)):
            def_short = float(act_inv)
        elif def_short is None and isinstance(raw_inv, (int, float)):
            def_short = float(raw_inv)

        def_long_val = float(def_long) if def_long is not None else None
        def_short_val = float(def_short) if def_short is not None else None
        def_inv_val = (
            def_long_val
            if def_long_val == def_short_val
            else (float(act_def) if act_def is not None else def_long_val)
        )

        def _parse_side_inv(act_side_inv, act_all_inv, raw_side_inv, raw_all_inv, side_name, fallback_val):
            if isinstance(act_side_inv, dict):
                return {k.upper(): float(v) for k, v in act_side_inv.items()}
            elif act_side_inv is not None:
                return float(act_side_inv)
            elif act_all_inv is not None:
                if isinstance(act_all_inv, dict):
                    if side_name in act_all_inv:
                        sub = act_all_inv[side_name]
                        return {k.upper(): float(x) for k, x in sub.items()} if isinstance(sub, dict) else float(sub)
                    return {k.upper(): float(v) for k, v in act_all_inv.items()}
                return float(act_all_inv)
            elif isinstance(raw_side_inv, dict):
                return {k.upper(): float(v) for k, v in raw_side_inv.items()}
            elif raw_side_inv is not None:
                return float(raw_side_inv)
            elif raw_all_inv is not None:
                if isinstance(raw_all_inv, dict):
                    if side_name in raw_all_inv:
                        sub = raw_all_inv[side_name]
                        return {k.upper(): float(x) for k, x in sub.items()} if isinstance(sub, dict) else float(sub)
                    return {k.upper(): float(v) for k, v in raw_all_inv.items()}
                return float(raw_all_inv)
            if fallback_val is not None:
                return float(fallback_val)
            return None

        final_long = _parse_side_inv(act_long_inv, act_inv, raw_long_inv, raw_inv, "LONG", def_long_val)
        final_short = _parse_side_inv(act_short_inv, act_inv, raw_short_inv, raw_inv, "SHORT", def_short_val)
        final_inv = final_long if final_long == final_short else {"LONG": final_long, "SHORT": final_short}

        if "LONG" not in overrides:
            overrides["LONG"] = {}
        overrides["LONG"]["invest_size"] = final_long
        if "SHORT" not in overrides:
            overrides["SHORT"] = {}
        overrides["SHORT"]["invest_size"] = final_short

        act_lev = active_settings.get("leverage")
        raw_lev = raw_meta.get("leverage", 20)
        final_lev = act_lev if act_lev is not None else raw_lev
        leverage = {k.upper(): int(v) for k, v in final_lev.items()} if isinstance(final_lev, dict) else int(final_lev)

        act_mt = active_settings.get("margin_type")
        raw_mt = raw_meta.get("margin_type", "CROSSED")
        margin_type = str(act_mt if act_mt is not None else raw_mt).upper()
        overrides["margin_type"] = margin_type

        for side_k in ("LONG", "SHORT"):
            if side_k in active_settings and isinstance(active_settings[side_k], dict):
                overrides[side_k].update(active_settings[side_k])

        return {
            "default_invest_size_long": def_long_val,
            "default_invest_size_short": def_short_val,
            "default_invest_size": def_inv_val,
            "invest_size_long": final_long,
            "invest_size_short": final_short,
            "invest_size": final_inv,
            "leverage": leverage,
            "margin_type": margin_type,
        }

    @staticmethod
    def resolve_domes(
        active_settings: Dict[str, Any],
        raw_meta: Dict[str, Any],
        overrides: Dict[str, Any]
    ) -> Dict[str, Any]:
        strat_analytics = active_settings.get("analytics")
        if "start_balance_usdt" in active_settings:
            start_balance_usdt = float(active_settings["start_balance_usdt"])
        elif isinstance(strat_analytics, dict) and "start_balance_usdt" in strat_analytics:
            start_balance_usdt = float(strat_analytics["start_balance_usdt"])
        elif "analytics" in raw_meta and isinstance(raw_meta["analytics"], dict) and "start_balance_usdt" in raw_meta["analytics"]:
            start_balance_usdt = float(raw_meta["analytics"]["start_balance_usdt"])
        elif "start_balance_usdt" in raw_meta:
            start_balance_usdt = float(raw_meta["start_balance_usdt"])
        else:
            start_balance_usdt = 434.0
        analytics_cfg = {"start_balance_usdt": start_balance_usdt}

        sg_override = (
            active_settings.get("super_grid")
            or raw_meta.get("super_grid")
            or overrides.get("super_grid")
        )
        if isinstance(sg_override, bool):
            super_grid_enabled = sg_override
            super_grid_cfg = {
                "enabled": super_grid_enabled, "timeframe": "1w", "window": 12, "multiplier": 2.0,
                "min_volatility_pct": 15.0, "max_volatility_pct": None, "update_interval_hours": 24
            }
        elif isinstance(sg_override, str):
            sg_str = sg_override.strip().lower()
            super_grid_enabled = sg_str in ("true", "1", "yes", "on")
            super_grid_cfg = {
                "enabled": super_grid_enabled, "mode": sg_override, "timeframe": "1w", "window": 12,
                "multiplier": 2.0, "min_volatility_pct": 15.0, "max_volatility_pct": None, "update_interval_hours": 24
            }
        elif sg_override is not None and isinstance(sg_override, dict):
            super_grid_cfg = copy.deepcopy(sg_override)
            super_grid_enabled = bool(sg_override.get("enabled", True))
        elif "super_grid_enabled" in raw_meta:
            super_grid_enabled = bool(raw_meta["super_grid_enabled"])
            super_grid_cfg = {
                "enabled": super_grid_enabled, "timeframe": "1w", "window": 12, "multiplier": 2.0,
                "min_volatility_pct": 15.0, "max_volatility_pct": None, "update_interval_hours": 24
            }
        else:
            super_grid_enabled = False
            super_grid_cfg = {
                "enabled": False, "timeframe": "1w", "window": 12, "multiplier": 2.0,
                "min_volatility_pct": 15.0, "max_volatility_pct": None, "update_interval_hours": 24
            }

        notif_cfg = (
            active_settings.get("notifications")
            or raw_meta.get("notifications")
            or overrides.get("notifications")
            or _CFG.get("notifications")
        )
        notifications_cfg = copy.deepcopy(notif_cfg) if notif_cfg and isinstance(notif_cfg, dict) else {
            "enabled": True, "negative_threshold": -50.0, "positive_threshold": 100.0
        }

        raw_ac = raw_meta.get("auto_closing") or active_settings.get("auto_closing") or _CFG.get("auto_closing", {})
        base_ac = copy.deepcopy(raw_ac)
        override_ac = (
            active_settings.get("override", {}).get("auto_closing")
            or active_settings.get("overrides", {}).get("auto_closing")
            or active_settings.get("auto_closing")
            or overrides.get("auto_closing")
        )
        if override_ac and isinstance(override_ac, dict) and override_ac != base_ac:
            base_ac = deep_merge_dict(base_ac, override_ac)
        auto_closing_cfg = base_ac

        mode_val = (
            active_settings.get("auto_closing_mode")
            or active_settings.get("mode")
            or raw_meta.get("auto_closing_mode")
            or raw_meta.get("mode")
            or overrides.get("auto_closing_mode")
            or overrides.get("mode")
        )
        if not mode_val and active_settings.get("modes"):
            legacy_m = active_settings.get("modes")
            mode_val = legacy_m[0] if isinstance(legacy_m, list) and legacy_m else str(legacy_m)

        if mode_val and isinstance(mode_val, str):
            clean_m = mode_val.lower().strip()
            mode = "trailing_ratchet" if clean_m == "classic" else clean_m
        else:
            mode = "off"

        return {
            "start_balance_usdt": start_balance_usdt,
            "analytics_cfg": analytics_cfg,
            "super_grid_enabled": super_grid_enabled,
            "super_grid_cfg": super_grid_cfg,
            "notifications_cfg": notifications_cfg,
            "auto_closing_cfg": auto_closing_cfg,
            "mode": mode,
            "auto_closing_mode": mode,
        }

    @staticmethod
    def resolve_smart_features(
        active_settings: Dict[str, Any],
        raw_meta: Dict[str, Any],
        overrides: Dict[str, Any]
    ) -> Dict[str, Any]:
        raw_tsd = raw_meta.get("trend_size_depender") or active_settings.get("trend_size_depender") or raw_meta.get("sizing_mode") or active_settings.get("sizing_mode")
        if isinstance(raw_tsd, str) and raw_tsd.lower().strip() == "dyn_ema_16":
            sizing_mode = "dyn_ema_16"
            trend_size_depender = "dyn_ema_16"
            trend_size_cfg = {
                "enabled": True, "mode": "dyn_ema_16", "trend_mode": "ema_trend_4h", "timeframe": "4h",
                "eval_window": 30, "eval_window_bars": 3360, "sma_fast": 10, "sma_slow": 30,
                "confirmation_candles": 5, "require_rising": True, "trend_positive": True, "scale": 16,
                "min_side_pct": 33.3333, "max_side_pct": 66.6667
            }
        else:
            sizing_mode = "static"
            trend_size_depender = "static"
            trend_size_cfg = {"enabled": False, "mode": "static"}

        base_smart_grace = copy.deepcopy(DEFAULT_SMART_GRACE)
        sg_cfg = raw_meta.get("smart_grace") or overrides.get("smart_grace")
        if sg_cfg and isinstance(sg_cfg, dict):
            base_smart_grace.update(sg_cfg)
        if "shift_interval_sec" not in base_smart_grace:
            base_smart_grace["shift_interval_sec"] = DEFAULT_SHIFT_INTERVAL_SEC

        smart_grace_cfg = base_smart_grace
        brownian_controller = BrownianNoiseController(smart_grace_cfg=smart_grace_cfg)
        brownian_noise_factor = brownian_controller.get_jitter_range()

        base_smart_size = copy.deepcopy(DEFAULT_SMART_SIZE)
        ss_cfg = raw_meta.get("smart_size") or overrides.get("smart_size")
        if ss_cfg and isinstance(ss_cfg, dict):
            base_smart_size.update(ss_cfg)
        smart_size_cfg = base_smart_size
        smart_size_trend_determ = smart_size_cfg.get("trend_determ")

        if ss_cfg and "knife_blocker_threshold" in ss_cfg and "knife_blocker_enabled" not in ss_cfg:
            knife_blocker_enabled = ss_cfg["knife_blocker_threshold"] is not None
            knife_blocker_threshold = int(ss_cfg["knife_blocker_threshold"])
        elif "knife_blocker" in smart_size_cfg and isinstance(smart_size_cfg["knife_blocker"], dict):
            kb = smart_size_cfg["knife_blocker"]
            knife_blocker_enabled = bool(kb.get("enabled", False))
            knife_blocker_threshold = int(kb.get("threshold", 2))
        else:
            knife_blocker_enabled = bool(smart_size_cfg.get("knife_blocker_enabled", False))
            knife_blocker_threshold = int(smart_size_cfg.get("knife_blocker_threshold", 2))
        smart_size_cfg["knife_blocker_enabled"] = knife_blocker_enabled
        smart_size_cfg["knife_blocker_threshold"] = knife_blocker_threshold

        base_smart_tp = copy.deepcopy(DEFAULT_SMART_TP)
        st_cfg = raw_meta.get("smart_tp") or overrides.get("smart_tp")
        if st_cfg and isinstance(st_cfg, dict):
            base_smart_tp.update(st_cfg)
        smart_tp_cfg = base_smart_tp
        smart_tp_trend_determ = smart_tp_cfg.get("trend_determ")

        momentum_engine = MomentumEngine(
            smart_size_cfg=smart_size_cfg,
            smart_tp_cfg=smart_tp_cfg,
        )
        raw_min_tp = (
            raw_meta.get("min_tp_indent")
            or overrides.get("min_tp_indent")
            or _CFG.get("min_tp_indent")
        )
        min_tp_indent = float(raw_min_tp) if raw_min_tp is not None else 0.50

        return {
            "sizing_mode": sizing_mode,
            "trend_size_depender": trend_size_depender,
            "trend_size_cfg": trend_size_cfg,
            "smart_grace_cfg": smart_grace_cfg,
            "brownian_controller": brownian_controller,
            "brownian_noise_factor": brownian_noise_factor,
            "smart_size_cfg": smart_size_cfg,
            "smart_size_trend_determ": smart_size_trend_determ,
            "knife_blocker_enabled": knife_blocker_enabled,
            "knife_blocker_threshold": knife_blocker_threshold,
            "smart_tp_cfg": smart_tp_cfg,
            "smart_tp_trend_determ": smart_tp_trend_determ,
            "momentum_engine": momentum_engine,
            "min_tp_indent": min_tp_indent,
        }

    @staticmethod
    def resolve_recycling_and_atr(
        active_settings: Dict[str, Any],
        raw_meta: Dict[str, Any],
        overrides: Dict[str, Any],
        strategy_id: str
    ) -> Dict[str, Any]:
        raw_atr_grid = (
            active_settings.get("dynamic_atr_grid")
            or raw_meta.get("dynamic_atr_grid")
            or overrides.get("dynamic_atr_grid")
            or _CFG.get("dynamic_atr_grid")
        )
        dynamic_atr_grid_cfg = copy.deepcopy(raw_atr_grid) if raw_atr_grid and isinstance(raw_atr_grid, dict) else {"enabled": False}

        dynamic_atr_tp_cfg = copy.deepcopy(raw_meta.get("dynamic_atr_tp") or overrides.get("dynamic_atr_tp") or {})
        if dynamic_atr_tp_cfg.get("enabled"):
            for req_k in ("timeframe", "period", "cache_ttl_sec", "min_guaranteed_floor_pct", "max_ceiling_pct", "fallback_atr_pct", "level_multipliers"):
                if req_k not in dynamic_atr_tp_cfg:
                    raise KeyError(f"[StrategyConfig][{strategy_id}] Missing required dynamic_atr_tp field: '{req_k}'")

        cross_recycling_cfg = copy.deepcopy(raw_meta.get("cross_side_recycling") or overrides.get("cross_side_recycling") or {})
        if cross_recycling_cfg.get("enabled"):
            has_ratio = (
                "recycle_ratio" in cross_recycling_cfg
                or "recycle_ratio_tiers" in cross_recycling_cfg
                or "recycle_ratio_map" in cross_recycling_cfg
            )
            if not has_ratio:
                raise KeyError(f"[StrategyConfig][{strategy_id}] Missing required cross_side_recycling ratio: either 'recycle_ratio_tiers' (дискретная карта) or 'recycle_ratio' (скаляр)")
            has_burn_level = (
                "min_burn_level" in cross_recycling_cfg
                or "recycle_ratio_tiers" in cross_recycling_cfg
                or "recycle_ratio_map" in cross_recycling_cfg
            )
            if not has_burn_level:
                raise KeyError(f"[StrategyConfig][{strategy_id}] Missing required cross_side_recycling field: 'min_burn_level' (или дискретная карта 'recycle_ratio_tiers')")
            for req_k in ("min_drawdown_pct", "max_slice_pct"):
                if req_k not in cross_recycling_cfg:
                    raise KeyError(f"[StrategyConfig][{strategy_id}] Missing required cross_side_recycling field: '{req_k}'")
            recycling_min_drawdown_pct = float(cross_recycling_cfg["min_drawdown_pct"])
            recycling_max_slice_pct = float(cross_recycling_cfg["max_slice_pct"])
            recycling_max_slice_ratio = recycling_max_slice_pct / 100.0
        else:
            recycling_min_drawdown_pct = 0.0
            recycling_max_slice_pct = 0.0
            recycling_max_slice_ratio = 0.0

        raw_ms_filter = (
            active_settings.get("micro_step_filter")
            or raw_meta.get("micro_step_filter")
            or overrides.get("micro_step_filter")
        )
        if raw_ms_filter and isinstance(raw_ms_filter, dict):
            micro_step_filter_cfg = copy.deepcopy(raw_ms_filter)
        else:
            micro_step_filter_cfg = {"enabled": False}

        if micro_step_filter_cfg.get("enabled"):
            for req_k in ("timeframe", "period", "target_level", "max_atr_pct"):
                if req_k not in micro_step_filter_cfg:
                    raise KeyError(f"[StrategyConfig][{strategy_id}] Missing required micro_step_filter field: '{req_k}'")

        tfr_val = raw_meta.get("signal_heartbeat_tfr") or overrides.get("signal_heartbeat_tfr")
        if not tfr_val:
            for alias in ("heartbeat_tfr", "timeframe"):
                if alias in raw_meta and raw_meta[alias]:
                    tfr_val = str(raw_meta[alias])
                    break
        if not tfr_val:
            if DEFAULT_TIMEFRAME:
                tfr_val = DEFAULT_TIMEFRAME
            else:
                raise KeyError(
                    f"[StrategyConfig][{strategy_id}] Отсутствует обязательный параметр "
                    f"'signal_heartbeat_tfr' в паспорте стратегии (CFG/universes/*.json)!"
                )

        tfr_str = str(tfr_val)
        heartbeat_seconds = TimeControl.interval_to_seconds(tfr_str)

        api_key_name = active_settings.get("api_key_name")
        api_secret_name = active_settings.get("api_secret_name")

        return {
            "dynamic_atr_grid_cfg": dynamic_atr_grid_cfg,
            "dynamic_atr_tp_cfg": dynamic_atr_tp_cfg,
            "cross_recycling_cfg": cross_recycling_cfg,
            "recycling_min_drawdown_pct": recycling_min_drawdown_pct,
            "recycling_max_slice_pct": recycling_max_slice_pct,
            "recycling_max_slice_ratio": recycling_max_slice_ratio,
            "micro_step_filter_cfg": micro_step_filter_cfg,
            "signal_heartbeat_tfr": tfr_str,
            "heartbeat_tfr": tfr_str,
            "timeframe": tfr_str,
            "heartbeat_seconds": heartbeat_seconds,
            "api_key_name": api_key_name,
            "api_secret_name": api_secret_name,
        }
