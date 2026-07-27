# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\ANALYTICS\plotter.py
# Role: plotter.py module

# ==============================================================================
# Path: ANALYTICS/plotter.py
# Role: Генератор графиков эквити и метрик
# ==============================================================================

import csv
import json
from pathlib import Path
import matplotlib.pyplot as plt
from datetime import datetime

from datetime import timedelta
ANALYTICS_DIR = Path(__file__).parent
IMAGES_DIR = ANALYTICS_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

CSV_FILE = ANALYTICS_DIR / "trades_ledger.txt"
JSON_FILE = ANALYTICS_DIR / "analytics.json"
PLOT_FILE = IMAGES_DIR / "equity_curve.png"

def generate_equity_curve() -> str:
    if not CSV_FILE.exists():
        return ""
        
    start_balance = 0.0
    if JSON_FILE.exists():
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                start_balance = float(data.get("start_balance_usdt", 0.0))
        except Exception:
            pass

    times = []
    balances = []
    
    current_balance = start_balance
    
    # We add the initial point
    try:
        with open(CSV_FILE, mode="r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=";")
            header = next(reader, None)
            if not header:
                return ""
                
            has_balance_col = "Balance" in header
            bal_idx = header.index("Balance") if has_balance_col else -1
            pnl_idx = header.index("PnL") if "PnL" in header else 4
            close_idx = header.index("Close Time") if "Close Time" in header else 3
            
            for row in reader:
                if len(row) <= close_idx:
                    continue
                
                # Parse time
                t_str = row[close_idx]
                try:
                    dt = datetime.strptime(t_str, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
                    
                # Parse pnl / balance
                try:
                    if has_balance_col and len(row) > bal_idx and row[bal_idx].strip() != "":
                        current_balance = float(row[bal_idx])
                    else:
                        pnl = float(row[pnl_idx])
                        current_balance += pnl
                except ValueError:
                    continue
                    
                times.append(dt)
                balances.append(current_balance)
                
    except Exception as e:
        print(f"Error reading CSV for plot: {e}")
        return ""

    if not times:
        return ""

    # Prepend the start balance before the first trade (e.g. 1 hour before)
    times.insert(0, times[0] - timedelta(hours=1))
    balances.insert(0, start_balance)
    
    plt.figure(figsize=(10, 5))
    plt.plot(times, balances, color="#2196F3", linewidth=2, label="Balance")
    
    # Fill under curve
    plt.fill_between(times, balances, min(balances), color="#2196F3", alpha=0.1)
    
    # Adjust Y axis dynamically based on data scale
    min_b = min(balances)
    max_b = max(balances)
    spread = max_b - min_b
    
    if spread < 5:
        pad = 1
    elif spread < 20:
        pad = 5
    else:
        pad = 10
        
    plt.ylim(min_b - pad, max_b + pad)
    
    plt.title("Equity Curve", fontsize=16, pad=15)
    plt.xlabel("Date", fontsize=12)
    plt.ylabel("USDT", fontsize=12)
    plt.grid(True, linestyle="--", alpha=0.7)
    plt.xticks(rotation=45)
    plt.tight_layout()
    
    plt.savefig(PLOT_FILE, dpi=100)
    plt.close()
    
    return str(PLOT_FILE)

def generate_coin_analytics(symbol: str) -> str:
    if not JSON_FILE.exists():
        return ""
        
    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return ""
        
    per_coin = data.get("per_coin", {})
    if symbol not in per_coin:
        return ""
        
    cdata = per_coin[symbol]
    
    # Extract metrics
    avg_daily = cdata.get("avg_daily_profit", 0.0)
    drme = cdata.get("DRME", 0.0)
    mdme = cdata.get("MDME", 0.0)
    max_dd = cdata.get("max_drawdown", 0.0)
    min_dd = cdata.get("min_drawdown", 0.0)
    cur_dd = cdata.get("current_drawdown", 0.0)
    
    net_profit = cdata.get("net_profit_usdt", 0.0)
    realized_pnl = cdata.get("realized_pnl_usdt", 0.0)
    realized_pnl_net = cdata.get("realized_pnl_net_usdt", realized_pnl)
    max_np = cdata.get("max_net_profit", net_profit)
    min_np = cdata.get("min_net_profit", net_profit)
    
    metrics = {
        "Max Hist. Drawdown": max_dd,
        "Unrealized PnL": cur_dd,
        "Min Hist. Drawdown": min_dd,
        "MDME": mdme,
        "DRME": drme,
        "Min Net Profit": min_np,
        "Avg Daily Profit (Net)": avg_daily,
        "Net Profit": net_profit,
        "Max Net Profit": max_np,
        "Realized PnL (Net)": realized_pnl_net,
        "Realized PnL (Gross)": realized_pnl
    }
    
    labels = list(metrics.keys())
    values = list(metrics.values())
    
    # Create horizontal bar chart
    plt.figure(figsize=(8, 7))
    
    # Color bars based on value (positive green, negative red, neutral blue)
    colors = []
    for val in values:
        if val > 0:
            colors.append("#4CAF50") # Green
        elif val < 0:
            colors.append("#F44336") # Red
        else:
            colors.append("#2196F3") # Blue
            
    bars = plt.barh(labels, values, color=colors, height=0.6)
    
    # Add values in the center of the chart
    for bar in bars:
        width = bar.get_width()
        
        plt.text(0, bar.get_y() + bar.get_height()/2, f'{width:.4f}', 
                 va='center', ha='center', fontweight='bold',
                 bbox=dict(facecolor='white', alpha=0.8, edgecolor='none', pad=2))
                 
    plt.title(f"Advanced Analytics: {symbol}", fontsize=16, pad=15)
    plt.xlabel("Value", fontsize=12)
    
    # Add a vertical line at 0
    plt.axvline(x=0, color='black', linewidth=1, alpha=0.3)
    
    plt.grid(axis='x', linestyle='--', alpha=0.7)
    plt.tight_layout()
    
    out_file = IMAGES_DIR / f"coin_analytics_{symbol.lower()}.png"
    plt.savefig(out_file)
    plt.close()
    
    return str(out_file)

if __name__ == "__main__":
    generate_equity_curve()
