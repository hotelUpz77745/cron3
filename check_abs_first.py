import asyncio
from API.BINANCE.client import BinanceClient
from consts import API_KEY, API_SECRET

async def main():
    client = BinanceClient(API_KEY, API_SECRET)
    inc_res = await client._request(
        "GET", 
        "https://fapi.binance.com/fapi/v1/income", 
        params={"limit": 1000, "startTime": 1780000000000},  # Much earlier!
        signed=True
    )
    if inc_res.success:
        print(f"Total records: {len(inc_res.data)}")
        for r in inc_res.data[:5]:
            print(r)
    else:
        print("Error", inc_res)

asyncio.run(main())
