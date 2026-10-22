# ==============================================================================
# Path: ANALYTICS/report_service.py
# Role: Сервис формирования аналитических отчетов, сводок и графиков для презентационного слоя
# ==============================================================================

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from consts import (
    ANALYTICS_DIR,
    ANALYTICS_LEDGERS_DIR,
    ANALYTICS_START_BALANCE_USDT,
    ANALYTICS_STATS_DIR,
    _CFG,
)
from c_log import UnifiedLogger
from ANALYTICS.plotter import generate_equity_curve, generate_balance_curve

logger = UnifiedLogger("AnalyticsReportService")


class AnalyticsReportService:
    """
    Канонический сервис формирования аналитических карточек, лидерборда,
    ранжирования монет и выгрузки графиков. Вся бизнес-логика и рендеринг аналитики
    сосредоточены здесь, освобождая Telegram-слой от вычислений (Dumb Executor).
    """

    def __init__(self, analytics_manager=None, stats_dir: Optional[Path] = None, ledgers_dir: Optional[Path] = None):
        self.analytics_manager = analytics_manager
        self.stats_dir: Path = stats_dir or ANALYTICS_STATS_DIR
        self.ledgers_dir: Path = ledgers_dir or ANALYTICS_LEDGERS_DIR

    def get_equity_chart_path(self, universe_id: Optional[str] = None) -> Optional[Path]:
        """Генерирует график эквити купола или портфеля и возвращает путь к файлу."""
        try:
            chart_file_str = generate_equity_curve(universe_id=universe_id)
            if chart_file_str and Path(chart_file_str).exists():
                return Path(chart_file_str)
        except Exception as e:
            logger.error(f"[REPORT_SERVICE] Error generating equity chart: {e}")
        return None

    def get_coin_chart_path(self, symbol: str, universe_id: Optional[str] = None) -> Optional[Path]:
        """Генерирует график баланса по конкретной монете и возвращает путь к файлу."""
        try:
            chart_file_str = generate_balance_curve(symbol=symbol, universe_id=universe_id)
            if chart_file_str and Path(chart_file_str).exists():
                return Path(chart_file_str)
        except Exception as e:
            logger.error(f"[REPORT_SERVICE] Error generating coin chart for {symbol}: {e}")
        return None

    def get_ledger_file_path(self, universe_id: Optional[str] = None) -> Optional[Path]:
        """Возвращает путь к текстовому леджеру сделок, если он существует и не пуст."""
        if universe_id:
            cand = self.ledgers_dir / f"trades_ledger_{universe_id}.txt"
            if cand.exists() and cand.stat().st_size > 60:
                return cand
            matches = list(self.ledgers_dir.glob(f"trades_ledger_{universe_id}*.txt"))
            found = [m for m in matches if m.stat().st_size > 60]
            if found:
                return found[0]

        general = self.ledgers_dir / "trades_ledger.txt"
        if general.exists() and general.stat().st_size > 60:
            return general
        return None

    @staticmethod
    def format_trade_velocity_block(vel_dict: Optional[Dict[str, Any]], title: str = "Trade Velocity (Частота сделок)") -> str:
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

    def render_universe_analytics_card(self, current_uid: str, univ_name: str) -> Tuple[str, List[List[Dict[str, str]]]]:
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

        analytics_path = self.stats_dir / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            matches = list(self.stats_dir.glob(f"analytics_{current_uid}*.json"))
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
                logger.error(f"[REPORT_SERVICE] Error reading analytics for {current_uid}: {e}")

        if not dome_vel and self.analytics_manager and hasattr(self.analytics_manager, "get_trade_velocity"):
            dome_vel = self.analytics_manager.get_trade_velocity(current_uid)

        vel_block = self.format_trade_velocity_block(dome_vel, title="⚡ Trade Velocity (Купол)")

        roi_pct = round((net_profit / start_bal * 100.0), 2) if start_bal > 0 else 0.0
        net_sign = "+" if net_profit > 0 else ""
        roi_sign = "+" if roi_pct > 0 else ""
        real_sign = "+" if realized > 0 else ""
        unreal_sign = "+" if unrealized > 0 else ""

        peel_cnt, peel_vol, rec_cnt, rec_vol = 0, 0.0, 0, 0.0
        try:
            from ANALYTICS.leaderboard import LeaderboardEngine
            lb_engine = getattr(self.analytics_manager, "leaderboard_engine", None) or LeaderboardEngine(stats_dir=self.stats_dir)
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

    def render_portfolio_big_board(self, current_uid: str, univ_name: str, active_strats: Optional[dict] = None) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Формирует HTML-текст и кнопки портфельного Биг Борда."""
        portfolio_summary = {}
        engine = getattr(self.analytics_manager, "leaderboard_engine", None)
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
                logger.error(f"[REPORT_SERVICE] Error getting portfolio summary: {e}")

        if not portfolio_summary:
            p_path = self.stats_dir / "analytics_all.json"
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
        if not port_vel and self.analytics_manager and hasattr(self.analytics_manager, "get_trade_velocity"):
            port_vel = self.analytics_manager.get_trade_velocity("default")
        vel_block = self.format_trade_velocity_block(port_vel, title="Trade Velocity (Частота сделок портфеля)")

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

    def render_full_report_text(self, current_uid: str, univ_name: str) -> str:
        """Формирует полный текстовый отчет для выгрузки в файл TXT."""
        analytics_path = self.stats_dir / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            return f"No analytics found for {current_uid}"

        try:
            data = json.loads(analytics_path.read_text(encoding="utf-8"))
            lines = [
                "=" * 50,
                f"STRATEGY DOME ANALYTICS REPORT: {univ_name} ({current_uid})",
                "=" * 50 + "\n",
            ]
            start_val = data.get("start_balance_usdt", 0)
            cur_val = data.get("cur_balance_usdt", start_val)
            peak_val = data.get("peak_balance_usdt", cur_val)
            min_val = data.get("min_balance_usdt", cur_val)

            lines.append("Balances:")
            lines.append(f"  Start: {start_val:,.2f} USDT")
            lines.append(f"  Current: {cur_val:,.2f} USDT")
            lines.append(f"  Peak: {peak_val:,.2f} USDT")
            lines.append(f"  Min (Bottom): {min_val:,.2f} USDT\n")

            per_coin_dict = data.get("per_coin", {})
            realized = float(data.get("realized_pnl_usdt", 0))
            realized_net = float(data.get("realized_pnl_net_usdt", realized))
            comm = float(data.get("total_commission_usdt", 0))
            fund = float(data.get("total_funding_usdt", 0))
            if comm == 0.0 and per_coin_dict:
                comm = sum(float(c.get("commission_usdt", 0.0)) for c in per_coin_dict.values())
            if fund == 0.0 and per_coin_dict:
                fund = sum(float(c.get("funding_usdt", 0.0)) for c in per_coin_dict.values())

            lines.append("PnL & Performance:")
            lines.append(f"  Net Profit: {data.get('net_profit_usdt', 0):+,.2f} USDT")
            lines.append(f"  Realized Net: {realized_net:+,.2f} USDT")
            lines.append(f"  Floating (Unrealized): {data.get('unrealized_pnl_usdt', 0):+,.2f} USDT")
            lines.append(f"  Commission: {comm:,.2f} USDT")
            lines.append(f"  Funding: {fund:,.2f} USDT")
            lines.append(f"  Max Drawdown: {data.get('max_drawdown_usdt', 0):,.2f} USDT")
            lines.append(f"  Recovery Factor: {data.get('recovery_factor', 0):.2f}\n")

            lines.append("Trading Activity:")
            lines.append(f"  Total Trades: {data.get('total_trades', 0)}")
            lines.append(f"  Winning Trades: {data.get('winning_trades', 0)}")
            lines.append(f"  Iteration: #{data.get('iteration_index', 1)}\n")

            lines.append("=" * 50)
            lines.append("PER-COIN BREAKDOWN")
            lines.append("=" * 50)
            for sym, c in sorted(per_coin_dict.items(), key=lambda x: float(x[1].get("net_profit_usdt", 0)), reverse=True):
                lines.append(f"[{sym}]")
                lines.append(f"  Net Profit: {c.get('net_profit_usdt', 0):+,.2f} USDT")
                lines.append(f"  Trades: {c.get('total_trades', 0)}")
                lines.append(f"  Max DD: {c.get('max_drawdown', 0):,.2f} USDT")
                lines.append(f"  Recovery: {c.get('recovery_factor', 0):.2f}\n")

            return "\n".join(lines)
        except Exception as e:
            return f"Error building full report for {current_uid}: {e}"

    def render_ranking_report(
        self, current_uid: str, univ_name: str, criterion: str = "net", active_symbols: Optional[List[str]] = None
    ) -> Tuple[List[str], List[List[Dict[str, str]]]]:
        """Формирует отчет рейтинга монет по выбранному критерию (net, rec, abs_rec)."""
        clean_name = html.escape(univ_name)
        analytics_path = self.stats_dir / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            matches = list(self.stats_dir.glob(f"analytics_{current_uid}*.json"))
            if matches:
                analytics_path = sorted(matches)[0]

        if not analytics_path.exists():
            return [f"Analytics JSON для купола <code>{current_uid}</code> не найден."], []

        try:
            data = json.loads(analytics_path.read_text(encoding="utf-8"))
            per_coin = dict(data.get("per_coin", {}))

            if active_symbols:
                for s_sym in active_symbols:
                    per_coin.setdefault(
                        s_sym,
                        {
                            "net_profit_usdt": 0.0,
                            "realized_pnl_net_usdt": 0.0,
                            "recovery_factor": 0.0,
                            "abs_recovery_factor": 0.0,
                            "total_trades": 0,
                        },
                    )

            if not per_coin:
                return [f"Нет данных по монетам для купола <code>{current_uid}</code>."], []

            def _sf(v, d=0.0):
                try:
                    return float(v) if v is not None else d
                except (ValueError, TypeError):
                    return d

            if criterion == "rec":
                sorted_coins = sorted(per_coin.items(), key=lambda x: _sf(x[1].get("recovery_factor", -float("inf"))), reverse=True)
                title = "по Recovery Factor (Тек) (больше = лучше)"
            elif criterion == "abs_rec":
                sorted_coins = sorted(per_coin.items(), key=lambda x: _sf(x[1].get("abs_recovery_factor", -float("inf"))), reverse=True)
                title = "по Abs Recovery (Пик/DD) (больше = лучше)"
            else:
                criterion = "net"
                sorted_coins = sorted(per_coin.items(), key=lambda x: _sf(x[1].get("net_profit_usdt", -float("inf"))), reverse=True)
                title = "по Net Profit (больше = лучше)"

            strat_cfg = _CFG.get("active_strategies", {}).get(current_uid, {}) if isinstance(_CFG, dict) else {}
            cfg_start = float(strat_cfg.get("start_balance_usdt", ANALYTICS_START_BALANCE_USDT)) if isinstance(strat_cfg, dict) else float(ANALYTICS_START_BALANCE_USDT)
            st_bal = _sf(data.get("start_balance_usdt", cfg_start))

            lines = [f"<b>🏆 Рейтинг монет [Купол: {clean_name}] ({title})</b>\n"]
            for i, (sym, cdata) in enumerate(sorted_coins, 1):
                if criterion == "rec":
                    rec = _sf(cdata.get("recovery_factor", 0.0))
                    net = _sf(cdata.get("net_profit_usdt", 0.0))
                    dd = abs(_sf(cdata.get("max_drawdown_net_usdt", cdata.get("max_drawdown", 0.0))))
                    val_str = f"Rec: <b>{rec:.2f}</b> <i>(Net: {net:+.2f} / |DD|: {dd:.2f})</i>"
                elif criterion == "abs_rec":
                    abs_rec = _sf(cdata.get("abs_recovery_factor", 0.0))
                    peak = _sf(cdata.get("peak_net_profit_usdt", max(0.0, _sf(cdata.get("net_profit_usdt", 0.0)))))
                    dd = abs(_sf(cdata.get("max_drawdown_net_usdt", cdata.get("max_drawdown", 0.0))))
                    val_str = f"Abs Rec: <b>{abs_rec:.2f}</b> <i>(Пик Net: {peak:+.2f} / |DD|: {dd:.2f})</i>"
                else:
                    net_profit = round(_sf(cdata.get("net_profit_usdt", 0)), 2)
                    realized_net = round(_sf(cdata.get("realized_pnl_net_usdt", 0)), 2)
                    tr = cdata.get("total_trades", cdata.get("trades", 0))
                    c_roi = round(net_profit / st_bal * 100.0, 2) if st_bal > 0 else 0.0
                    c_roi_sign = "+" if c_roi > 0 else ""
                    val_str = f"{net_profit:+.2f} USDT ({c_roi_sign}{c_roi:.2f}% ROI) <i>({tr} сд., Realized: {realized_net:+.2f})</i>"

                is_act = cdata.get("is_active_symbol", cdata.get("is_active", True))
                active_badge = "" if is_act else " ⏸<i>[неактивна]</i>"
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "▪️"

                coin_peel = cdata.get("peeling", {})
                coin_rec = cdata.get("cross_recycling", {})
                p_loss = abs(_sf(coin_peel.get("peel_loss_usdt", 0.0)))
                r_loss = abs(_sf(coin_rec.get("loss_absorbed_usdt", coin_rec.get("profit_spent_usdt", 0.0))))
                realized_net = round(_sf(cdata.get("realized_pnl_net_usdt", 0)), 2)
                gross_tp = _sf(cdata.get("gross_realized_usdt", 0.0))
                if gross_tp <= 0.0:
                    gross_tp = realized_net + p_loss + r_loss
                gross_tp = max(0.0, round(gross_tp, 2))

                lines.append(f"{medal} <b>{sym}</b>{active_badge}: {val_str}")
                lines.append(f"   └ 🛡️ TP: <b>+{gross_tp:.2f}$</b> | Пилинг: <b>-{p_loss:.2f}$</b> | Ресайкл: <b>-{r_loss:.2f}$</b>")

            switch_buttons = [
                [
                    {"text": "Net Profit", "callback_data": f"arank:net:{current_uid}:s"},
                    {"text": "Recovery (Тек)", "callback_data": f"arank:rec:{current_uid}:s"},
                    {"text": "Abs Recovery", "callback_data": f"arank:abs_rec:{current_uid}:s"},
                ],
                [{"text": "❌ Закрыть рейтинг", "callback_data": "delete_ranking_msg"}],
            ]

            messages = []
            curr = ""
            for line in lines:
                if len(curr) + len(line) + 1 > 4000:
                    messages.append(curr)
                    curr = line + "\n"
                else:
                    curr += line + "\n"
            if curr:
                messages.append(curr)

            return messages, switch_buttons
        except Exception as e:
            logger.error(f"[REPORT_SERVICE] Error generating ranking for {current_uid}: {e}")
            return [f"⚠️ Ошибка отображения рейтинга монет: {e}"], []

    def render_coin_detail_report(self, current_uid: str, univ_name: str, symbol: str, is_active: bool) -> Optional[str]:
        """Формирует текстовый отчет детализации по конкретной монете."""
        clean_name = html.escape(univ_name)
        status_text = "🟢 Активна" if is_active else "🔴 Отключена"
        analytics_path = self.stats_dir / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            matches = list(self.stats_dir.glob(f"analytics_{current_uid}*.json"))
            if matches:
                analytics_path = sorted(matches)[0]

        if not analytics_path.exists():
            return None

        try:
            data = json.loads(analytics_path.read_text(encoding="utf-8"))
            per_coin = data.get("per_coin", {})
            if symbol not in per_coin:
                return None
            cdata = per_coin[symbol]

            net_profit = float(cdata.get("net_profit_usdt", 0.0))
            realized = float(cdata.get("realized_pnl_usdt", 0.0))
            realized_net = float(cdata.get("realized_pnl_net_usdt", realized))
            max_np = float(cdata.get("max_net_profit", net_profit))
            min_np = float(cdata.get("min_net_profit", net_profit))
            cur_dd = float(cdata.get("current_drawdown", 0.0))
            max_dd = float(cdata.get("max_drawdown", 0.0))
            trades = int(cdata.get("total_trades", cdata.get("trades", 0)))
            wins = int(cdata.get("winning_trades", cdata.get("win_count", 0)))
            wr = float(cdata.get("winrate_pct", 0.0))
            comm = float(cdata.get("commission_usdt", 0.0))
            fund = float(cdata.get("funding_usdt", 0.0))
            gload = float(cdata.get("current_grid_load_pct", 0.0))
            mused = float(cdata.get("current_margin_usdt", 0.0))

            strat_cfg = _CFG.get("active_strategies", {}).get(current_uid, {}) if isinstance(_CFG, dict) else {}
            cfg_start = float(strat_cfg.get("start_balance_usdt", ANALYTICS_START_BALANCE_USDT)) if isinstance(strat_cfg, dict) else float(ANALYTICS_START_BALANCE_USDT)
            start_bal_u = float(data.get("start_balance_usdt", cfg_start))

            coin_roi_pct = round((net_profit / start_bal_u * 100.0), 2) if start_bal_u > 0 else 0.0
            coin_roi_sign = "+" if coin_roi_pct > 0 else ""

            return (
                f"🪙 <b>ДЕТАЛИЗАЦИЯ БАЛАНСОВ: {symbol}</b> ({status_text})\n"
                f"🌌 <b>{clean_name}</b> (<code>{current_uid}</code>)\n\n"
                f"💰 <b>БАЛАНСЫ И PnL:</b>\n"
                f"  • <b>Текущий PnL (Floating):</b> <code>{cur_dd:+.2f} USDT</code>\n"
                f"  • <b>Пиковый Net Profit:</b> <code>{max_np:+.2f} USDT</code>\n"
                f"  • <b>Самый низкий Net Profit:</b> <code>{min_np:+.2f} USDT</code>\n"
                f"  • <b>Исторический Max Drawdown:</b> <code>{max_dd:.2f} USDT</code>\n"
                f"  • <b>Накопленный Net Profit:</b> <code>{net_profit:+.2f} USDT</code> ({coin_roi_sign}{coin_roi_pct:.2f}% ROI)\n"
                f"  • <b>Realized PnL (Net):</b> <code>{realized_net:+.2f} USDT</code> (Gross: <code>{realized:+.2f} $</code>)\n\n"
                f"📊 <b>ТОРГОВЫЕ МЕТРИКИ:</b>\n"
                f"  • Сделок: <b>{trades}</b> (Побед: {wins} | {wr:.1f}%)\n"
                f"  • Grid Load: <b>{gload:.1f}%</b> | Margin Used: <b>{mused:.2f} USDT</b>\n"
                f"  • Комиссии / Funding: Comm <b>{comm:.4f} $</b> | Fund <b>{fund:.4f} $</b>\n"
            )
        except Exception as e:
            logger.error(f"[REPORT_SERVICE] Error reading per_coin for {symbol}: {e}")
            return None

    def render_coin_menu(
        self, current_uid: str, univ_name: str, active_symbols: List[str]
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Формирует меню выбора монеты купола со статусами активности."""
        clean_name = html.escape(univ_name)
        universe_coins = set(active_symbols)

        analytics_path = self.stats_dir / f"analytics_{current_uid}.json"
        if analytics_path.exists():
            try:
                data = json.loads(analytics_path.read_text(encoding="utf-8"))
                if "per_coin" in data:
                    universe_coins.update(data["per_coin"].keys())
            except Exception:
                pass

        if not universe_coins:
            return f"❌ Нет данных о монетах для аналитики вселенной <code>{current_uid}</code>.", []

        buttons = []
        row = []
        for coin in sorted(universe_coins):
            is_active = coin in active_symbols
            status_icon = "🟢" if is_active else "⏸"
            status_tag = "" if is_active else " [неактивна]"
            row.append({"text": f"🪙 {coin}{status_tag} {status_icon}", "callback_data": f"analytics_coin:{coin}:{current_uid}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([{"text": "🔙 Назад", "callback_data": f"analytics_dome:{current_uid}"}])

        text = (
            f"🪙 <b>Монеты вселенной:</b> {clean_name} (<code>{current_uid}</code>)\n"
            f"Выберите монету для детальной аналитики баланса:"
        )
        return text, buttons

    SIZING_MODE_BADGES = {
        "static": ("⚡ Static (50/50)", "Фиксированный сайзинг 50/50 (макро-тренд выключен)"),
        "dyn_ema_16": ("📈 Dyn EMA 16", "EMA Trend 4h 10/30 (шкала 16, коридор 33.3%..66.7%)"),
    }

    CLOSER_MODE_BADGES = {
        "classic": ("🎯 Classic", "Фибоначчи кумулятивный трейлинг-тейк"),
        "fixed": ("📌 Fixed", "Плоский тейк-профит по корзине"),
        "toxic": ("☣️ Toxic", "Институциональный динамический токсик-выход"),
        "off": ("⏹ Off", "Авто-закрытие отключено"),
    }

    def render_universes_list(
        self,
        current_uid: str,
        active_strats: Dict[str, Any],
        page: int = 1,
        page_size: int = 6,
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """
        Формирует презентационный текст и структуру кнопок для страницы списка вселенных.
        """
        engine = getattr(self.analytics_manager, "leaderboard_engine", None)
        if not engine:
            from ANALYTICS.leaderboard import LeaderboardEngine
            engine = LeaderboardEngine(analytics_dir=ANALYTICS_DIR, stats_dir=self.stats_dir)

        universes_meta = {}
        for sid, scfg in active_strats.items():
            raw_meta = getattr(scfg, "raw_meta", {}) if not isinstance(scfg, dict) else scfg
            name = getattr(scfg, "name", sid) if not isinstance(scfg, dict) else scfg.get("name", sid)
            universes_meta[sid] = {**raw_meta, "name": name, "enabled": True}

        entries = engine.generate_leaderboard(universes_meta) if engine else []
        if not entries:
            entries = [
                {
                    "universe_id": sid,
                    "name": getattr(scfg, "name", sid) if not isinstance(scfg, dict) else scfg.get("name", sid),
                    "net_profit_usdt": 0.0,
                    "realized_pnl_usdt": 0.0,
                    "unrealized_pnl_usdt": 0.0,
                    "max_drawdown_usdt": 0.0,
                    "total_trades": 0,
                }
                for sid, scfg in active_strats.items()
            ]

        total_entries = len(entries)
        total_pages = max(1, (total_entries + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))

        start_i = (page - 1) * page_size
        end_i = min(start_i + page_size, total_entries)
        page_entries = entries[start_i:end_i]

        text_lines = [
            f"🌌 <b>СПИСОК ВСЕЛЕННЫХ (Стр. {page}/{total_pages})</b>",
            f"Нажмите на купол для настройки сайзинга и автозакрытия.\n",
        ]

        buttons = []
        row_buttons = []
        for item in page_entries:
            uid = item["universe_id"]
            scfg = active_strats.get(uid)
            sz_mode = (
                getattr(scfg, "sizing_mode", "static")
                if not isinstance(scfg, dict)
                else scfg.get("sizing_mode", "static")
            )
            cl_mode = (
                getattr(scfg, "mode", "classic")
                if not isinstance(scfg, dict)
                else scfg.get("mode", "classic")
            )
            badge = f"[{str(sz_mode).upper()}]"

            mark = "👉 " if uid == current_uid else ""
            net_str = f"{item.get('net_profit_usdt', 0.0):+.2f}$"
            text_lines.append(
                f"{mark}<code>{uid}</code>: <b>Net: {net_str}</b> | Sizing: <code>{sz_mode}</code> | Mode: <code>{cl_mode}</code>"
            )
            btn_label = f"⚙️ {str(uid)[:18]} {badge}"
            row_buttons.append({"text": btn_label, "callback_data": f"use_univ:{uid}:{page}"})
            if len(row_buttons) == 2:
                buttons.append(row_buttons)
                row_buttons = []

        if row_buttons:
            buttons.append(row_buttons)

        # Строка навигации пагинации
        nav_row = []
        if page > 1:
            nav_row.append({"text": "⬅️ Назад", "callback_data": f"univ_page:{page - 1}"})
        nav_row.append({"text": f"📄 {page}/{total_pages}", "callback_data": "univ_page:noop"})
        if page < total_pages:
            nav_row.append({"text": "Вперед ➡️", "callback_data": f"univ_page:{page + 1}"})

        buttons.append(nav_row)
        buttons.append([{"text": "🔙 Назад к аналитике", "callback_data": "analytics_back_to_univ"}])
        return "\n".join(text_lines), buttons

    @classmethod
    def render_universe_control_panel(
        cls,
        uid: str,
        s_cfg: Dict[str, Any],
        page: int = 1,
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """
        Формирует презентационный текст и кнопки детальной панели купола.
        """
        cur_sizing = str(s_cfg.get("trend_size_depender") or s_cfg.get("sizing_mode", "static")).lower().strip()
        cur_closer = str(s_cfg.get("mode", "classic")).lower().strip()
        raw_inv = (
            (s_cfg.get("LONG") or {}).get("invest_size")
            or (s_cfg.get("SHORT") or {}).get("invest_size")
            or s_cfg.get("invest_size")
        )
        def_inv = (
            (s_cfg.get("LONG") or {}).get("default_invest_size")
            or (s_cfg.get("SHORT") or {}).get("default_invest_size")
            or s_cfg.get("default_invest_size")
        )
        symbols = s_cfg.get("symbols")
        if not symbols:
            if isinstance(raw_inv, dict):
                symbols = list(raw_inv.keys())
            else:
                symbols = []
        sym_str = ", ".join(symbols) if symbols else "нет"

        if isinstance(raw_inv, dict):
            inv_display = ", ".join(f"{k}: {float(v):.0f}$" for k, v in raw_inv.items())
            if def_inv is not None:
                inv_display += f" (дефолт: {float(def_inv):.0f}$)"
        elif raw_inv is not None:
            inv_display = f"{float(raw_inv):.1f} USDT"
        elif def_inv is not None:
            inv_display = f"{float(def_inv):.1f} USDT (дефолт)"
        else:
            inv_display = "Не задан (ошибка)"

        sz_badge, sz_desc = cls.SIZING_MODE_BADGES.get(cur_sizing, (cur_sizing, ""))
        cl_badge, cl_desc = cls.CLOSER_MODE_BADGES.get(cur_closer, (cur_closer, ""))

        text = (
            f"🐆 <b>УПРАВЛЕНИЕ КУПОЛОМ:</b> <code>{uid}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Инвест-сайз:</b> <code>{inv_display}</code>\n"
            f"🎯 <b>Пул монет ({len(symbols)}):</b> <code>{sym_str}</code>\n\n"
            f"⚖️ <b>Сайзинг (trend_size_depender):</b>\n"
            f"• Текущий режим: <b>{sz_badge}</b>\n"
            f"• Описание: <i>{sz_desc}</i>\n\n"
            f"⚙️ <b>Режим автозакрытия (auto-closer):</b>\n"
            f"• Текущий режим: <b>{cl_badge}</b>\n"
            f"• Описание: <i>{cl_desc}</i>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Выберите кнопку ниже для мгновенного переключения:"
        )

        buttons = []
        sz_row = []
        for mode_key in ("static", "dyn_ema_16"):
            label, _ = cls.SIZING_MODE_BADGES[mode_key]
            prefix = "✅ " if mode_key == cur_sizing else ""
            sz_row.append({"text": f"{prefix}{label}", "callback_data": f"set_sizing:{uid}:{mode_key}:{page}"})
        buttons.append(sz_row)

        cl_row = []
        for cl_key in ("classic", "fixed", "toxic", "off"):
            label, _ = cls.CLOSER_MODE_BADGES[cl_key]
            prefix = "✅ " if cl_key == cur_closer else ""
            cl_row.append({"text": f"{prefix}{label[:9]}", "callback_data": f"set_closer:{uid}:{cl_key}:{page}"})
        buttons.append(cl_row)

        buttons.append([
            {"text": "🔄 Обновить", "callback_data": f"manage_univ:{uid}:{page}"},
            {"text": "🔙 К списку вселенных", "callback_data": f"back_to_univ_list:{page}"}
        ])
        return text, buttons

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
