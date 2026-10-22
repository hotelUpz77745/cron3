# ============================================================
# FILE: ANALYTICS/leaderboard.py
# ROLE: Leaderboard engine, portfolio aggregator, and performance rankings
# ============================================================

import json
import os
import time
from pathlib import Path
import csv
from typing import Dict, List, Any, Optional

from consts import ANALYTICS_DIR, ANALYTICS_STATS_DIR, ANALYTICS_LEDGERS_DIR
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("Leaderboard")


class LeaderboardEngine:
    """
    Агрегатор и витрина лидерборда для параллельных стратегий-шаблонов (вселенных).
    Рассчитывает сравнительную эффективность (Net Profit, Max DD, Recovery Factor, Winrate).
    """

    MODE_BADGES = {
        "trailing_ratchet": "[RATCHET]",
        "ratchet": "[RATCHET]",
        "toxicity_adaptive": "[TOXIC]",
        "toxic": "[TOXIC]",
        "fixed_net": "[FIXED]",
        "fixed": "[FIXED]",
        "alpha_rotation": "[ALPHA]",
        "alpha": "[ALPHA]",
        "disabled": "[OFF]",
        "off": "[OFF]",
        "no_autoclose": "[OFF]",
        "none": "[OFF]",
    }

    def __init__(self, analytics_dir: Optional[Path] = None, stats_dir: Optional[Path] = None):
        self.analytics_dir = analytics_dir or ANALYTICS_DIR
        self.stats_dir = stats_dir or ANALYTICS_STATS_DIR
        self.ledgers_dir = ANALYTICS_LEDGERS_DIR
        self.leaderboard_file = self.analytics_dir / "leaderboard.txt"
        self.portfolio_file = self.stats_dir / "analytics_all.json"

    def read_universe_analytics(self, universe_id: str) -> Optional[dict]:
        """Читает JSON-файл аналитики конкретной стратегии/вселенной из STATS/."""
        if universe_id == "default":
            file_path = self.stats_dir / "analytics.json"
        else:
            file_path = self.stats_dir / f"analytics_{universe_id}.json"

        if not file_path.exists():
            return None

        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to read analytics for universe {universe_id}: {e}")
            return None

    def calculate_universe_summary(
        self, universe_id: str, meta: Optional[dict] = None, analytics_data: Optional[dict] = None
    ) -> dict:
        """Формирует срез показателей для одной стратегии (из памяти или с диска)."""
        meta = meta or {}
        data = analytics_data if analytics_data is not None else (self.read_universe_analytics(universe_id) or {})

        meta_start = meta.get("start_balance_usdt")
        total_trades_raw = int(data.get("total_trades", 0))
        if meta_start is not None and (total_trades_raw == 0 or "start_balance_usdt" not in data):
            start_bal = float(meta_start)
            cur_bal = start_bal + float(data.get("net_profit_usdt", 0.0))
        else:
            start_bal = float(data.get("start_balance_usdt", meta_start or 150.0))
            cur_bal = float(data.get("cur_balance_usdt", start_bal))
        net_profit = float(data.get("net_profit_usdt", 0.0))
        realized_pnl_gross = float(data.get("realized_pnl_usdt", 0.0))
        realized_pnl = float(data.get("realized_pnl_net_usdt", realized_pnl_gross))
        unrealized_pnl = float(data.get("unrealized_pnl_usdt", 0.0))
        max_dd_usdt = float(data.get("max_drawdown_usdt", 0.0))
        max_dd_pct = float(data.get("max_drawdown_pct", 0.0))
        total_trades = int(data.get("total_trades", 0))
        winning_trades = int(data.get("winning_trades", 0))

        # Peak Net Profit (максимальный за историю Нет профит текущей отработки)
        peak_bal = float(data.get("peak_balance_usdt", max(start_bal, cur_bal)))
        min_bal = float(data.get("min_balance_usdt", min(start_bal, cur_bal)))
        perf_usdt = float(data.get("performance_usdt", 0.0))
        saved_peak_net = float(data.get("peak_net_profit_usdt", 0.0))
        peak_net_profit = max(net_profit, round(peak_bal - start_bal, 2), round(perf_usdt, 2), saved_peak_net)

        peak_age_h = float(data.get("peak_age_hours", 0.0))
        net_vel = float(data.get("net_velocity", 0.0))

        winrate_pct = round((winning_trades / total_trades * 100.0), 2) if total_trades > 0 else 0.0
        roi_pct = round(((cur_bal - start_bal) / start_bal * 100.0), 2) if start_bal > 0 else 0.0

        # Recovery Factor = Net Profit / |Max Drawdown|
        if max_dd_usdt < 0:
            rec_factor = round(net_profit / abs(max_dd_usdt), 2)
        else:
            rec_factor = round(net_profit / 1.0, 2) if net_profit > 0 else 0.0

        iteration_index = int(data.get("iteration_index", 1))
        iteration_net = round(float(data.get("iteration_net_profit_usdt", net_profit)), 2)
        iteration_realized = round(float(data.get("iteration_realized_pnl_usdt", realized_pnl)), 2)

        # Определение режима автозакрытия стратегии (4 режима: trailing_ratchet, toxic, fixed, off)
        # Суффикс имени вселенной является высшим приоритетом
        if universe_id.endswith("__off") or "__off" in universe_id:
            mode = "off"
            mode_badge = "[OFF]"
        elif universe_id.endswith("__trailing_ratchet") or "__trailing_ratchet" in universe_id:
            mode = "trailing_ratchet"
            mode_badge = "[RATCHET]"
        elif universe_id.endswith("__toxic") or "__toxic" in universe_id:
            mode = "toxic"
            mode_badge = "[TOXIC]"
        elif universe_id.endswith("__fixed") or "__fixed" in universe_id:
            mode = "fixed"
            target_val = (
                ((meta.get("auto_closing") or {}).get("positive") or {}).get("fixed_net", {}).get("target_net_usdt")
                or ((meta.get("override") or meta.get("overrides") or {}).get("auto_closing", {}).get("positive", {}).get("fixed_net", {}).get("target_net_usdt"))
            )
            mode_badge = f"[FIXED ${float(target_val):g}]" if target_val else "[FIXED]"
        else:
            # Чтение режима из конфигурации стратегии (без скрытых дефолтов в toxic)
            raw_mode = (
                meta.get("auto_closing_mode")
                or (meta.get("auto_closing") or {}).get("mode")
                or ((meta.get("auto_closing") or {}).get("positive") or {}).get("mode")
                or (meta.get("overrides", {}).get("auto_closing") or {}).get("mode")
                or ((meta.get("overrides", {}).get("auto_closing") or {}).get("positive") or {}).get("mode")
            )
            raw_mode_str = str(raw_mode).lower().strip() if raw_mode else "off"
            if raw_mode_str in ("trailing_ratchet", "ratchet"):
                mode = "trailing_ratchet"
                mode_badge = "[RATCHET]"
            elif raw_mode_str in ("toxic", "toxicity_adaptive"):
                mode = "toxic"
                mode_badge = "[TOXIC]"
            elif raw_mode_str in ("fixed", "fixed_net"):
                mode = "fixed"
                target_val = (
                    ((meta.get("auto_closing") or {}).get("positive") or {}).get("fixed_net", {}).get("target_net_usdt")
                    or ((meta.get("override") or meta.get("overrides") or {}).get("auto_closing", {}).get("positive", {}).get("fixed_net", {}).get("target_net_usdt"))
                )
                mode_badge = f"[FIXED ${float(target_val):g}]" if target_val else "[FIXED]"
            else:
                mode = "off"
                mode_badge = "[OFF]"

        tsd_val = meta.get("trend_size_depender")
        tsd_mode = tsd_val if isinstance(tsd_val, str) else (tsd_val.get("mode") if isinstance(tsd_val, dict) else None)
        raw_sm = (
            meta.get("sizing_mode")
            or tsd_mode
            or meta.get("trend_size_mode")
        )
        sm_str = str(raw_sm).lower().strip() if raw_sm else "static"
        if sm_str in ("dyn_inv_16", "dyn_bb", "dyn_bb_inv"):
            sizing_mode = "dyn_inv_16"
            sizing_badge = "[INV_16]"
        elif sm_str in ("dyn_ema_16", "dyn_ema_trend"):
            sizing_mode = "dyn_ema_16"
            sizing_badge = "[EMA_16]"
        elif sm_str == "dyn_ema_10":
            sizing_mode = "dyn_ema_10"
            sizing_badge = "[EMA_10]"
        else:
            sizing_mode = "static"
            sizing_badge = "[STATIC]"

        # Источник истины для Toxic Defense: Ledger-First подход
        peel_events, peel_vol, peel_pnl = 0, 0.0, 0.0
        rec_events, rec_vol, rec_loss, rec_spent = 0, 0.0, 0.0, 0.0

        strat_peel_txt = self.ledgers_dir / f"peels_ledger_{universe_id}.txt"
        if not strat_peel_txt.exists():
            strat_peel_txt = self.ledgers_dir / "peels_ledger.txt"

        strat_rec_txt = self.ledgers_dir / f"recycling_ledger_{universe_id}.txt"
        if not strat_rec_txt.exists():
            strat_rec_txt = self.ledgers_dir / "recycling_ledger.txt"

        if strat_peel_txt.exists():
            try:
                with open(strat_peel_txt, "r", encoding="utf-8") as pf:
                    r = csv.reader(pf, delimiter=';')
                    next(r, None)
                    for row in r:
                        if len(row) >= 12:
                            if strat_peel_txt.name == "peels_ledger.txt" and len(row) > 2:
                                if row[2] and row[2] != universe_id and universe_id != "default":
                                    continue
                            peel_events += 1
                            peel_vol += float(row[10])
                            peel_pnl += float(row[11])
            except Exception:
                pass

        if strat_rec_txt.exists():
            try:
                with open(strat_rec_txt, "r", encoding="utf-8") as rf:
                    r = csv.reader(rf, delimiter=';')
                    next(r, None)
                    for row in r:
                        if len(row) >= 13:
                            if strat_rec_txt.name == "recycling_ledger.txt" and len(row) > 2:
                                if row[2] and row[2] != universe_id and universe_id != "default":
                                    continue
                            rec_events += 1
                            rec_vol += float(row[10])
                            rec_loss += float(row[11])
                            rec_spent += float(row[12])
            except Exception:
                pass

        # Фолбэк на in-memory / json toxic_defense, если леджеры пусты
        toxic_def = data.get("toxic_defense", {})
        if peel_events == 0:
            peel_def = toxic_def.get("subgrid_peeling", {})
            peel_events = int(peel_def.get("events_count", 0))
            peel_vol = float(peel_def.get("total_peeled_usdt", 0.0))
            peel_pnl = float(peel_def.get("locked_micro_pnl_usdt", 0.0))

        if rec_events == 0:
            rec_def = toxic_def.get("cross_recycling", {})
            rec_events = int(rec_def.get("events_count", 0))
            rec_vol = float(rec_def.get("burned_volume_usdt", 0.0))
            rec_loss = float(rec_def.get("loss_absorbed_usdt", 0.0))
            rec_spent = float(rec_def.get("profit_spent_usdt", 0.0))

        toxic_def = {
            "subgrid_peeling": {
                "events_count": peel_events,
                "total_peeled_usdt": round(peel_vol, 4),
                "loss_absorbed_usdt": round(peel_pnl, 4),
                "locked_micro_pnl_usdt": round(peel_pnl, 4)
            },
            "cross_recycling": {
                "events_count": rec_events,
                "burned_volume_usdt": round(rec_vol, 4),
                "profit_spent_usdt": round(rec_spent, 4),
                "loss_absorbed_usdt": round(rec_loss, 4),
                "locked_micro_pnl_usdt": round(rec_loss, 4)
            }
        }
        def_badge = f"{peel_events}P/{rec_events}R"

        return {
            "universe_id": universe_id,
            "name": meta.get("name", universe_id),
            "description": meta.get("description", ""),
            "status": "RUNNING" if meta.get("enabled", meta.get("is_active", True)) else "PAUSED",
            "start_balance_usdt": round(start_bal, 2),
            "cur_balance_usdt": round(cur_bal, 2),
            "peak_balance_usdt": round(peak_bal, 2),
            "min_balance_usdt": round(min_bal, 2),
            "net_profit_usdt": round(net_profit, 2),
            "iteration_index": iteration_index,
            "iteration_net_profit_usdt": iteration_net,
            "iteration_realized_pnl_usdt": iteration_realized,
            "peak_net_profit_usdt": round(peak_net_profit, 2),
            "peak_age_hours": round(peak_age_h, 1),
            "net_velocity": round(net_vel, 2),
            "realized_pnl_usdt": round(realized_pnl, 2),
            "realized_pnl_gross_usdt": round(realized_pnl_gross, 2),
            "unrealized_pnl_usdt": round(unrealized_pnl, 2),
            "max_drawdown_usdt": round(max_dd_usdt, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "recovery_factor": rec_factor,
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "winrate_pct": winrate_pct,
            "roi_pct": roi_pct,
            "mode": mode,
            "mode_badge": mode_badge,
            "sizing_mode": sizing_mode,
            "sizing_badge": sizing_badge,
            "trade_velocity": data.get("trade_velocity", {}),
            "toxic_defense": toxic_def,
            "peel_events": peel_events,
            "peel_vol_usdt": round(peel_vol, 2),
            "peel_pnl_usdt": round(peel_pnl, 2),
            "rec_events": rec_events,
            "rec_burn_usdt": round(rec_vol, 2),
            "rec_loss_usdt": round(rec_loss, 2),
            "rec_spent_usdt": round(rec_spent, 2),
            "defense_badge": def_badge,
        }

    def generate_leaderboard(
        self, universes_meta: Dict[str, dict], analytics_data_map: Optional[Dict[str, dict]] = None
    ) -> List[dict]:
        """Собирает и сортирует стратегии строго по чистому кумулятивному Net Profit."""
        entries = []
        for uid, meta in universes_meta.items():
            if meta.get("enabled", meta.get("is_active", True)):
                raw_data = analytics_data_map.get(uid) if analytics_data_map else None
                summary = self.calculate_universe_summary(uid, meta, analytics_data=raw_data)
                entries.append(summary)

        # Сортировка строго по чистому кумулятивному Net Profit, затем Realized PnL и Recovery Factor
        entries.sort(key=lambda x: (x["net_profit_usdt"], x["realized_pnl_usdt"], x["recovery_factor"]), reverse=True)
        return entries

    def format_table(
        self, entries: List[dict], evaluated_coins_count: int = 0, start_rank: int = 1, title_suffix: str = ""
    ) -> str:
        """Форматирует компактную ASCII таблицу Leaderboard с динамической подгонкой ширины ID стратегии."""
        active_count = len(entries)
        has_sizing = any("sizing_badge" in item or "sizing_mode" in item for item in entries)
        title = f"STRATEGIES LEADERBOARD{title_suffix} (Active: {active_count} | Coins: {evaluated_coins_count})"

        # Рассчитываем динамическую ширину для Strategy / Universe ID
        header_title = "Strategy / Universe ID"
        max_uid_len = max([len(str(item.get("universe_id", ""))) for item in entries] + [len(header_title)]) if entries else len(header_title)
        id_width = max(len(header_title), min(38, max_uid_len + 1))

        if has_sizing:
            header_line = (
                f"{'#':<3} {header_title:<{id_width}} {'Mode':<9} {'Sizing':<9} "
                f"{'Net Profit':<12} {'Iter Net':<13} {'Realized':<11} {'Iter Real':<11} "
                f"{'Floating':<11} {'Peak Net (Age)':<16} {'Max DD':<10} {'Trades':<7} {'Rec':<6} {'Def (P/R)':<10}"
            )
        else:
            header_line = (
                f"{'#':<3} {header_title:<{id_width}} {'Mode':<9} "
                f"{'Net Profit':<12} {'Iter Net':<13} {'Realized':<11} {'Iter Real':<11} "
                f"{'Floating':<11} {'Peak Net (Age)':<16} {'Max DD':<10} {'Trades':<7} {'Rec':<6} {'Def (P/R)':<10}"
            )

        total_width = len(header_line)
        header = (
            "=" * total_width + "\n"
            f"{title}\n"
            f"Ranked strictly by Cumulative Net Profit USDT (Winrate excluded - DCA quantitative focus)\n"
            + "=" * total_width + "\n"
            + header_line + "\n"
            + "-" * total_width
        )

        rows = []
        for idx, item in enumerate(entries, start=start_rank):
            net_str = f"{item.get('net_profit_usdt', 0.0):+.2f} USDT"
            iter_idx = item.get('iteration_index', 1)
            iter_val = item.get('iteration_net_profit_usdt', item.get('net_profit_usdt', 0.0))
            iter_str = f"{iter_val:+.2f} $ (#{iter_idx})"
            realized_str = f"{item.get('realized_pnl_usdt', 0.0):+.2f} $"
            iter_real_str = f"{item.get('iteration_realized_pnl_usdt', 0.0):+.2f} $"
            unrealized_str = f"{item.get('unrealized_pnl_usdt', 0.0):+.2f} $"
            peak_val = item.get('peak_net_profit_usdt', 0.0)
            peak_age = item.get('peak_age_hours', 0.0)
            peak_net_str = f"{peak_val:+.2f} $ ({peak_age:.1f}h)" if peak_age > 0 else f"{peak_val:+.2f} $"
            dd_str = f"{item.get('max_drawdown_usdt', 0.0):.2f} $"
            rec_str = f"{item.get('recovery_factor', 0.0):.2f}"
            mode_badge = item.get("mode_badge") or (f"[{str(item.get('mode', 'OFF')).upper()}]")
            uid_short = item.get('universe_id', '')[:id_width]
            def_badge = item.get("defense_badge", "0P/0R")

            if has_sizing:
                sizing_badge = item.get("sizing_badge", "[STATIC]")
                row = (
                    f"{idx:<3} {uid_short:<{id_width}} {mode_badge:<9} {sizing_badge:<9} "
                    f"{net_str:<12} {iter_str:<13} {realized_str:<11} {iter_real_str:<11} "
                    f"{unrealized_str:<11} {peak_net_str:<16} {dd_str:<10} {item.get('total_trades', 0):<7} {rec_str:<6} {def_badge:<10}"
                )
            else:
                row = (
                    f"{idx:<3} {uid_short:<{id_width}} {mode_badge:<9} "
                    f"{net_str:<12} {iter_str:<13} {realized_str:<11} {iter_real_str:<11} "
                    f"{unrealized_str:<11} {peak_net_str:<16} {dd_str:<10} {item.get('total_trades', 0):<7} {rec_str:<6} {def_badge:<10}"
                )
            rows.append(row)

        notes_lines = []
        tot_peels = sum(item.get("peel_events", 0) for item in entries)
        tot_recs = sum(item.get("rec_events", 0) for item in entries)
        if tot_peels > 0 or tot_recs > 0:
            peel_pnl_sum = sum(item.get("peel_pnl_usdt", 0.0) for item in entries)
            rec_loss_sum = sum(item.get("rec_loss_usdt", 0.0) for item in entries)
            rec_spent_sum = sum(item.get("rec_spent_usdt", 0.0) for item in entries)
            peel_desc = f"{peel_pnl_sum:+.2f}$ cash PnL"
            rec_desc = f"{rec_loss_sum:.2f}$ cash burned ({rec_spent_sum:.2f}$ profit spent)"
            notes_lines.append(
                f"[DEFENSE BOOKKEEPING]: Subgrid Peels: {tot_peels} ({peel_desc}) | "
                f"Cross-Recycling: {tot_recs} ({rec_desc})"
            )

        footer = "=" * total_width
        all_lines = [header] + rows + [footer]
        if notes_lines:
            all_lines.extend(notes_lines + ["=" * total_width])
        return "\n".join(all_lines)

    def format_table_chunks(
        self, entries: List[dict], evaluated_coins_count: int = 0, chunk_size: int = 11, title_suffix: str = "", top_limit: Optional[int] = 32
    ) -> List[str]:
        """
        Разбивает таблицу лидерборда на порции (чанки) с сохранением заголовков колонок
        и сквозной нумерации, гарантируя непревышение лимита Telegram (4096 символов).
        По умолчанию срез ограничивается top_limit элементами (дефолт 32).
        """
        if not entries:
            return ["Таблица лидеров пуста."]

        excluded_modes = ("dyn_bb", "dyn_bb_legacy", "dyn_bb_breakout", "dyn_brk_16", "dyn_brk_10")
        clean_entries = [
            e for e in entries
            if not (
                e.get("sizing_mode") in excluded_modes
                or any(e.get("universe_id", "").endswith(f"__{m}") for m in excluded_modes)
            )
        ]

        if top_limit is not None and top_limit > 0:
            clean_entries = clean_entries[:top_limit]

        total_entries = len(clean_entries)
        if total_entries <= chunk_size:
            tbl = self.format_table(clean_entries, evaluated_coins_count=evaluated_coins_count, start_rank=1, title_suffix=title_suffix)
            if len(tbl) <= 3500:
                return [tbl]

        chunks = []
        num_chunks = (total_entries + chunk_size - 1) // chunk_size

        for chunk_idx in range(num_chunks):
            start_i = chunk_idx * chunk_size
            end_i = min(start_i + chunk_size, total_entries)
            chunk_entries = clean_entries[start_i:end_i]
            start_rank = start_i + 1
            end_rank = end_i

            tbl = self.format_table(
                chunk_entries,
                evaluated_coins_count=evaluated_coins_count,
                start_rank=start_rank,
                title_suffix=f"{title_suffix} (Part {chunk_idx + 1}/{num_chunks} | #{start_rank}-#{end_rank} of {total_entries})"
            )
            chunks.append(tbl)

        return chunks

    def save_portfolio(self, entries: List[dict], evaluated_coins_count: int = 0) -> None:
        """Сохраняет сводный json и текстовый отчет лидерборда."""
        try:
            # 1. Текстовая витрина
            table_text = self.format_table(entries, evaluated_coins_count=evaluated_coins_count)
            Utils.atomic_write_text(self.leaderboard_file, table_text)

            # 2. JSON витрина с расчетом совокупных балансов портфеля
            total_net = sum(x.get("net_profit_usdt", 0.0) for x in entries)
            total_realized = sum(x.get("realized_pnl_usdt", 0.0) for x in entries)
            total_unrealized = sum(x.get("unrealized_pnl_usdt", 0.0) for x in entries)
            total_trades = sum(x.get("total_trades", 0) for x in entries)
            total_start_bal = sum(x.get("start_balance_usdt", 0.0) for x in entries)
            total_cur_bal = total_start_bal + total_net

            prev_peak = total_cur_bal
            prev_min = total_cur_bal
            if self.portfolio_file.exists():
                try:
                    prev_data = json.loads(self.portfolio_file.read_text(encoding="utf-8"))
                    if "aggregate_peak_balance_usdt" in prev_data:
                        prev_peak = float(prev_data["aggregate_peak_balance_usdt"])
                    if "aggregate_min_balance_usdt" in prev_data:
                        prev_min = float(prev_data["aggregate_min_balance_usdt"])
                except Exception:
                    pass

            if total_trades == 0:
                agg_peak = total_start_bal
                agg_min = total_start_bal
            else:
                agg_peak = max(prev_peak, total_cur_bal, total_start_bal)
                agg_min = min(prev_min, total_cur_bal, total_start_bal)

            # Портфельный расчет Trade Velocity
            port_vel = {}
            try:
                from ANALYTICS.trade_velocity import TradeVelocityEngine
                from consts import ANALYTICS_LEDGERS_DIR
                port_ledger = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
                port_engine = TradeVelocityEngine(universe_id="default", ledger_path=port_ledger)
                port_vel = port_engine.calculate_velocity({"per_coin": {}})
            except Exception as pe:
                logger.warning(f"[LEADERBOARD] Could not compute portfolio trade velocity: {pe}")

            portfolio_payload = {
                "updated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "total_universes_active": len(entries),
                "evaluated_coins_count": evaluated_coins_count,
                "aggregate_start_balance_usdt": round(total_start_bal, 2),
                "aggregate_cur_balance_usdt": round(total_cur_bal, 2),
                "aggregate_peak_balance_usdt": round(agg_peak, 2),
                "aggregate_min_balance_usdt": round(agg_min, 2),
                "aggregate_net_profit_usdt": round(total_net, 2),
                "aggregate_realized_pnl_usdt": round(total_realized, 2),
                "aggregate_unrealized_pnl_usdt": round(total_unrealized, 2),
                "aggregate_total_trades": total_trades,
                "trade_velocity": port_vel,
                "leaderboard": entries,
            }
            Utils.atomic_write_json(self.portfolio_file, portfolio_payload)

        except Exception as e:
            logger.error(f"Error saving portfolio leaderboard: {e}")

    def get_portfolio_summary(self, entries: Optional[List[dict]] = None) -> dict:
        """Возвращает сводные агрегированные метрики портфеля из кэша или по переданным стратегиям."""
        if not entries and self.portfolio_file.exists():
            try:
                data = json.loads(self.portfolio_file.read_text(encoding="utf-8"))
                if "aggregate_start_balance_usdt" in data and float(data.get("aggregate_start_balance_usdt", 0)) > 0:
                    return data
                if "leaderboard" in data:
                    entries = data["leaderboard"]
            except Exception:
                pass

        entries = entries or []
        tot_start = sum(float(x.get("start_balance_usdt", 0.0)) for x in entries)
        tot_net = sum(float(x.get("net_profit_usdt", 0.0)) for x in entries)
        tot_real = sum(float(x.get("realized_pnl_usdt", 0.0)) for x in entries)
        tot_unreal = sum(float(x.get("unrealized_pnl_usdt", 0.0)) for x in entries)
        tot_trades = sum(int(x.get("total_trades", 0)) for x in entries)
        tot_cur = tot_start + tot_net

        prev_peak = tot_cur
        prev_min = tot_cur
        if self.portfolio_file.exists():
            try:
                prev_data = json.loads(self.portfolio_file.read_text(encoding="utf-8"))
                if "aggregate_peak_balance_usdt" in prev_data:
                    prev_peak = float(prev_data["aggregate_peak_balance_usdt"])
                if "aggregate_min_balance_usdt" in prev_data:
                    prev_min = float(prev_data["aggregate_min_balance_usdt"])
            except Exception:
                pass

        if tot_trades == 0:
            agg_peak = tot_start
            agg_min = tot_start
        else:
            agg_peak = max(prev_peak, tot_cur, tot_start)
            agg_min = min(prev_min, tot_cur, tot_start)

        return {
            "total_universes_active": len(entries),
            "aggregate_start_balance_usdt": round(tot_start, 2),
            "aggregate_cur_balance_usdt": round(tot_cur, 2),
            "aggregate_peak_balance_usdt": round(agg_peak, 2),
            "aggregate_min_balance_usdt": round(agg_min, 2),
            "aggregate_net_profit_usdt": round(tot_net, 2),
            "aggregate_realized_pnl_usdt": round(tot_real, 2),
            "aggregate_unrealized_pnl_usdt": round(tot_unreal, 2),
            "aggregate_total_trades": tot_trades,
            "leaderboard": entries,
        }
