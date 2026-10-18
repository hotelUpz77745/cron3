# ==============================================================================
# Path: CORE/STRATEGY/canonical_strategy_processor.py
# Role: Единый канонический процессор стратегий и шаблонов HRON_3
#       Единый источник истины (Single Source of Truth) для:
#       1. Боевого бота (cron3)
#       2. Бумажного полигона (cron3PapperUniverses)
#       3. Бэктест-движка (cron3TradeBacktest)
# ==============================================================================

import copy
import json
import re
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Union


class CanonicalStrategyProcessor:
    """
    Канонический процессор стратегий и вселенных.
    Гарантирует 100% совпадение сигнатур, логики расчетов, параметров сеток
    и правил рециркуляции во всех подсистемах экосистемы HRON_3.
    """

    CRON3_DIR = Path(__file__).resolve().parent.parent.parent
    ROOT_DIR = CRON3_DIR.parent

    # -------------------------------------------------------------------------
    # 0. СЛИЯНИЕ СЛОВАРЕЙ (DEEP MERGE)
    # -------------------------------------------------------------------------
    @classmethod
    def deep_merge(cls, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Рекурсивно объединяет два словаря, переопределяя значения из override."""
        result = copy.deepcopy(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = cls.deep_merge(result[k], v)
            else:
                result[k] = copy.deepcopy(v)
        return result

    # -------------------------------------------------------------------------
    # 1. РАЗРЕШЕНИЕ КОНФИГУРАЦИИ СТРАТЕГИЙ
    # -------------------------------------------------------------------------
    @classmethod
    def find_universe_file(cls, universe_id: str) -> Path:
        """Находит файл вселенной в стандартных директориях экосистемы."""
        clean = universe_id.replace(".json", "").strip()

        # Очистка от суффиксов рантайм-режимов
        for sfx in (
            "__classic__static", "__classic__dyn_bb", "__toxic__static",
            "__classic", "__toxic", "__fixed", "__off", "__static"
        ):
            if clean.endswith(sfx):
                clean = clean[:-len(sfx)]
                break

        # Канонический базовый шаблон: _base.json
        if clean in ("_base", "base"):
            base_candidates = [
                cls.CRON3_DIR / "CFG" / "_base.json",
                cls.ROOT_DIR / "cron3PapperUniverses" / "CFG" / "_base.json",
                cls.CRON3_DIR / "CFG" / "universes" / "_base.json",
            ]
            for p in base_candidates:
                if p.exists():
                    return p

        candidates = [
            cls.ROOT_DIR / "cron3" / "CFG" / "universes" / f"{clean}.json",
            cls.CRON3_DIR / "CFG" / "universes" / f"{clean}.json",
            cls.ROOT_DIR / "cron3PapperUniverses" / "CFG" / "universes" / f"{clean}.json",
            cls.ROOT_DIR / "cron3PapperUniverses" / "CFG" / f"{clean}.json",
            cls.ROOT_DIR / "cron3" / "CFG" / f"{clean}.json",
            cls.ROOT_DIR / "cron3" / "CFG" / "artifacts" / "classic_tribute" / f"{clean}.json",
            cls.ROOT_DIR / "cron3PapperUniverses" / "CFG" / "artifacts" / "classic_tribute" / f"{clean}.json",
        ]

        for p in candidates:
            if p.exists():
                return p

        raise FileNotFoundError(
            f"[CanonicalStrategyProcessor] Universe file not found for: '{universe_id}'. "
            f"Searched paths: {[str(c) for c in candidates]}"
        )

    @classmethod
    def get_base_template(cls) -> Dict[str, Any]:
        """Возвращает канонический базовый шаблон _base.json."""
        fpath = cls.find_universe_file("_base")
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)

    @classmethod
    def resolve_universe(cls, universe_spec: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        """
        Канонический нормализатор: принимает имя стратегии (или сырой словарь)
        и возвращает эталонную структуру данных со 100% стандартизированными ключами.
        Гарантирует синхронизацию боевого, бумажного и бэктест окружений.
        """
        is_base_query = False
        if isinstance(universe_spec, str):
            clean = universe_spec.replace(".json", "").strip()
            if clean in ("_base", "base"):
                is_base_query = True
            fpath = cls.find_universe_file(universe_spec)
            with open(fpath, "r", encoding="utf-8") as f:
                raw_cfg = json.load(f)
        else:
            raw_cfg = copy.deepcopy(universe_spec)
            if raw_cfg.get("name") in ("_base", "base"):
                is_base_query = True

        # Если запрашивается не сам базовый шаблон и явно указано наследование — наследуем базовый шаблон
        if not is_base_query:
            base_ref = raw_cfg.get("base_template") or raw_cfg.get("template")
            if base_ref:
                try:
                    base_path = cls.find_universe_file(base_ref)
                    with open(base_path, "r", encoding="utf-8") as bf:
                        base_data = json.load(bf)
                    meta = cls.deep_merge(base_data, raw_cfg)
                except Exception:
                    meta = copy.deepcopy(raw_cfg)
            else:
                meta = copy.deepcopy(raw_cfg)
        else:
            meta = copy.deepcopy(raw_cfg)

        overrides = meta.get("overrides", {})

        # Полярные ветви LONG и SHORT
        long_cfg = meta.get("LONG") or overrides.get("LONG") or {}
        short_cfg = meta.get("SHORT") or overrides.get("SHORT") or {}

        # Сетки уровней (приоритет сырого конфига без загрязнения шаблоном)
        grid_long = long_cfg.get("grid") or meta.get("grid") or overrides.get("grid") or {}
        grid_short = short_cfg.get("grid") or meta.get("grid") or overrides.get("grid") or grid_long

        # Карты Take-Profit
        tp_map_long = long_cfg.get("tp_map") or meta.get("tp_map") or overrides.get("tp_map") or {}
        tp_map_short = short_cfg.get("tp_map") or meta.get("tp_map") or overrides.get("tp_map") or tp_map_long

        # Рециркуляция прибыли (Cross-Side Profit Recycling)
        recycling_cfg = (
            meta.get("cross_side_recycling")
            or overrides.get("cross_side_recycling")
        )

        # Режим динамического кулака (Nadaraya-Watson / Dynamic Fist)
        fist_regime = (
            meta.get("first_order_weight_regime")
            or overrides.get("first_order_weight_regime")
        )

        # Сайзинг и плечо (Строгий Протокол №2: без скрытых дефолтов)
        raw_inv = long_cfg.get("invest_size") if long_cfg.get("invest_size") is not None else meta.get("invest_size")
        if raw_inv is None:
            raise KeyError(f"[CanonicalStrategyProcessor] Отсутствует обязательный параметр 'invest_size' в стратегии: {meta.get('name')}")
        invest_size = float(raw_inv)

        raw_lev = meta.get("leverage")
        if raw_lev is None:
            raise KeyError(f"[CanonicalStrategyProcessor] Отсутствует обязательный параметр 'leverage' в стратегии: {meta.get('name')}")
        leverage = int(raw_lev)

        base_tpl = cls.get_base_template() if not is_base_query else {}

        raw_min_tp = meta.get("min_tp_indent") or long_cfg.get("min_tp_indent") or overrides.get("min_tp_indent") or base_tpl.get("min_tp_indent")
        if raw_min_tp is None:
            raw_min_tp = 0.003
        min_tp_indent = float(raw_min_tp)

        avg_behavior = meta.get("avg_free_volume_behavior") or overrides.get("avg_free_volume_behavior") or base_tpl.get("avg_free_volume_behavior")
        if not avg_behavior:
            avg_behavior = "mark_executed"

        tp_purpose = meta.get("tp_purpose") or long_cfg.get("tp_purpose") or overrides.get("tp_purpose") or base_tpl.get("tp_purpose")
        if not tp_purpose:
            tp_purpose = "self"

        base_order_type = meta.get("base_order_type", "LIMIT")
        fallback_order_type = meta.get("fallback_order_type", "MARKET")
        return {
            "name": meta.get("name", "CanonicalStrategy"),
            "invest_size": invest_size,
            "leverage": leverage,
            "min_tp_indent": min_tp_indent,
            "avg_free_volume_behavior": avg_behavior,
            "base_order_type": base_order_type,
            "fallback_order_type": fallback_order_type,
            "tp_purpose": tp_purpose,
            "grid_long": grid_long,
            "grid_short": grid_short,
            "tp_map_long": tp_map_long,
            "tp_map_short": tp_map_short,
            "cross_side_recycling": recycling_cfg,
            "first_order_weight_regime": fist_regime,
            "trend_size_depender": meta.get("trend_size_depender") or overrides.get("trend_size_depender"),
            "auto_closing": meta.get("auto_closing") or overrides.get("auto_closing"),
            "smart_size": meta.get("smart_size") or overrides.get("smart_size"),
            "smart_tp": meta.get("smart_tp") or overrides.get("smart_tp"),
            "smart_grace": meta.get("smart_grace") or overrides.get("smart_grace"),
            "raw_meta": meta
        }

    # -------------------------------------------------------------------------
    # 2. КАНОНИЧЕСКИЙ РЕСАЙКЛИНГ (CROSS-SIDE RECYCLING)
    # -------------------------------------------------------------------------
    @staticmethod
    def resolve_recycle_ratio(recycling_cfg: Optional[Dict[str, Any]], step: int) -> float:
        """
        Дискретно определяет долю рециркуляции прибыли (burn_ratio) по номеру колена step.
        Поддерживает все форматы:
          - "2-4": 0.50
          - "2..4": 0.50
          - ">=5": 0.75
          - "5+": 0.75
          - прямые номера: "2": 0.50
          - вложенные словари: {"burn_ratio": 0.50}
          - fallback 'recycle_ratio'
        """
        if not recycling_cfg or not recycling_cfg.get("enabled", True):
            return 0.0

        fallback = float(recycling_cfg["recycle_ratio"]) if ("recycle_ratio" in recycling_cfg and recycling_cfg["recycle_ratio"] is not None) else None
        tiers = (
            recycling_cfg.get("recycle_ratio_tiers")
            or recycling_cfg.get("recycle_ratio_map")
            or recycling_cfg.get("step_map")
        )
        if not tiers or not isinstance(tiers, dict):
            return fallback if fallback is not None else 0.50

        step_str = str(step)
        if step_str in tiers:
            v = tiers[step_str]
            return float(v.get("burn_ratio", v)) if isinstance(v, dict) else float(v)

        for pattern, val in tiers.items():
            pat = str(pattern).strip()
            v = float(val.get("burn_ratio", val)) if isinstance(val, dict) else float(val)

            if "-" in pat or ".." in pat:
                delim = ".." if ".." in pat else "-"
                parts = pat.split(delim)
                if len(parts) == 2:
                    try:
                        low, high = int(parts[0].strip()), int(parts[1].strip())
                        if low <= step <= high:
                            return v
                    except ValueError:
                        pass
            elif pat.endswith("+"):
                try:
                    thresh = int(pat[:-1].strip())
                    if step >= thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith(">="):
                try:
                    thresh = int(pat[2:].strip())
                    if step >= thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith(">"):
                try:
                    thresh = int(pat[1:].strip())
                    if step > thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith("<="):
                try:
                    thresh = int(pat[2:].strip())
                    if step <= thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith("<"):
                try:
                    thresh = int(pat[1:].strip())
                    if step < thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith("=="):
                try:
                    thresh = int(pat[2:].strip())
                    if step == thresh:
                        return v
                except ValueError:
                    pass

        return fallback if fallback is not None else 0.0

    @staticmethod
    def resolve_min_burn_level(recycling_cfg: Optional[Dict[str, Any]]) -> int:
        """Определяет минимальный уровень сетки для начала сжигания объема."""
        if not recycling_cfg or not recycling_cfg.get("enabled", True):
            return 999
        if "min_burn_level" in recycling_cfg and recycling_cfg["min_burn_level"] is not None:
            return int(recycling_cfg["min_burn_level"])

        tiers = recycling_cfg.get("recycle_ratio_tiers") or recycling_cfg.get("recycle_ratio_map")
        if tiers and isinstance(tiers, dict):
            levels = []
            for pat in tiers.keys():
                p = str(pat).strip()
                if "-" in p or ".." in p:
                    delim = ".." if ".." in p else "-"
                    parts = p.split(delim)
                    try:
                        levels.append(int(parts[0].strip()))
                    except ValueError:
                        pass
                elif p.endswith("+"):
                    try:
                        levels.append(int(p[:-1].strip()))
                    except ValueError:
                        pass
                elif p.startswith(">="):
                    try:
                        levels.append(int(p[2:].strip()))
                    except ValueError:
                        pass
                elif p.startswith(">"):
                    try:
                        levels.append(int(p[1:].strip()) + 1)
                    except ValueError:
                        pass
                elif p.isdigit():
                    levels.append(int(p))
            if levels:
                return min(levels)

        return 2

    @classmethod
    def calculate_burn_slice(
        cls,
        net_profit: float,
        opp_side: str,
        opp_avg_price: float,
        opp_vol_qty: float,
        opp_cur_step: int,
        current_price: float,
        recycling_cfg: Optional[Dict[str, Any]],
        taker_fee: float = 0.0005
    ) -> Tuple[float, float, float]:
        """
        Канонический математический расчет объема и затрат на срезку:
        Возвращает: (burned_qty, spent_profit, burned_notional)
        """
        if not recycling_cfg or not recycling_cfg.get("enabled", True) or net_profit <= 0:
            return 0.0, 0.0, 0.0

        min_lvl = cls.resolve_min_burn_level(recycling_cfg)
        if opp_cur_step < min_lvl or opp_vol_qty <= 0.0:
            return 0.0, 0.0, 0.0

        burn_ratio = cls.resolve_recycle_ratio(recycling_cfg, opp_cur_step)
        if burn_ratio <= 0.0:
            return 0.0, 0.0, 0.0

        loss_per_unit = (opp_avg_price - current_price) if opp_side == "LONG" else (current_price - opp_avg_price)
        if loss_per_unit <= 0.0:
            return 0.0, 0.0, 0.0

        min_dd = float(recycling_cfg.get("min_drawdown_pct", 3.0))
        dd_pct = (loss_per_unit / opp_avg_price) * 100.0 if opp_avg_price > 0 else 0.0
        if dd_pct < min_dd:
            return 0.0, 0.0, 0.0

        loss_with_fee = loss_per_unit + (current_price * taker_fee)
        profit_budget = net_profit * burn_ratio
        max_burn_qty = profit_budget / loss_with_fee

        max_slice_pct = float(recycling_cfg.get("max_slice_pct", 35.0)) / 100.0
        allowed_slice_qty = opp_vol_qty * max_slice_pct

        burn_qty = min(max_burn_qty, allowed_slice_qty, opp_vol_qty)
        if burn_qty <= 0.0:
            return 0.0, 0.0, 0.0

        spent = burn_qty * loss_with_fee
        burned_notional = burn_qty * current_price

        return burn_qty, spent, burned_notional

    # -------------------------------------------------------------------------
    # 3. КАНОНИЧЕСКИЙ АВТО-КЛОУЗЕР (AUTO-CLOSING RESOLUTION)
    # -------------------------------------------------------------------------
    @classmethod
    def resolve_auto_closing(cls, universe_spec: Union[str, Dict[str, Any]], mode_suffix: str = "classic") -> Dict[str, Any]:
        """
        Канонически формирует конфигурацию auto_closing строго для целевого режима
        из переданного словаря или ID стратегии.
        """
        canonical = cls.resolve_universe(universe_spec)
        raw_ac = canonical.get("auto_closing") or {}
        pos = raw_ac.get("positive", {})

        cfg: Dict[str, Any] = {
            "mode": mode_suffix,
            "quant_base_usdt": float(pos.get("quant_base_usdt", 10.0)),
            "quant_reference_size": float(pos.get("quant_reference_size", 600.0)),
            "negative": raw_ac.get("negative", {})
        }

        if mode_suffix == "off":
            cfg["enabled"] = False
        elif mode_suffix == "classic":
            cfg["enabled"] = True
            cfg["trailing_ratchet"] = pos.get("trailing_ratchet", {})
        elif mode_suffix == "toxic":
            cfg["enabled"] = True
            cfg["toxicity_adaptive"] = pos.get("toxicity_adaptive", {})
        elif mode_suffix == "fixed":
            cfg["enabled"] = True
            cfg["fixed_net"] = pos.get("fixed_net", {})
        else:
            cfg["enabled"] = True
            cfg["trailing_ratchet"] = pos.get("trailing_ratchet", {})

        return cfg
