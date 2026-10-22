# ==============================================================================
# Path: ANALYTICS/FORMATTERS/bigboard_formatter.py
# Role: Рендеринг портфельного Биг Борда, аналитических карточек куполов и текстовых отчетов
# ==============================================================================

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from consts import (
    ANALYTICS_START_BALANCE_USDT,
    _CFG,
)
from c_log import UnifiedLogger
from ANALYTICS.leaderboard import LeaderboardEngine

logger = UnifiedLogger("BigBoardFormatter")


class BigBoardFormatter:
    """Форматтер презентационного слоя для портфельного Биг Борда и сводок куполов."""

    @staticmethod
    def format_trade_velocity_block(
        vel_dict: Optional[Dict[str, Any]],
        title: str = "Trade Velocity (Частота сделок)",
    ) -> str:
        """Форматирует информационный блок Trade Velocity строго по данным без скрытого хардкода."""
        if not vel_dict or not isinstance(vel_dict, dict) or "timeframe_hours" not in vel_dict:
            return f"⚡ <b>{title}:</b>\n└ <code>Нет данных о скорости сделок</code>\n\n"

        tf_h = float(vel_dict["timeframe_hours"])
        acc_h = float(vel_dict.get("accumulation_hours") or 0.0)
        min_acc = float(vel_dict.get("min_accumulation_hours") or tf_h)
        benchmark = float(vel_dict.get("benchmark_speed") or 0.0)
        slice_trades = int(vel_dict.get("slice_trades_count") or 0)
        slice_coins = int(vel_dict.get("slice_coins_count") or 0)
        status_desc = str(vel_dict.get("status_desc") or "Накопление")
        status = str(vel_dict.get("status") or "ACCUMULATING")

        if status == "HIGH_VOLATILITY":
            status_icon = "🔥"
        elif status == "LOW_VOLATILITY":
            status_icon = "❄️"
        elif status == "ACCUMULATING":
            status_icon = "⏳"
        else:
            status_icon = "🟢"

        if not vel_dict.get("is_ready_for_alerts") or vel_dict.get("avg_effective_speed") is None:
            return (
                f"⚡ <b>{title}:</b>\n"
                f"├ <b>Скорость:</b> ⏳ <i>Накопление ({acc_h:.1f}ч из {min_acc:.1f}ч)</i>\n"
                f"├ <b>Сделок в окне:</b> <code>{slice_trades} сд.</code> ({slice_coins} монет)\n"
                f"└ <b>Эталон:</b> <code>{benchmark:.1f} сд/монета за {tf_h:.1f}ч</code>\n\n"
            )

        eff_speed = float(vel_dict.get("avg_effective_speed") or 0.0)
        ratio = float(vel_dict.get("speed_ratio") or 1.0)
        dev_pct = float(vel_dict.get("deviation_pct") or 0.0)
        dev_sign = "+" if dev_pct > 0 else ""

        return (
            f"⚡ <b>{title}:</b>\n"
            f"├ <b>Скорость:</b> <b>{eff_speed:.2f} сд/монета</b> ({status_icon} <b>{status_desc}</b>)\n"
            f"├ <b>Активность:</b> <code>x{ratio:.2f}</code> ({dev_sign}{dev_pct:.1f}% к эталону {benchmark:.1f})\n"
            f"└ <b>Окно ({tf_h:.1f}ч):</b> <code>{slice_trades} сд.</code> на {slice_coins} монет (накоплено <code>{acc_h:.1f}ч</code>)\n\n"
        )

    @classmethod
    def render_universe_analytics_card(
        cls,
        stats_dir: Path,
        current_uid: str,
        univ_name: str,
        analytics_manager: Optional[Any] = None,
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Формирует HTML-текст и кнопки карточки детальной аналитики купола."""
        clean_name = html.escape(univ_name)
        strat_cfg = _CFG.get("active_strategies", {}).get(current_uid, {}) if isinstance(_CFG, dict) else {}
        cfg_start = float(strat_cfg.get("start_balance_usdt", ANALYTICS_START_BALANCE_USDT)) if isinstance(strat_cfg, dict) else float(ANALYTICS_START_BALANCE_USDT)

        start_bal = cfg_start
        cur_bal = cfg_start
        peak_bal = cfg_start
        min_bal = cfg_start
        net_profit = 0.0
        realized = 0.0
        unrealized = 0.0
        max_dd = 0.0
        recovery = 0.0
        trades_cnt = 0
        iter_idx = 1
        dome_vel = None

        analytics_path = stats_dir / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            matches = list(stats_dir.glob(f"analytics_{current_uid}*.json"))
            if matches:
                analytics_path = sorted(matches)[0]

        if analytics_path.exists():
            try:
                adata = json.loads(analytics_path.read_text(encoding="utf-8"))
                start_bal = float(adata.get("start_balance_usdt", start_bal))
                cur_bal = float(adata.get("cur_balance_usdt", start_bal))
                peak_bal = float(adata.get("peak_balance_usdt", max(start_bal, cur_bal)))
                min_bal = float(adata.get("min_balance_usdt", min(start_bal, cur_bal)))
                net_profit = float(adata.get("net_profit_usdt", 0.0))
                realized = float(adata.get("realized_pnl_net_usdt", adata.get("realized_pnl_usdt", 0.0)))
                unrealized = float(adata.get("unrealized_pnl_usdt", 0.0))
                max_dd = float(adata.get("max_drawdown_usdt", 0.0))
                recovery = float(adata.get("recovery_factor", 0.0))
                trades_cnt = int(adata.get("total_trades", 0))
                iter_idx = int(adata.get("iteration_index", 1))
                dome_vel = adata.get("trade_velocity")
            except Exception as e:
                logger.error(f"[BIGBOARD_FORMATTER] Error reading analytics for {current_uid}: {e}")

        if not dome_vel and analytics_manager and hasattr(analytics_manager, "get_trade_velocity"):
            dome_vel = analytics_manager.get_trade_velocity(current_uid)

        vel_block = cls.format_trade_velocity_block(dome_vel, title="⚡ Trade Velocity (Купол)")

        roi_pct = round((net_profit / start_bal * 100.0), 2) if start_bal > 0 else 0.0
        net_sign = "+" if net_profit > 0 else ""
        roi_sign = "+" if roi_pct > 0 else ""
        real_sign = "+" if realized > 0 else ""
        unreal_sign = "+" if unrealized > 0 else ""

        peel_cnt, peel_vol, rec_cnt, rec_vol = 0, 0.0, 0, 0.0
        try:
            lb_engine = getattr(analytics_manager, "leaderboard_engine", None) or LeaderboardEngine(stats_dir=stats_dir)
            u_summary = lb_engine.calculate_universe_summary(current_uid, {"name": univ_name})
            peel_cnt = int(u_summary.get("peel_events", 0))
            peel_vol = float(u_summary.get("peel_vol_usdt", 0.0))
            rec_cnt = int(u_summary.get("rec_events", 0))
            rec_vol = float(u_summary.get("rec_burn_usdt", 0.0))
        except Exception:
            pass

        def_line = f"├ <b>Дефенс:</b> 🍊 {peel_cnt} пилинг ({peel_vol:.1f}$) | ♻️ {rec_cnt} рециклер ({rec_vol:.1f}$)\n"

        text = (
            f"📊 <b>АНАЛИТИКА КУПОЛА СТРАТЕГИИ</b>\n"
            f"🌌 <b>{clean_name}</b> (<code>{current_uid}</code>)\n\n"
            f"💰 <b>Баланс и Пики купола:</b>\n"
            f"├ <b>Текущий баланс:</b> <code>{cur_bal:,.2f}$</code>\n"
            f"├ <b>Начальный баланс:</b> <code>{start_bal:,.2f}$</code>\n"
            f"├ <b>ATH (Пик баланса):</b> <code>{peak_bal:,.2f}$</code>\n"
            f"└ <b>Дно (Минимум):</b> <code>{min_bal:,.2f}$</code>\n\n"
            f"📈 <b>Результаты купола:</b>\n"
            f"├ <b>Net Profit:</b> <b>{net_sign}{net_profit:,.2f}$</b> ({roi_sign}{roi_pct:.2f}%)\n"
            f"├ <b>Realized:</b> {real_sign}{realized:,.2f}$ | <b>Floating:</b> {unreal_sign}{unrealized:,.2f}$\n"
            f"{def_line}"
            f"├ <b>Max Drawdown:</b> {max_dd:,.2f}$ | <b>Recovery:</b> {recovery:.2f}\n"
            f"└ <b>Сделок:</b> {trades_cnt} сд. (Итерация #{iter_idx})\n\n"
            f"{vel_block}"
            f"<i>Выберите раздел аналитики:</i>"
        )

        buttons = [
            [{"text": "🏆 Рейтинг монет", "callback_data": f"arank:net:{current_uid}:new"}],
            [{"text": "🪙 Аналитика по монетам", "callback_data": f"analytics_coin_menu:{current_uid}"}],
            [{"text": "📈 График эквити", "callback_data": f"analytics_chart:{current_uid}"}],
            [{"text": "📝 Лента сделок (TXT)", "callback_data": f"analytics_txt:{current_uid}"}],
            [{"text": "📄 Выгрузить весь отчет (TXT)", "callback_data": f"analytics_full_report:{current_uid}"}],
            [
                {"text": "🔄 Сменить вселенную", "callback_data": "univ_page:1"},
                {"text": "🏆 Лидерборд стратегий", "callback_data": "analytics_bb_leaderboard"},
            ],
            [{"text": "📚 Шпаргалка (Cheat Sheet)", "callback_data": "analytics_cheat_sheet"}],
        ]

        return text, buttons

    @classmethod
    def render_portfolio_big_board(
        cls,
        stats_dir: Path,
        current_uid: str,
        univ_name: str,
        active_strats: Optional[dict] = None,
        analytics_manager: Optional[Any] = None,
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Формирует HTML-текст и кнопки портфельного Биг Борда."""
        portfolio_summary = {}
        engine = getattr(analytics_manager, "leaderboard_engine", None)
        if engine and hasattr(engine, "get_portfolio_summary"):
            try:
                active_strats = active_strats or {}
                universes_meta = {
                    sid: {**getattr(scfg, "raw_meta", {}), "name": getattr(scfg, "name", sid), "enabled": True}
                    for sid, scfg in active_strats.items()
                }
                entries = engine.generate_leaderboard(universes_meta)
                portfolio_summary = engine.get_portfolio_summary(entries)
            except Exception as e:
                logger.error(f"[BIGBOARD_FORMATTER] Error getting portfolio summary: {e}")

        if not portfolio_summary:
            p_path = stats_dir / "analytics_all.json"
            if p_path.exists():
                try:
                    portfolio_summary = json.loads(p_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

        clean_name = html.escape(univ_name)
        start_bal = float(portfolio_summary.get("aggregate_start_balance_usdt", 0.0))
        cur_bal = float(portfolio_summary.get("aggregate_cur_balance_usdt", start_bal))
        peak_bal = float(portfolio_summary.get("aggregate_peak_balance_usdt", max(start_bal, cur_bal)))
        min_bal = float(portfolio_summary.get("aggregate_min_balance_usdt", min(start_bal, cur_bal)))
        net_profit = float(portfolio_summary.get("aggregate_net_profit_usdt", 0.0))
        realized = float(portfolio_summary.get("aggregate_realized_pnl_usdt", 0.0))
        unrealized = float(portfolio_summary.get("aggregate_unrealized_pnl_usdt", 0.0))
        trades_cnt = int(portfolio_summary.get("aggregate_total_trades", 0))
        active_count = int(portfolio_summary.get("total_universes_active", len(active_strats) if active_strats else 1))

        roi_pct = round((net_profit / start_bal * 100.0), 2) if start_bal > 0 else 0.0
        net_sign = "+" if net_profit > 0 else ""
        roi_sign = "+" if roi_pct > 0 else ""
        real_sign = "+" if realized > 0 else ""
        unreal_sign = "+" if unrealized > 0 else ""

        port_vel = portfolio_summary.get("trade_velocity")
        if not port_vel and analytics_manager and hasattr(analytics_manager, "get_trade_velocity"):
            port_vel = analytics_manager.get_trade_velocity("default")
        vel_block = cls.format_trade_velocity_block(port_vel, title="Trade Velocity (Частота сделок портфеля)")

        text = (
            f"🏛️ <b>ПОРТФЕЛЬНЫЙ БИГ БОРД (КУМУЛЯТИВНЫЙ БАЛАНС)</b>\n"
            f"🌐 <i>Агрегированная сводка по всем стратегиям ({active_count} куполов)</i>\n\n"
            f"💰 <b>Капитал портфеля:</b>\n"
            f"├ <b>Текущий баланс:</b> <code>{cur_bal:,.2f}$</code>\n"
            f"├ <b>Начальный баланс:</b> <code>{start_bal:,.2f}$</code>\n"
            f"├ <b>ATH (Пик баланса):</b> <code>{peak_bal:,.2f}$</code>\n"
            f"└ <b>Дно (Минимум):</b> <code>{min_bal:,.2f}$</code>\n\n"
            f"📊 <b>Совокупный PnL:</b>\n"
            f"├ <b>Net Profit:</b> <b>{net_sign}{net_profit:,.2f}$</b> ({roi_sign}{roi_pct:.2f}%)\n"
            f"├ <b>Realized:</b> <code>{real_sign}{realized:,.2f}$</code>\n"
            f"├ <b>Floating (Нереализ.):</b> <code>{unreal_sign}{unrealized:,.2f}$</code>\n"
            f"└ <b>Всего сделок:</b> <code>{trades_cnt}</code>\n\n"
            f"{vel_block}"
            f"🌌 <b>Текущий выбранный купол:</b>\n"
            f"▶️ <b>{clean_name}</b> (<code>{current_uid}</code>)\n\n"
            f"<i>Выберите действие или перейдите в детальную аналитику купола:</i>"
        )

        short_uid = (current_uid[:20] + "..") if len(current_uid) > 20 else current_uid
        buttons = [
            [{"text": f"🌌 Аналитика купола: {short_uid}", "callback_data": f"analytics_dome:{current_uid}"}],
            [
                {"text": "🔄 Сменить купол", "callback_data": "univ_page:1"},
                {"text": "🏆 Лидерборд стратегий", "callback_data": "analytics_bb_leaderboard"},
            ],
        ]

        return text, buttons

    @staticmethod
    def render_full_report_text(stats_dir: Path, current_uid: str, univ_name: str) -> str:
        """Формирует полный детальный текстовый отчет для выгрузки в файл TXT со всеми глобальными и per-coin метриками."""
        analytics_path = stats_dir / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            matches = list(stats_dir.glob(f"analytics_{current_uid}*.json"))
            if matches:
                analytics_path = sorted(matches)[0]
            else:
                return f"No analytics found for {current_uid}"

        try:
            data = json.loads(analytics_path.read_text(encoding="utf-8"))
            lines = [
                "=" * 50,
                f"STRATEGY DOME ANALYTICS REPORT: {univ_name} ({current_uid})",
                "=" * 50 + "\n",
            ]

            def _f(v, d=0.0):
                try:
                    return float(v) if v is not None else d
                except (ValueError, TypeError):
                    return d

            def _i(v, d=0):
                try:
                    return int(v) if v is not None else d
                except (ValueError, TypeError):
                    return d

            # 1. Balances
            start_val = _f(data.get("start_balance_usdt", 0))
            cur_val = _f(data.get("cur_balance_usdt", start_val))
            peak_val = _f(data.get("peak_balance_usdt", cur_val))
            min_val = _f(data.get("min_balance_usdt", cur_val))

            lines.append("Balances:")
            lines.append(f"  Start: {start_val:,.2f} USDT")
            lines.append(f"  Current: {cur_val:,.2f} USDT")
            lines.append(f"  Peak: {peak_val:,.2f} USDT")
            lines.append(f"  Min (Bottom): {min_val:,.2f} USDT\n")

            # 2. Performance (Global)
            per_coin_dict = data.get("per_coin", {})
            realized = _f(data.get("realized_pnl_usdt", 0))
            realized_net = _f(data.get("realized_pnl_net_usdt", realized))
            comm = _f(data.get("total_commission_usdt", 0))
            fund = _f(data.get("total_funding_usdt", 0))
            if comm == 0.0 and per_coin_dict:
                comm = sum(_f(c.get("commission_usdt", 0.0)) for c in per_coin_dict.values())
            if fund == 0.0 and per_coin_dict:
                fund = sum(_f(c.get("funding_usdt", 0.0)) for c in per_coin_dict.values())

            net_profit = _f(data.get("net_profit_usdt", 0))
            unrealized = _f(data.get("unrealized_pnl_usdt", 0))
            max_dd = _f(data.get("max_drawdown_usdt", 0))
            rec_factor = _f(data.get("recovery_factor", 0))
            perf_growth = _f(data.get("performance_usdt", peak_val - start_val))
            roi = _f(data.get("roi_pct", (net_profit / start_val * 100.0) if start_val > 0 else 0.0))

            gload_pct = _f(data.get("grid_load_pct", 0))
            curr_gload = _f(data.get("current_grid_load_usdt", 0))
            tot_gsize = _f(data.get("total_grid_size_usdt", 0))
            max_gload_pct = _f(data.get("max_grid_load_pct", 0))

            mload_pct = _f(data.get("margin_load_pct", 0))
            tot_mused = _f(data.get("total_margin_used_usdt", 0))
            max_mload_pct = _f(data.get("max_margin_load_pct", 0))

            free_m = _f(data.get("free_margin_usdt", cur_val - tot_mused))
            free_m_pct = _f(data.get("free_margin_pct", (free_m / cur_val * 100.0) if cur_val > 0 else 0.0))
            min_free_m = _f(data.get("min_free_margin_usdt", free_m))

            total_trades = _i(data.get("total_trades", 0))
            win_trades = _i(data.get("winning_trades", 0))
            winrate = _f(data.get("winrate_pct", (win_trades / total_trades * 100.0) if total_trades > 0 else 0.0))

            lines.append("Performance:")
            lines.append(f"  Net Profit: {net_profit:+,.2f} USDT")
            lines.append(f"  Realized PnL (Gross): {realized:+,.2f} USDT")
            lines.append(f"  Realized PnL (Net): {realized_net:+,.2f} USDT")
            lines.append(f"  Total Commission: {comm:,.4f} USDT")
            lines.append(f"  Total Funding: {fund:,.4f} USDT")
            lines.append(f"  Unrealized PnL: {unrealized:+,.2f} USDT")
            lines.append(f"  Max Drawdown: {max_dd:,.2f} USDT")
            lines.append(f"  Performance (Peak-Start): {perf_growth:+,.2f} USDT")
            lines.append(f"  ROI: {roi:.2f}%")
            lines.append(f"  Grid Load: {gload_pct:.2f}% ({curr_gload:,.2f} / {tot_gsize:,.2f} USDT | Max: {max_gload_pct:.2f}%)")
            lines.append(f"  Margin Load: {mload_pct:.2f}% (Used: {tot_mused:,.2f} USDT | Max: {max_mload_pct:.2f}%)")
            lines.append(f"  Free Margin: {free_m:,.2f} USDT ({free_m_pct:.2f}% | Min: {min_free_m:,.2f} USDT)")
            lines.append(f"  Recovery Factor: {rec_factor:.2f}")
            lines.append(f"  Trades: {total_trades} (Wins: {win_trades} | {winrate:.1f}%)\n")

            # 3. Iteration info
            it_idx = data.get("iteration_index", 1)
            it_net = _f(data.get("iteration_net_profit_usdt", net_profit))
            it_vel = _f(data.get("iteration_net_velocity", 0))
            lines.append("Trading Activity & Iteration:")
            lines.append(f"  Iteration: #{it_idx} (Net: {it_net:+,.2f} USDT | Velocity: {it_vel:.2f} $/h)\n")

            # 4. Toxic Defense / Subgrid Peeling
            td = data.get("toxic_defense", {})
            if td:
                peel = td.get("subgrid_peeling", {})
                rec = td.get("cross_recycling", {})
                p_cnt = _i(peel.get("events_count"))
                r_cnt = _i(rec.get("events_count"))
                if p_cnt > 0 or r_cnt > 0:
                    lines.append("Toxic Defense & Peeling:")
                    lines.append(
                        f"  Subgrid Peeling: {p_cnt} events | "
                        f"Loss: -{_f(peel.get('peel_loss_usdt')):.2f} USDT | "
                        f"Micro-PnL: +{_f(peel.get('locked_micro_pnl_usdt')):.2f} USDT"
                    )
                    lines.append(
                        f"  Cross-Recycling: {r_cnt} events | "
                        f"Loss Absorbed: -{_f(rec.get('loss_absorbed_usdt')):.2f} USDT | "
                        f"Profit Spent: {_f(rec.get('profit_spent_usdt')):.2f} USDT\n"
                    )

            # 5. PER-COIN METRICS
            lines.append("=" * 50)
            lines.append("PER-COIN METRICS")
            lines.append("=" * 50 + "\n")

            for sym, c in sorted(per_coin_dict.items(), key=lambda x: _f(x[1].get("net_profit_usdt", 0)), reverse=True):
                is_active = c.get("is_active_symbol", c.get("is_active", True))
                status_str = "[АКТИВНА]" if is_active else "[ВЫВЕДЕНА / АРХИВ]"

                c_trades = _i(c.get("total_trades", c.get("trades", 0)))
                c_wins = _i(c.get("winning_trades", c.get("win_count", 0)))
                c_wr = _f(c.get("winrate_pct", (c_wins / c_trades * 100.0) if c_trades > 0 else 0.0))

                c_np = _f(c.get("net_profit_usdt", 0))
                c_roi = round(c_np / start_val * 100.0, 2) if start_val > 0 else 0.0
                c_roi_sign = "+" if c_roi > 0 else ""

                c_realized_gross = _f(c.get("realized_pnl_usdt", c.get("gross_realized_usdt", 0)))
                c_realized_net = _f(c.get("realized_pnl_net_usdt", c_realized_gross))
                c_comm = _f(c.get("commission_usdt", 0))
                c_fund = _f(c.get("funding_usdt", 0))

                c_unrealized = _f(c.get("current_drawdown", _f(c.get("long_unrealized", 0)) + _f(c.get("short_unrealized", 0))))
                c_max_dd = _f(c.get("max_drawdown", 0))
                c_min_dd = _f(c.get("min_drawdown", 0))

                c_avg_daily = _f(c.get("avg_daily_profit", 0))
                c_gload_pct = _f(c.get("current_grid_load_pct", 0))
                c_gload_usdt = _f(c.get("current_grid_load_usdt", 0))
                c_gcap_usdt = _f(c.get("grid_capacity_usdt", 0))
                c_max_gload_pct = _f(c.get("max_grid_load_pct", 0))

                c_mused = _f(c.get("current_margin_usdt", 0))
                c_mload_pct = _f(c.get("margin_load_pct", 0))
                c_max_mused = _f(c.get("max_margin_usdt", 0))

                c_pos_size = _f(c.get("max_position_size", c_gload_usdt))
                c_rec = _f(c.get("recovery_factor", 0))
                c_rrr = _f(c.get("risk_reward_ratio", 0))
                c_abs_rec = _f(c.get("abs_recovery_factor", 0))

                lines.append(f"--- {sym} {status_str} ---")
                lines.append(f"  Trades: {c_trades} (Wins: {c_wins} | {c_wr:.1f}%)")
                lines.append(f"  Net Profit: {c_np:+,.2f} USDT ({c_roi_sign}{c_roi:.2f}% ROI)")
                lines.append(f"  Realized (Gross): {c_realized_gross:+,.2f} USDT")
                lines.append(f"  Realized (Net): {c_realized_net:+,.2f} USDT")
                lines.append(f"  Fees: Comm {c_comm:,.4f} / Fund {c_fund:,.4f}")
                lines.append(f"  Unrealized PnL: {c_unrealized:+,.2f} USDT")
                lines.append(f"  Hist. Drawdown: Max {c_max_dd:,.2f} / Min {c_min_dd:,.2f} USDT")
                lines.append(f"  Avg Daily Profit (Net): {c_avg_daily:+,.2f} USDT")
                lines.append(f"  Grid Load: {c_gload_pct:.2f}% ({c_gload_usdt:,.2f} / {c_gcap_usdt:,.2f} USDT | Max: {c_max_gload_pct:.2f}%)")
                lines.append(f"  Margin Used: {c_mused:,.2f} USDT ({c_mload_pct:.2f}% balance | Max: {c_max_mused:,.2f} USDT)")
                lines.append(f"  Max Position Size: {c_pos_size:,.2f} USDT")
                lines.append(f"  Recovery Factor: {c_rec:.2f}")
                lines.append(f"  Risk/Reward Ratio: {c_rrr:.2f}")
                lines.append(f"  Abs Recovery Factor: {c_abs_rec:.2f}\n")

            return "\n".join(lines)
        except Exception as e:
            return f"Error building full report for {current_uid}: {e}"

    @staticmethod
    def render_cheat_sheet() -> str:
        """Возвращает шпаргалку (Cheat Sheet) по метрикам аналитики."""
        return (
            "<b>📚 Analytics Cheat Sheet (Global)</b>\n"
            "▪️ <b>roi_pct</b>: Return on Investment (%) → <i>(Cur_Balance - Start_Balance) / Start_Balance * 100</i>\n"
            "▪️ <b>grid_load_pct</b>: Grid Capacity Utilization (%) → <i>Current Grid Load / Total Grid Size * 100</i>\n"
            "▪️ <b>margin_load_pct</b>: Margin Usage (%) → <i>Margin Used / Floating Balance * 100</i>\n"
            "▪️ <b>free_margin_usdt</b>: Free Margin → <i>Floating Balance - Margin Used</i>\n"
            "▪️ <b>recovery_factor</b>: Recovery Factor → <i>Net Profit / |Max Drawdown|</i>\n"
            "▪️ <b>abs_recovery_factor</b>: Abs Recovery Factor → <i>Peak Net Profit / |Max Drawdown|</i>\n"
            "▪️ <b>realized_pnl_usdt</b>: Total realized profit from all closed trades\n"
            "▪️ <b>net_profit_usdt</b>: True mathematical growth → <i>Realized PnL + Unrealized PnL</i>\n"
            "▪️ <b>cur_balance_usdt</b>: Current margin balance → <i>Start Balance + Net Profit</i>\n\n"
            "<b>🪙 Per-Coin Metrics</b>\n"
            "▪️ <b>avg_daily_profit</b>: Average profit per active day of trading\n"
            "▪️ <b>max_position_size</b>: Max historical notional size actually reached (real volume * price)\n"
            "▪️ <b>risk_reward_ratio</b>: <i>|Max Drawdown| / Avg Daily Profit</i>\n"
            "▪️ <b>recovery_factor</b> / <b>abs_recovery_factor</b>: Current and absolute recovery factors\n"
            "▪️ <b>max_drawdown</b> / <b>min_drawdown</b>: Max/Min historical floating drawdowns\n"
        )
