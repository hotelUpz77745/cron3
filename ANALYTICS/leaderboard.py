# ============================================================
# FILE: ANALYTICS/leaderboard.py
# ROLE: Leaderboard engine, portfolio aggregator, and performance rankings
# ============================================================

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Any, Optional

from consts import ANALYTICS_DIR, ANALYTICS_STATS_DIR
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("Leaderboard")


class LeaderboardEngine:
    """
    Агрегатор и витрина лидерборда для параллельных стратегий-шаблонов (вселенных).
    Рассчитывает сравнительную эффективность (Net Profit, Max DD, Recovery Factor, Winrate).
    """

    MODE_BADGES = {
        "toxicity_adaptive": "[TOXIC]",
        "toxic": "[TOXIC]",
        "iter_net_classic": "[CLASSIC]",
        "classic": "[CLASSIC]",
        "trailing_ratchet": "[CLASSIC]",
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

        start_bal = float(data.get("start_balance_usdt", meta.get("start_balance_usdt", 150.0)))
        cur_bal = float(data.get("cur_balance_usdt", start_bal))
        net_profit = float(data.get("net_profit_usdt", 0.0))
        realized_pnl = float(data.get("realized_pnl_usdt", 0.0))
        unrealized_pnl = float(data.get("unrealized_pnl_usdt", 0.0))
        max_dd_usdt = float(data.get("max_drawdown_usdt", 0.0))
        max_dd_pct = float(data.get("max_drawdown_pct", 0.0))
        total_trades = int(data.get("total_trades", 0))
        winning_trades = int(data.get("winning_trades", 0))

        # Peak Net Profit (максимальный за историю Нет профит текущей отработки)
        peak_bal = float(data.get("peak_balance_usdt", start_bal))
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

        # Определение режима автозакрытия стратегии (4 режима: classic, toxic, fixed, off)
        # Суффикс имени вселенной является высшим приоритетом
        if universe_id.endswith("__off"):
            mode = "off"
            mode_badge = "[OFF]"
        elif universe_id.endswith("__classic"):
            mode = "classic"
            mode_badge = "[CLASSIC]"
        elif universe_id.endswith("__toxic"):
            mode = "toxic"
            mode_badge = "[TOXIC]"
        elif universe_id.endswith("__fixed"):
            mode = "fixed"
            mode_badge = "[FIXED]"
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
            if raw_mode_str in ("classic", "iter_net_classic", "trailing_ratchet"):
                mode = "classic"
                mode_badge = "[CLASSIC]"
            elif raw_mode_str in ("toxic", "toxicity_adaptive"):
                mode = "toxic"
                mode_badge = "[TOXIC]"
            elif raw_mode_str in ("fixed", "fixed_net"):
                mode = "fixed"
                mode_badge = "[FIXED]"
            else:
                mode = "off"
                mode_badge = "[OFF]"

        return {
            "universe_id": universe_id,
            "name": meta.get("name", universe_id),
            "description": meta.get("description", ""),
            "status": "RUNNING" if meta.get("enabled", meta.get("is_active", True)) else "PAUSED",
            "start_balance_usdt": round(start_bal, 2),
            "cur_balance_usdt": round(cur_bal, 2),
            "net_profit_usdt": round(net_profit, 2),
            "iteration_index": iteration_index,
            "iteration_net_profit_usdt": iteration_net,
            "iteration_realized_pnl_usdt": iteration_realized,
            "peak_net_profit_usdt": round(peak_net_profit, 2),
            "peak_age_hours": round(peak_age_h, 1),
            "net_velocity": round(net_vel, 2),
            "realized_pnl_usdt": round(realized_pnl, 2),
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
        """Форматирует ASCII таблицу Leaderboard с колонкой Mode (160 симв)."""
        active_count = len(entries)
        title = f"STRATEGIES LEADERBOARD{title_suffix} (Active: {active_count} | Coins: {evaluated_coins_count})"
        header = (
            "=" * 160 + "\n"
            f"{title}\n"
            f"Ranked strictly by Cumulative Net Profit USDT (Winrate excluded - DCA quantitative focus)\n"
            + "=" * 160 + "\n"
            f"{'#':<3} {'Strategy / Universe ID':<43} {'Mode':<9} {'Net Profit':<13} {'Iter Net':<14} {'Realized':<12} {'Iter Real':<12} {'Floating':<11} {'Peak Net (Age)':<17} {'Max DD':<11} {'Trades':<8} {'Rec':<6}\n"
            + "-" * 160
        )

        rows = []
        for idx, item in enumerate(entries, start=start_rank):
            net_str = f"{item['net_profit_usdt']:+.2f} USDT"
            iter_idx = item.get('iteration_index', 1)
            iter_val = item.get('iteration_net_profit_usdt', item['net_profit_usdt'])
            iter_str = f"{iter_val:+.2f} $ (#{iter_idx})"
            realized_str = f"{item['realized_pnl_usdt']:+.2f} $"
            iter_real_str = f"{item.get('iteration_realized_pnl_usdt', 0.0):+.2f} $"
            unrealized_str = f"{item['unrealized_pnl_usdt']:+.2f} $"
            peak_val = item.get('peak_net_profit_usdt', 0.0)
            peak_age = item.get('peak_age_hours', 0.0)
            peak_net_str = f"{peak_val:+.2f} $ ({peak_age:.1f}h)" if peak_age > 0 else f"{peak_val:+.2f} $"
            dd_str = f"{item['max_drawdown_usdt']:.2f} $"
            rec_str = f"{item['recovery_factor']:.2f}"
            uid_short = item['universe_id'][:42]
            mode_badge = item.get("mode_badge", "[OFF]")

            row = (
                f"{idx:<3} {uid_short:<43} {mode_badge:<9} {net_str:<13} {iter_str:<14} {realized_str:<12} {iter_real_str:<12} "
                f"{unrealized_str:<11} {peak_net_str:<17} {dd_str:<11} {item['total_trades']:<8} {rec_str:<6}"
            )
            rows.append(row)

        footer = "=" * 160
        return "\n".join([header] + rows + [footer])

    def format_table_chunks(
        self, entries: List[dict], evaluated_coins_count: int = 0, chunk_size: int = 12, title_suffix: str = ""
    ) -> List[str]:
        """
        Разбивает таблицу лидерборда на порции (чанки) с сохранением заголовков колонок
        и сквозной нумерации, гарантируя непревышение лимита Telegram (4096 символов).
        """
        if not entries:
            return ["Таблица лидеров пуста."]

        total_entries = len(entries)
        if total_entries <= chunk_size:
            tbl = self.format_table(entries, evaluated_coins_count=evaluated_coins_count, start_rank=1, title_suffix=title_suffix)
            if len(tbl) <= 3500:
                return [tbl]

        chunks = []
        num_chunks = (total_entries + chunk_size - 1) // chunk_size

        for chunk_idx in range(num_chunks):
            start_i = chunk_idx * chunk_size
            end_i = min(start_i + chunk_size, total_entries)
            chunk_entries = entries[start_i:end_i]
            start_rank = start_i + 1
            end_rank = end_i

            header = (
                "=" * 160 + "\n"
                f"STRATEGIES LEADERBOARD{title_suffix} (Part {chunk_idx + 1}/{num_chunks} | #{start_rank}-#{end_rank} of {total_entries} | Coins: {evaluated_coins_count})\n"
                f"Ranked strictly by Cumulative Net Profit USDT (Winrate excluded - DCA quantitative focus)\n"
                + "=" * 160 + "\n"
                f"{'#':<3} {'Strategy / Universe ID':<43} {'Mode':<9} {'Net Profit':<13} {'Iter Net':<14} {'Realized':<12} {'Iter Real':<12} {'Floating':<11} {'Peak Net (Age)':<17} {'Max DD':<11} {'Trades':<8} {'Rec':<6}\n"
                + "-" * 160
            )

            rows = []
            for idx, item in enumerate(chunk_entries, start=start_rank):
                net_str = f"{item['net_profit_usdt']:+.2f} USDT"
                iter_idx = item.get('iteration_index', 1)
                iter_val = item.get('iteration_net_profit_usdt', item['net_profit_usdt'])
                iter_str = f"{iter_val:+.2f} $ (#{iter_idx})"
                realized_str = f"{item['realized_pnl_usdt']:+.2f} $"
                iter_real_str = f"{item.get('iteration_realized_pnl_usdt', 0.0):+.2f} $"
                unrealized_str = f"{item['unrealized_pnl_usdt']:+.2f} $"
                peak_val = item.get('peak_net_profit_usdt', 0.0)
                peak_age = item.get('peak_age_hours', 0.0)
                peak_net_str = f"{peak_val:+.2f} $ ({peak_age:.1f}h)" if peak_age > 0 else f"{peak_val:+.2f} $"
                dd_str = f"{item['max_drawdown_usdt']:.2f} $"
                rec_str = f"{item['recovery_factor']:.2f}"
                uid_short = item['universe_id'][:42]
                mode_badge = item.get("mode_badge", "[OFF]")

                row = (
                    f"{idx:<3} {uid_short:<43} {mode_badge:<9} {net_str:<13} {iter_str:<14} {realized_str:<12} {iter_real_str:<12} "
                    f"{unrealized_str:<11} {peak_net_str:<17} {dd_str:<11} {item['total_trades']:<8} {rec_str:<6}"
                )
                rows.append(row)

            footer = "=" * 160
            chunks.append("\n".join([header] + rows + [footer]))

        return chunks

    def save_portfolio(self, entries: List[dict], evaluated_coins_count: int = 0) -> None:
        """Сохраняет сводный json и текстовый отчет лидерборда."""
        try:
            # 1. Текстовая витрина
            table_text = self.format_table(entries, evaluated_coins_count=evaluated_coins_count)
            Utils.atomic_write_text(self.leaderboard_file, table_text)

            # 2. JSON витрина
            total_net = sum(x["net_profit_usdt"] for x in entries)
            total_trades = sum(x["total_trades"] for x in entries)
            portfolio_payload = {
                "updated_at_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                "total_universes_active": len(entries),
                "evaluated_coins_count": evaluated_coins_count,
                "aggregate_net_profit_usdt": round(total_net, 2),
                "aggregate_total_trades": total_trades,
                "leaderboard": entries,
            }
            Utils.atomic_write_json(self.portfolio_file, portfolio_payload)

        except Exception as e:
            logger.error(f"Error saving portfolio leaderboard: {e}")
