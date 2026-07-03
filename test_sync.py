import asyncio
from API.BINANCE.client import BinanceClient
from consts import API_KEY, API_SECRET
from ANALYTICS.analytics import AnalyticsManager
import logging
import sys

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)

async def main():
    client = BinanceClient(API_KEY, API_SECRET)
    manager = AnalyticsManager()
    await manager.deep_sync_analytics(client)
    
    # Read and count
    with open('ANALYTICS/trades_ledger.txt', 'r', encoding='utf-8') as f:
        lines = f.readlines()
        print(f"Total trades in ledger now: {len(lines) - 1}")

asyncio.run(main())
