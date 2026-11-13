# ==============================================================================
# Path: ANALYTICS/plotter.py
# Role: Генератор графиков эквити, баланса и аналитических кривых
# ==============================================================================

import csv
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple

from ANALYTICS.equity_trajectory import DEFAULT_SNAPSHOT_STORE

try:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

try:
    from consts import (
        ANALYTICS_DIR,
        ANALYTICS_STATS_DIR,
        ANALYTICS_LEDGERS_DIR,
        ANALYTICS_START_BALANCE_USDT,
        FLOAT_ZERO_EPSILON,
        CACHE_DIR,
        _CFG,
    )
except ImportError:
    ANALYTICS_DIR = Path(__file__).resolve().parent
    ANALYTICS_STATS_DIR = ANALYTICS_DIR / "STATS"
    ANALYTICS_LEDGERS_DIR = ANALYTICS_DIR / "LEDGERS"
    CACHE_DIR = ANALYTICS_DIR.parent / "CACHE"
    ANALYTICS_START_BALANCE_USDT = 0.0
    FLOAT_ZERO_EPSILON = 1e-6
    _CFG = {}


def _resolve_default_start_balance(universe_id: Optional[str] = None) -> float:
    """Определяет стартовый баланс из конфигурации стратегий CFG/app.json либо ANALYTICS_START_BALANCE_USDT."""
    if universe_id and isinstance(_CFG, dict):
        active_strats = _CFG.get("active_strategies", {})
        if universe_id in active_strats:
            strat_cfg = active_strats[universe_id]
            if isinstance(strat_cfg, dict):
                if "start_balance_usdt" in strat_cfg:
                    return float(strat_cfg["start_balance_usdt"])
                if "analytics" in strat_cfg and "start_balance_usdt" in strat_cfg["analytics"]:
                    return float(strat_cfg["analytics"]["start_balance_usdt"])
        for s_id, s_cfg in active_strats.items():
            if not isinstance(s_cfg, dict):
                continue
            if s_id.startswith(universe_id) or universe_id.startswith(s_id) or s_cfg.get("template") == universe_id:
                if "start_balance_usdt" in s_cfg:
                    return float(s_cfg["start_balance_usdt"])
                if "analytics" in s_cfg and "start_balance_usdt" in s_cfg["analytics"]:
                    return float(s_cfg["analytics"]["start_balance_usdt"])
    return float(ANALYTICS_START_BALANCE_USDT)


IMAGES_DIR = ANALYTICS_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

CSV_FILE = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
JSON_FILE = ANALYTICS_STATS_DIR / "analytics.json"

PLOT_FILE = IMAGES_DIR / "equity_curve.png"


def _resolve_paths(universe_id: Optional[str] = None):
    """Подбирает точные пути к файлам статистики и леджера для вселенной."""
    json_path = JSON_FILE
    csv_path = CSV_FILE

    if universe_id:
        cand_json = ANALYTICS_STATS_DIR / f"analytics_{universe_id}.json"
        if cand_json.exists():
            json_path = cand_json
        else:
            matches = list(ANALYTICS_STATS_DIR.glob(f"analytics_{universe_id}*.json"))
            if matches:
                json_path = matches[0]

        cand_csv = ANALYTICS_LEDGERS_DIR / f"trades_ledger_{universe_id}.txt"
        if cand_csv.exists() and cand_csv.stat().st_size > 60:
            csv_path = cand_csv
        else:
            matches_csv = list(ANALYTICS_LEDGERS_DIR.glob(f"trades_ledger_{universe_id}*.txt"))
            found = [m for m in matches_csv if m.stat().st_size > 60]
            if found:
                csv_path = found[0]
            else:
                csv_path = CSV_FILE

    return json_path, csv_path


