# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\ANALYTICS\metrics.py
# Role: metrics.py module

import time
import json
from consts import DATA_DIR

class AnalyticsMathEngine:
    """
    Движок для расчета всех математических показателей аналитики.
    Содержит логику для ROI, DRME, MDME, Drawdowns и других метрик.
    """

    @staticmethod
    def calculate(data: dict) -> None:
        """
        Основной метод расчета метрик. Мутирует переданный словарь data, 
        добавляя или обновляя расчетные поля.
        """
        AnalyticsMathEngine._inject_help(data)
        
        if "per_coin" not in data:
            return
            
        AnalyticsMathEngine._calculate_global_metrics(data)
        AnalyticsMathEngine._calculate_per_coin_metrics(data)

    @staticmethod
    def _inject_help(data: dict) -> None:
        """
        Внедряет справочник полей аналитики.
        """
        data["_help"] = {
            "roi_pct": "Return on Investment (%). (cur_balance_usdt - start_balance_usdt) / start_balance_usdt * 100",
            "load_ratio": "Grid Load Ratio. abs(unrealized_pnl_usdt) / realized_pnl_usdt. Shows how much floating risk is taken per 1 USDT of closed profit.",
            "recovery_factor": "Recovery Factor. realized_pnl_usdt / abs(max_drawdown_usdt). Shows if the bots profit can cover historical max drawdowns.",
            "realized_pnl_usdt": "Total realized profit including commissions and funding fees from all closed trades.",
            "net_profit_usdt": "realized_pnl_usdt + unrealized_pnl_usdt. The true mathematical growth of the account.",
            "unrealized_pnl_usdt": "Current floating drawdown (unrealized PnL) of all open positions.",
            "start_balance_usdt": "Initial configured account balance.",
            "cur_balance_usdt": "Current mathematical margin balance (start_balance_usdt + net_profit_usdt).",
            "peak_balance_usdt": "Absolute highest margin balance reached.",
            "min_balance_usdt": "Absolute lowest margin balance reached.",
            "max_drawdown_usdt": "Maximum historical drawdown (trough - peak).",
            "performance_usdt": "Maximum historical growth from start balance (peak - start_balance).",
            "avg_daily_profit": "[Per-Coin] Average profit per active day of trading for this coin.",
            "max_position_size": "[Per-Coin] Max historical notional size actually reached (total volume * price).",
            "risk_reward_ratio": "[Per-Coin] abs(max_drawdown) / avg_daily_profit.",
            "DRME": "[Per-Coin] Daily Return on Max Exposure: avg_daily_profit / max_position_size.",
            "MDME": "[Per-Coin] Max Drawdown on Max Exposure: abs(max_drawdown) / max_position_size.",
            "max_net_profit": "[Per-Coin] Historical maximum of the coin's fixed net profit.",
            "min_net_profit": "[Per-Coin] Historical minimum of the coin's fixed net profit.",
            "max_drawdown": "[Per-Coin] Historical maximum floating drawdown for this coin.",
            "min_drawdown": "[Per-Coin] Historical minimum floating drawdown for this coin."
        }

    @staticmethod
    def _calculate_global_metrics(data: dict) -> None:
        """
        Рассчитывает глобальные балансы и ROI.
        """
        initial = float(data.get("start_balance_usdt", 0.0))
        net_profit = float(data.get("net_profit_usdt", 0.0))
        bot_cur_balance = round(initial + net_profit, 4)
        data["cur_balance_usdt"] = bot_cur_balance
        if initial > 0:
            data["roi_pct"] = round(((bot_cur_balance - initial) / initial) * 100, 2)
        else:
            data["roi_pct"] = 0.0

    @staticmethod
    def _calculate_per_coin_metrics(data: dict) -> None:
        """
        Рассчитывает метрики для каждой монеты (DRME, MDME, Drawdowns).
        """
        current_ts = int(time.time() * 1000)
        
        for sym, cdata in data["per_coin"].items():
            first_trade_ts = cdata.get("first_trade_ts")
            if not first_trade_ts:
                days_active = 1.0
            else:
                days_active = max(1.0, (current_ts - first_trade_ts) / 86400000.0)
            
            realized_net = cdata.get("realized_pnl_net_usdt", 0.0)
            net_profit = cdata.get("net_profit_usdt", 0.0)
            
            cdata["max_net_profit"] = round(max(cdata.get("max_net_profit", net_profit), net_profit), 4)
            cdata["min_net_profit"] = round(min(cdata.get("min_net_profit", net_profit), net_profit), 4)
            
            avg_daily_profit = round(realized_net / days_active, 4)
            cdata["avg_daily_profit"] = avg_daily_profit
            
            max_dd = abs(cdata.get("max_drawdown", 0.0))
            if avg_daily_profit > 0:
                cdata["risk_reward_ratio"] = round(max_dd / avg_daily_profit, 2)
            else:
                cdata["risk_reward_ratio"] = 0.0
                
            # Remove obsolete legacy fields
            legacy_keys = [
                "reward_risk_surplus_pct", 
                "avg_daily_return_pct", 
                "cumulative_return_pct", 
                "current_drawdown_pct", 
                "max_drawdown_pct"
            ]
            for lk in legacy_keys:
                if lk in cdata:
                    del cdata[lk]
                
            # Calculate actual historical Max Position Size from runtime config
            runtime_path = DATA_DIR / "runtime" / f"{sym.lower()}.json"
            current_margin = 0.0
            if runtime_path.exists():
                try:
                    rt_data = json.loads(runtime_path.read_text(encoding="utf-8"))
                    long_size = float(rt_data.get("LONG", {}).get("invest_size", 0.0)) if rt_data.get("LONG", {}).get("enable") else 0.0
                    short_size = float(rt_data.get("SHORT", {}).get("invest_size", 0.0)) if rt_data.get("SHORT", {}).get("enable") else 0.0
                    current_margin = (long_size + short_size) / 2.0
                except Exception:
                    pass
                    
            cdata["max_position_size"] = round(current_margin, 4)
            
            safe_max = cdata["max_position_size"] if cdata["max_position_size"] > 0 else 1.0
            global_drme = cdata.get("avg_daily_profit", 0.0) / safe_max
            global_mdme = abs(cdata.get("max_drawdown", 0.0)) / safe_max
            
            if "epoch_state" in cdata:
                est = cdata["epoch_state"]
                current_profit = cdata.get("realized_pnl_net_usdt", 0.0) - est.get("pnl_at_start", 0.0)
                current_duration = (int(time.time() * 1000) - est.get("start_ts", 0)) / 86400000
                
                active_count = est.get("closed_count", 0)
                cur_drme = 0.0
                cur_mdme = 0.0
                
                if current_duration >= 1.0:
                    safe_sz = est.get("size", 1.0) if est.get("size", 0.0) > 0 else 1.0
                    cur_drme = (current_profit / current_duration) / safe_sz
                    cur_mdme = abs(est.get("max_dd", 0.0)) / safe_sz
                    active_count += 1
                
                if active_count > 0:
                    cdata["DRME"] = round((est.get("closed_drme_sum", 0.0) + cur_drme) / active_count, 4)
                    cdata["MDME"] = round((est.get("closed_mdme_sum", 0.0) + cur_mdme) / active_count, 4)
                else:
                    cdata["DRME"] = round(global_drme, 4)
                    cdata["MDME"] = round(global_mdme, 4)
            else:
                cdata["DRME"] = round(global_drme, 4)
                cdata["MDME"] = round(global_mdme, 4)
