import asyncio, json
import sys
sys.path.insert(0, "./")
from API.BINANCE.client import BinanceClient
from consts import _CFG

import os
from dotenv import load_dotenv
load_dotenv()

async def main():
    client = BinanceClient(os.getenv("API_KEY"), os.getenv("API_SECRET"))
    res = await client._request("GET", "https://fapi.binance.com/fapi/v2/account", signed=True)
    if res.success:
        print("Wallet Balance:", res.data.get("totalWalletBalance"))
        print("Margin Balance:", res.data.get("totalMarginBalance"))
        print("Unrealized PNL:", res.data.get("totalUnrealizedProfit"))
        for p in res.data.get("positions", []):
            if float(p["positionAmt"]) != 0:
                print(f"Pos: {p['symbol']} {p['positionSide']} Amt: {p['positionAmt']} UnRealized: {p['unrealizedProfit']}")
    else:
        print("Failed to get account data")

asyncio.run(main())
