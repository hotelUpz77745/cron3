import asyncio, datetime
from API.BINANCE.client_prod import BinanceClient
from consts import API_KEY, API_SECRET

async def inspect():
    c = BinanceClient(API_KEY, API_SECRET)
    trades = await c._request('GET', 'https://fapi.binance.com/fapi/v1/userTrades', {'symbol': 'RLCUSDT', 'limit': 100}, signed=True)
    all_trades = trades.data or []
    print(f"Total trades fetched: {len(all_trades)}")
    print("Most recent 45 trades:")
    total_pnl = sum(float(t.get('realizedPnl', 0)) for t in all_trades)
    print(f"Total Realized PnL across fetched RLC trades: {total_pnl:+.2f}$")
    for t in all_trades[-45:]:
        dt = datetime.datetime.fromtimestamp(t['time']/1000).strftime('%m-%d %H:%M:%S')
        qty = float(t['qty'])
        price = float(t['price'])
        pnl = float(t['realizedPnl'])
        side = t['side']
        pos_side = t['positionSide']
        print(f"{dt} | {side:4s} {pos_side:5s} | Qty: {qty:8.1f} @ {price:.4f} | PnL: {pnl:+6.2f}$ | id: {t['orderId']}")
    await c.shutdown()

asyncio.run(inspect())
