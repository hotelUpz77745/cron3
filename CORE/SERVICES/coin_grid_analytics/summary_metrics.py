# ==============================================================================
# Path: CORE/SERVICES/coin_grid_analytics/summary_metrics.py
# Role: Построение структурированного агрегированного отчета и расчет метрик
#       выживаемости воронки, средних глубин и рисков просадки.
# ==============================================================================

from collections import defaultdict
from typing import Dict, Any, List
from .grid_schema import RiskThresholds


def build_summary(tracker: Any) -> Dict[str, Any]:
    """
    Возвращает агрегированный статистический отчет в структурированном виде
    по всем завершенным циклам, уровням сетки и инструментам.
    """
    total_from_coins = sum(s.get("total_cycles", 0) for s in tracker.per_coin_stats.values())
    total_cycles = total_from_coins if total_from_coins > 0 else len(tracker.completed_cycles)
    total_fills = sum(tracker.level_fills_count.values())

    # 1. Распределение уровней сетки (Fills & Depth) по каноническим ступеням L0..L8
    canonical_lvl_keys = [f"L{i}" for i in range(RiskThresholds.CANONICAL_GRID_LEVELS)]
    level_dist: Dict[str, Dict[str, Any]] = {}

    # Воронка выживания: сколько циклов дошли хотя бы до уровня i
    cycles_reached_at_least: Dict[str, int] = defaultdict(int)
    for c in tracker.completed_cycles:
        max_lvl = int(c.get("max_level_reached", 0))
        for i in range(max_lvl + 1):
            cycles_reached_at_least[f"L{i}"] += 1

    for lvl_key in canonical_lvl_keys:
        fills = tracker.level_fills_count.get(lvl_key, 0)
        fills_pct = (fills / total_fills * 100.0) if total_fills > 0 else 0.0
        notional = tracker.level_fills_notional.get(lvl_key, 0.0)
        avg_notional = (notional / fills) if fills > 0 else 0.0

        depth_cnt = tracker.cycle_depth_counts.get(lvl_key, 0)
        depth_pct = (depth_cnt / total_cycles * 100.0) if total_cycles > 0 else 0.0
        survival_cnt = cycles_reached_at_least.get(lvl_key, 0)
        survival_pct = (survival_cnt / total_cycles * 100.0) if total_cycles > 0 else 0.0

        meta = tracker.level_metadata.get(lvl_key, {})
        level_dist[lvl_key] = {
            "level": lvl_key,
            "role": meta.get("role", lvl_key),
            "indent_pct": meta.get("indent_pct", 0.0),
            "volume_pct": meta.get("volume_pct", 0.0),
            "fills_count": fills,
            "fills_share_pct": round(fills_pct, 2),
            "total_notional_usdt": round(notional, 2),
            "avg_notional_usdt": round(avg_notional, 2),
            "cycles_max_depth_count": depth_cnt,
            "cycles_max_depth_share_pct": round(depth_pct, 2),
            "survival_funnel_count": survival_cnt,
            "survival_funnel_pct": round(survival_pct, 2),
        }

    # 2. Средняя глубина цикла
    avg_cycle_depth = 0.0
    if total_cycles > 0:
        tot_levels_sum = sum(s.get("level_depths_sum", 0) for s in tracker.per_coin_stats.values())
        if tot_levels_sum > 0:
            avg_cycle_depth = tot_levels_sum / total_cycles
        elif tracker.completed_cycles:
            avg_cycle_depth = sum(c.get("max_level_reached", 0) for c in tracker.completed_cycles) / len(tracker.completed_cycles)

    # 3. Per-Coin Ledger
    coins_summary: Dict[str, Dict[str, Any]] = {}
    for sym, s in tracker.per_coin_stats.items():
        tot = s["total_cycles"]
        win_r = (s["winning_cycles"] / tot * 100.0) if tot > 0 else 0.0
        avg_lvl = (s["level_depths_sum"] / tot) if tot > 0 else 0.0
        avg_dur = (s["total_duration_hours"] / tot) if tot > 0 else 0.0

        coins_summary[sym] = {
            "symbol": sym,
            "total_cycles": tot,
            "winning_cycles": s["winning_cycles"],
            "losing_cycles": s["losing_cycles"],
            "win_rate_pct": round(win_r, 1),
            "realized_pnl": round(s["realized_pnl"], 2),
            "fees_paid": round(s["fees_paid"], 2),
            "net_pnl": round(s["net_pnl"], 2),
            "max_level_ever": s["max_level_ever"],
            "avg_cycle_level": round(avg_lvl, 2),
            "max_adverse_from_entry0_pct": round(s["max_adverse_from_entry0_pct"], 2),
            "max_adverse_from_avg_pct": round(s["max_adverse_from_avg_pct"], 2),
            "max_floating_dd_usdt": round(s["max_floating_dd_usdt"], 2),
            "avg_duration_hours": round(avg_dur, 2),
            "exceeded_l8_count": s["exceeded_l8_count"],
            "would_hit_sl_120_count": s.get("would_hit_sl_120_count", s.get("would_hit_sl_100_count", 0)),
            "would_hit_sl_130_count": s.get("would_hit_sl_130_count", 0),
        }

    # 4. Топ худших просадок (Worst 15 Adverse Excursions)
    sorted_worst = sorted(
        tracker.completed_cycles,
        key=lambda x: (x.get("max_adverse_from_entry0_pct", 0.0), x.get("max_floating_dd_usdt", 0.0)),
        reverse=True
    )[:RiskThresholds.TOP_WORST_EVENTS_LIMIT]

    # 5. Глобальные счетчики L8, SL-120% и SL-130%
    tot_l8_exceeded = sum(s.get("exceeded_l8_count", 0) for s in coins_summary.values())
    tot_sl_120 = sum(s.get("would_hit_sl_120_count", 0) for s in coins_summary.values())
    tot_sl_130 = sum(s.get("would_hit_sl_130_count", 0) for s in coins_summary.values())

    return {
        "strategy_id": tracker.strategy_id,
        "total_cycles": total_cycles,
        "total_fills": total_fills,
        "average_cycle_level": round(avg_cycle_depth, 2),
        "total_exceeded_l8": tot_l8_exceeded,
        "total_would_hit_sl_120": tot_sl_120,
        "total_would_hit_sl_130": tot_sl_130,
        "grid_level_distribution": level_dist,
        "per_coin_summary": coins_summary,
        "worst_drawdown_events": sorted_worst,
    }
