"""
Быстрый тест: проверяем что sync_current_drawdowns вызывается и пишет данные
"""
import asyncio
import sys
sys.path.insert(0, ".")

from ANALYTICS.analytics import AnalyticsManager

class FakeResult:
    def __init__(self, data):
        self.success = True
        self.data = data

class FakeClient:
    async def fetch_account_info(self):
        # Реальные данные которые вернул Binance в test_binance.py
        return FakeResult({
            "totalMarginBalance": "323.77513396",
            "totalUnrealizedProfit": "-0.02179999",
            "positions": [
                {"symbol": "DASHUSDT", "positionSide": "LONG", "positionAmt": "0.806", "unrealizedProfit": "-0.00298991"},
                {"symbol": "DASHUSDT", "positionSide": "SHORT", "positionAmt": "-0.806", "unrealizedProfit": "-0.00507008"},
                {"symbol": "STABLEUSDT", "positionSide": "LONG", "positionAmt": "687.0", "unrealizedProfit": "-0.01828107"},
                {"symbol": "STABLEUSDT", "positionSide": "SHORT", "positionAmt": "-687.0", "unrealizedProfit": "0.00454107"},
            ]
        })

async def main():
    import json
    with open("ANALYTICS/analytics.json", "r", encoding="utf-8") as f:
        before = json.load(f)
    
    print(f"BEFORE: cur_balance={before['cur_balance_usdt']}, unrealized={before['unrealized_pnl_usdt']}")
    
    am = AnalyticsManager()
    client = FakeClient()
    
    await am.sync_current_drawdowns(client)
    
    with open("ANALYTICS/analytics.json", "r", encoding="utf-8") as f:
        after = json.load(f)
    
    print(f"AFTER:  cur_balance={after['cur_balance_usdt']}, unrealized={after['unrealized_pnl_usdt']}")
    
    if before['unrealized_pnl_usdt'] != after['unrealized_pnl_usdt'] or before['cur_balance_usdt'] != after['cur_balance_usdt']:
        print("✅ ДАННЫЕ ОБНОВИЛИСЬ!")
    else:
        print("❌ ДАННЫЕ НЕ ИЗМЕНИЛИСЬ (возможно значения совпадают или ошибка)")
    
    # Покажи per_coin
    for sym, cdata in after.get("per_coin", {}).items():
        print(f"  {sym}: current_drawdown={cdata.get('current_drawdown')}, long_unrealized={cdata.get('long_unrealized')}, short_unrealized={cdata.get('short_unrealized')}")

asyncio.run(main())
