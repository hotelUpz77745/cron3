# ==============================================================================
# Path: CORE/SERVICES/coin_grid_analytics/merge_service.py
# Role: Сервис меж-декадного слияния (Cross-Decade Accumulation & Merge)
#       сеточной аналитики из экземпляров или сериализованных отчетов.
# ==============================================================================

import copy
from typing import Any
from .grid_schema import create_empty_coin_record


def merge_analytics(target: Any, other: Any) -> Any:
    """
    Сливает статистику из другого экземпляра CoinGridRiskAnalytics или сериализованного словаря.
    Позволяет сквозное накопление данных по всем декадам торгового марафона.
    """
    if isinstance(other, dict):
        # 1. Слияние из словаря
        for lvl_key, ldata in other.get("grid_level_distribution", {}).items():
            target.level_fills_count[lvl_key] += ldata.get("fills_count", 0)
            target.level_fills_notional[lvl_key] += ldata.get("total_notional_usdt", 0.0)
            target.cycle_depth_counts[lvl_key] += ldata.get("cycles_max_depth_count", 0)

        for sym, cdata in other.get("per_coin_summary", {}).items():
            if sym not in target.per_coin_stats:
                target.per_coin_stats[sym] = create_empty_coin_record(sym)
            s = target.per_coin_stats[sym]
            s["total_cycles"] += cdata.get("total_cycles", 0)
            s["winning_cycles"] += cdata.get("winning_cycles", 0)
            s["losing_cycles"] += cdata.get("losing_cycles", 0)
            s["realized_pnl"] += cdata.get("realized_pnl", 0.0)
            s["fees_paid"] += cdata.get("fees_paid", 0.0)
            s["net_pnl"] += cdata.get("net_pnl", 0.0)
            s["max_level_ever"] = max(s["max_level_ever"], cdata.get("max_level_ever", 0))
            s["level_depths_sum"] += int(cdata.get("avg_cycle_level", 0.0) * cdata.get("total_cycles", 0))
            s["total_duration_hours"] += float(cdata.get("avg_duration_hours", 0.0) * cdata.get("total_cycles", 0))
            s["max_adverse_from_entry0_pct"] = max(s["max_adverse_from_entry0_pct"], cdata.get("max_adverse_from_entry0_pct", 0.0))
            s["max_adverse_from_avg_pct"] = max(s["max_adverse_from_avg_pct"], cdata.get("max_adverse_from_avg_pct", 0.0))
            s["max_floating_dd_usdt"] = max(s["max_floating_dd_usdt"], cdata.get("max_floating_dd_usdt", 0.0))
            s["exceeded_l8_count"] += cdata.get("exceeded_l8_count", 0)
            s["would_hit_sl_120_count"] += cdata.get("would_hit_sl_120_count", cdata.get("would_hit_sl_100_count", 0))
            s["would_hit_sl_130_count"] += cdata.get("would_hit_sl_130_count", 0)

        for ev in other.get("worst_drawdown_events", []):
            target.completed_cycles.append(ev)

    elif hasattr(other, "level_fills_count") and hasattr(other, "per_coin_stats"):
        # 2. Слияние из экземпляра CoinGridRiskAnalytics
        for lvl_key, cnt in other.level_fills_count.items():
            target.level_fills_count[lvl_key] += cnt
            target.level_fills_notional[lvl_key] += other.level_fills_notional.get(lvl_key, 0.0)

        for lvl_key, cnt in other.cycle_depth_counts.items():
            target.cycle_depth_counts[lvl_key] += cnt

        for c in other.completed_cycles:
            target.completed_cycles.append(c)

        for sym, s_other in other.per_coin_stats.items():
            if sym not in target.per_coin_stats:
                target.per_coin_stats[sym] = copy.deepcopy(s_other)
            else:
                s = target.per_coin_stats[sym]
                s["total_cycles"] += s_other["total_cycles"]
                s["winning_cycles"] += s_other["winning_cycles"]
                s["losing_cycles"] += s_other["losing_cycles"]
                s["realized_pnl"] += s_other["realized_pnl"]
                s["fees_paid"] += s_other["fees_paid"]
                s["net_pnl"] += s_other["net_pnl"]
                s["max_level_ever"] = max(s["max_level_ever"], s_other["max_level_ever"])
                s["level_depths_sum"] += s_other["level_depths_sum"]
                s["total_duration_hours"] += s_other["total_duration_hours"]
                s["max_adverse_from_entry0_pct"] = max(s["max_adverse_from_entry0_pct"], s_other["max_adverse_from_entry0_pct"])
                s["max_adverse_from_avg_pct"] = max(s["max_adverse_from_avg_pct"], s_other["max_adverse_from_avg_pct"])
                s["max_floating_dd_usdt"] = max(s["max_floating_dd_usdt"], s_other["max_floating_dd_usdt"])
                s["exceeded_l8_count"] += s_other["exceeded_l8_count"]
                s["would_hit_sl_120_count"] += s_other.get("would_hit_sl_120_count", s_other.get("would_hit_sl_100_count", 0))
                s["would_hit_sl_130_count"] += s_other.get("would_hit_sl_130_count", 0)

    return target

