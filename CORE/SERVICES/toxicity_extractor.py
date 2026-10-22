# ============================================================
# FILE: CORE/SERVICES/toxicity_extractor.py
# ROLE: Extracts inventory, margin, and drawdown metrics for Toxicity Engine
# ============================================================

from typing import Any, Tuple
from CORE.STRATEGY.trailing_system import ToxicityEngine


class ToxicityMetricsExtractor:
    """
    Извлекает компоненты инвентаря для расчета Toxicity Score:
      - margin_load: текущая загрузка маржи (0.0..1.0)
      - depth_ratio: максимальное погружение сетки (0.0..1.0)
      - float_ratio: отношение висящего минуса к накопленному профиту
      - delta_recovery: степень рассасывания ямы просадки (0.0..1.0)
    """

    @staticmethod
    def extract(strat_cfg: Any, analytics_dict: dict, bot_core: Any) -> Tuple[float, float, float, float]:
        cur_floating = float(analytics_dict.get("unrealized_pnl_usdt", 0.0))
        max_dd = float(analytics_dict.get("max_drawdown_usdt", cur_floating))
        realized = float(analytics_dict.get("iteration_realized_pnl_usdt", analytics_dict.get("realized_pnl_usdt", 0.0)))
        deposit = float(getattr(strat_cfg, "start_balance_usdt", 4000.0))

        # Margin Load
        total_margin = 0.0
        per_coin = analytics_dict.get("per_coin", {})
        if isinstance(per_coin, dict) and per_coin:
            for c_data in per_coin.values():
                if isinstance(c_data, dict):
                    total_margin += float(c_data.get("current_margin_usdt", 0.0))
        margin_load = (total_margin / deposit) if deposit > 0 else 0.0

        # Max Depth Ratio
        max_step = 0
        fsm_mgr = getattr(bot_core, "runtime_manager", None)
        fsm_states = getattr(fsm_mgr, "fsm_states", None) if fsm_mgr else getattr(bot_core, "position_states", None)
        if isinstance(fsm_states, dict) and fsm_states:
            for sym_sides in fsm_states.values():
                if isinstance(sym_sides, dict):
                    for st in sym_sides.values():
                        step = getattr(st, "active_step", 0)
                        if isinstance(step, int) and step > max_step:
                            max_step = step
                elif hasattr(sym_sides, "active_step"):
                    step = getattr(sym_sides, "active_step", 0)
                    if isinstance(step, int) and step > max_step:
                        max_step = step
        elif isinstance(per_coin, dict) and per_coin:
            for c_data in per_coin.values():
                if isinstance(c_data, dict):
                    g_load = float(c_data.get("current_grid_load_pct", 0.0))
                    approx_step = int(g_load / 12.5)
                    if approx_step > max_step:
                        max_step = approx_step
        depth_ratio = min(1.0, max_step / 8.0)

        # Float Ratio
        float_ratio = abs(cur_floating) / (abs(cur_floating) + max(0.0, realized) + 1.0)

        # Delta Recovery
        delta_recovery = ToxicityEngine.calculate_delta_recovery(cur_floating, max_dd)

        return margin_load, depth_ratio, float_ratio, delta_recovery
