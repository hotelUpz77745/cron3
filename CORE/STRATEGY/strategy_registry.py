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

        # 4. Купол Авто-закрытия (Auto-closing Dome): берется из стратегии, либо базовый шаблон из app.json
        # 4. Купол Авто-закрытия (Auto-closing Dome): базовый шаблон из app.json с переопределениями стратегии
        base_ac = copy.deepcopy(_CFG.get("auto_closing") or DEFAULT_AUTO_CLOSING)
        override_ac = (
            self.active_settings.get("auto_closing")
            or raw_meta.get("auto_closing")
            or self.overrides.get("auto_closing")
        )
        if override_ac and isinstance(override_ac, dict):
            for k, v in override_ac.items():
                if k == "positive" and isinstance(v, dict) and "positive" in base_ac and isinstance(base_ac["positive"], dict):
                    base_ac["positive"].update(v)
                else:
                    base_ac[k] = v
        self.auto_closing_cfg: Dict[str, Any] = base_ac

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

        # Персональный таймфрейм вселенной (по умолчанию 5m)
        self.timeframe: str = str(raw_meta.get("timeframe") or self.overrides.get("timeframe", DEFAULT_TIMEFRAME))

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

    def _build_mode_auto_closing(self, mode: str) -> Dict[str, Any]:
        """Формирует конфигурацию auto_closing строго для целевого режима."""
        mode = mode.lower().strip()
        base_ac = _CFG.get("auto_closing") or copy.deepcopy(DEFAULT_AUTO_CLOSING)
        if mode == "off":
            return {
                "enabled": False,
                "mode": "disabled",
                "positive": {"enabled": False, "mode": "disabled"},
                "negative": {"enabled": False, "threshold": None, "threshold_increment": None}
            }
        elif mode == "fixed":
            pos_cfg = {
                "enabled": True,
                "mode": "fixed_net",
                "quant_base_usdt": base_ac.get("positive", {}).get("quant_base_usdt", 10.0),
                "quant_reference_size": base_ac.get("positive", {}).get("quant_reference_size", 600.0),
                "fixed_net": base_ac.get("positive", {}).get("fixed_net") or {
                    "target_net_usdt": 21.0,
                    "scale_by_invest_size": False
                }
            }
            return {
                "enabled": True,
                "mode": "fixed_net",
                "positive": pos_cfg,
                "negative": base_ac.get("negative") or {"threshold": None, "threshold_increment": None}
            }
        elif mode == "toxic":
            pos_cfg = {
                "enabled": True,
                "mode": "toxicity_adaptive",
                "quant_base_usdt": base_ac.get("positive", {}).get("quant_base_usdt", 10.0),
                "quant_reference_size": base_ac.get("positive", {}).get("quant_reference_size", 600.0),
                "toxicity_adaptive": base_ac.get("positive", {}).get("toxicity_adaptive") or {}
            }
            return {
                "enabled": True,
                "mode": "toxicity_adaptive",
                "positive": pos_cfg,
                "negative": base_ac.get("negative") or {"threshold": None, "threshold_increment": None}
            }
        elif mode == "classic":
            pos_cfg = {
                "enabled": True,
                "mode": "iter_net_classic",
                "quant_base_usdt": base_ac.get("positive", {}).get("quant_base_usdt", 10.0),
                "quant_reference_size": base_ac.get("positive", {}).get("quant_reference_size", 600.0),
                "trailing_ratchet": base_ac.get("positive", {}).get("trailing_ratchet") or {
                    "enabled": True,
                    "levels": [
                        {"activation": 8.0, "floor": 5.5, "min_age_hours": 24.0, "enabled": True},
                        {"activation": 15.5, "floor": 13.0, "min_age_hours": 0.0, "enabled": True},
                        {"activation": 23.5, "floor": 21.0, "min_age_hours": 0.0, "enabled": True},
                        {"activation": 36.5, "floor": 34.0, "min_age_hours": 0.0, "enabled": True},
                        {"activation": 57.5, "floor": 55.0, "min_age_hours": 0.0, "enabled": True},
                        {"activation": 89.0, "floor": 89.0, "min_age_hours": 0.0, "enabled": True}
                    ]
                }
            }
            return {
                "enabled": True,
                "mode": "iter_net_classic",
                "positive": pos_cfg,
                "negative": base_ac.get("negative") or {"threshold": None, "threshold_increment": None}
            }
        else:
            return {"enabled": False, "mode": "disabled"}

    def _load_universes_db(self) -> Dict[str, Dict[str, Any]]:
        """Загружает базу шаблонов: модульная папка CFG/universes/*.json."""
        data: Dict[str, Dict[str, Any]] = {}
        universes_dir = self.universes_path.parent / "universes"
        if universes_dir.exists() and universes_dir.is_dir():
            for jf in sorted(universes_dir.glob("*.json")):
                data[jf.stem] = Utils.read_json_file(jf)

        # Fallback к universes.json (для обратной совместимости внешних утилит)
        if not data and self.universes_path.exists():
            ujson = Utils.read_json_file(self.universes_path)
            if isinstance(ujson, dict):
                for k, v in ujson.items():
                    if k not in data:
                        data[k] = v

        # Автоматическое размножение чистых шаблонов-архетипов на режимы автоклозера (внешне из app.json)
        expanded_variants: Dict[str, Dict[str, Any]] = {}
        all_candidate_modes = ["fixed", "off", "classic", "toxic"]
        for uid, udata in list(data.items()):
            # Проверяем, является ли шаблон чистым архетипом (без суффикса)
            is_suffixed = any(uid.endswith(sfx) for sfx in ("__off", "__classic", "__fixed", "__toxic"))
            if not is_suffixed:
                clean_name = udata.get("name", uid)
                for m in all_candidate_modes:
                    m_key = m.lower().strip()
                    variant_id = f"{uid}__{m_key}"
                    if variant_id not in data and variant_id not in expanded_variants:
                        v_copy = copy.deepcopy(udata)
                        v_copy.pop("modes", None)
                        v_copy["auto_closing_mode"] = m_key
                        v_copy["name"] = f"[{variant_id}] {clean_name} [{m_key.upper()}]"
                        ac_block = self._build_mode_auto_closing(m_key)
                        v_copy["auto_closing"] = ac_block
                        expanded_variants[variant_id] = v_copy
            else:
                for sfx in ("__off", "__classic", "__fixed", "__toxic"):
                    if uid.endswith(sfx):
                        base_name = uid[:-len(sfx)]
                        if base_name not in data and base_name not in expanded_variants:
                            expanded_variants[base_name] = udata
                        break

        data.update(expanded_variants)

        if not data:
            raise FileNotFoundError(
                f"[StrategyRegistry] Критическая ошибка: шаблоны стратегий не найдены в {universes_dir}!"
            )
        return data

    def _resolve_and_validate(self) -> None:
        """
        Проверяет наличие объявленных стратегий в базе и регистрирует символы.
        Поддерживает мультистратегичность и торговлю одинаковыми монетами под разными куполами/аккаунтами.
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
            is_enabled = bool(strat_settings["enabled"])
            if not is_enabled:
                continue

            # Извлекаем чистое имя стратегии и определяем целевой режим
            clean_id = strat_id
            suffix_mode = None
            for sfx in ("__off", "__classic", "__fixed", "__toxic"):
                if clean_id.endswith(sfx):
                    suffix_mode = sfx[2:]
                    clean_id = clean_id[:-len(sfx)]
                    break

            strat_modes = strat_settings.get("modes") or ([suffix_mode] if suffix_mode else None) or getattr(self, "modes", None) or ["fixed"]
            target_mode = (strat_modes[0] if isinstance(strat_modes, list) and strat_modes else str(strat_modes)).lower().strip()

            variant_key = f"{clean_id}__{target_mode}"
            if variant_key in self.universes_db:
                raw_meta = copy.deepcopy(self.universes_db[variant_key])
            elif strat_id in self.universes_db:
                raw_meta = copy.deepcopy(self.universes_db[strat_id])
            elif clean_id in self.universes_db:
                raw_meta = copy.deepcopy(self.universes_db[clean_id])
                ac_block = self._build_mode_auto_closing(target_mode)
                raw_meta["auto_closing"] = ac_block
                raw_meta["auto_closing_mode"] = target_mode
                if "overrides" not in raw_meta or not isinstance(raw_meta["overrides"], dict):
                    raw_meta["overrides"] = {}
                raw_meta["overrides"]["auto_closing"] = ac_block
            else:
                raise KeyError(
                    f"[StrategyRegistry] Стратегия '{strat_id}' (или '{clean_id}') "
                    f"отсутствует в базе шаблонов CFG/universes/*.json!"
                )

            # Прямое чтение списка монет (Правило 2)
            raw_symbols: List[str] = strat_settings["symbols"]
            strat_symbols = [s.upper().strip() for s in raw_symbols if s and s.strip()]

            strat_config = StrategyConfig(
                strategy_id=strat_id,
                raw_meta=raw_meta,
                symbols=strat_symbols,
                active_settings=strat_settings
            )
            strat_config.auto_closing_mode = target_mode
            self.active_strategies[strat_id] = strat_config

            # Регистрируем алиасы для взаимной однозначности в TG/утилитах
            if strat_id != variant_key and variant_key not in self.active_strategies:
                self.active_strategies[variant_key] = strat_config
            if strat_id != clean_id and clean_id not in self.active_strategies:
                self.active_strategies[clean_id] = strat_config

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
            f"[StrategyRegistry] Loaded {len(self.active_strategies)} active strategies (including mode aliases). "
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

    def reload(self, active_strategies_cfg: Optional[Dict[str, Any]] = None) -> None:
        """Перезагружает базу стратегий и пересобирает активные маппинги символов."""
        self.universes_db = self._load_universes_db()
        if active_strategies_cfg is not None:
            self.active_cfg = active_strategies_cfg
        else:
            app_data = Utils.read_json_file(DATA_DIR / "app.json")
            self.active_cfg = app_data.get("active_strategies", {})
        self._resolve_and_validate()
