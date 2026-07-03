import asyncio
from API.BINANCE.client import BinanceClient
from consts import API_KEY, API_SECRET

async def main():
    client = BinanceClient(API_KEY, API_SECRET)
    res = await client.fetch_account_info()
    if res.success:
        print("Total Margin Balance:", res.data.get("totalMarginBalance"))
        print("Total Wallet Balance:", res.data.get("totalWalletBalance"))
    else:
        print("Error:", res.error_msg)

asyncio.run(main())
