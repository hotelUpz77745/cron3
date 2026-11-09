# ==============================================================================
# Path: CORE/SERVICES/coin_grid_analytics/table_formatters.py
# Role: Визуализация и форматирование аналитических институциональных
#       ASCII-таблиц распределения сетки, риск-реестра монет и просадок.
# ==============================================================================

from typing import Dict, List, Any, Optional, Set


def format_ascii_table(
    title: str,
    headers: List[str],
    rows: List[List[str]],
    right_align_indices: Set[int],
    footer: Optional[str] = None,
    banner_char: str = "=",
    title_prefix: str = "",
) -> str:
    """Универсальный институциональный рендерер ASCII-таблиц (DRY-стандарт)."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(val))

    def make_row(cols: List[str]) -> str:
        return " | ".join(
            val.rjust(col_widths[i]) if i in right_align_indices else val.ljust(col_widths[i])
            for i, val in enumerate(cols)
        )

    sep = "-+-".join("-" * w for w in col_widths)
    banner = banner_char * len(make_row(headers))
    title_line = f"{title_prefix} {title}".strip()

    table_lines = [
        banner,
        title_line,
        banner,
        make_row(headers),
        sep,
    ] + [make_row(r) for r in rows]

    if footer:
        table_lines.extend([
            sep,
            footer,
        ])
    table_lines.append(banner)

    return "\n" + "\n".join(table_lines) + "\n"


def _extract_summary(analytics_or_summary: Any) -> Dict[str, Any]:
    if hasattr(analytics_or_summary, "get_summary"):
        return analytics_or_summary.get_summary()
    return analytics_or_summary


def format_grid_distribution_table(
    analytics_or_summary: Any,
    title: str = "РАСПРЕДЕЛЕНИЕ УРОВНЕЙ СЕТКИ И ВОРОНКА ВЫЖИВАНИЯ ЦИКЛОВ",
) -> str:
    """Форматирует визуальную ASCII-таблицу распределения уровней сетки и воронки циклов."""
    summary = _extract_summary(analytics_or_summary)
    dist = summary.get("grid_level_distribution", {})
    total_cyc = summary.get("total_cycles", 0)
    total_fls = summary.get("total_fills", 0)

    if total_fls == 0:
        return f"\n[{title}]: Срабатывания сетки не зафиксированы (0 ордеров)\n"

    headers = [
        "Level", "Описание / Роль ступени", "Отступ %", "Объем %",
        "Ордеров", "Доля ордеров %", "Сумм. объем $",
        "Циклов (Max Lvl)", "Доля циклов %", "Воронка (Дошли >= L)"
    ]
    rows = []

    level_keys = sorted(dist.keys(), key=lambda k: int(k[1:]) if k[1:].isdigit() else 999)
    for lvl_key in level_keys:
        data = dist[lvl_key]
        fls = data.get("fills_count", 0)
        fls_p = data.get("fills_share_pct", 0.0)
        notion = data.get("total_notional_usdt", 0.0)
        cyc_cnt = data.get("cycles_max_depth_count", 0)
        cyc_p = data.get("cycles_max_depth_share_pct", 0.0)
        surv_cnt = data.get("survival_funnel_count", 0)
        surv_p = data.get("survival_funnel_pct", 0.0)
        indent = data.get("indent_pct", 0.0)
        vol_p = data.get("volume_pct", 0.0)
        role = data.get("role", lvl_key)

        rows.append([
            lvl_key,
            role,
            f"{indent:+.1f}%" if indent != 0 else "0.0% (Entry)",
            f"{vol_p:.2f}%",
            f"{fls:,}",
            f"{fls_p:5.1f}%",
            f"{notion:,.1f}$",
            f"{cyc_cnt:,}",
            f"{cyc_p:5.1f}%",
            f"{surv_cnt:,} ({surv_p:5.1f}%)"
        ])

    title_str = f"{title.upper()} (ЦИКЛОВ: {total_cyc:,} | ОРДЕРОВ: {total_fls:,} | СРЕДНЯЯ ГЛУБИНА: L{summary.get('average_cycle_level', 0.0):.2f})"
    footer = (
        f"ИТОГО: {total_cyc:,} закрытых циклов | {total_fls:,} исполнений | "
        f"Средняя глубина: L{summary.get('average_cycle_level', 0.0):.2f} | "
        f"Превысили L8 (-89%): {summary.get('total_exceeded_l8', 0)} | Коснулись SL-120%: {summary.get('total_would_hit_sl_120', 0)}"
    )

    return format_ascii_table(
        title=title_str,
        headers=headers,
        rows=rows,
        right_align_indices={0, 2, 3, 4, 5, 6, 7, 8, 9},
        footer=footer,
        title_prefix="📊",
    )


def format_per_coin_table(
    analytics_or_summary: Any,
    title: str = "РИСК-РЕЕСТР МОНЕТ (PER-COIN DRAWDOWN & RISK LEDGER)",
) -> str:
    """Форматирует визуальную ASCII-таблицу риска и показателей по каждой монете."""
    summary = _extract_summary(analytics_or_summary)
    coins = list(summary.get("per_coin_summary", {}).values())

    if not coins:
        return f"\n[{title}]: Данные по монетам отсутствуют\n"

    sorted_coins = sorted(coins, key=lambda c: c.get("max_adverse_from_entry0_pct", 0.0), reverse=True)

    headers = [
        "Rank", "Символ", "Циклов", "WinRate %", "Макс Lvl", "Ср. Lvl",
        "Откат от L0 %", "Макс DD $", "Net PnL $", "Ср. время", ">L8", ">SL-120%"
    ]
    rows = []

    for rank, c in enumerate(sorted_coins, 1):
        sym = c.get("symbol", "")
        cyc = c.get("total_cycles", 0)
        wr = c.get("win_rate_pct", 0.0)
        max_l = f"L{c.get('max_level_ever', 0)}"
        avg_l = f"L{c.get('avg_cycle_level', 0.0):.2f}"
        adv_l0 = f"{c.get('max_adverse_from_entry0_pct', 0.0):5.1f}%"
        m_dd = f"{c.get('max_floating_dd_usdt', 0.0):6.2f}$"
        net_p = f"{c.get('net_pnl', 0.0):+7.2f}$"
        avg_dur = f"{c.get('avg_duration_hours', 0.0):4.1f}ч"
        ex_l8 = str(c.get("exceeded_l8_count", 0)) if c.get("exceeded_l8_count", 0) > 0 else "-"
        sl120_val = c.get("would_hit_sl_120_count", c.get("would_hit_sl_100_count", 0))
        sl120 = str(sl120_val) if sl120_val > 0 else "-"

        rows.append([
            str(rank),
            sym,
            f"{cyc:,}",
            f"{wr:5.1f}%",
            max_l,
            avg_l,
            adv_l0,
            m_dd,
            net_p,
            avg_dur,
            ex_l8,
            sl120
        ])

    title_str = f"{title.upper()} (МОНЕТ В КОРЗИНЕ: {len(coins)})"

    return format_ascii_table(
        title=title_str,
        headers=headers,
        rows=rows,
        right_align_indices={0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11},
        title_prefix="🛡️",
    )


def format_worst_drawdowns_table(
    analytics_or_summary: Any,
    top_n: int = 10,
    title: str = "ТОП НАИХУДШИХ ПРОСАДОК (WORST ADVERSE EXCURSIONS)",
) -> str:
    """Форматирует визуальную ASCII-таблицу топ наихудших просадок за историю."""
    summary = _extract_summary(analytics_or_summary)
    worst = summary.get("worst_drawdown_events", [])[:top_n]

    if not worst:
        return f"\n[{title}]: События глубоких просадок отсутствуют\n"

    headers = [
        "№", "Символ", "Side", "Откат от L0 %",
        "Макс DD $", "Макс Lvl", "Net PnL $", "Длительность", "Причина закрытия"
    ]
    rows = []

    for idx, ev in enumerate(worst, 1):
        sym = ev.get("symbol", "N/A")
        side = ev.get("side", "N/A")
        adv_l0 = f"{ev.get('max_adverse_from_entry0_pct', 0.0):5.1f}%"
        dd = f"{ev.get('max_floating_dd_usdt', 0.0):6.2f}$"
        lvl = f"L{ev.get('max_level_reached', 0)}"
        net_p = f"{ev.get('net_pnl', 0.0):+7.2f}$"
        dur = f"{ev.get('duration_hours', 0.0):4.1f}ч"
        reason = ev.get("close_reason", "TP")

        rows.append([
            str(idx),
            sym,
            side,
            adv_l0,
            dd,
            lvl,
            net_p,
            dur,
            reason
        ])

    title_str = f"{title.upper()} (ТОП {len(worst)} ГЛУБОКИХ ДВИЖЕНИЙ)"

    return format_ascii_table(
        title=title_str,
        headers=headers,
        rows=rows,
        right_align_indices={0, 3, 4, 5, 6, 7},
        title_prefix="⚠️",
    )

