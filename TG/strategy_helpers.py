# ==============================================================================
# Path: TG/strategy_helpers.py
# Role: Сервисный класс метаданных и конфигураций куполов стратегий (OOP Helper)
# ==============================================================================

from typing import Any, Dict, List, Optional
from pathlib import Path

from consts import ANALYTICS_START_BALANCE_USDT, DATA_DIR
from c_utils import Utils


class StrategyMetadataHelper:
    """Сервис извлечения и нормализации метаданных стратегий куполов для интерфейса Telegram."""

    @staticmethod
    def extract_symbols(sdata: Any) -> List[str]:
        """Универсально извлекает монеты стратегии: из .symbols, ключа 'symbols' или invest_size."""
        return Utils.extract_strategy_symbols(sdata)

    @classmethod
    def get_strategy_meta(cls, strat_id: str, bot_core: Optional[Any] = None) -> Dict[str, Any]:
        """Возвращает нормализованные метаданные стратегии из реестра или конфигов."""
        if bot_core and hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
            strat = bot_core.strategy_registry.get_strategy(strat_id)
            if strat and isinstance(getattr(strat, "name", None), str):
                def_inv = float(getattr(strat, "default_invest_size", 0.0) or 0.0)
                inv_size = def_inv if isinstance(strat.invest_size, dict) else float(strat.invest_size)
                return {
                    "strategy_id": strat_id,
                    "name": strat.name,
                    "description": strat.description or "Пользовательская стратегия",
                    "invest_size": inv_size,
                    "default_invest_size": def_inv,
                    "invest_size_map": strat.invest_size if isinstance(strat.invest_size, dict) else None,
                    "leverage": strat.leverage,
                    "super_grid": getattr(strat, "super_grid_enabled", False),
                    "super_grid_cfg": getattr(strat, "super_grid_cfg", {}),
                    "tp_summary": "Настроен",
                    "start_balance_usdt": getattr(strat, "start_balance_usdt", ANALYTICS_START_BALANCE_USDT),
                }

        univ_dir = DATA_DIR / "universes"
        app_data = Utils.read_json_file(DATA_DIR / "app.json") or {}
        active_strats = app_data.get("active_strategies", {})
        strat_app = active_strats.get(strat_id, {})

        pure_id = strat_app.get("template") or strat_id
        if not (univ_dir / f"{pure_id}.json").exists():
            if "__" in pure_id:
                pure_id = pure_id.split("__")[0]
            else:
                for sfx in ("__off", "__classic", "__fixed", "__toxic"):
                    if pure_id.endswith(sfx):
                        pure_id = pure_id[: -len(sfx)]
                        break

        jf = univ_dir / f"{pure_id}.json"
        if not jf.exists():
            art_jf = DATA_DIR / "artifacts" / "classic_tribute" / f"{pure_id}.json"
            if art_jf.exists():
                jf = art_jf

        if jf.exists():
            u_meta = Utils.read_json_file(jf) or {}
        else:
            univ_data = Utils.read_json_file(DATA_DIR / "universes.json") or {}
            u_meta = univ_data.get(strat_id, {})

        name = u_meta.get("name", strat_id)
        description = u_meta.get("description", "Пользовательская стратегия")
        def_inv = (
            (strat_app.get("LONG") or {}).get("default_invest_size")
            or (strat_app.get("SHORT") or {}).get("default_invest_size")
            or strat_app.get("default_invest_size")
            or u_meta.get("default_invest_size")
        )
        raw_inv = (
            (strat_app.get("LONG") or {}).get("invest_size")
            or (strat_app.get("SHORT") or {}).get("invest_size")
            or strat_app.get("invest_size")
            or (u_meta.get("LONG") or {}).get("invest_size")
            or (u_meta.get("SHORT") or {}).get("invest_size")
            or u_meta.get("invest_size")
        )
        if def_inv is not None:
            final_def = float(def_inv)
        elif isinstance(raw_inv, dict) and raw_inv:
            final_def = float(next(iter(raw_inv.values())))
        elif isinstance(raw_inv, (int, float)):
            final_def = float(raw_inv)
        else:
            final_def = 0.0
        invest_size = final_def if isinstance(raw_inv, dict) else float(raw_inv or final_def)
        leverage = strat_app.get("leverage") or u_meta.get("leverage", 20)
        start_bal = float(
            strat_app.get("start_balance_usdt")
            or (strat_app.get("analytics") or {}).get("start_balance_usdt")
            or u_meta.get("start_balance_usdt")
            or ANALYTICS_START_BALANCE_USDT
        )

        sg_cfg = strat_app.get("super_grid") or u_meta.get("super_grid") or u_meta.get("overrides", {}).get("super_grid", {})
        sg_enabled = sg_cfg.get("enabled", u_meta.get("super_grid_enabled", False))

        tp_map = (
            (u_meta.get("LONG") or {}).get("tp_map")
            or (u_meta.get("SHORT") or {}).get("tp_map")
            or u_meta.get("tp_map")
            or u_meta.get("overrides", {}).get("tp_map", {})
        )
        if tp_map and isinstance(tp_map, dict):
            indents = [str(v.get("indent", "?")) for v in tp_map.values() if isinstance(v, dict)]
            if len(set(indents)) == 1:
                tp_summary = f"Плоский {indents[0]}% (все уровни)"
            else:
                tp_summary = f"{', '.join(indents[:3])}%..."
        else:
            tp_summary = "Базовый 0.6%..2.5%"

        return {
            "strategy_id": strat_id,
            "name": name,
            "description": description,
            "invest_size": invest_size,
            "default_invest_size": final_def,
            "invest_size_map": raw_inv if isinstance(raw_inv, dict) else None,
            "leverage": leverage,
            "super_grid": sg_enabled,
            "super_grid_cfg": sg_cfg,
            "tp_summary": tp_summary,
            "start_balance_usdt": start_bal,
        }


# Делегаты для обратной совместимости
extract_strategy_symbols = StrategyMetadataHelper.extract_symbols
get_strategy_meta = StrategyMetadataHelper.get_strategy_meta
