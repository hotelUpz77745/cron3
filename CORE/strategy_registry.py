# ============================================================
# FILE: CORE/strategy_registry.py
# ROLE: Strategy template registry and symbol validation
# ============================================================

import copy
import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Set, Tuple

from consts import DATA_DIR, _CFG
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("StrategyRegistry")


from CORE.STRATEGY.strategy_config import StrategyConfig, deep_merge_dict

__all__ = ["StrategyConfig", "StrategyRegistry", "deep_merge_dict"]


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
        from CORE.STRATEGY.auto_closing_builder import AutoClosingConfigBuilder
        return AutoClosingConfigBuilder.build_mode_auto_closing(mode, base_ac)

    def _load_universes_db(self) -> Dict[str, Dict[str, Any]]:
        """Загружает базу спецификаций стратегий из папки CFG/universes/*.json."""
        data: Dict[str, Dict[str, Any]] = {}
        self.common_defaults: Dict[str, Any] = {}
        universes_dir = self.universes_path.parent / "universes"
        if universes_dir.exists() and universes_dir.is_dir():
            for jf in sorted(universes_dir.glob("*.json")):
                if jf.stem.startswith("_"):
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

            # Слияние метаданных: universes/<template>.json -> strat_settings (override)
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
