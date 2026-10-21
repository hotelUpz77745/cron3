# ============================================================
# FILE: CORE/strategy_registry.py
# ROLE: Strategy template registry and symbol validation
# ============================================================

import copy
import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Set, Tuple

from consts import (
    DATA_DIR,
    _CFG,
    DEFAULT_SMART_GRACE,
    DEFAULT_TIMEFRAME,
    DEFAULT_SHIFT_INTERVAL_SEC,
    DEFAULT_SMART_SIZE,
    DEFAULT_SMART_TP,
    DEFAULT_AUTO_CLOSING,
    _BASE_CFG,
)
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("StrategyRegistry")


def deep_merge_dict(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Рекурсивно объединяет два словаря, переопределяя поля overlay над base."""
    merged = copy.deepcopy(base)
    for k, v in overlay.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge_dict(merged[k], v)
        else:
            merged[k] = copy.deepcopy(v)
    return merged


class StrategyConfig:
    """Инкапсулирует конфигурацию и параметры переопределения одной торговой стратегии (купола)."""

    def __init__(
        self,
        strategy_id: str,
        raw_meta: Dict[str, Any],
        symbols: Optional[List[str]] = None,
        active_settings: Optional[Dict[str, Any]] = None
    ):
        self.strategy_id: str = strategy_id
        self.name: str = raw_meta["name"]
        self.description: str = raw_meta.get("description", "")
        self.raw_meta: Dict[str, Any] = copy.deepcopy(raw_meta)
        self.active_settings: Dict[str, Any] = copy.deepcopy(active_settings or {})

        # Автоматическое извлечение symbols из ключей per-symbol invest_size, если явно не задан
        if not symbols:
            found_syms = []
            for src in [
                self.active_settings.get("symbols"),
                (self.active_settings.get("LONG") or {}).get("invest_size"),
                (self.active_settings.get("SHORT") or {}).get("invest_size"),
                self.active_settings.get("invest_size")
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
            symbols = found_syms

        self.symbols: List[str] = [s.upper().strip() for s in (symbols or [])]

        meta_overrides = raw_meta.get("overrides") or {}
        # Сохраняем self.overrides для обратной совместимости, но наполняем его напрямую из raw_meta
        self.overrides: Dict[str, Any] = copy.deepcopy(meta_overrides)
        for k, v in raw_meta.items():
            if k != "overrides" and k not in self.overrides:
                self.overrides[k] = copy.deepcopy(v)
        if "grid" not in self.overrides:
            grid_cand = (raw_meta.get("LONG") or {}).get("grid") or (raw_meta.get("SHORT") or {}).get("grid")
            if grid_cand:
                self.overrides["grid"] = copy.deepcopy(grid_cand)
        if "tp_map" not in self.overrides:
            tp_cand = (raw_meta.get("LONG") or {}).get("tp_map") or (raw_meta.get("SHORT") or {}).get("tp_map")
            if tp_cand:
                self.overrides["tp_map"] = copy.deepcopy(tp_cand)

        # Разрешение invest_size: строго на уровне клиента (active_settings), с безопасным fallback
        # 1. Приоритет клиента (active_settings)
        act_long_inv = (self.active_settings.get("LONG") or {}).get("invest_size")
        if act_long_inv is None:
            act_long_inv = self.active_settings.get("invest_size_long")
        act_short_inv = (self.active_settings.get("SHORT") or {}).get("invest_size")
        if act_short_inv is None:
            act_short_inv = self.active_settings.get("invest_size_short")
        act_inv = self.active_settings.get("invest_size")

        # 2. Fallback на raw_meta (обратная совместимость со старыми тестами/шаблонами)
        raw_long_cfg = raw_meta.get("LONG") or meta_overrides.get("LONG") or {}
        raw_short_cfg = raw_meta.get("SHORT") or meta_overrides.get("SHORT") or {}
        raw_long_inv = raw_long_cfg.get("invest_size")
        raw_short_inv = raw_short_cfg.get("invest_size")
        raw_inv = raw_meta.get("invest_size")

        # Явный default_invest_size из конфига (active_settings -> raw_meta)
        act_def = self.active_settings.get("default_invest_size")
        act_long_def = (self.active_settings.get("LONG") or {}).get("default_invest_size", act_def)
        act_short_def = (self.active_settings.get("SHORT") or {}).get("default_invest_size", act_def)

        raw_def = raw_meta.get("default_invest_size")
        raw_long_def = (raw_long_cfg).get("default_invest_size", raw_def)
        raw_short_def = (raw_short_cfg).get("default_invest_size", raw_def)

        def_long = act_long_def if act_long_def is not None else raw_long_def
        def_short = act_short_def if act_short_def is not None else raw_short_def

        # Если явный default_invest_size не задан, проверяем скалярный invest_size
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

        self.default_invest_size_long: Optional[float] = float(def_long) if def_long is not None else None
        self.default_invest_size_short: Optional[float] = float(def_short) if def_short is not None else None
        self.default_invest_size: Optional[float] = (
            self.default_invest_size_long
            if self.default_invest_size_long == self.default_invest_size_short
            else (float(act_def) if act_def is not None else self.default_invest_size_long)
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

        final_long = _parse_side_inv(act_long_inv, act_inv, raw_long_inv, raw_inv, "LONG", self.default_invest_size_long)
        final_short = _parse_side_inv(act_short_inv, act_inv, raw_short_inv, raw_inv, "SHORT", self.default_invest_size_short)

        self.invest_size_long: Union[float, Dict[str, float]] = final_long
        self.invest_size_short: Union[float, Dict[str, float]] = final_short
        if final_long == final_short:
            self.invest_size = final_long
        else:
            self.invest_size = {"LONG": final_long, "SHORT": final_short}

        if "LONG" not in self.overrides:
            self.overrides["LONG"] = {}
        self.overrides["LONG"]["invest_size"] = final_long
        if "SHORT" not in self.overrides:
            self.overrides["SHORT"] = {}
        self.overrides["SHORT"]["invest_size"] = final_short

        # Leverage resolution (active_settings -> raw_meta -> 20)
        act_lev = self.active_settings.get("leverage")
        raw_lev = raw_meta.get("leverage", 20)
        final_lev = act_lev if act_lev is not None else raw_lev
        if isinstance(final_lev, dict):
            self.leverage = {k.upper(): int(v) for k, v in final_lev.items()}
        else:
            self.leverage = int(final_lev)

        # Margin type resolution (active_settings -> raw_meta -> "CROSSED")
        act_mt = self.active_settings.get("margin_type")
        raw_mt = raw_meta.get("margin_type", "CROSSED")
        self.margin_type: str = str(act_mt if act_mt is not None else raw_mt).upper()
        self.overrides["margin_type"] = self.margin_type

        for side_k in ("LONG", "SHORT"):
            if side_k in self.active_settings and isinstance(self.active_settings[side_k], dict):
                self.overrides[side_k].update(self.active_settings[side_k])

        # 1. Купол Аналитики (Analytics Dome): канонически из app.json -> active_strategies[strategy_id]["start_balance_usdt"]
        strat_analytics = self.active_settings.get("analytics")
        if "start_balance_usdt" in self.active_settings:
            self.start_balance_usdt: float = float(self.active_settings["start_balance_usdt"])
        elif isinstance(strat_analytics, dict) and "start_balance_usdt" in strat_analytics:
            self.start_balance_usdt: float = float(strat_analytics["start_balance_usdt"])
        elif "analytics" in raw_meta and isinstance(raw_meta["analytics"], dict) and "start_balance_usdt" in raw_meta["analytics"]:
            self.start_balance_usdt: float = float(raw_meta["analytics"]["start_balance_usdt"])
        elif "start_balance_usdt" in raw_meta:
            self.start_balance_usdt: float = float(raw_meta["start_balance_usdt"])
        else:
            self.start_balance_usdt: float = 449.0
        self.analytics_cfg: Dict[str, Any] = {"start_balance_usdt": self.start_balance_usdt}

        # 2. Купол Super Grid (Volatility Grid Dome)
        sg_override = (
            self.active_settings.get("super_grid")
            or raw_meta.get("super_grid")
            or self.overrides.get("super_grid")
        )
        if isinstance(sg_override, bool):
            self.super_grid_enabled: bool = sg_override
            self.super_grid_cfg = {
                "enabled": self.super_grid_enabled,
                "timeframe": "1w",
                "window": 12,
                "multiplier": 2.0,
                "min_volatility_pct": 15.0,
                "max_volatility_pct": None,
                "update_interval_hours": 24
            }
        elif isinstance(sg_override, str):
            sg_str = sg_override.strip().lower()
            self.super_grid_enabled: bool = sg_str in ("true", "1", "yes", "on")
            self.super_grid_cfg = {
                "enabled": self.super_grid_enabled,
                "mode": sg_override,
                "timeframe": "1w",
                "window": 12,
                "multiplier": 2.0,
                "min_volatility_pct": 15.0,
                "max_volatility_pct": None,
                "update_interval_hours": 24
            }
        elif sg_override is not None and isinstance(sg_override, dict):
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
            or raw_meta.get("notifications")
            or self.overrides.get("notifications")
            or _CFG.get("notifications")
        )
        if notif_cfg and isinstance(notif_cfg, dict):
            self.notifications_cfg: Dict[str, Any] = copy.deepcopy(notif_cfg)
        else:
            self.notifications_cfg: Dict[str, Any] = {
                "enabled": True,
                "negative_threshold": -50.0,
                "positive_threshold": 100.0
            }

        # 4. Купол Авто-закрытия (Auto-closing Dome): базовый шаблон из raw_meta / _base.json с переопределениями стратегии
        raw_ac = raw_meta.get("auto_closing") or self.active_settings.get("auto_closing") or _CFG.get("auto_closing", {})
        base_ac = copy.deepcopy(raw_ac)
        override_ac = (
            self.active_settings.get("override", {}).get("auto_closing")
            or self.active_settings.get("overrides", {}).get("auto_closing")
            or self.active_settings.get("auto_closing")
            or self.overrides.get("auto_closing")
        )
        if override_ac and isinstance(override_ac, dict) and override_ac != base_ac:
            base_ac = deep_merge_dict(base_ac, override_ac)
        self.auto_closing_cfg: Dict[str, Any] = base_ac

        # 5. Купол Режима Автозакрытия (Auto-Closing Mode Dome): "auto_closing_mode"
        mode_val = (
            self.active_settings.get("auto_closing_mode")
            or self.active_settings.get("mode")
            or raw_meta.get("auto_closing_mode")
            or raw_meta.get("mode")
            or self.overrides.get("auto_closing_mode")
            or self.overrides.get("mode")
        )
        if not mode_val and self.active_settings.get("modes"):
            legacy_m = self.active_settings.get("modes")
            mode_val = legacy_m[0] if isinstance(legacy_m, list) and legacy_m else str(legacy_m)

        if mode_val and isinstance(mode_val, str):
            clean_m = mode_val.lower().strip()
            self.mode: str = "trailing_ratchet" if clean_m == "classic" else clean_m
        else:
            self.mode = "off"
        self.auto_closing_mode: str = self.mode

        # 6. Купол Режима Сайзинга (Sizing Mode Dome): строго "trend_size_depender": "static" (строковый тип: "static", "dyn_inv_16", "dyn_ema_16", "dyn_ema_10")
        raw_tsd = raw_meta.get("trend_size_depender")
        active_tsd = self.active_settings.get("trend_size_depender")

        sizing_mode_val = (
            active_tsd
            or self.active_settings.get("sizing_mode")
            or raw_tsd
            or raw_meta.get("sizing_mode")
            or self.overrides.get("trend_size_depender")
            or self.overrides.get("sizing_mode")
        )
        if isinstance(sizing_mode_val, dict):
            sizing_mode_val = sizing_mode_val.get("mode") or sizing_mode_val.get("trend_mode")
        elif not sizing_mode_val and self.active_settings.get("sizing_modes"):
            legacy_sm = self.active_settings.get("sizing_modes")
            sizing_mode_val = legacy_sm[0] if isinstance(legacy_sm, list) and legacy_sm else str(legacy_sm)
        elif not sizing_mode_val and raw_meta.get("sizing_modes"):
            legacy_sm = raw_meta.get("sizing_modes")
            sizing_mode_val = legacy_sm[0] if isinstance(legacy_sm, list) and legacy_sm else str(legacy_sm)

        if sizing_mode_val and isinstance(sizing_mode_val, str):
            sm = sizing_mode_val.lower().strip()
            if sm in ("dyn_bb", "dyn_bb_inv"):
                sm = "dyn_inv_16"
            elif sm == "dyn_ema_trend":
                sm = "dyn_ema_16"
            self.sizing_mode: str = sm
        else:
            self.sizing_mode = "static"
        self.trend_size_depender: str = self.sizing_mode

        VALID_SIZING_MODES = ("static", "dyn_inv_16", "dyn_ema_16", "dyn_ema_10")
        if self.sizing_mode not in VALID_SIZING_MODES:
            raise ValueError(
                f"[StrategyConfig][{self.strategy_id}] Неизвестный sizing_mode '{self.sizing_mode}'! "
                f"Допустимые режимы: {VALID_SIZING_MODES}"
            )

        # Фабрика конфигурации trend_size_depender:
        # "static": enabled = false (статический сайзинг 50/50, макро-тренд выключен).
        # "dyn_inv_16": enabled = true (Bollinger Inverted Lockout 4h, шкала 16, коридор 33.3% / 66.7% + 4h-заморозка 50/50 при пробое).
        # "dyn_ema_16": enabled = true (EMA Trend 4h, быстрая 10 / медленная 30, шкала 16, коридор 33.3% / 66.7%).
        # "dyn_ema_10": enabled = true (EMA Trend 4h, быстрая 10 / медленная 30, мягкая шкала 10, коридор 40.0% / 60.0%).
        if self.sizing_mode == "static":
            self.trend_size_cfg = {"enabled": False, "mode": "static"}
        elif self.sizing_mode == "dyn_inv_16":
            self.trend_size_cfg = {
                "enabled": True,
                "mode": "dyn_inv_16",
                "trend_mode": "bb_inverted_lockout",
                "timeframe": "4h",
                "eval_window": 20,
                "eval_window_bars": 3360,
                "multiplier": 2.0,
                "lockout_duration_sec": 14400.0,
                "confirmation_candles": 5,
                "scale": 16,
                "min_side_pct": 33.3333,
                "max_side_pct": 66.6667
            }
        elif self.sizing_mode == "dyn_ema_16":
            self.trend_size_cfg = {
                "enabled": True,
                "mode": "dyn_ema_16",
                "trend_mode": "ema_trend_4h",
                "timeframe": "4h",
                "eval_window": 30,
                "eval_window_bars": 3360,
                "sma_fast": 10,
                "sma_slow": 30,
                "confirmation_candles": 5,
                "require_rising": True,
                "trend_positive": True,
                "scale": 16,
                "min_side_pct": 33.3333,
                "max_side_pct": 66.6667
            }
        elif self.sizing_mode == "dyn_ema_10":
            self.trend_size_cfg = {
                "enabled": True,
                "mode": "dyn_ema_10",
                "trend_mode": "ema_trend_4h",
                "timeframe": "4h",
                "eval_window": 30,
                "eval_window_bars": 3360,
                "sma_fast": 10,
                "sma_slow": 30,
                "confirmation_candles": 5,
                "require_rising": True,
                "trend_positive": True,
                "scale": 10,
                "min_side_pct": 40.0,
                "max_side_pct": 60.0
            }

        user_tsd_override = (
            self.active_settings.get("override", {}).get("trend_size_depender")
            or self.active_settings.get("overrides", {}).get("trend_size_depender")
        )
        if isinstance(user_tsd_override, str):
            self.trend_size_cfg["mode"] = user_tsd_override
            self.trend_size_cfg["enabled"] = (user_tsd_override != "static")
        elif user_tsd_override and isinstance(user_tsd_override, dict):
            self.trend_size_cfg.update(user_tsd_override)

        self.overrides["trend_size_depender"] = self.sizing_mode
        self.overrides["sizing_mode"] = self.sizing_mode

        # Конфигурация Smart Grace
        base_smart_grace = copy.deepcopy(DEFAULT_SMART_GRACE)
        sg_cfg = raw_meta.get("smart_grace") or self.overrides.get("smart_grace")
        if sg_cfg and isinstance(sg_cfg, dict):
            base_smart_grace.update(sg_cfg)
        if "shift_interval_sec" not in base_smart_grace:
            base_smart_grace["shift_interval_sec"] = DEFAULT_SHIFT_INTERVAL_SEC
        self.smart_grace_cfg: Dict[str, Any] = base_smart_grace

        # Конфигурации Smart Size и Smart TP
        base_smart_size = copy.deepcopy(DEFAULT_SMART_SIZE)
        ss_cfg = raw_meta.get("smart_size") or self.overrides.get("smart_size")
        if ss_cfg and isinstance(ss_cfg, dict):
            base_smart_size.update(ss_cfg)
            if "knife_blocker_threshold" in ss_cfg and "knife_blocker_enabled" not in ss_cfg:
                base_smart_size["knife_blocker_enabled"] = ss_cfg["knife_blocker_threshold"] is not None
        self.smart_size_cfg: Dict[str, Any] = base_smart_size

        base_smart_tp = copy.deepcopy(DEFAULT_SMART_TP)
        st_cfg = raw_meta.get("smart_tp") or self.overrides.get("smart_tp")
        if st_cfg and isinstance(st_cfg, dict):
            base_smart_tp.update(st_cfg)
        self.smart_tp_cfg: Dict[str, Any] = base_smart_tp
        raw_min_tp = (
            raw_meta.get("min_tp_indent")
            or self.overrides.get("min_tp_indent")
            or _BASE_CFG.get("min_tp_indent")
        )
        self.min_tp_indent: float = float(raw_min_tp) if raw_min_tp is not None else 0.50

        # 6. Механика подтяжки тейк-профита по таймауту (TP Backoff)
        raw_backoff = (
            self.active_settings.get("tp_backoff")
            or raw_meta.get("tp_backoff")
            or self.overrides.get("tp_backoff")
            or _BASE_CFG.get("tp_backoff")
        )
        if raw_backoff and isinstance(raw_backoff, dict):
            self.tp_backoff_cfg: Dict[str, Any] = copy.deepcopy(raw_backoff)
        else:
            self.tp_backoff_cfg = {"enabled": False}

        raw_atr_grid = (
            self.active_settings.get("dynamic_atr_grid")
            or raw_meta.get("dynamic_atr_grid")
            or self.overrides.get("dynamic_atr_grid")
            or _BASE_CFG.get("dynamic_atr_grid")
        )
        if raw_atr_grid and isinstance(raw_atr_grid, dict):
            self.dynamic_atr_grid_cfg: Dict[str, Any] = copy.deepcopy(raw_atr_grid)
        else:
            self.dynamic_atr_grid_cfg = {"enabled": False}

        self.dynamic_atr_tp_cfg: Dict[str, Any] = copy.deepcopy(raw_meta.get("dynamic_atr_tp") or self.overrides.get("dynamic_atr_tp") or {})
        if self.dynamic_atr_tp_cfg.get("enabled"):
            for req_k in ("timeframe", "period", "cache_ttl_sec", "min_guaranteed_floor_pct", "max_ceiling_pct", "fallback_atr_pct", "level_multipliers"):
                if req_k not in self.dynamic_atr_tp_cfg:
                    raise KeyError(f"[StrategyConfig][{self.strategy_id}] Missing required dynamic_atr_tp field: '{req_k}'")

        self.cross_recycling_cfg: Dict[str, Any] = copy.deepcopy(raw_meta.get("cross_side_recycling") or self.overrides.get("cross_side_recycling") or {})
        if self.cross_recycling_cfg.get("enabled"):
            has_ratio = (
                "recycle_ratio" in self.cross_recycling_cfg
                or "recycle_ratio_tiers" in self.cross_recycling_cfg
                or "recycle_ratio_map" in self.cross_recycling_cfg
            )
            if not has_ratio:
                raise KeyError(f"[StrategyConfig][{self.strategy_id}] Missing required cross_side_recycling ratio: either 'recycle_ratio_tiers' (дискретная карта) or 'recycle_ratio' (скаляр)")
            has_burn_level = (
                "min_burn_level" in self.cross_recycling_cfg
                or "recycle_ratio_tiers" in self.cross_recycling_cfg
                or "recycle_ratio_map" in self.cross_recycling_cfg
            )
            if not has_burn_level:
                raise KeyError(f"[StrategyConfig][{self.strategy_id}] Missing required cross_side_recycling field: 'min_burn_level' (или дискретная карта 'recycle_ratio_tiers')")
            for req_k in ("min_drawdown_pct", "max_slice_pct"):
                if req_k not in self.cross_recycling_cfg:
                    raise KeyError(f"[StrategyConfig][{self.strategy_id}] Missing required cross_side_recycling field: '{req_k}'")

        # 7. Фильтр динамической микро-ступени волатильности (Micro-Step Filter Dome)
        raw_ms_filter = (
            self.active_settings.get("micro_step_filter")
            or raw_meta.get("micro_step_filter")
            or self.overrides.get("micro_step_filter")
        )
        if raw_ms_filter and isinstance(raw_ms_filter, dict):
            self.micro_step_filter_cfg: Dict[str, Any] = copy.deepcopy(raw_ms_filter)
        else:
            self.micro_step_filter_cfg = {"enabled": False}

        if self.micro_step_filter_cfg.get("enabled"):
            for req_k in ("timeframe", "period", "target_level", "max_atr_pct"):
                if req_k not in self.micro_step_filter_cfg:
                    raise KeyError(f"[StrategyConfig][{self.strategy_id}] Missing required micro_step_filter field: '{req_k}'")

        # Персональный пульс/хартбит стратегии: прямое чтение 'signal_heartbeat_tfr'
        # с поддержкой легаси-алиасов ('heartbeat_tfr', 'timeframe') по Протоколу №2 (без скрытых дефолтов)
        tfr_val = None
        for key in ("signal_heartbeat_tfr", "heartbeat_tfr", "timeframe"):
            if key in raw_meta and raw_meta[key]:
                tfr_val = str(raw_meta[key])
                break
            elif key in self.overrides and self.overrides[key]:
                tfr_val = str(self.overrides[key])
                break

        if not tfr_val:
            if DEFAULT_TIMEFRAME:
                tfr_val = DEFAULT_TIMEFRAME
            else:
                raise KeyError(
                    f"[StrategyConfig][{self.strategy_id}] Отсутствует обязательный параметр "
                    f"'signal_heartbeat_tfr' (или алиасы 'heartbeat_tfr' / 'timeframe') в конфиге стратегии!"
                )

        self.signal_heartbeat_tfr: str = tfr_val
        self.heartbeat_tfr: str = tfr_val
        self.timeframe: str = tfr_val
        from CORE.ENTRY.signal import TimeControl
        self.heartbeat_seconds: int = TimeControl.interval_to_seconds(self.signal_heartbeat_tfr)

        # Мультиаккаунтность: привязка стратегии к ключам в .env
        self.api_key_name: Optional[str] = self.active_settings.get("api_key_name")
        self.api_secret_name: Optional[str] = self.active_settings.get("api_secret_name")

    def get_api_credentials(self) -> tuple[str, str]:
        """
        Возвращает (api_key, api_secret) для аккаунта данной стратегии.
        Если указаны api_key_name / api_secret_name в app.json — читает их из .env.
        Также проверяет суффикс стратегии API_KEY_<STRAT_ID>.
        Если не найдены — fallback на базовые глобальные API_KEY и API_SECRET.
        """
        import os
        from consts import API_KEY, API_SECRET
        if self.api_key_name:
            custom_key = os.getenv(self.api_key_name)
            custom_sec = os.getenv(self.api_secret_name or "") if self.api_secret_name else None
            if custom_key and custom_sec:
                return custom_key, custom_sec

        strat_key_env = f"API_KEY_{self.strategy_id.upper()}"
        strat_sec_env = f"API_SECRET_{self.strategy_id.upper()}"
        strat_key = os.getenv(strat_key_env)
        strat_sec = os.getenv(strat_sec_env)
        if strat_key and strat_sec:
            return strat_key, strat_sec

        return API_KEY, API_SECRET

    def get_account_id(self) -> str:
        """
        Возвращает идентификатор аккаунта Binance (имя переменной ключа в .env).
        Если имена ключей одинаковые — стратегии делят один физический аккаунт биржи.
        """
        if self.api_key_name:
            return str(self.api_key_name).strip()
        import os
        strat_key_env = f"API_KEY_{self.strategy_id.upper()}"
        if os.getenv(strat_key_env):
            return strat_key_env
        return "API_KEY"

    @property
    def grid(self) -> Dict[str, Any]:
        return (self.raw_meta.get("LONG") or {}).get("grid") or (self.raw_meta.get("SHORT") or {}).get("grid") or self.overrides.get("grid", {})

    @property
    def grid_long(self) -> Dict[str, Any]:
        return (self.raw_meta.get("LONG") or {}).get("grid") or self.overrides.get("grid", {})

    @property
    def grid_short(self) -> Dict[str, Any]:
        return (self.raw_meta.get("SHORT") or {}).get("grid") or self.overrides.get("grid", {})

    @property
    def first_order_weight_regime(self) -> Dict[str, Any]:
        return self.raw_meta.get("first_order_weight_regime") or self.overrides.get("first_order_weight_regime") or {}

    @property
    def tp_map(self) -> Dict[str, Any]:
        return (self.raw_meta.get("LONG") or {}).get("tp_map") or (self.raw_meta.get("SHORT") or {}).get("tp_map") or self.overrides.get("tp_map", {})

    @property
    def tp_map_long(self) -> Dict[str, Any]:
        return (self.raw_meta.get("LONG") or {}).get("tp_map") or self.overrides.get("tp_map", {})

    @property
    def tp_map_short(self) -> Dict[str, Any]:
        return (self.raw_meta.get("SHORT") or {}).get("tp_map") or self.overrides.get("tp_map", {})

    @property
    def cross_side_recycling(self) -> Dict[str, Any]:
        return self.cross_recycling_cfg

    @property
    def modes(self) -> List[str]:
        """Обратная совместимость: возвращает список с текущим строковым режимом."""
        return [self.mode]

    @property
    def sizing_modes(self) -> List[str]:
        """Обратная совместимость: возвращает список с текущим строковым режимом сайзинга."""
        return [self.sizing_mode]

    def get_invest_size(self, side: str, symbol: Optional[str] = None) -> float:
        """
        Возвращает размер инвестиции для стороны (LONG / SHORT) и конкретной монеты (symbol).
        Иерархия разрешения:
        1. active_settings['symbols'][symbol][side]['invest_size'] (если задано для монеты)
        2. active_settings[side]['invest_size'][symbol] (если в стороне задан per-symbol dict)
        3. self.invest_size_long / self.invest_size_short (если dict -> по монете, если float -> напрямую)
        4. self.invest_size (если dict -> по стороне или монете, если float -> напрямую)
        5. Fallback: 400.0
        """
        sym = symbol.upper().strip() if symbol else None
        side_upper = side.upper().strip()

        # 1. active_settings['symbols'][symbol]
        act_symbols = self.active_settings.get("symbols")
        if isinstance(act_symbols, dict) and sym and sym in act_symbols:
            sym_cfg = act_symbols[sym]
            if isinstance(sym_cfg, dict):
                if side_upper in sym_cfg and isinstance(sym_cfg[side_upper], dict) and "invest_size" in sym_cfg[side_upper]:
                    return float(sym_cfg[side_upper]["invest_size"])
                if "invest_size" in sym_cfg:
                    val = sym_cfg["invest_size"]
                    if isinstance(val, dict) and side_upper in val:
                        return float(val[side_upper])
                    elif isinstance(val, (int, float)):
                        return float(val)

        # 2. Выборка из self.invest_size_long / self.invest_size_short
        side_val = self.invest_size_long if side_upper == "LONG" else self.invest_size_short
        side_def = self.default_invest_size_long if side_upper == "LONG" else self.default_invest_size_short
        if isinstance(side_val, dict):
            if sym and sym in side_val:
                return float(side_val[sym])
            if "DEFAULT" in side_val:
                return float(side_val["DEFAULT"])
            if side_def is not None:
                return float(side_def)
            if side_val:
                return float(next(iter(side_val.values())))
        elif isinstance(side_val, (int, float)):
            return float(side_val)

        # 3. Fallback на явный default_invest_size стороны/стратегии
        if side_def is not None:
            return float(side_def)
        if getattr(self, "default_invest_size", None) is not None:
            return float(self.default_invest_size)

        # 4. Fallback на self.invest_size
        if isinstance(self.invest_size, dict):
            if side_upper in self.invest_size:
                v = self.invest_size[side_upper]
                if isinstance(v, dict) and sym and sym in v:
                    return float(v[sym])
                elif isinstance(v, (int, float)):
                    return float(v)
            if sym and sym in self.invest_size:
                return float(self.invest_size[sym])
        elif isinstance(self.invest_size, (int, float)):
            return float(self.invest_size)

        raise ValueError(
            f"Missing required invest_size for symbol '{symbol}' ({side_upper}) under strategy '{self.strategy_id}'"
        )

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
        universes_path: Optional[Path] = None,
        modes: Optional[List[str]] = None
    ):
        self.universes_path: Path = universes_path or (DATA_DIR / "universes.json")
        self.modes: List[str] = modes or _CFG.get("modes") or ["off"]
        self.universes_db: Dict[str, Dict[str, Any]] = self._load_universes_db()

        # Чтение конфигурации активных стратегий строго напрямую (Правило 2)
        if active_strategies_cfg is not None:
            self.active_cfg: Dict[str, Any] = active_strategies_cfg
        else:
            self.active_cfg: Dict[str, Any] = _CFG["active_strategies"]

        self.active_strategies: Dict[str, StrategyConfig] = {}
        self.symbol_to_strategy: Dict[str, StrategyConfig] = {}
        self.symbol_to_strategies: Dict[str, List[StrategyConfig]] = {}
        self.all_active_symbols: List[str] = []

        self._resolve_and_validate()

    def _build_mode_auto_closing(self, mode: str, base_ac: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Формирует конфигурацию auto_closing строго для целевого режима из переданного base_ac (или app.json)."""
        mode = mode.lower().strip()
        if base_ac is None:
            base_ac = _CFG.get("auto_closing", {})
        if not base_ac or not isinstance(base_ac, dict):
            base_ac = {"positive": {}, "negative": {}}

        neg_cfg = copy.deepcopy(base_ac.get("negative") or {})
        pos_base = copy.deepcopy(base_ac.get("positive") or {})
        raw_scale_val = pos_base.get("scale_money")
        scale_money = float(raw_scale_val) if raw_scale_val is not None else None
        desc_scale_money = pos_base.get(
            "_desc_scale_money",
            "Масштаб 1.0 рассчитан на матожидание системы — 400 USDT на сторону."
        )

        auto_scale_money = pos_base.get("auto_scale_money", False)
        margin_relative = copy.deepcopy(pos_base.get("margin_relative") or {})
        mode_scale = pos_base.get("mode_scale", "inverted_notional")
        inverted_notional = copy.deepcopy(pos_base.get("inverted_notional") or {})

        if mode == "off":
            return {
                "positive": {},
                "negative": neg_cfg
            }
        elif mode == "fixed":
            fixed_net = copy.deepcopy(pos_base.get("fixed_net") or {"target_net_usdt": 15.0})
            if "target_net_usdt" not in fixed_net:
                fixed_net["target_net_usdt"] = 15.0
            pos_cfg = {
                "scale_money": scale_money,
                "_desc_scale_money": desc_scale_money,
                "auto_scale_money": auto_scale_money,
                "mode_scale": mode_scale,
                "inverted_notional": inverted_notional,
                "margin_relative": margin_relative,
                "fixed_net": fixed_net
            }
            return {
                "positive": pos_cfg,
                "negative": neg_cfg
            }
        elif mode == "toxic":
            toxicity_cfg = copy.deepcopy(pos_base.get("toxicity_adaptive") or {
                "panic_toxicity_threshold": 0.70,
                "relaxed_toxicity_threshold": 0.35,
                "emergency_exit_floor_usdt": 3.0,
                "moderate_exit_floor_usdt": 8.0,
                "relaxed_exit_floor_usdt": 15.0,
                "min_panic_recovery": 0.40,
                "min_moderate_recovery": 0.20,
            })
            pos_cfg = {
                "scale_money": scale_money,
                "_desc_scale_money": desc_scale_money,
                "auto_scale_money": auto_scale_money,
                "mode_scale": mode_scale,
                "inverted_notional": inverted_notional,
                "margin_relative": margin_relative,
                "toxicity_adaptive": toxicity_cfg
            }
            return {
                "positive": pos_cfg,
                "negative": neg_cfg
            }
        elif mode in ("trailing_ratchet", "classic", "iter_net_classic"):
            if "trailing_ratchet" not in pos_base:
                raise KeyError("[StrategyRegistry] В секции auto_closing.positive отсутствует обязательный блок 'trailing_ratchet'!")
            pos_cfg = {
                "scale_money": scale_money,
                "_desc_scale_money": desc_scale_money,
                "auto_scale_money": auto_scale_money,
                "mode_scale": mode_scale,
                "inverted_notional": inverted_notional,
                "margin_relative": margin_relative,
                "trailing_ratchet": copy.deepcopy(pos_base["trailing_ratchet"])
            }
            return {
                "positive": pos_cfg,
                "negative": neg_cfg
            }
        else:
            raise ValueError(f"[StrategyRegistry] Неизвестный режим автозакрытия '{mode}'! Допустимые режимы: 'trailing_ratchet', 'toxic', 'fixed', 'off'.")

    def _load_universes_db(self) -> Dict[str, Dict[str, Any]]:
        """Загружает базу спецификаций стратегий из папки CFG/universes/*.json."""
        data: Dict[str, Dict[str, Any]] = {}
        self.common_defaults: Dict[str, Any] = {}
        universes_dir = self.universes_path.parent / "universes"
        if universes_dir.exists() and universes_dir.is_dir():
            for jf in sorted(universes_dir.glob("*.json")):
                if jf.name == "_base.json" or jf.stem.startswith("_"):
                    continue
                data[jf.stem] = Utils.read_json_file(jf)

            # Архивные/классические шаблоны из CFG/artifacts/classic_tribute/*.json (для обратной совместимости и бэктестов)
            artifacts_tribute_dir = self.universes_path.parent / "artifacts" / "classic_tribute"
            if artifacts_tribute_dir.exists() and artifacts_tribute_dir.is_dir():
                for jf in sorted(artifacts_tribute_dir.glob("*.json")):
                    if not jf.stem.startswith("_"):
                        tribute_content = Utils.read_json_file(jf)
                        if isinstance(tribute_content, dict):
                            # Если спецификация завернута внутрь ключа с именем файла (как в u0_base_classic_supergrid)
                            if jf.stem in tribute_content and isinstance(tribute_content[jf.stem], dict):
                                tribute_content = tribute_content[jf.stem]
                            if "name" in tribute_content:
                                if jf.stem not in data:
                                    data[jf.stem] = tribute_content
                                if jf.stem == "u16_chita":
                                    data["u16_chita_v30"] = copy.deepcopy(tribute_content)
                                    data["u16_chita_30"] = copy.deepcopy(tribute_content)
                                    data["30"] = copy.deepcopy(tribute_content)
                                elif jf.stem == "u0_base_classic_supergrid":
                                    data["u0"] = copy.deepcopy(tribute_content)

        # Fallback к universes.json (для обратной совместимости внешних утилит)
        if not data and self.universes_path.exists():
            ujson = Utils.read_json_file(self.universes_path)
            if isinstance(ujson, dict):
                for k, v in ujson.items():
                    if k not in data and not k.startswith("_"):
                        data[k] = v

        if not data:
            raise FileNotFoundError(
                f"[StrategyRegistry] Критическая ошибка: шаблоны стратегий не найдены в {universes_dir}!"
            )

        # Поддержка наследования шаблонов в архивных трибьют-файлах
        for u_id, u_meta in list(data.items()):
            if isinstance(u_meta, dict):
                base_ref = u_meta.get("base_template") or u_meta.get("template")
                if base_ref and base_ref in data and base_ref != u_id:
                    parent_meta = copy.deepcopy(data[base_ref])
                    data[u_id] = deep_merge_dict(parent_meta, u_meta)

        return data

    @classmethod
    def resolve_template_id(cls, raw_id: str, universes_db: Dict[str, Any]) -> Optional[str]:
        """
        Интеллектуальное и гибкое разрешение алиасов, версий и суффиксов стратегий в канонический universe_id:
        1. Точное совпадение с ключом в universes_db (с учетом и без учета регистра).
        2. Каноническая таблица версий и алиасов (u16, u16_v30, u16_v31, u6, u96, omen, copper_snake, chita, medny_zmey и т.д.).
        3. Гибкий матчинг пользовательских суффиксов (например, 'u16_chita_v31_client1' -> 'u16_chita_v31').
        4. Гибкий матчинг пользовательских префиксов (например, 'client_alex_u16_chita_v31' -> 'u16_chita_v31').
        5. Токенный поиск по компонентам имени ('alex_u16' -> 'u16_chita', 'friend_u6' -> 'u6_symbiosis_gamma_fortress35').
        6. Префиксный поиск (например, 'u6' среди ['u6_symbiosis_gamma_fortress35']).
        7. Нечувствительный к регистру поиск по подстроке.
        """
        if not raw_id:
            return None
        target = str(raw_id).strip()
        if target in universes_db:
            return target
        target_lower = target.lower()
        if target_lower in universes_db:
            return target_lower

        ALIAS_MAP = {
            # U16 Чита v3.1 (Hybrid Snake Bridge + Panic Damper)
            "u16_v31": "u16_chita_v31",
            "u16_31": "u16_chita_v31",
            "u16_chita_v31": "u16_chita_v31",
            "chita_v31": "u16_chita_v31",
            "chita_31": "u16_chita_v31",
            "chita31": "u16_chita_v31",
            "cheetah_v31": "u16_chita_v31",
            "cheetah_31": "u16_chita_v31",
            "cheetah31": "u16_chita_v31",
            "чита_v31": "u16_chita_v31",
            "чита_31": "u16_chita_v31",
            "чита31": "u16_chita_v31",
            "чита_3.1": "u16_chita_v31",
            "чита 3.1": "u16_chita_v31",
            "chita_3.1": "u16_chita_v31",
            "chita 3.1": "u16_chita_v31",

            # U16 Чита v3.0 (Каноническая броня)
            "u16": "u16_chita",
            "chita": "u16_chita",
            "cheetah": "u16_chita",
            "u16_chita": "u16_chita",
            "u16_titan": "u16_chita",
            "u16_apex": "u16_chita",
            "чита": "u16_chita",
            "u16_v30": "u16_chita",
            "u16_30": "u16_chita",
            "chita_v30": "u16_chita",
            "chita_30": "u16_chita",
            "chita30": "u16_chita",
            "чита_v30": "u16_chita",
            "чита_30": "u16_chita",
            "чита30": "u16_chita",
            "чита_3.0": "u16_chita",
            "chita_3.0": "u16_chita",

            # U6 Медный Змей (Gamma Fortress 35)
            "u6": "u6_symbiosis_gamma_fortress35",
            "u6_symbiosis": "u6_symbiosis_gamma_fortress35",
            "u6_gamma": "u6_symbiosis_gamma_fortress35",
            "u6_fortress": "u6_symbiosis_gamma_fortress35",
            "copper_snake": "u6_symbiosis_gamma_fortress35",
            "medny_zmey": "u6_symbiosis_gamma_fortress35",
            "medny": "u6_symbiosis_gamma_fortress35",
            "змея": "u6_symbiosis_gamma_fortress35",
            "змей": "u6_symbiosis_gamma_fortress35",
            "медный": "u6_symbiosis_gamma_fortress35",
            "медный_змей": "u6_symbiosis_gamma_fortress35",

            # U96 Омен (Omni Zenith Apex)
            "u96": "u96_omni_zenith_apex",
            "u9": "u96_omni_zenith_apex",
            "u96_omni": "u96_omni_zenith_apex",
            "omen": "u96_omni_zenith_apex",
            "omen96": "u96_omni_zenith_apex",
            "omni": "u96_omni_zenith_apex",
            "омен": "u96_omni_zenith_apex",
            "омен96": "u96_omni_zenith_apex",

            # U0 Классик Супергрид (Base Classic SuperGrid)
            "u0": "u0_base_classic_supergrid",
            "u0_classic": "u0_base_classic_supergrid",
            "u0_base": "u0_base_classic_supergrid",
            "supergrid": "u0_base_classic_supergrid",
            "u0_base_classic_supergrid": "u0_base_classic_supergrid",

            # U16 Чита Origin
            "u16_chita_origin": "u16_chita_origin",
            "chita_origin": "u16_chita_origin",
            "origin": "u16_chita_origin",

            # U10 Spinning Early Rebound
            "u10": "u10_spinning_early_rebound",
            "u10_spinning": "u10_spinning_early_rebound",

            # U12 Cross Side Recycler & Spinning
            "u12": "u12_cross_side_recycler",
            "u12_atr": "u12_cross_side_recycler",
            "u12_recycler": "u12_cross_side_recycler",
            "u12_v1": "u12_cross_side_recycler",
            "u12_v2": "u12_spinning_cross_rebound_recycler",
            "u12_spinning": "u12_spinning_cross_rebound_recycler",
            "u12_rebound": "u12_spinning_cross_rebound_recycler",

            # U16 Chita v3.6E3 / v3.6E4 / v3.6E5 (Auto-EMA Fist)
            "v36e": "u16_chita_v36E3",
            "u16_v36e": "u16_chita_v36E3",
            "u16_chita_v36e": "u16_chita_v36E3",
            "chita_v36e": "u16_chita_v36E3",
            "v36e3": "u16_chita_v36E3",
            "u16_v36e3": "u16_chita_v36E3",
            "u16_chita_v36e3": "u16_chita_v36E3",
            "chita_v36e3": "u16_chita_v36E3",
            "v36e4": "u16_chita_v36E4",
            "u16_v36e4": "u16_chita_v36E4",
            "u16_chita_v36e4": "u16_chita_v36E4",
            "chita_v36e4": "u16_chita_v36E4",
            "v36e5": "u16_chita_v36E5",
            "u16_v36e5": "u16_chita_v36E5",
            "u16_chita_v36e5": "u16_chita_v36E5",
            "chita_v36e5": "u16_chita_v36E5",

            # U16 Chita v3.6n (Neutral Hand 9/9)
            "v36n": "u16_chita_v36n",
            "u16_v36n": "u16_chita_v36n",
            "u16_chita_v36n": "u16_chita_v36n",
            "chita_v36n": "u16_chita_v36n",

            # U16 Chita v3.3FK (Hybrid Apex with Golden-Cut 38.2% Fibo-Kurki Triggers)
            "v33fk": "u16_chita_v33FK",
            "u16_v33fk": "u16_chita_v33FK",
            "u16_chita_v33fk": "u16_chita_v33FK",
            "chita_v33fk": "u16_chita_v33FK",
            "u16_chita_v33FK": "u16_chita_v33FK",
        }
        if target_lower in ALIAS_MAP:
            cand = ALIAS_MAP[target_lower]
            if cand in universes_db:
                return cand

        # Поиск по началу строки: база + суффикс (например, 'u16_chita_v31_client1' -> 'u16_chita_v31')
        # Сортируем по убыванию длины, чтобы более специфичные имена (u16_chita_v31) имели приоритет перед (u16_chita)
        sorted_keys = sorted(universes_db.keys(), key=len, reverse=True)
        for k in sorted_keys:
            if target_lower.startswith(k.lower() + "_"):
                return k

        # Поиск по окончанию строки: префикс + база (например, 'client_alex_u16_chita_v31' -> 'u16_chita_v31')
        for k in sorted_keys:
            if target_lower.endswith("_" + k.lower()):
                return k

        # Проверяем вхождение известных составных алиасов (сортируем по длине убывания, чтобы 'u16_v31' / 'chita_v31' проверялись раньше 'u16' / 'chita')
        for alias_key in sorted(ALIAS_MAP.keys(), key=len, reverse=True):
            if "_" in alias_key or len(alias_key) >= 4:
                if alias_key in target_lower:
                    cand = ALIAS_MAP[alias_key]
                    if cand in universes_db:
                        return cand

        # Токенный поиск по компонентам (разделенным '_') среди известных алиасов
        tokens = target_lower.split("_")
        for tok in tokens:
            if tok in ALIAS_MAP:
                cand = ALIAS_MAP[tok]
                if cand in universes_db:
                    return cand
            if tok in universes_db:
                return tok

        # Префиксный поиск (например, 'u6' среди ['u6_symbiosis_gamma_fortress35'])
        matching_prefix = [k for k in universes_db if k.lower().startswith(target_lower + "_") or k.lower() == target_lower]
        if len(matching_prefix) == 1:
            return matching_prefix[0]

        # Поиск по подстроке
        matching_substr = [k for k in universes_db if target_lower in k.lower()]
        if len(matching_substr) == 1:
            return matching_substr[0]

        return None

    def _resolve_and_validate(self) -> None:
        """
        Проверяет наличие объявленных стратегий в базе и регистрирует символы.
        Поддерживает мультистратегичность и торговлю одинаковыми монетами под разными куполами/аккаунтами.
        Каждая активная стратегия имеет детерминированное имя с суффиксом режима (__classic, __toxic, __fixed, __off)
        и строго изолированный набор монет symbols.
        """
        self.active_strategies.clear()
        self.symbol_to_strategy.clear()
        self.symbol_to_strategies.clear()
        resolved_symbols: List[str] = []
        seen_symbols: set[str] = set()
        account_symbols: Dict[str, Dict[str, str]] = {}

        for strat_id, strat_settings in self.active_cfg.items():
            if strat_id.startswith("_") or not isinstance(strat_settings, dict):
                continue
            is_enabled = bool(strat_settings.get("enabled", False))
            if not is_enabled:
                continue

            # Поиск спецификации базового шаблона стратегии с поддержкой алиасов
            raw_template = strat_settings.get("template")
            resolved_template = None
            if raw_template:
                resolved_template = self.resolve_template_id(raw_template, self.universes_db)
                if not resolved_template:
                    raise KeyError(
                        f"[StrategyRegistry] Указанный шаблон стратегии '{raw_template}' (для купола '{strat_id}') "
                        f"отсутствует в CFG/universes/*.json (доступно: {list(self.universes_db.keys())})!"
                    )
            else:
                clean_id = strat_id
                for sfx in ("__off", "__trailing_ratchet", "__classic", "__fixed", "__toxic"):
                    if clean_id.endswith(sfx):
                        clean_id = clean_id[:-len(sfx)]
                        break
                resolved_template = self.resolve_template_id(clean_id, self.universes_db)
                if not resolved_template and "__" in strat_id:
                    part_0 = strat_id.split("__")[0]
                    resolved_template = self.resolve_template_id(part_0, self.universes_db)
                if not resolved_template:
                    raise KeyError(
                        f"[StrategyRegistry] Спецификация стратегии для купола '{strat_id}' (чистый ID: '{clean_id}') "
                        f"отсутствует в CFG/universes/*.json (доступно: {list(self.universes_db.keys())})!"
                    )
            template_id = resolved_template

            # Определяем целевой строковый режим автозакрытия
            raw_mode = strat_settings.get("auto_closing_mode") or strat_settings.get("mode")
            if raw_mode is None and "modes" in strat_settings:
                legacy_m = strat_settings["modes"]
                raw_mode = legacy_m[0] if isinstance(legacy_m, list) and legacy_m else str(legacy_m)

            if raw_mode:
                clean_m = str(raw_mode).lower().strip()
                target_mode = "trailing_ratchet" if clean_m == "classic" else clean_m
            elif "__trailing_ratchet" in strat_id:
                target_mode = "trailing_ratchet"
            elif "__fixed" in strat_id:
                target_mode = "fixed"
            elif "__classic" in strat_id:
                target_mode = "trailing_ratchet"
            elif "__toxic" in strat_id:
                target_mode = "toxic"
            elif "__off" in strat_id:
                target_mode = "off"
            else:
                target_mode = "off"

            # Слияние метаданных: _base.json -> universes/<template>.json -> strat_settings (override)
            base_meta = copy.deepcopy(getattr(self, "common_defaults", {}))
            template_meta = copy.deepcopy(self.universes_db[template_id])
            raw_meta = deep_merge_dict(base_meta, template_meta)

            dome_override = strat_settings.get("override") or strat_settings.get("overrides") or {}
            if dome_override and isinstance(dome_override, dict):
                raw_meta = deep_merge_dict(raw_meta, dome_override)

            # Накладываем автозакрытие строго под целевой строковый режим купола
            ac_base = copy.deepcopy(
                raw_meta.get("auto_closing")
                or getattr(self, "common_defaults", {}).get("auto_closing")
                or _CFG.get("auto_closing", {})
            )
            ac_block = self._build_mode_auto_closing(target_mode, base_ac=ac_base)
            raw_meta["auto_closing"] = ac_block
            raw_meta["auto_closing_mode"] = target_mode
            if "overrides" not in raw_meta or not isinstance(raw_meta["overrides"], dict):
                raw_meta["overrides"] = {}
            raw_meta["overrides"]["auto_closing"] = ac_block

            # Прямое чтение списка монет купола (Правило 2):
            # Если symbols не задан явно, единым источником истины являются ключи per-symbol invest_size
            raw_symbols = strat_settings.get("symbols")
            if not raw_symbols:
                found_syms = []
                for inv_source in [
                    (strat_settings.get("LONG") or {}).get("invest_size"),
                    (strat_settings.get("SHORT") or {}).get("invest_size"),
                    strat_settings.get("invest_size")
                ]:
                    if isinstance(inv_source, dict):
                        for k in inv_source.keys():
                            k_clean = str(k).upper().strip()
                            if k_clean and k_clean not in found_syms and k_clean not in ("DEFAULT", "LONG", "SHORT"):
                                found_syms.append(k_clean)
                raw_symbols = found_syms

            if isinstance(raw_symbols, dict):
                strat_symbols = [s.upper().strip() for s in raw_symbols.keys() if s and s.strip()]
            else:
                strat_symbols = [s.upper().strip() for s in raw_symbols if s and s.strip()]

            strat_settings["symbols"] = list(strat_symbols)

            strat_config = StrategyConfig(
                strategy_id=strat_id,
                raw_meta=raw_meta,
                symbols=strat_symbols,
                active_settings=strat_settings
            )
            strat_config.mode = target_mode
            strat_config.auto_closing_mode = target_mode
            self.active_strategies[strat_id] = strat_config
            raw_sm = ac_block.get("positive", {}).get("scale_money")
            scale_money_disp = f"{float(raw_sm):.2f}x" if raw_sm is not None else "auto (динамический по марже)"
            logger.info(
                f"[StrategyRegistry] 🚀 Стратегия [{strat_id}] активирована: "
                f"mode={target_mode}, start_balance={strat_config.start_balance_usdt:.2f} USDT, "
                f"scale_money={scale_money_disp} (масштаб капитала автоклозера), "
                f"монеты={strat_symbols}"
            )

            acc_id = strat_config.get_account_id()
            if acc_id not in account_symbols:
                account_symbols[acc_id] = {}

            for sym in strat_symbols:
                # ОБЯЗАТЕЛЬНАЯ ВАЛИДАЦИЯ: на одном аккаунте монеты не могут пересекаться
                if sym in account_symbols[acc_id]:
                    conflicting_strat = account_symbols[acc_id][sym]
                    raise ValueError(
                        f"[StrategyRegistry] КОНФЛИКТ В РАМКАХ ОДНОГО АККАУНТА ('{acc_id}')! "
                        f"Символ '{sym}' одновременно назначен в две стратегии: '{conflicting_strat}' и '{strat_id}'. "
                        f"На одном биржевом счете Binance монеты между стратегиями обязаны быть строго уникальными! "
                        f"Для одновременной торговли одной монеты в разных стратегиях укажите разные api_key_name в app.json."
                    )
                account_symbols[acc_id][sym] = strat_id

                self.symbol_to_strategies.setdefault(sym, []).append(strat_config)
                if sym in self.symbol_to_strategy:
                    other_strat = self.symbol_to_strategy[sym].strategy_id
                    logger.info(
                        f"[StrategyRegistry] Символ '{sym}' зарегистрирован в нескольких стратегиях: "
                        f"'{other_strat}' и '{strat_id}' (на РАЗНЫХ аккаунтах)."
                    )
                self.symbol_to_strategy[sym] = strat_config

                if sym not in seen_symbols:
                    seen_symbols.add(sym)
                    resolved_symbols.append(sym)

        self.all_active_symbols = resolved_symbols
        logger.info(
            f"[StrategyRegistry] Loaded {len(self.active_strategies)} active strategies: {list(self.active_strategies.keys())}. "
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

    def get_strategies_for_symbol(self, symbol: str) -> List[StrategyConfig]:
        """Возвращает список всех стратегий, к которым привязан данный символ."""
        return list(self.symbol_to_strategies.get(symbol.upper().strip(), []))

    def get_all_active_symbols(self) -> List[str]:
        """Возвращает общий объединенный список символов всех активных стратегий."""
        return list(self.all_active_symbols)

    def get_active_strategies(self) -> Dict[str, StrategyConfig]:
        """Возвращает словарь всех активно сконфигурированных стратегий из active_cfg."""
        return {sid: self.active_strategies[sid] for sid in self.active_cfg.keys() if sid in self.active_strategies}

    def get_strategy(self, strategy_id: str) -> Optional[StrategyConfig]:
        """Возвращает конфигурацию стратегии по ID или None."""
        if strategy_id in self.active_strategies:
            return self.active_strategies[strategy_id]
        # Безопасный поиск если передан базовый ID без суффикса
        for sid, scfg in self.active_strategies.items():
            if sid.startswith(f"{strategy_id}__") or strategy_id.startswith(f"{sid}__"):
                return scfg
        if hasattr(self, "universes_db") and strategy_id in self.universes_db:
            base_meta = copy.deepcopy(getattr(self, "common_defaults", {}))
            template_meta = copy.deepcopy(self.universes_db[strategy_id])
            raw_meta = deep_merge_dict(base_meta, template_meta)
            return StrategyConfig(
                strategy_id=strategy_id,
                raw_meta=raw_meta,
                symbols=[]
            )
        return None

    def reload(self, active_strategies_cfg: Optional[Dict[str, Any]] = None) -> None:
        """Перезагружает базу стратегий и пересобирает активные маппинги символов."""
        self.universes_db = self._load_universes_db()
        if active_strategies_cfg is not None:
            self.active_cfg = active_strategies_cfg
        else:
            app_data = Utils.read_json_file(DATA_DIR / "app.json")
            self.active_cfg = app_data.get("active_strategies", {})
        self._resolve_and_validate()
