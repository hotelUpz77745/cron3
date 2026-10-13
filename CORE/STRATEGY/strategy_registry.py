# ============================================================
# FILE: CORE/STRATEGY/strategy_registry.py
# ROLE: Strategy template registry and symbol validation
# ============================================================

import copy
import json
from pathlib import Path
from typing import Dict, List, Optional, Any

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
        symbols: List[str],
        active_settings: Optional[Dict[str, Any]] = None
    ):
        self.strategy_id: str = strategy_id
        self.name: str = raw_meta["name"]
        self.description: str = raw_meta.get("description", "")
        self.raw_meta: Dict[str, Any] = copy.deepcopy(raw_meta)

        meta_overrides = raw_meta.get("overrides") or {}
        long_inv = (raw_meta.get("LONG") or meta_overrides.get("LONG") or {}).get("invest_size")
        short_inv = (raw_meta.get("SHORT") or meta_overrides.get("SHORT") or {}).get("invest_size")

        if long_inv is not None and short_inv is not None:
            if float(long_inv) == float(short_inv):
                self.invest_size = float(long_inv)
            else:
                self.invest_size = {"LONG": float(long_inv), "SHORT": float(short_inv)}
        elif "invest_size" in raw_meta:
            raw_inv = raw_meta["invest_size"]
            if isinstance(raw_inv, dict):
                self.invest_size = {k.upper(): float(v) for k, v in raw_inv.items()}
            else:
                self.invest_size = float(raw_inv)
        elif long_inv is not None:
            self.invest_size = float(long_inv)
        elif short_inv is not None:
            self.invest_size = float(short_inv)
        else:
            self.invest_size = 400.0

        raw_lev = raw_meta.get("leverage", 20)
        if isinstance(raw_lev, dict):
            self.leverage = {k.upper(): int(v) for k, v in raw_lev.items()}
        else:
            self.leverage = int(raw_lev)

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

        self.symbols: List[str] = [s.upper().strip() for s in symbols]
        self.active_settings: Dict[str, Any] = copy.deepcopy(active_settings or {})

        if self.active_settings:
            if "invest_size" in self.active_settings:
                act_inv = self.active_settings["invest_size"]
                if isinstance(act_inv, dict):
                    self.invest_size = {k.upper(): float(v) for k, v in act_inv.items()}
                else:
                    self.invest_size = float(act_inv)
            if "leverage" in self.active_settings:
                act_lev = self.active_settings["leverage"]
                if isinstance(act_lev, dict):
                    self.leverage = {k.upper(): int(v) for k, v in act_lev.items()}
                else:
                    self.leverage = int(act_lev)
            for side_k in ("LONG", "SHORT"):
                if side_k in self.active_settings and isinstance(self.active_settings[side_k], dict):
                    if side_k not in self.overrides:
                        self.overrides[side_k] = {}
                    self.overrides[side_k].update(self.active_settings[side_k])

        # 1. Купол Аналитики (Analytics Dome)
        strat_analytics = (
            self.active_settings.get("analytics")
            or raw_meta.get("analytics")
            or self.overrides.get("analytics")
        )
        if strat_analytics and isinstance(strat_analytics, dict) and "start_balance_usdt" in strat_analytics:
            self.start_balance_usdt: float = float(strat_analytics["start_balance_usdt"])
        elif "start_balance_usdt" in self.active_settings:
            self.start_balance_usdt: float = float(self.active_settings["start_balance_usdt"])
        elif "start_balance_usdt" in raw_meta:
            self.start_balance_usdt: float = float(raw_meta["start_balance_usdt"])
        else:
            self.start_balance_usdt: float = 150.0
        self.analytics_cfg: Dict[str, Any] = {"start_balance_usdt": self.start_balance_usdt}

        # 2. Купол Super Grid (Volatility Grid Dome)
        sg_override = (
            self.active_settings.get("super_grid")
            or raw_meta.get("super_grid")
            or self.overrides.get("super_grid")
        )
        if sg_override is not None and isinstance(sg_override, dict):
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
        )
        if notif_cfg and isinstance(notif_cfg, dict):
            self.notifications_cfg: Dict[str, Any] = copy.deepcopy(notif_cfg)
        else:
            self.notifications_cfg: Dict[str, Any] = {
                "enabled": True,
                "negative_threshold": -20.0,
                "positive_threshold": 20.0
            }

        # 4. Купол Авто-закрытия (Auto-closing Dome): базовый шаблон из raw_meta / _common с переопределениями стратегии
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

        # 5. Купол Режима Автозакрытия (Mode Dome): строго строковый тип
        mode_val = (
            self.active_settings.get("mode")
            or raw_meta.get("auto_closing_mode")
            or raw_meta.get("mode")
            or self.overrides.get("mode")
        )
        if not mode_val and self.active_settings.get("modes"):
            legacy_m = self.active_settings.get("modes")
            mode_val = legacy_m[0] if isinstance(legacy_m, list) and legacy_m else str(legacy_m)

        if mode_val and isinstance(mode_val, str):
            self.mode: str = mode_val.lower().strip()
        else:
            self.mode = "off"
        self.auto_closing_mode: str = self.mode

        # 6. Купол Режима Сайзинга (Sizing Mode Dome): строго строковый тип ("static", "dyn_inv_16", "dyn_ema_16", "dyn_ema_10")
        raw_tsd = raw_meta.get("trend_size_depender") or {}
        if not isinstance(raw_tsd, dict):
            raw_tsd = {}

        sizing_mode_val = (
            self.active_settings.get("sizing_mode")
            or raw_tsd.get("mode")
            or raw_meta.get("sizing_mode")
            or self.overrides.get("sizing_mode")
        )
        if not sizing_mode_val and self.active_settings.get("sizing_modes"):
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

        VALID_SIZING_MODES = ("static", "dyn_inv_16", "dyn_ema_16", "dyn_ema_10")
        if self.sizing_mode not in VALID_SIZING_MODES:
            raise ValueError(
                f"[StrategyConfig][{universe_id}] Неизвестный sizing_mode '{self.sizing_mode}'! "
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
            or self.overrides.get("trend_size_depender")
        )
        if user_tsd_override and isinstance(user_tsd_override, dict):
            self.trend_size_cfg.update(user_tsd_override)

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
        self.smart_size_cfg: Dict[str, Any] = base_smart_size

        base_smart_tp = copy.deepcopy(DEFAULT_SMART_TP)
        st_cfg = raw_meta.get("smart_tp") or self.overrides.get("smart_tp")
        if st_cfg and isinstance(st_cfg, dict):
            base_smart_tp.update(st_cfg)
        self.smart_tp_cfg: Dict[str, Any] = base_smart_tp
        raw_min_tp = raw_meta.get("min_tp_indent") or self.overrides.get("min_tp_indent")
        self.min_tp_indent: float = float(raw_min_tp) if raw_min_tp is not None else 0.50
        self.dynamic_atr_tp_cfg: Dict[str, Any] = copy.deepcopy(raw_meta.get("dynamic_atr_tp") or self.overrides.get("dynamic_atr_tp") or {})
        if self.dynamic_atr_tp_cfg.get("enabled"):
            for req_k in ("timeframe", "period", "cache_ttl_sec", "min_guaranteed_floor_pct", "max_ceiling_pct", "fallback_atr_pct", "level_multipliers"):
                if req_k not in self.dynamic_atr_tp_cfg:
                    raise KeyError(f"[StrategyConfig][{self.strategy_id}] Missing required dynamic_atr_tp field: '{req_k}'")

        self.cross_recycling_cfg: Dict[str, Any] = copy.deepcopy(raw_meta.get("cross_side_recycling") or self.overrides.get("cross_side_recycling") or {})
        if self.cross_recycling_cfg.get("enabled"):
            for req_k in ("recycle_ratio", "min_burn_level", "min_drawdown_pct", "max_slice_pct"):
                if req_k not in self.cross_recycling_cfg:
                    raise KeyError(f"[StrategyConfig][{self.strategy_id}] Missing required cross_side_recycling field: '{req_k}'")

        # Персональный пульс/хартбит стратегии (по умолчанию 5m)
        self.heartbeat_tfr: str = str(
            raw_meta.get("heartbeat_tfr")
            or self.overrides.get("heartbeat_tfr")
            or raw_meta.get("timeframe")
            or self.overrides.get("timeframe", DEFAULT_TIMEFRAME)
        )
        self.timeframe: str = self.heartbeat_tfr

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
    def tp_map(self) -> Dict[str, Any]:
        return (self.raw_meta.get("LONG") or {}).get("tp_map") or (self.raw_meta.get("SHORT") or {}).get("tp_map") or self.overrides.get("tp_map", {})

    @property
    def modes(self) -> List[str]:
        """Обратная совместимость: возвращает список с текущим строковым режимом."""
        return [self.mode]

    @property
    def sizing_modes(self) -> List[str]:
        """Обратная совместимость: возвращает список с текущим строковым режимом сайзинга."""
        return [self.sizing_mode]

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
        """Формирует конфигурацию auto_closing строго для целевого режима из переданного base_ac (или _common.json)."""
        mode = mode.lower().strip()
        if base_ac is None:
            base_ac = getattr(self, "common_defaults", {}).get("auto_closing") or _CFG.get("auto_closing", {})
        if not base_ac or not isinstance(base_ac, dict):
            raise KeyError("[StrategyRegistry] Отсутствует базовый шаблон auto_closing в _common.json или app.json!")

        neg_cfg = copy.deepcopy(base_ac.get("negative") or {})
        pos_base = copy.deepcopy(base_ac.get("positive") or {})
        quant_base = pos_base.get("quant_base_usdt", 10.0)
        quant_ref = pos_base.get("quant_reference_size", 600.0)

        if mode == "off":
            return {
                "enabled": False,
                "mode": "disabled",
                "positive": {"enabled": False, "mode": "disabled"},
                "negative": neg_cfg
            }
        elif mode == "fixed":
            if "fixed_net" not in pos_base or "target_net_usdt" not in pos_base["fixed_net"]:
                raise KeyError("[StrategyRegistry] В секции auto_closing.positive.fixed_net отсутствует обязательный параметр 'target_net_usdt'!")
            pos_cfg = {
                "enabled": True,
                "mode": "fixed_net",
                "quant_base_usdt": quant_base,
                "quant_reference_size": quant_ref,
                "fixed_net": copy.deepcopy(pos_base["fixed_net"])
            }
            return {
                "enabled": True,
                "mode": "fixed_net",
                "positive": pos_cfg,
                "negative": neg_cfg
            }
        elif mode == "toxic":
            if "toxicity_adaptive" not in pos_base:
                raise KeyError("[StrategyRegistry] В секции auto_closing.positive отсутствует обязательный блок 'toxicity_adaptive'!")
            pos_cfg = {
                "enabled": True,
                "mode": "toxicity_adaptive",
                "quant_base_usdt": quant_base,
                "quant_reference_size": quant_ref,
                "toxicity_adaptive": copy.deepcopy(pos_base["toxicity_adaptive"])
            }
            return {
                "enabled": True,
                "mode": "toxicity_adaptive",
                "positive": pos_cfg,
                "negative": neg_cfg
            }
        elif mode == "classic":
            if "trailing_ratchet" not in pos_base:
                raise KeyError("[StrategyRegistry] В секции auto_closing.positive отсутствует обязательный блок 'trailing_ratchet'!")
            pos_cfg = {
                "enabled": True,
                "mode": "iter_net_classic",
                "quant_base_usdt": quant_base,
                "quant_reference_size": quant_ref,
                "trailing_ratchet": copy.deepcopy(pos_base["trailing_ratchet"])
            }
            return {
                "enabled": True,
                "mode": "iter_net_classic",
                "positive": pos_cfg,
                "negative": neg_cfg
            }
        else:
            raise ValueError(f"[StrategyRegistry] Неизвестный режим автозакрытия '{mode}'! Допустимые режимы: 'fixed', 'classic', 'toxic', 'off'.")

    def _load_universes_db(self) -> Dict[str, Dict[str, Any]]:
        """Загружает базу спецификаций стратегий из папки CFG/universes/*.json и базовый шаблон _common.json."""
        data: Dict[str, Dict[str, Any]] = {}
        self.common_defaults: Dict[str, Any] = {}
        universes_dir = self.universes_path.parent / "universes"
        if universes_dir.exists() and universes_dir.is_dir():
            common_file = universes_dir / "_common.json"
            if common_file.exists():
                self.common_defaults = Utils.read_json_file(common_file) or {}
            for jf in sorted(universes_dir.glob("*.json")):
                if jf.name == "_common.json" or jf.stem.startswith("_"):
                    if not self.common_defaults and jf.name == "_common.json":
                        self.common_defaults = Utils.read_json_file(jf) or {}
                    continue
                data[jf.stem] = Utils.read_json_file(jf)

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
        return data

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

            # Поиск спецификации базового шаблона стратегии
            template_id = strat_settings.get("template")
            if not template_id:
                if strat_id in self.universes_db:
                    template_id = strat_id
                elif "__" in strat_id and strat_id.split("__")[0] in self.universes_db:
                    template_id = strat_id.split("__")[0]
                else:
                    clean_id = strat_id
                    for sfx in ("__off", "__classic", "__fixed", "__toxic"):
                        if clean_id.endswith(sfx):
                            clean_id = clean_id[:-len(sfx)]
                            break
                    if clean_id in self.universes_db:
                        template_id = clean_id
                    else:
                        raise KeyError(
                            f"[StrategyRegistry] Спецификация стратегии '{clean_id}' (для купола '{strat_id}') "
                            f"отсутствует в CFG/universes/*.json!"
                        )
            elif template_id not in self.universes_db:
                raise KeyError(
                    f"[StrategyRegistry] Указанный шаблон стратегии '{template_id}' (для купола '{strat_id}') "
                    f"отсутствует в CFG/universes/*.json!"
                )

            # Определяем целевой строковый режим автозакрытия
            raw_mode = strat_settings.get("mode")
            if raw_mode is None and "modes" in strat_settings:
                legacy_m = strat_settings["modes"]
                raw_mode = legacy_m[0] if isinstance(legacy_m, list) and legacy_m else str(legacy_m)

            if raw_mode:
                target_mode = str(raw_mode).lower().strip()
            elif "__fixed" in strat_id:
                target_mode = "fixed"
            elif "__classic" in strat_id:
                target_mode = "classic"
            elif "__toxic" in strat_id:
                target_mode = "toxic"
            elif "__off" in strat_id:
                target_mode = "off"
            else:
                target_mode = "off"

            # Слияние метаданных: _common.json -> universes/<template>.json -> strat_settings (override)
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

            # Прямое чтение списка монет купола (Правило 2)
            raw_symbols: List[str] = strat_settings.get("symbols", [])
            strat_symbols = [s.upper().strip() for s in raw_symbols if s and s.strip()]

            strat_config = StrategyConfig(
                strategy_id=strat_id,
                raw_meta=raw_meta,
                symbols=strat_symbols,
                active_settings=strat_settings
            )
            strat_config.mode = target_mode
            strat_config.auto_closing_mode = target_mode
            strat_config.auto_closing_cfg = ac_block
            self.active_strategies[strat_id] = strat_config

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
            return self.universes_db[strategy_id]
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
