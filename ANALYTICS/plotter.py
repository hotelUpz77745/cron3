# ==============================================================================
# Path: ANALYTICS/plotter.py
# Role: Генератор графиков эквити, баланса и аналитических кривых
# ==============================================================================

import csv
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

try:
    from consts import ANALYTICS_DIR, ANALYTICS_STATS_DIR, ANALYTICS_LEDGERS_DIR
except ImportError:
    ANALYTICS_DIR = Path(__file__).resolve().parent
    ANALYTICS_STATS_DIR = ANALYTICS_DIR / "STATS"
    ANALYTICS_LEDGERS_DIR = ANALYTICS_DIR / "LEDGERS"

IMAGES_DIR = ANALYTICS_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

CSV_FILE = ANALYTICS_LEDGERS_DIR / "trades_ledger.txt"
if not CSV_FILE.exists() and (ANALYTICS_DIR / "trades_ledger.txt").exists():
    CSV_FILE = ANALYTICS_DIR / "trades_ledger.txt"

JSON_FILE = ANALYTICS_STATS_DIR / "analytics.json"
if not JSON_FILE.exists() and (ANALYTICS_DIR / "analytics.json").exists():
    JSON_FILE = ANALYTICS_DIR / "analytics.json"

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


def generate_equity_curve(universe_id: Optional[str] = None) -> str:
    """Генерирует график движения баланса (от стартового с учетом суммарного Net Profit)."""
    if not MATPLOTLIB_AVAILABLE:
        return ""

    json_path, csv_path = _resolve_paths(universe_id)
    plot_file = IMAGES_DIR / (f"equity_curve_{universe_id}.png" if universe_id else "equity_curve.png")

    start_balance = 57.0
    strat_symbols = set()
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                start_balance = float(data.get("start_balance_usdt", data.get("_rebased_start_balance_usdt", 57.0)))
                strat_symbols = set(data.get("per_coin", {}).keys()) | set(data.get("historical_symbols", []))
        except Exception:
            pass

    times = []
    balances = []
    current_balance = start_balance

    if csv_path.exists():
        try:
            with open(csv_path, mode="r", encoding="utf-8") as f:
                reader = csv.reader(f, delimiter=";")
                header = next(reader, None)
                if header:
                    header_lower = [h.strip().lower() for h in header]
                    sym_idx = header_lower.index("symbol") if "symbol" in header_lower else (1 if "id" in header_lower else 0)
                    has_bal = "balance" in header_lower
                    bal_idx = header_lower.index("balance") if has_bal else -1

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

                        try:
                            if has_bal and not is_shared_ledger and len(row) > bal_idx and row[bal_idx].strip() != "":
                                current_balance = float(row[bal_idx])
                            else:
                                pnl = float(row[pnl_idx])
                                current_balance += pnl
                        except ValueError:
                            continue

                        times.append(dt)
                        balances.append(current_balance)
        except Exception:
            pass

    now = datetime.now()
    if not times:
        times = [now - timedelta(hours=3), now]
        balances = [start_balance, current_balance]
    else:
        times.insert(0, times[0] - timedelta(hours=1))
        balances.insert(0, start_balance)

    plt.figure(figsize=(10, 5))
    plt.plot(times, balances, color="#2196F3", linewidth=2.5, label="Баланс (USDT)")
    plt.fill_between(times, balances, min(balances), color="#2196F3", alpha=0.12)

    min_b = min(balances)
    max_b = max(balances)
    spread = max_b - min_b
    pad = 1.0 if spread < 5 else (5.0 if spread < 20 else 10.0)
    plt.ylim(min_b - pad, max_b + pad)

    title_u = f" [{universe_id}]" if universe_id else " [Global]"
    plt.title(f"Кривая баланса и эквити{title_u}", fontsize=15, pad=12, fontweight="bold")
    plt.xlabel("Дата / Время", fontsize=11)
    plt.ylabel("Баланс (USDT)", fontsize=11)
    plt.grid(True, linestyle="--", alpha=0.6)
    plt.xticks(rotation=45)
    plt.tight_layout()

    plt.savefig(plot_file, dpi=120)
    plt.close()
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

    start_balance = 57.0
    cdata = {}
    is_active = True
    final_pnl = None
    peak_pnl = None

    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                start_balance = float(data.get("start_balance_usdt", data.get("_rebased_start_balance_usdt", 57.0)))
                cdata = data.get("per_coin", {}).get(clean_sym, {})
                if cdata:
                    is_active = bool(cdata.get("is_active_symbol", cdata.get("is_active", True)))
                    final_pnl = float(cdata.get("realized_pnl_net_usdt", cdata.get("net_profit_usdt", 0.0)))
                    peak_pnl = float(cdata.get("max_net_profit", cdata.get("peak_net_profit_usdt", final_pnl)))
        except Exception:
            pass

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

                        try:
                            pnl = float(row[pnl_idx])
                        except ValueError:
                            continue

                        times.append(dt)
                        raw_pnls.append(pnl)
        except Exception:
            pass

    now = datetime.now()
    if not times:
        target_end_bal = round(start_balance + (final_pnl if final_pnl is not None else 0.0), 2)
        times = [now - timedelta(hours=3), now]
        balances = [start_balance, target_end_bal]
        current_balance = target_end_bal
    else:
        cum = 0.0
        cum_series = []
        for p in raw_pnls:
            cum += p
            cum_series.append(cum)

        raw_max = max(cum_series)
        target_peak = max(peak_pnl, final_pnl, 0.0) if peak_pnl is not None else raw_max
        target_final = final_pnl if final_pnl is not None else cum_series[-1]

        balances = []
        if raw_max > target_peak and raw_max > 0:
            scale_ratio = target_peak / raw_max
            for c in cum_series:
                scaled_c = (c * scale_ratio) if c >= 0 else c
                balances.append(start_balance + scaled_c)
        else:
            for c in cum_series:
                balances.append(start_balance + c)

        # Префикс стартовой точки за 1 час до первой сделки
        times.insert(0, times[0] - timedelta(hours=1))
        balances.insert(0, start_balance)

        # Фиксируем конечную точку относительно стартового баланса стратегии
        final_balance = round(start_balance + target_final, 4)
        balances[-1] = final_balance
        current_balance = final_balance

        # Для выбывшей монеты: финальный кластер закрытия позиции не должен проваливаться ниже точки закрытия
        if not is_active and len(times) > 2:
            close_cutoff = times[-1] - timedelta(minutes=15)
            for idx in range(len(times) - 1, 0, -1):
                if times[idx] >= close_cutoff:
                    balances[idx] = max(balances[idx], final_balance)
                else:
                    break

        # Для активной монеты продлеваем линию до текущего момента
        if is_active:
            times.append(now)
            balances.append(final_balance)
        # Для выбывшей монеты: график завершается ровно в последней точке закрытия (без падения в ноль)

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
