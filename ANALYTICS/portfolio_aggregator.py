# ==============================================================================
# Path: ANALYTICS/portfolio_aggregator.py
# Role: Агрегатор показателей аналитики портфеля по всем куполам и монетам
# ==============================================================================

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from consts import (
    ANALYTICS_STATS_DIR,
    ANALYTICS_LEDGERS_DIR,
    ANALYTICS_START_BALANCE_USDT,
    _CFG,
)
from c_log import UnifiedLogger

logger = UnifiedLogger("PortfolioAggregator")


class PortfolioAggregator:
    """
    Агрегатор сводных глобальных данных и данных per_coin со всех активных куполов/стратегий.
    Служит единым источником истины (Single Source of Truth) для презентационных слоев.
    """

    def __init__(self, analytics_manager=None, stats_dir: Optional[Path] = None, ledgers_dir: Optional[Path] = None):
        self.analytics_manager = analytics_manager
        self.stats_dir: Path = stats_dir or ANALYTICS_STATS_DIR
        self.ledgers_dir: Path = ledgers_dir or ANALYTICS_LEDGERS_DIR

    def get_portfolio_all_data(self) -> Tuple[Dict[str, Any], Dict[str, dict], str]:
        """
        Агрегирует сводные глобальные данные и данные per_coin со всех активных куполов/стратегий.
        Возвращает: (agg_summary, agg_per_coin, last_trade_time_portfolio)
        """
        agg_summary: Dict[str, Any] = {
            "start_balance_usdt": 0.0,
            "cur_balance_usdt": 0.0,
            "peak_balance_usdt": 0.0,
            "min_balance_usdt": 0.0,
            "net_profit_usdt": 0.0,
            "realized_pnl_usdt": 0.0,
            "realized_pnl_net_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "total_commission_usdt": 0.0,
            "total_funding_usdt": 0.0,
            "max_drawdown_usdt": 0.0,
            "performance_usdt": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_margin_used_usdt": 0.0,
            "free_margin_usdt": 0.0,
            "total_grid_size_usdt": 0.0,
            "current_grid_load_usdt": 0.0,
            "universes_count": 0,
        }
        agg_per_coin: Dict[str, dict] = {}
        last_trade_dt_str = "Нет сделок"

        active_uids: List[str] = []
        if self.analytics_manager and getattr(self.analytics_manager, "strategy_registry", None):
            reg = self.analytics_manager.strategy_registry
            if hasattr(reg, "get_active_strategies"):
                active_uids = list(reg.get_active_strategies().keys())
            elif hasattr(reg, "active_strategies"):
                active_uids = list(reg.active_strategies.keys())

        if not active_uids and isinstance(_CFG, dict):
            active_uids = [
                k for k, v in _CFG.get("active_strategies", {}).items()
                if isinstance(v, dict) and v.get("enabled", True)
            ]

        files_to_read: List[Tuple[str, Path]] = []
        if active_uids:
            for uid in active_uids:
                p = self.stats_dir / f"analytics_{uid}.json"
                if p.exists():
                    files_to_read.append((uid, p))
                else:
                    matches = list(self.stats_dir.glob(f"analytics_{uid}*.json"))
                    if matches:
                        files_to_read.append((uid, sorted(matches)[0]))

        if not files_to_read:
            for p in self.stats_dir.glob("analytics_*.json"):
                if p.name in ("analytics_all.json", "analytics.json"):
                    continue
                uid = p.stem.replace("analytics_", "")
                files_to_read.append((uid, p))

        agg_summary["universes_count"] = len(files_to_read)

        for uid, p in files_to_read:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                # Разрешаем стартовый баланс из конфига стратегии или аналитики
                strat_cfg = _CFG.get("active_strategies", {}).get(uid, {}) if isinstance(_CFG, dict) else {}
                cfg_start = float(strat_cfg.get("start_balance_usdt", ANALYTICS_START_BALANCE_USDT)) if isinstance(strat_cfg, dict) else float(ANALYTICS_START_BALANCE_USDT)
                st_bal = float(data.get("start_balance_usdt", cfg_start))
                cu_bal = float(data.get("cur_balance_usdt", st_bal))
                pk_bal = float(data.get("peak_balance_usdt", max(st_bal, cu_bal)))
                mn_bal = float(data.get("min_balance_usdt", min(st_bal, cu_bal)))
                net_p = float(data.get("net_profit_usdt", 0.0))
                rl_p = float(data.get("realized_pnl_usdt", 0.0))
                rl_net = float(data.get("realized_pnl_net_usdt", rl_p))
                unrl = float(data.get("unrealized_pnl_usdt", 0.0))
                comm = float(data.get("total_commission_usdt", 0.0))
                fund = float(data.get("total_funding_usdt", 0.0))
                dd = float(data.get("max_drawdown_usdt", 0.0))
                perf = float(data.get("performance_usdt", 0.0))
                tr_cnt = int(data.get("total_trades", 0))
                w_cnt = int(data.get("winning_trades", 0))
                l_cnt = int(data.get("losing_trades", 0))
                m_used = float(data.get("total_margin_used_usdt", 0.0))
                f_marg = float(data.get("free_margin_usdt", 0.0))
                g_load = float(data.get("current_grid_load_usdt", 0.0))
                g_size = float(data.get("total_grid_size_usdt", 0.0))

                per_coin = data.get("per_coin", {})
                if comm == 0.0 and per_coin:
                    comm = sum(float(c.get("commission_usdt", 0.0)) for c in per_coin.values())
                if fund == 0.0 and per_coin:
                    fund = sum(float(c.get("funding_usdt", 0.0)) for c in per_coin.values())
                if rl_net == 0.0 and per_coin:
                    rl_net = sum(float(c.get("realized_pnl_net_usdt", c.get("realized_pnl_usdt", 0.0))) for c in per_coin.values())
                if g_load == 0.0 and per_coin:
                    g_load = sum(float(c.get("current_grid_load_usdt", 0.0)) for c in per_coin.values())
                if g_size == 0.0 and per_coin:
                    g_size = sum(float(c.get("grid_capacity_usdt", 0.0)) for c in per_coin.values())
                if m_used == 0.0 and per_coin:
                    m_used = sum(float(c.get("current_margin_usdt", 0.0)) for c in per_coin.values())
                if f_marg == 0.0 and cu_bal > 0:
                    f_marg = max(0.0, cu_bal - m_used)

                agg_summary["start_balance_usdt"] += st_bal
                agg_summary["cur_balance_usdt"] += cu_bal
                agg_summary["peak_balance_usdt"] += pk_bal
                agg_summary["min_balance_usdt"] += mn_bal
                agg_summary["net_profit_usdt"] += net_p
                agg_summary["realized_pnl_usdt"] += rl_p
                agg_summary["realized_pnl_net_usdt"] += rl_net
                agg_summary["unrealized_pnl_usdt"] += unrl
                agg_summary["total_commission_usdt"] += comm
                agg_summary["total_funding_usdt"] += fund
                agg_summary["max_drawdown_usdt"] += dd
                agg_summary["performance_usdt"] += perf
                agg_summary["total_trades"] += tr_cnt
                agg_summary["winning_trades"] += w_cnt
                agg_summary["losing_trades"] += l_cnt
                agg_summary["total_margin_used_usdt"] += m_used
                agg_summary["free_margin_usdt"] += f_marg
                agg_summary["current_grid_load_usdt"] += g_load
                agg_summary["total_grid_size_usdt"] += g_size

                for sym, cinfo in per_coin.items():
                    if sym not in agg_per_coin:
                        agg_per_coin[sym] = {
                            "net_profit_usdt": 0.0,
                            "realized_pnl_usdt": 0.0,
                            "realized_pnl_net_usdt": 0.0,
                            "commission_usdt": 0.0,
                            "funding_usdt": 0.0,
                            "total_trades": 0,
                            "winning_trades": 0,
                            "loss_trades": 0,
                            "current_drawdown": 0.0,
                            "max_drawdown": 0.0,
                            "min_drawdown": 0.0,
                            "universes_active": 0,
                            "grid_capacity_usdt": 0.0,
                            "current_grid_load_usdt": 0.0,
                            "current_margin_usdt": 0.0,
                            "peak_net_profit_usdt": 0.0,
                            "max_drawdown_net_usdt": 0.0,
                            "recovery_factor": 0.0,
                            "abs_recovery_factor": 0.0,
                            "risk_reward_ratio": 0.0,
                        }
                    coin_entry = agg_per_coin[sym]
                    coin_entry["net_profit_usdt"] += float(cinfo.get("net_profit_usdt", 0.0))
                    coin_entry["realized_pnl_usdt"] += float(cinfo.get("realized_pnl_usdt", 0.0))
                    coin_entry["realized_pnl_net_usdt"] += float(cinfo.get("realized_pnl_net_usdt", 0.0))
                    coin_entry["commission_usdt"] += float(cinfo.get("commission_usdt", 0.0))
                    coin_entry["funding_usdt"] += float(cinfo.get("funding_usdt", 0.0))
                    coin_entry["total_trades"] += int(cinfo.get("total_trades", cinfo.get("trades", 0)))
                    coin_entry["winning_trades"] += int(cinfo.get("winning_trades", cinfo.get("win_count", 0)))
                    coin_entry["loss_trades"] += int(cinfo.get("loss_trades", cinfo.get("loss_count", 0)))
                    coin_entry["current_drawdown"] += float(cinfo.get("current_drawdown", 0.0))
                    c_dd = float(cinfo.get("max_drawdown", 0.0))
                    coin_entry["max_drawdown"] = min(coin_entry["max_drawdown"], c_dd)
                    c_peak = float(cinfo.get("peak_net_profit_usdt", max(0.0, float(cinfo.get("net_profit_usdt", 0.0)))))
                    coin_entry["peak_net_profit_usdt"] = round(coin_entry.get("peak_net_profit_usdt", 0.0) + c_peak, 4)
                    c_dd_net = float(cinfo.get("max_drawdown_net_usdt", cinfo.get("max_drawdown", 0.0)))
                    coin_entry["max_drawdown_net_usdt"] = min(coin_entry.get("max_drawdown_net_usdt", 0.0), c_dd_net)
                    coin_entry["universes_active"] += 1
                    coin_entry["is_active"] = coin_entry.get("is_active", False) or cinfo.get("is_active_symbol", True)
                    coin_entry["grid_capacity_usdt"] += float(cinfo.get("grid_capacity_usdt", 0.0))
                    coin_entry["current_grid_load_usdt"] += float(cinfo.get("current_grid_load_usdt", 0.0))
                    coin_entry["current_margin_usdt"] += float(cinfo.get("current_margin_usdt", 0.0))
                    if float(cinfo.get("risk_reward_ratio", 0.0)) != 0:
                        prev_rrr = coin_entry["risk_reward_ratio"]
                        cur_rrr = float(cinfo.get("risk_reward_ratio", 0.0))
                        coin_entry["risk_reward_ratio"] = round((prev_rrr + cur_rrr) / 2.0, 4) if prev_rrr != 0 else cur_rrr

                    coin_peel = cinfo.get("peeling", {})
                    coin_rec = cinfo.get("cross_recycling", {})
                    if coin_peel:
                        coin_entry["peel_events"] = coin_entry.get("peel_events", 0) + int(coin_peel.get("events_count", 0))
                        coin_entry["peel_vol_usdt"] = round(coin_entry.get("peel_vol_usdt", 0.0) + float(coin_peel.get("total_peeled_usdt", 0.0)), 2)
                        coin_entry["peel_pnl_usdt"] = round(coin_entry.get("peel_pnl_usdt", 0.0) + float(coin_peel.get("locked_micro_pnl_usdt", 0.0)), 2)
                    if coin_rec:
                        coin_entry["rec_events"] = coin_entry.get("rec_events", 0) + int(coin_rec.get("events_count", 0))
                        coin_entry["rec_burn_usdt"] = round(coin_entry.get("rec_burn_usdt", 0.0) + float(coin_rec.get("burned_volume_usdt", 0.0)), 2)
                        coin_entry["rec_loss_usdt"] = round(coin_entry.get("rec_loss_usdt", 0.0) + float(coin_rec.get("loss_absorbed_usdt", 0.0)), 2)
                        coin_entry["rec_spent_usdt"] = round(coin_entry.get("rec_spent_usdt", 0.0) + float(coin_rec.get("profit_spent_usdt", 0.0)), 2)
            except Exception as e:
                logger.error(f"[PORTFOLIO_AGGREGATOR] Error processing {p.name}: {e}")

        # Рассчитываем recovery_factor по каждой монете
        for sym, coin_entry in agg_per_coin.items():
            c_np = coin_entry["net_profit_usdt"]
            c_mdd = coin_entry["max_drawdown"]
            c_mdd_net = coin_entry["max_drawdown_net_usdt"]
            if c_mdd < 0:
                coin_entry["recovery_factor"] = round(c_np / abs(c_mdd), 2)
                coin_entry["abs_recovery_factor"] = round(abs(c_np) / abs(c_mdd), 2)
            elif c_mdd_net < 0:
                coin_entry["recovery_factor"] = round(c_np / abs(c_mdd_net), 2)
                coin_entry["abs_recovery_factor"] = round(abs(c_np) / abs(c_mdd_net), 2)
            else:
                coin_entry["recovery_factor"] = 0.0
                coin_entry["abs_recovery_factor"] = 0.0

        for ledger_file in list(self.ledgers_dir.glob("trades_ledger*.txt")):
            try:
                with open(ledger_file, 'r', encoding='utf-8') as f:
                    r = csv.reader(f, delimiter=';')
                    lines = list(r)
                    if len(lines) > 1:
                        row = lines[-1]
                        dt_cols = [c.strip() for c in row if len(c.strip()) == 19 and c.strip()[4] == '-' and c.strip()[7] == '-']
                        if dt_cols:
                            t_str = dt_cols[-1]
                            if t_str > last_trade_dt_str or last_trade_dt_str == "Нет сделок":
                                last_trade_dt_str = t_str
            except Exception:
                pass

        tot_tr = agg_summary["total_trades"]
        win_tr = agg_summary["winning_trades"]
        agg_summary["winrate_pct"] = round((win_tr / tot_tr * 100.0), 2) if tot_tr > 0 else 0.0
        st_b = agg_summary["start_balance_usdt"]
        np_b = agg_summary["net_profit_usdt"]
        agg_summary["roi_pct"] = round((np_b / st_b * 100.0), 2) if st_b > 0 else 0.0
        m_dd = agg_summary["max_drawdown_usdt"]
        agg_summary["recovery_factor"] = round(np_b / abs(m_dd), 2) if m_dd < 0 else 0.0

        cur_b = agg_summary["cur_balance_usdt"]
        m_used_tot = agg_summary["total_margin_used_usdt"]
        agg_summary["margin_load_pct"] = round((m_used_tot / cur_b * 100.0), 2) if cur_b > 0 else 0.0
        f_marg_tot = agg_summary["free_margin_usdt"]
        agg_summary["free_margin_pct"] = round((f_marg_tot / cur_b * 100.0), 2) if cur_b > 0 else 0.0
        tot_grid = agg_summary["total_grid_size_usdt"]
        cur_grid = agg_summary["current_grid_load_usdt"]
        agg_summary["grid_load_pct"] = round((cur_grid / tot_grid * 100.0), 2) if tot_grid > 0 else 0.0

        return agg_summary, agg_per_coin, last_trade_dt_str
