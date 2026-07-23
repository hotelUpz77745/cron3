import asyncio, json, os, time, sys
from dotenv import load_dotenv
sys.path.insert(0, './')
from API.BINANCE.client import BinanceClient
from consts import _CFG

async def main():
    load_dotenv()
    client = BinanceClient(os.getenv('API_KEY'), os.getenv('API_SECRET'))
    with open('ANALYTICS/analytics.json') as f: data = json.load(f)
    start_ts = data['first_trade_ts']
    res = await client._request('GET', 'https://fapi.binance.com/fapi/v1/income', params={'limit': 1000, 'startTime': start_ts - 600000}, signed=True)
    if res.success:
        for r in res.data:
            if float(r.get('income', 0)) != 0:
                print(f"{r['symbol']} {r['incomeType']} {r['income']}")

asyncio.run(main())
