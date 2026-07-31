# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\quant_calculator.py
# Role: quant_calculator.py module

# ==============================================================================
# Path: quant_calculator.py
# Role: Калькулятор ликвидации для DCA-стратегии (Crossed Margin)
# ==============================================================================

import sys
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
from pathlib import Path
from c_utils import Utils
from consts import DATA_DIR, ANALYTICS_DIR

class QuantCalculator:
    def __init__(self, deposit=None, leverage=None, coins_count=None, grid_steps=None, volumes=None, invest_size=None):
        self.maintenance_margin_rate = 0.004
        self.super_grid_multiplier = 1.0
        self.super_grid_enabled = False
        
        # Динамическая подгрузка из файлов, если параметры не переданы
        if deposit is None:
            analytics_path = ANALYTICS_DIR / "analytics.json"
            if analytics_path.exists():
                data = Utils.read_json_file(analytics_path)
                deposit = float(data.get("cur_balance_usdt", 300.0))
            else:
                deposit = 300.0
                
        app_data = {}
        app_path = DATA_DIR / "app.json"
        if app_path.exists():
            app_data = Utils.read_json_file(app_path)
            
        if coins_count is None:
            symbols = app_data.get("symbols", [])
            coins_count = len(symbols) if symbols else 4
                
        # Если нет монет, ставим минимум 1 для избежания деления на 0 в симуляциях
        if coins_count == 0:
            coins_count = 1
            
        base_path = DATA_DIR / "_base.json"
        if base_path.exists() and (leverage is None or grid_steps is None or volumes is None or invest_size is None):
            base_data = Utils.read_json_file(base_path)
            long_cfg = base_data.get("LONG", {})
            
            if invest_size is None:
                invest_size = float(long_cfg.get("invest_size", 100.0))
            if leverage is None:
                leverage = long_cfg.get("leverage", 10)
                
            if grid_steps is None or volumes is None:
                grid = long_cfg.get("grid", {})
                loaded_steps = []
                loaded_vols = []
                for k in sorted(grid.keys(), key=lambda x: int(x)):
                    loaded_steps.append(float(grid[k].get("indent", 0.0)))
                    loaded_vols.append(float(grid[k].get("volume", 0.0)))
                
                if not grid_steps and loaded_steps:
                    grid_steps = loaded_steps
                if not volumes and loaded_vols:
                    volumes = loaded_vols

        self.total_deposit = deposit
        self.leverage = leverage if leverage else 10
        self.coins_count = coins_count
        self.invest_size = invest_size if invest_size else 100.0
        
        grid_steps = grid_steps if grid_steps else [0, -5, -8, -13, -21, -34]
        
        if "super_grid" in app_data:
            super_grid_cfg = app_data["super_grid"]
            if super_grid_cfg["enabled"]:
                self.super_grid_enabled = True
                self.super_grid_multiplier = float(super_grid_cfg["multiplier"])
                grid_steps = [round(step * self.super_grid_multiplier, 2) for step in grid_steps]
            
        self.grid_steps_pct = grid_steps
        self.volume_pct = volumes if volumes else [12.96, 14.26, 15.68, 17.25, 18.98, 20.87]

    def simulate_drop(self, drop_pct, active_coins, invest_size):
        total_unrealized_pnl = 0.0
        total_position_size = 0.0
        total_margin_used = 0.0
        
        for _ in range(active_coins):
            coin_pos_size_usdt = 0.0
            coin_coins_amt = 0.0
            
            initial_price = 100.0
            current_price = initial_price * (1 + drop_pct / 100.0)
            
            for step_pct, vol_pct in zip(self.grid_steps_pct, self.volume_pct):
                if drop_pct <= step_pct:
                    exec_price = initial_price * (1 + step_pct / 100.0)
                    order_size_usdt = invest_size * (vol_pct / 100.0)
                    order_coins = order_size_usdt / exec_price
                    
                    coin_pos_size_usdt += order_size_usdt
                    coin_coins_amt += order_coins
                    
            if coin_coins_amt > 0:
                coin_avg_price = coin_pos_size_usdt / coin_coins_amt
                unrealized_pnl = (current_price - coin_avg_price) * coin_coins_amt
                total_unrealized_pnl += unrealized_pnl
                current_notional = coin_coins_amt * current_price
                total_position_size += current_notional
                total_margin_used += current_notional / self.leverage
                
        for _ in range(self.coins_count - active_coins):
            if not self.volume_pct:
                continue
            exec_price = 100.0
            order_size_usdt = invest_size * (self.volume_pct[0] / 100.0)
            total_position_size += order_size_usdt
            total_margin_used += order_size_usdt / self.leverage

        margin_balance = self.total_deposit + total_unrealized_pnl
        maintenance_margin = total_position_size * self.maintenance_margin_rate
        
        return margin_balance, maintenance_margin

    def find_liquidation_point(self, active_coins, invest_size):
        for drop in np.arange(0, -100, -0.1):
            mb, mm = self.simulate_drop(drop, active_coins, invest_size)
            if mb <= mm:
                return drop
        return -100.0

    def generate_report(self):
        lines = []
        lines.append("="*40)
        lines.append("🧪 QUANT LIQUIDATION CALCULATOR")
        if self.super_grid_enabled:
            lines.append(f"🔥 АКТИВЕН SUPER GRID (Множитель: {self.super_grid_multiplier}x)")
        lines.append(f"Депозит: {self.total_deposit} USDT | Монет: {self.coins_count} | Плечо: {self.leverage}x")
        lines.append(f"Текущий invest_size: {self.invest_size} USDT")
        lines.append(f"Сетка (%): {self.grid_steps_pct}")
        lines.append(f"Объемы (%): {self.volume_pct}")
        lines.append("="*40)
        
        if self.leverage > 0:
            max_margin_per_side = self.invest_size / self.leverage
            max_margin_total = max_margin_per_side * self.coins_count
            lines.append(f"\n💡 ТЕКУЩАЯ НАГРУЗКА ПРИ ПОЛНОЙ СЕТКЕ:")
            lines.append(f"   Маржа 1 стороны (фулл сетка): ~{max_margin_per_side:.2f} USDT")
            lines.append(f"   Максимальная маржа ({self.coins_count} монет): ~{max_margin_total:.2f} USDT")
            lines.append(f"   Свободная маржа (запас): ~{self.total_deposit - max_margin_total:.2f} USDT")
        
        lines.append("\n" + "="*40)
        lines.append(f"📈 СЦЕНАРИИ ЛИКВИДАЦИИ\n")
        
        if self.coins_count > 1:
            liq_swan = self.find_liquidation_point(1, self.invest_size)
            lines.append(f"🦢 'Черный лебедь' ({self.coins_count-1} стоят, 1 падает):")
            lines.append(f"   Ликвидация при падении проблемной монеты на {liq_swan:.1f}%")
        
        liq_apoc = self.find_liquidation_point(self.coins_count, self.invest_size)
        lines.append(f"🌋 'Апокалипсис' (Все {self.coins_count} синхронно падают):")
        lines.append(f"   Ликвидация аккаунта при обвале на {liq_apoc:.1f}%")
        lines.append("="*40)
        
        return "\n".join(lines)

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8')
    calc = QuantCalculator()
    print(calc.generate_report())
