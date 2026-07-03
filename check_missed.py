import asyncio
from API.BINANCE.client import BinanceClient
from consts import API_KEY, API_SECRET

async def main():
    client = BinanceClient(API_KEY, API_SECRET)
    inc_res = await client._request(
        "GET", 
        "https://fapi.binance.com/fapi/v1/income", 
        params={"limit": 1000, "startTime": 1782750000000},
        signed=True
    )
    if inc_res.success:
        total_missed = 0.0
        for r in inc_res.data:
            if r["time"] < 1782778565000:
                print(r)
                total_missed += float(r["income"])
        print(f"Total missed before 10-min window: {total_missed}")

asyncio.run(main())
