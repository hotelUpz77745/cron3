import asyncio
from API.BINANCE.client import BinanceClient
from consts import API_KEY, API_SECRET

async def main():
    client = BinanceClient(API_KEY, API_SECRET)
    inc_res = await client._request(
        "GET", 
        "https://fapi.binance.com/fapi/v1/income", 
        params={"limit": 1000, "symbol": "VVVUSDT"}, 
        signed=True
    )
    if inc_res.success:
        for r in inc_res.data:
            if r["time"] > 1782938000000:
                print(r)

asyncio.run(main())
