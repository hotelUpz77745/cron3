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
import os
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Set, Tuple

from consts import (
    DATA_DIR,
    _CFG,
    API_KEY,
    API_SECRET,
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


from CORE.STRATEGY.strategy_config_parser import StrategyConfigParser, deep_merge_dict


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

        # 1. Автоматическое извлечение symbols из ключей per-symbol invest_size
        self.symbols: List[str] = StrategyConfigParser.resolve_symbols(self.active_settings, symbols)

        # 2. Инициализация overrides
        meta_overrides = raw_meta.get("overrides") or {}
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

        # 3. Разрешение invest_size и leverage
        inv_res = StrategyConfigParser.resolve_invest_and_leverage(
            active_settings=self.active_settings,
            raw_meta=self.raw_meta,
            meta_overrides=meta_overrides,
            overrides=self.overrides,
        )
        self.default_invest_size_long: Optional[float] = inv_res["default_invest_size_long"]
        self.default_invest_size_short: Optional[float] = inv_res["default_invest_size_short"]
        self.default_invest_size: Optional[float] = inv_res["default_invest_size"]
        self.invest_size_long: Union[float, Dict[str, float]] = inv_res["invest_size_long"]
        self.invest_size_short: Union[float, Dict[str, float]] = inv_res["invest_size_short"]
        self.invest_size = inv_res["invest_size"]
        self.leverage = inv_res["leverage"]
        self.margin_type: str = inv_res["margin_type"]

        # 4. Разрешение куполов (Analytics, Super Grid, Notifications, Auto-closing)
        domes_res = StrategyConfigParser.resolve_domes(
            active_settings=self.active_settings,
            raw_meta=self.raw_meta,
            overrides=self.overrides,
        )
        self.start_balance_usdt: float = domes_res["start_balance_usdt"]
        self.analytics_cfg: Dict[str, Any] = domes_res["analytics_cfg"]
        self.super_grid_enabled: bool = domes_res["super_grid_enabled"]
        self.super_grid_cfg = domes_res["super_grid_cfg"]
        self.notifications_cfg: Dict[str, Any] = domes_res["notifications_cfg"]
        self.auto_closing_cfg: Dict[str, Any] = domes_res["auto_closing_cfg"]
        self.mode: str = domes_res["mode"]
        self.auto_closing_mode: str = domes_res["auto_closing_mode"]

        # 5. Разрешение Smart Features & Momentum
        smart_res = StrategyConfigParser.resolve_smart_features(
            active_settings=self.active_settings,
            raw_meta=self.raw_meta,
            overrides=self.overrides,
        )
        self.sizing_mode: str = smart_res["sizing_mode"]
        self.trend_size_depender: str = smart_res["trend_size_depender"]
        self.trend_size_cfg = smart_res["trend_size_cfg"]
        self.smart_grace_cfg: Dict[str, Any] = smart_res["smart_grace_cfg"]
        self.brownian_controller: BrownianNoiseController = smart_res["brownian_controller"]
        self.brownian_noise_factor: Optional[Tuple[float, float]] = smart_res["brownian_noise_factor"]
        self.smart_size_cfg: Dict[str, Any] = smart_res["smart_size_cfg"]
        self.smart_size_trend_determ: Optional[str] = smart_res["smart_size_trend_determ"]
        self.knife_blocker_enabled: bool = smart_res["knife_blocker_enabled"]
        self.knife_blocker_threshold: int = smart_res["knife_blocker_threshold"]
        self.smart_tp_cfg: Dict[str, Any] = smart_res["smart_tp_cfg"]
        self.smart_tp_trend_determ: Optional[str] = smart_res["smart_tp_trend_determ"]
        self.momentum_engine: MomentumEngine = smart_res["momentum_engine"]
        self.min_tp_indent: float = smart_res["min_tp_indent"]

        # 6. Разрешение ATR, Recycling, Timeframe и API Credentials
        rec_res = StrategyConfigParser.resolve_recycling_and_atr(
            active_settings=self.active_settings,
            raw_meta=self.raw_meta,
            overrides=self.overrides,
            strategy_id=self.strategy_id,
        )
        self.dynamic_atr_grid_cfg: Dict[str, Any] = rec_res["dynamic_atr_grid_cfg"]
        self.dynamic_atr_tp_cfg: Dict[str, Any] = rec_res["dynamic_atr_tp_cfg"]
        self.cross_recycling_cfg: Dict[str, Any] = rec_res["cross_recycling_cfg"]
        self.recycling_min_drawdown_pct: float = rec_res["recycling_min_drawdown_pct"]
        self.recycling_max_slice_pct: float = rec_res["recycling_max_slice_pct"]
        self.recycling_max_slice_ratio: float = rec_res["recycling_max_slice_ratio"]
        self.micro_step_filter_cfg: Dict[str, Any] = rec_res["micro_step_filter_cfg"]
        self.signal_heartbeat_tfr: str = rec_res["signal_heartbeat_tfr"]
        self.heartbeat_tfr: str = rec_res["heartbeat_tfr"]
        self.timeframe: str = rec_res["timeframe"]
        self.heartbeat_seconds: int = rec_res["heartbeat_seconds"]
        self.api_key_name: Optional[str] = rec_res["api_key_name"]
        self.api_secret_name: Optional[str] = rec_res["api_secret_name"]

    def get_api_credentials(self) -> tuple[str, str]:
        """
        Возвращает (api_key, api_secret) для аккаунта данной стратегии.
        Если указаны api_key_name / api_secret_name в app.json — читает их из .env.
        Также проверяет суффикс стратегии API_KEY_<STRAT_ID>.
        Если не найдены — fallback на базовые глобальные API_KEY и API_SECRET.
        """
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