def _load_autoclose_events(
    universe_id: Optional[str] = None,
    start_balance: float = 0.0,
    history_file: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Загружает историю фиксаций автоклозера из CACHE/auto_close_history.json.
    Возвращает хронологический список событий фиксации с отметками времени и эквити/баланса.
    """
    h_file = history_file if history_file is not None else (CACHE_DIR / "auto_close_history.json")
    if not h_file.exists():
        return []

    try:
        raw_history = json.loads(h_file.read_text(encoding="utf-8"))
        if not isinstance(raw_history, dict):
            return []
    except Exception:
        return []

    u_target = str(universe_id or "ALL").strip()
    matching_records: List[Dict[str, Any]] = []

    for strat_key, records in raw_history.items():
        if not isinstance(records, list):
            continue
        # Фильтрация по universe_id
        if u_target != "ALL":
            if not (
                strat_key == u_target
                or strat_key.startswith(u_target)
                or u_target.startswith(strat_key)
                or strat_key == "ALL"
            ):
                continue

        for r in records:
            if not isinstance(r, dict):
                continue

            dt = None
            if "timestamp_ms" in r and r["timestamp_ms"]:
                try:
                    dt = datetime.fromtimestamp(float(r["timestamp_ms"]) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
                except Exception:
                    pass
            if dt is None and "closed_at" in r and r["closed_at"]:
                c_str = str(r["closed_at"]).replace(" UTC", "").strip()
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        dt = datetime.strptime(c_str, fmt)
                        break
                    except ValueError:
                        pass
            if dt is None:
                continue

            strat_net = float(r.get("strat_net", r.get("settled_realized", 0.0)) or 0.0)
            if "balance" in r and r["balance"] is not None:
                bal = float(r["balance"])
            elif "equity" in r and r["equity"] is not None:
                bal = float(r["equity"])
            else:
                bal = round(start_balance + strat_net, 4)

            matching_records.append({
                "datetime": dt,
                "balance": bal,
                "net_profit": strat_net,
                "iter_net": float(r.get("iter_net", 0.0) or 0.0),
                "close_type": str(r.get("close_type", "AUTO-CLOSE")).strip(),
                "iteration_index": int(r.get("iteration_index", 0) or 0),
            })

    matching_records.sort(key=lambda x: x["datetime"])
    return matching_records


def _load_armed_target_floor(
    universe_id: Optional[str] = None,
    start_balance: float = 0.0,
    state_file: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """
    Проверяет, вооружен ли сейчас порог автоклозера в CACHE/auto_closer_state.json
    (уровень, где будет происходить фиксация при достижении/откате).
    """
    s_file = state_file if state_file is not None else (CACHE_DIR / "auto_closer_state.json")
    if not s_file.exists():
        return None

    try:
        raw_state = json.loads(s_file.read_text(encoding="utf-8"))
        if not isinstance(raw_state, dict):
            return None
    except Exception:
        return None

    u_target = str(universe_id or "ALL").strip()
    for strat_key, entry in raw_state.items():
        if not isinstance(entry, dict):
            continue
        if u_target != "ALL":
            if not (
                strat_key == u_target
                or strat_key.startswith(u_target)
                or u_target.startswith(strat_key)
            ):
                continue

        armed_f = entry.get("armed_floor")
        chronos_f = entry.get("chronos_armed_floor")
        stress_st = entry.get("regime_stress_state")
        if isinstance(stress_st, dict):
            if armed_f is None:
                armed_f = stress_st.get("armed_floor")
            if chronos_f is None:
                chronos_f = stress_st.get("chronos_armed_floor")

        target_floor = armed_f if armed_f is not None else chronos_f
        if target_floor is not None:
            fl_val = float(target_floor)
            t_eq = round(start_balance + fl_val, 4)
            return {
                "target_floor": fl_val,
                "target_equity": t_eq,
                "label": f"Порог автоклозера ({t_eq:.2f} $)",
            }
    return None


def generate_equity_curve(universe_id: Optional[str] = None) -> str:
    """
    Генерирует график чистого Net Profit и эквити баланса.
    Кривая строится строго по чистому Net Profit с учетом комиссий, фандинга и
    текущей плавающей просадки (unrealized_pnl) в финальной точке.
    """
    if not MATPLOTLIB_AVAILABLE:
        return ""

    json_path, csv_path = _resolve_paths(universe_id)
    plot_file = IMAGES_DIR / (f"equity_curve_{universe_id}.png" if universe_id else "equity_curve.png")

    default_start_bal = _resolve_default_start_balance(universe_id)
    start_balance = default_start_bal
    strat_symbols = set()
    iter_start_ts = None
    realized_gross = 0.0
    realized_net = 0.0
    unrealized = 0.0
    net_profit_final = 0.0
    cur_balance = default_start_bal
    data = {}

    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                start_balance = float(data.get("start_balance_usdt", data.get("_rebased_start_balance_usdt", default_start_bal)))
                strat_symbols = set(data.get("per_coin", {}).keys()) | set(data.get("historical_symbols", []))
                iter_start_ts = data.get("iteration_start_ts", data.get("first_trade_ts"))
                realized_gross = float(data.get("realized_pnl_usdt", 0.0))
                realized_net = float(data.get("realized_pnl_net_usdt", realized_gross))
                unrealized = float(data.get("unrealized_pnl_usdt", 0.0))
                net_profit_final = float(data.get("net_profit_usdt", realized_net + unrealized))
                cur_balance = float(data.get("cur_balance_usdt", start_balance + net_profit_final))
        except Exception:
            pass

    start_dt = datetime.fromtimestamp(iter_start_ts / 1000.0, tz=timezone.utc).replace(tzinfo=None) if iter_start_ts else None

    # Чтение сделок из леджера
    raw_trades = []
    if csv_path.exists():
        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.reader(f, delimiter=";")
                header = next(reader, None)
                if header:
                    header_lower = [h.strip().lower() for h in header]
                    sym_idx = header_lower.index("symbol") if "symbol" in header_lower else (1 if "id" in header_lower else 0)

                    pnl_idx = -1
                    for cand in ["pnl (usdt)", "pnl", "pnl(usdt)"]:
                        if cand in header_lower:
                            pnl_idx = header_lower.index(cand)
                            break
                    if pnl_idx == -1:
                        pnl_idx = 5 if ("id" in header_lower or len(header) >= 7) else 4

                    close_idx = -1
                    for cand in ["close time", "closetime", "close_time"]:
                        if cand in header_lower:
                            close_idx = header_lower.index(cand)
                            break
                    if close_idx == -1:
                        close_idx = 4 if ("id" in header_lower or len(header) >= 7) else 3

                    is_shared_ledger = (csv_path == CSV_FILE and bool(strat_symbols))

                    for row in reader:
                        if len(row) <= max(close_idx, pnl_idx):
                            continue
                        if strat_symbols and is_shared_ledger and len(row) > sym_idx:
                            row_sym = row[sym_idx].strip()
                            if row_sym not in strat_symbols:
                                continue

                        t_str = row[close_idx].strip()
                        try:
                            dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            continue

                        if start_dt and dt < start_dt:
                            continue

                        try:
                            pnl = float(row[pnl_idx])
                        except ValueError:
                            continue

                        raw_trades.append((dt, pnl))
        except Exception:
            pass

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    times, net_profits, balances = DEFAULT_SNAPSHOT_STORE.extract_series(
        scope_id=universe_id if universe_id else "ALL",
        start_balance=start_balance,
        current_equity=cur_balance,
        start_dt=start_dt,
        now_dt=now,
    )

    # Построение графика в темной теме
    plt.style.use("dark_background")
    fig, ax1 = plt.subplots(figsize=(11, 5.8), dpi=130)
    fig.patch.set_facecolor("#0F131A")
    ax1.set_facecolor("#161B24")

    # Основная кривая Баланса / Эквити
    ax1.plot(times, balances, color="#29B6F6", linewidth=2.4, label="Эквити портфеля", zorder=4)

    # Пунктирная базовая линия стартового баланса и заливка областей профита/просадки
    ax1.axhline(start_balance, color="#78909C", linestyle="--", linewidth=1.2, alpha=0.7, zorder=3)
    b_arr = np.array(balances) if 'np' in globals() else None
    if b_arr is not None:
        ax1.fill_between(times, b_arr, start_balance, where=(b_arr >= start_balance), color="#00D26A", alpha=0.18, interpolate=True)
        ax1.fill_between(times, b_arr, start_balance, where=(b_arr < start_balance), color="#FF5252", alpha=0.18, interpolate=True)

    # Отметки моментов фиксации автоклозером (красные точки с ценами/эквити)
    ac_events = _load_autoclose_events(universe_id=universe_id, start_balance=start_balance)
    armed_target = _load_armed_target_floor(universe_id=universe_id, start_balance=start_balance)

    valid_ac_events = []
    if times and ac_events:
        t_min = times[0] - timedelta(minutes=15)
        t_max = times[-1] + timedelta(minutes=15)
        for ev in ac_events:
            if t_min <= ev["datetime"] <= t_max:
                valid_ac_events.append(ev)
    elif ac_events:
        valid_ac_events = list(ac_events)

    if valid_ac_events:
        ac_dts = [e["datetime"] for e in valid_ac_events]
        ac_bals = [e["balance"] for e in valid_ac_events]
        ax1.scatter(
            ac_dts,
            ac_bals,
            color="#FF1744",
            s=80,
            zorder=7,
            edgecolors="#FFFFFF",
            linewidths=1.5,
            label="Фиксация автоклозером",
        )
        for idx, ev in enumerate(valid_ac_events):
            ac_dt = ev["datetime"]
            ac_bal = ev["balance"]
            it_net = ev["iter_net"]
            it_sign = "+" if it_net >= 0 else ""
            if abs(it_net) > 1e-4:
                lbl = f"{ac_bal:.2f} $\n({it_sign}{it_net:.2f}$)"
            else:
                lbl = f"{ac_bal:.2f} $"
            y_offset = 14 if idx % 2 == 0 else 24
            ax1.annotate(
                lbl,
                xy=(ac_dt, ac_bal),
                xytext=(0, y_offset),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=8.5,
                fontweight="bold",
                color="#FFCDD2",
                bbox=dict(boxstyle="round,pad=0.25", fc="#2A1215", ec="#FF1744", alpha=0.9, lw=0.9),
                arrowprops=dict(arrowstyle="->", color="#FF1744", lw=1.1, shrinkA=0, shrinkB=2),
                zorder=8,
            )

    # Линия порога автоклозера (где будет происходить следующая фиксация)
    if armed_target:
        ax1.axhline(
            armed_target["target_equity"],
            color="#FF9100",
            linestyle=":",
            linewidth=1.3,
            alpha=0.85,
            label=armed_target["label"],
            zorder=3,
        )

    # Финальная точка с плашкой значения
    end_color = "#00D26A" if net_profit_final >= 0 else "#FF5252"
    ax1.scatter([times[-1]], [balances[-1]], color=end_color, s=60, zorder=6, edgecolors="white", linewidths=1.5)
    sign = "+" if net_profit_final >= 0 else ""
    callout_txt = f"{balances[-1]:.2f} $ ({sign}{net_profit_final:.2f}$)"
    ax1.annotate(
        callout_txt,
        xy=(times[-1], balances[-1]),
        xytext=(-85, 15 if net_profit_final < 0 else -20),
        textcoords="offset points",
        color="white",
        fontweight="bold",
        fontsize=10,
        bbox=dict(boxstyle="round,pad=0.3", fc=end_color, ec="none", alpha=0.85),
        arrowprops=dict(arrowstyle="->", connectionstyle="arc3,rad=0.2", color=end_color, lw=1.2)
    )

    # Границы шкалы с учетом отметок автоклозера и порога
    all_b_vals = list(balances)
    if valid_ac_events:
        all_b_vals.extend(e["balance"] for e in valid_ac_events)
    if armed_target:
        all_b_vals.append(armed_target["target_equity"])
    min_b = min(all_b_vals)
    max_b = max(all_b_vals)
    span = max_b - min_b
    pad = max(2.0, span * 0.18)
    ax1.set_ylim(min_b - pad, max_b + pad)

    # Вторичная правая шкала: Чистый Net Profit
    ax2 = ax1.twinx()
    ax2.set_ylim(min_b - pad - start_balance, max_b + pad - start_balance)
    ax2.set_ylabel("Чистый Net Profit (USDT)", color="#90CAF9", fontsize=10, labelpad=8)
    ax2.tick_params(axis="y", labelcolor="#90CAF9")

    # Заголовки и сетка
    title_u = f" [{universe_id}]" if universe_id else ""
    ax1.set_title(f"Кривая баланса и эквити{title_u}", fontsize=14, fontweight="bold", pad=15, color="#ECEFF1")
    ax1.set_xlabel("Дата / Время (UTC)", fontsize=10, color="#B0BEC5")
    ax1.set_ylabel("Баланс / Эквити (USDT)", fontsize=10, color="#ECEFF1")
    ax1.grid(True, linestyle=":", alpha=0.25, color="#CFD8DC")
    ax1.tick_params(axis="x", rotation=30, labelsize=9, colors="#B0BEC5")
    ax1.tick_params(axis="y", labelsize=9, colors="#ECEFF1")
    ax1.legend(loc="lower left", facecolor="#161B24", edgecolor="#37474F", fontsize=8.5, labelcolor="#ECEFF1")

    # Информационный блок в углу
    ac_stat_line = f"\n• Автозакрытий: {len(valid_ac_events)}" if (valid_ac_events or ac_events) else ""
    stats_box = (
        f"ИТОГИ:\n"
        f"• Чистый Net Profit: {net_profit_final:+.2f} USDT\n"
        f"• Реализованный Net: {realized_net:+.2f} USDT\n"
        f"• Плавающий PnL (Float): {unrealized:+.2f} USDT\n"
        f"• Баланс: {cur_balance:.2f} $ (Старт: {start_balance:.2f} $)"
        f"{ac_stat_line}"
    )
    ax1.text(
        0.02, 0.95, stats_box,
        transform=ax1.transAxes,
        verticalalignment="top",
        fontsize=9,
        family="monospace",
        color="#ECEFF1",
        bbox=dict(boxstyle="round,pad=0.5", fc="#1E2633", ec="#37474F", alpha=0.9)
    )

    fig.tight_layout()
    plt.savefig(plot_file, dpi=130)
    plt.close(fig)
    return str(plot_file)



def generate_balance_curve(symbol: str, universe_id: Optional[str] = None) -> str:
    """
    Генерирует график движения баланса монеты (от стартового баланса стратегии с учетом Net Profit монеты).
    Для выбывших (неактивных) монет график завершается на последней сделке, фиксируя итоговый баланс.
    Для активных монет график продлевается до текущего момента.
    """
    if not MATPLOTLIB_AVAILABLE:
        return ""

    json_path, csv_path = _resolve_paths(universe_id)
    clean_sym = symbol.upper().strip()
    u_suffix = f"_{universe_id}" if universe_id else ""
    plot_file = IMAGES_DIR / f"balance_{clean_sym.lower()}{u_suffix}.png"

    default_start_bal = _resolve_default_start_balance(universe_id)
    start_balance = default_start_bal
    cdata = {}
    is_active = True
    realized_coin_net = 0.0
    unrealized_coin = 0.0
    final_pnl = 0.0
    iter_start_ts = None

    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                start_balance = float(data.get("start_balance_usdt", data.get("_rebased_start_balance_usdt", default_start_bal)))
                iter_start_ts = data.get("iteration_start_ts", data.get("first_trade_ts"))
                cdata = data.get("per_coin", {}).get(clean_sym, {})
                if cdata:
                    is_active = bool(cdata.get("is_active_symbol", cdata.get("is_active", True)))
                    realized_coin_net = float(cdata.get("realized_pnl_net_usdt", cdata.get("realized_pnl_usdt", cdata.get("net_profit_usdt", 0.0))))
                    unrealized_coin = float(cdata.get("current_drawdown", 0.0))
                    final_pnl = float(cdata.get("net_profit_usdt", realized_coin_net + unrealized_coin))
        except Exception:
            pass

    start_dt = datetime.fromtimestamp(iter_start_ts / 1000.0, tz=timezone.utc).replace(tzinfo=None) if iter_start_ts else None

    times = []
    raw_pnls = []

    if csv_path.exists():
        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.reader(f, delimiter=";")
                header = next(reader, None)
                if header:
                    header_lower = [h.strip().lower() for h in header]
                    sym_idx = header_lower.index("symbol") if "symbol" in header_lower else (1 if "id" in header_lower else 0)

                    pnl_idx = -1
                    for cand in ["pnl (usdt)", "pnl", "pnl(usdt)"]:
                        if cand in header_lower:
                            pnl_idx = header_lower.index(cand)
                            break
                    if pnl_idx == -1:
                        pnl_idx = 5 if ("id" in header_lower or len(header) >= 7) else 4

                    close_idx = -1
                    for cand in ["close time", "closetime", "close_time"]:
                        if cand in header_lower:
                            close_idx = header_lower.index(cand)
                            break
                    if close_idx == -1:
                        close_idx = 4 if ("id" in header_lower or len(header) >= 7) else 3

                    for row in reader:
                        if len(row) <= max(close_idx, pnl_idx):
                            continue
                        row_sym = row[sym_idx].strip().upper()
                        if row_sym != clean_sym:
                            continue

                        t_str = row[close_idx].strip()
                        try:
                            dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            continue

                        if start_dt and dt < start_dt:
                            continue

                        try:
                            pnl = float(row[pnl_idx])
                        except ValueError:
                            continue

                        times.append(dt)
                        raw_pnls.append(pnl)
        except Exception:
            pass

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if not times:
        target_end_bal = round(start_balance + final_pnl, 2)
        times = [now - timedelta(hours=3), now]
        balances = [start_balance, target_end_bal]
        current_balance = target_end_bal
    else:
        cum = 0.0
        cum_series = []
        for p in raw_pnls:
            cum += p
            cum_series.append(cum)

        raw_final = cum_series[-1]
        target_realized = realized_coin_net if cdata else raw_final
        fee_drift = target_realized - raw_final
        n_trades = len(cum_series)

        balances = []
        for i, c in enumerate(cum_series):
            adj_c = c + fee_drift * ((i + 1) / n_trades)
            balances.append(round(start_balance + adj_c, 4))

        # Префикс стартовой точки за 1 час до первой сделки
        times.insert(0, times[0] - timedelta(hours=1))
        balances.insert(0, start_balance)

        # Конечная точка закрытых сделок строго равна реализованному Net монеты
        realized_balance = round(start_balance + target_realized, 4)
        balances[-1] = realized_balance
        current_balance = realized_balance

        # Для активной монеты продлеваем линию до текущего момента с учетом открытого плавающего PnL
        if is_active:
            live_target = final_pnl if cdata else target_realized
            live_balance = round(start_balance + live_target, 4)
            if now <= times[-1]:
                now = times[-1] + timedelta(seconds=1)
            times.append(now)
            balances.append(live_balance)
            current_balance = live_balance
        # Для выбывшей монеты: график завершается строго в последней точке закрытия (без падения в ноль)

    plt.figure(figsize=(10, 5))
    line_color = "#00C853" if current_balance >= start_balance else "#D50000"
    plt.plot(times, balances, color=line_color, linewidth=2.5, label="Баланс")
    plt.fill_between(times, balances, min(balances), color=line_color, alpha=0.12)

    min_b = min(balances)
    max_b = max(balances)
    spread = max_b - min_b
    pad = 1.0 if spread < 5 else (5.0 if spread < 20 else 10.0)
    plt.ylim(min_b - pad, max_b + pad)

    title_u = f" ({universe_id})" if universe_id else ""
    status_tag = "" if is_active else " [ВЫВЕДЕНА]"
    plt.title(f"Движение баланса: {clean_sym}{status_tag}{title_u}", fontsize=15, pad=12, fontweight="bold")
    plt.xlabel("Дата / Время", fontsize=11)
    plt.ylabel("Баланс (USDT)", fontsize=11)
    plt.grid(True, linestyle="--", alpha=0.6)
    plt.xticks(rotation=45)
    plt.tight_layout()

    plt.savefig(plot_file, dpi=120)
    plt.close()
    return str(plot_file)


def generate_coin_analytics(symbol: str, universe_id: Optional[str] = None) -> str:
    """Для монеты возвращает график движения баланса (от стартового с учетом Net Profit)."""
    return generate_balance_curve(symbol, universe_id=universe_id)
