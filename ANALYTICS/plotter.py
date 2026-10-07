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
        if cand_csv.exists():
            csv_path = cand_csv
        else:
            matches_csv = list(ANALYTICS_LEDGERS_DIR.glob(f"trades_ledger_{universe_id}*.txt"))
            if matches_csv:
                csv_path = matches_csv[0]

    return json_path, csv_path


def generate_equity_curve(universe_id: Optional[str] = None) -> str:
    """Генерирует график движения баланса (от стартового с учетом суммарного Net Profit)."""
    if not MATPLOTLIB_AVAILABLE:
        return ""

    json_path, csv_path = _resolve_paths(universe_id)
    plot_file = IMAGES_DIR / (f"equity_curve_{universe_id}.png" if universe_id else "equity_curve.png")

    start_balance = 57.0
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                start_balance = float(data.get("start_balance_usdt", data.get("_rebased_start_balance_usdt", 57.0)))
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
                    has_bal = "Balance" in header
                    bal_idx = header.index("Balance") if has_bal else -1
                    pnl_idx = header.index("PnL") if "PnL" in header else 4
                    close_idx = header.index("Close Time") if "Close Time" in header else 3

                    for row in reader:
                        if len(row) <= close_idx:
                            continue
                        t_str = row[close_idx].strip()
                        try:
                            dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            continue

                        try:
                            if has_bal and len(row) > bal_idx and row[bal_idx].strip() != "":
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
    """Генерирует график движения баланса монеты (от стартового с учетом Net Profit монеты)."""
    if not MATPLOTLIB_AVAILABLE:
        return ""

    json_path, csv_path = _resolve_paths(universe_id)
    clean_sym = symbol.upper().strip()
    u_suffix = f"_{universe_id}" if universe_id else ""
    plot_file = IMAGES_DIR / f"balance_{clean_sym.lower()}{u_suffix}.png"

    start_balance = 57.0
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                start_balance = float(data.get("start_balance_usdt", data.get("_rebased_start_balance_usdt", 57.0)))
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
                    sym_idx = header.index("Symbol") if "Symbol" in header else 1
                    pnl_idx = header.index("PnL") if "PnL" in header else 4
                    close_idx = header.index("Close Time") if "Close Time" in header else 3

                    for row in reader:
                        if len(row) <= close_idx:
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
    plt.plot(times, balances, color="#00C853" if current_balance >= start_balance else "#D50000", linewidth=2.5, label="Баланс")
    plt.fill_between(times, balances, min(balances), color="#00C853" if current_balance >= start_balance else "#D50000", alpha=0.12)

    min_b = min(balances)
    max_b = max(balances)
    spread = max_b - min_b
    pad = 1.0 if spread < 5 else (5.0 if spread < 20 else 10.0)
    plt.ylim(min_b - pad, max_b + pad)

    title_u = f" ({universe_id})" if universe_id else ""
    plt.title(f"Движение баланса: {clean_sym}{title_u}", fontsize=15, pad=12, fontweight="bold")
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
