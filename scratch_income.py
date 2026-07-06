import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode

api_key = '7jKhkRVyWE8RxAJbrA00vJdzb4dlnVqwzPdNW3RUlJvQ2Qzp4IsRyzhGcvVC3rOx'
api_secret = 'MEY0emrK1HZrSfl7M9lZuOLF5MwSC7jhkUZ1AL1IMOXLK131f2ag8nZsZJHX9QIW'

timestamp = int(time.time() * 1000)
params = {
    'limit': 1000,
    'timestamp': timestamp,
    'recvWindow': 60000
}
query_string = urlencode(params)
signature = hmac.new(api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
params['signature'] = signature

headers = {'X-MBX-APIKEY': api_key}
response = requests.get('https://fapi.binance.com/fapi/v1/income', params=params, headers=headers)
data = response.json()

from datetime import datetime

dump_lines = []
if isinstance(data, list):
    for item in data:
        if item['incomeType'] == 'REALIZED_PNL':
            ts = item['time']
            dt_str = datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S')
            dump_lines.append(f"{ts} | {dt_str} | {item['symbol']} | {item['income']}")

    with open("ANALYTICS/income_dump.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(dump_lines))
    print(f"Dumped {len(dump_lines)} trades to ANALYTICS/income_dump.txt")
else:
    print(data)
