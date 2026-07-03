import asyncio
from API.BINANCE.client import BinanceClient
from consts import API_KEY, API_SECRET

async def main():
    client = BinanceClient(API_KEY, API_SECRET)
    inc_res = await client._request(
        "GET", 
        "https://fapi.binance.com/fapi/v1/income", 
        params={"limit": 1000}, # get latest 1000
        signed=True
    )
    if inc_res.success:
        transfers = 0.0
        for r in inc_res.data:
            if r["incomeType"] == "TRANSFER":
                transfers += float(r["income"])
                print(r)
        print("Total Transfers:", transfers)
    else:
        print("Error fetching", inc_res)

asyncio.run(main())
