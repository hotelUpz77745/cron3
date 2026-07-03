import asyncio
from API.BINANCE.client import BinanceClient
from consts import API_KEY, API_SECRET

async def main():
    client = BinanceClient(API_KEY, API_SECRET)
    start_ts = 1782777600000 
    inc_res = await client._request(
        "GET", 
        "https://fapi.binance.com/fapi/v1/income", 
        params={"limit": 1000, "startTime": start_ts}, 
        signed=True
    )
    if inc_res.success:
        events = [r for r in inc_res.data if r["incomeType"] == "REALIZED_PNL"]
        for e in events:
            val = float(e["income"])
            if val > 0.059 and val < 0.065:
                print(e)
    else:
        print("Error fetching", inc_res)

asyncio.run(main())
