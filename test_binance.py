import asyncio
from API.BINANCE.client import BinanceClient
from consts import API_KEY, API_SECRET

async def main():
    client = BinanceClient(API_KEY, API_SECRET)
    res = await client.fetch_account_info()
    if res.success:
        print("TOTAL MARGIN BALANCE:", res.data.get("totalMarginBalance"))
        print("TOTAL UNREALIZED:", res.data.get("totalUnrealizedProfit"))
        positions = res.data.get("positions", [])
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                print(f"[{p.get('symbol')}] SIDE: {p.get('positionSide')} | AMT: {amt} | UNREALIZED: {p.get('unrealizedProfit')}")
    else:
        print("ERROR")

if __name__ == "__main__":
    asyncio.run(main())
