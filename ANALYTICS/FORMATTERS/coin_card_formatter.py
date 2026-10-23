# ==============================================================================
# Path: ANALYTICS/FORMATTERS/coin_card_formatter.py
# Role: Рендеринг карточек монет, меню выбора монет и рейтингов доходности/восстановления
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

logger = UnifiedLogger("CoinCardFormatter")


class CoinCardFormatter:
    """Форматтер презентационного слоя для монетных метрик, меню и рейтингов."""

    @staticmethod
    def render_coin_detail_report(
        stats_dir: Path,
        current_uid: str,
        univ_name: str,
        symbol: str,
        is_active: bool,
    ) -> Optional[str]:
        """Формирует текстовый отчет детализации по конкретной монете."""
        clean_name = html.escape(univ_name)
        status_text = "🟢 Активна" if is_active else "🔴 Отключена"
        analytics_path = stats_dir / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            matches = list(stats_dir.glob(f"analytics_{current_uid}*.json"))
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

            rec = float(cdata.get("recovery_factor", 0.0))
            abs_rec = float(cdata.get("abs_recovery_factor", 0.0))
            avg_daily = float(cdata.get("avg_daily_profit", 0.0))
            rrr = float(cdata.get("risk_reward_ratio", 0.0))
            max_pos = float(cdata.get("max_position_size", 0.0))

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
                f"  • Recovery Factor: <b>{rec:.2f}</b> (Abs: <b>{abs_rec:.2f}</b>)\n"
                f"  • Ср. профит/день: <b>{avg_daily:+.2f} $</b> | R/R: <b>{rrr:.2f}</b> | Max Pos: <b>{max_pos:.2f} $</b>\n"
            )
        except Exception as e:
            logger.error(f"[COIN_CARD_FORMATTER] Error reading per_coin for {symbol}: {e}")
            return None

    @staticmethod
    def render_coin_menu(
        stats_dir: Path,
        current_uid: str,
        univ_name: str,
        active_symbols: List[str],
    ) -> Tuple[str, List[List[Dict[str, str]]]]:
        """Формирует меню выбора монеты купола со статусами активности."""
        clean_name = html.escape(univ_name)
        universe_coins = set(active_symbols)

        analytics_path = stats_dir / f"analytics_{current_uid}.json"
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

    @staticmethod
    def render_ranking_report(
        stats_dir: Path,
        current_uid: str,
        univ_name: str,
        criterion: str = "net",
        active_symbols: Optional[List[str]] = None,
    ) -> Tuple[List[str], List[List[Dict[str, str]]]]:
        """Формирует отчет рейтинга монет по выбранному критерию (net, rec, abs_rec)."""
        clean_name = html.escape(univ_name)
        analytics_path = stats_dir / f"analytics_{current_uid}.json"
        if not analytics_path.exists():
            matches = list(stats_dir.glob(f"analytics_{current_uid}*.json"))
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

            lines = [
                f"<b>🏆 Рейтинг монет • {clean_name}</b>",
                f"<i>Сортировка: {title}</i>",
                f"<i>💡 Realized: чистый итог (Тейки − Пилинг − Ресайкл)</i>",
                "",
            ]
            for i, (sym, cdata) in enumerate(sorted_coins, 1):
                realized_net = round(_sf(cdata.get("realized_pnl_net_usdt", 0)), 2)
                tr = cdata.get("total_trades", cdata.get("trades", 0))

                if criterion == "rec":
                    rec = _sf(cdata.get("recovery_factor", 0.0))
                    net = _sf(cdata.get("net_profit_usdt", 0.0))
                    dd = abs(_sf(cdata.get("max_drawdown_net_usdt", cdata.get("max_drawdown", 0.0))))
                    val_str = f"Rec: <b>{rec:.2f}</b> <i>(Net: {net:+.2f} / |DD|: {dd:.2f})</i> • {tr} сд. | Realized: <b>{realized_net:+.2f}$</b>"
                elif criterion == "abs_rec":
                    abs_rec = _sf(cdata.get("abs_recovery_factor", 0.0))
                    peak = _sf(cdata.get("peak_net_profit_usdt", max(0.0, _sf(cdata.get("net_profit_usdt", 0.0)))))
                    dd = abs(_sf(cdata.get("max_drawdown_net_usdt", cdata.get("max_drawdown", 0.0))))
                    val_str = f"Abs Rec: <b>{abs_rec:.2f}</b> <i>(Пик Net: {peak:+.2f} / |DD|: {dd:.2f})</i> • {tr} сд. | Realized: <b>{realized_net:+.2f}$</b>"
                else:
                    net_profit = round(_sf(cdata.get("net_profit_usdt", 0)), 2)
                    c_roi = round(net_profit / st_bal * 100.0, 2) if st_bal > 0 else 0.0
                    c_roi_sign = "+" if c_roi > 0 else ""
                    val_str = f"Net: <b>{net_profit:+.2f} USDT</b> ({c_roi_sign}{c_roi:.2f}% ROI) • {tr} сд. | Realized: <b>{realized_net:+.2f}$</b>"

                is_act = cdata.get("is_active_symbol", cdata.get("is_active", True))
                active_badge = "" if is_act else " ⏸<i>[неактивна]</i>"
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else "▪️"

                coin_peel = cdata.get("peeling", {})
                coin_rec = cdata.get("cross_recycling", {})
                p_loss = abs(_sf(coin_peel.get("peel_loss_usdt", 0.0)))
                r_loss = abs(_sf(coin_rec.get("loss_absorbed_usdt", coin_rec.get("profit_spent_usdt", 0.0))))
                # Тейки строго чистыми с учетом комиссий и фандинга (Net):
                # Realized_Net = Net_Takes - Peel_Loss - Recycle_Loss
                # => Net_Takes = Realized_Net + Peel_Loss + Recycle_Loss
                net_tp = round(realized_net + p_loss + r_loss, 2)
                tp_sign = "+" if net_tp >= 0 else ""

                lines.append(f"{medal} <b>{sym}</b>{active_badge}")
                lines.append(f"   ├ {val_str}")
                lines.append(f"   └ 🛡️ Защита: Пилинг: <b>-{p_loss:.2f}$</b> | Ресайкл: <b>-{r_loss:.2f}$</b> <i>(Тейки: {tp_sign}{net_tp:.2f}$)</i>")
                lines.append("")

            if lines and lines[-1] == "":
                lines.pop()

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
            logger.error(f"[COIN_CARD_FORMATTER] Error generating ranking for {current_uid}: {e}")
            return [f"⚠️ Ошибка отображения рейтинга монет: {e}"], []
