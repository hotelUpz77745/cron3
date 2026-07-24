import asyncio
from ANALYTICS.analytics import AnalyticsManager
from API.BINANCE.client import BinanceClient
from consts import API_KEY, API_SECRET

async def main():
    client = BinanceClient(API_KEY, API_SECRET)
    mgr = AnalyticsManager()
    await mgr.sync_current_drawdowns(client)
    data = mgr._read_data()
    print("Unrealized:", data.get("unrealized_pnl_usdt"))
    print("Cur Balance:", data.get("cur_balance_usdt"))
    print("STABLEUSDT DD:", data.get("per_coin", {}).get("STABLEUSDT", {}).get("current_drawdown"))
    print("DASHUSDT DD:", data.get("per_coin", {}).get("DASHUSDT", {}).get("current_drawdown"))

if __name__ == "__main__":
    asyncio.run(main())
