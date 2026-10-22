# CORE/STRATEGY/strategy_config.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.STRATEGY.strategy_config
ROLE: Инкапсуляция конфигурации и нормализация параметров торговой стратегии (купола)
==============================================================================
"""

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
)
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.STRATEGY.brownian_noise import BrownianNoiseController
from CORE.STRATEGY.momentum_engine import MomentumEngine

logger = UnifiedLogger("StrategyConfig")


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
        act_short_inv = (self.active_settings.get("SHORT") or {}).get("invest_size")
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
            self.start_balance_usdt: float = 434.0
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

        # 4. Купол Авто-закрытия (Auto-closing Dome): конфигурация из паспорта стратегии и app.json
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

        # 6. Режим сайзинга: в HRON-3 весь сайзинг по умолчанию статический (базовые объемы сетки).
        # Вся динамика управляется сугубо через smart_size и/или first_order_weight_regime в паспорте стратегии.
        raw_tsd = raw_meta.get("trend_size_depender") or self.active_settings.get("trend_size_depender") or raw_meta.get("sizing_mode") or self.active_settings.get("sizing_mode")
        if isinstance(raw_tsd, str) and raw_tsd.lower().strip() == "dyn_ema_16":
            self.sizing_mode: str = "dyn_ema_16"
            self.trend_size_depender: str = "dyn_ema_16"
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
        else:
            self.sizing_mode: str = "static"
            self.trend_size_depender: str = "static"
            self.trend_size_cfg = {"enabled": False, "mode": "static"}

        # Конфигурация Smart Grace & Brownian Noise
        base_smart_grace = copy.deepcopy(DEFAULT_SMART_GRACE)
        sg_cfg = raw_meta.get("smart_grace") or self.overrides.get("smart_grace")
        if sg_cfg and isinstance(sg_cfg, dict):
            base_smart_grace.update(sg_cfg)
        if "shift_interval_sec" not in base_smart_grace:
            base_smart_grace["shift_interval_sec"] = DEFAULT_SHIFT_INTERVAL_SEC
        from CORE.STRATEGY.brownian_noise import BrownianNoiseController
        from CORE.STRATEGY.momentum_engine import MomentumEngine

        self.smart_grace_cfg: Dict[str, Any] = base_smart_grace
        self.brownian_controller: BrownianNoiseController = BrownianNoiseController(
            smart_grace_cfg=self.smart_grace_cfg
        )
        self.brownian_noise_factor: Optional[Tuple[float, float]] = (
            self.brownian_controller.get_jitter_range()
        )

        # Конфигурации Smart Size и Smart TP (нормализация на старте)
        base_smart_size = copy.deepcopy(DEFAULT_SMART_SIZE)
        ss_cfg = raw_meta.get("smart_size") or self.overrides.get("smart_size")
        if ss_cfg and isinstance(ss_cfg, dict):
            base_smart_size.update(ss_cfg)

        self.smart_size_cfg: Dict[str, Any] = base_smart_size
        self.smart_size_trend_determ: Optional[str] = self.smart_size_cfg.get("trend_determ")
        
        # Knife blocker нормализация: пре-инициализируем прямые флаги и порог
        if ss_cfg and "knife_blocker_threshold" in ss_cfg and "knife_blocker_enabled" not in ss_cfg:
            self.knife_blocker_enabled: bool = ss_cfg["knife_blocker_threshold"] is not None
            self.knife_blocker_threshold: int = int(ss_cfg["knife_blocker_threshold"])
        elif "knife_blocker" in self.smart_size_cfg and isinstance(self.smart_size_cfg["knife_blocker"], dict):
            kb = self.smart_size_cfg["knife_blocker"]
            self.knife_blocker_enabled: bool = bool(kb.get("enabled", False))
            self.knife_blocker_threshold: int = int(kb.get("threshold", 2))
        else:
            self.knife_blocker_enabled: bool = bool(self.smart_size_cfg.get("knife_blocker_enabled", False))
            self.knife_blocker_threshold: int = int(self.smart_size_cfg.get("knife_blocker_threshold", 2))
        self.smart_size_cfg["knife_blocker_enabled"] = self.knife_blocker_enabled
        self.smart_size_cfg["knife_blocker_threshold"] = self.knife_blocker_threshold

        base_smart_tp = copy.deepcopy(DEFAULT_SMART_TP)
        st_cfg = raw_meta.get("smart_tp") or self.overrides.get("smart_tp")
        if st_cfg and isinstance(st_cfg, dict):
            base_smart_tp.update(st_cfg)
        self.smart_tp_cfg: Dict[str, Any] = base_smart_tp
        self.smart_tp_trend_determ: Optional[str] = self.smart_tp_cfg.get("trend_determ")

        self.momentum_engine: MomentumEngine = MomentumEngine(
            smart_size_cfg=self.smart_size_cfg,
            smart_tp_cfg=self.smart_tp_cfg,
        )
        raw_min_tp = (
            raw_meta.get("min_tp_indent")
            or self.overrides.get("min_tp_indent")
            or _CFG.get("min_tp_indent")
        )
        self.min_tp_indent: float = float(raw_min_tp) if raw_min_tp is not None else 0.50


        raw_atr_grid = (
            self.active_settings.get("dynamic_atr_grid")
            or raw_meta.get("dynamic_atr_grid")
            or self.overrides.get("dynamic_atr_grid")
            or _CFG.get("dynamic_atr_grid")
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
            self.recycling_min_drawdown_pct: float = float(self.cross_recycling_cfg["min_drawdown_pct"])
            self.recycling_max_slice_pct: float = float(self.cross_recycling_cfg["max_slice_pct"])
            self.recycling_max_slice_ratio: float = self.recycling_max_slice_pct / 100.0
        else:
            self.recycling_min_drawdown_pct = 0.0
            self.recycling_max_slice_pct = 0.0
            self.recycling_max_slice_ratio = 0.0

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

        # Персональный пульс/хартбит стратегии: строгое прямое чтение 'signal_heartbeat_tfr'
        # из паспорта стратегии (CFG/universes/*.json) без скрытых дефолтов
        tfr_val = raw_meta.get("signal_heartbeat_tfr") or self.overrides.get("signal_heartbeat_tfr")
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
                    f"[StrategyConfig][{self.strategy_id}] Отсутствует обязательный параметр "
                    f"'signal_heartbeat_tfr' в паспорте стратегии (CFG/universes/*.json)!"
                )

        self.signal_heartbeat_tfr: str = str(tfr_val)
        self.heartbeat_tfr: str = str(tfr_val)
        self.timeframe: str = str(tfr_val)
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

