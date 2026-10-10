import asyncio
from API.BINANCE.client_prod import BinanceClient
from consts import API_KEY, API_SECRET

async def query_binance():
    c = BinanceClient(API_KEY, API_SECRET)
    print("==================== BINANCE LIVE AUDIT ====================")
    total_pos = 0
    total_orders = 0
    symbols = ['RLCUSDT', 'STRKUSDT', 'PUMPUSDT', 'METUSDT']
    for sym in symbols:
        positions = await c.fetch_positions(sym)
        orders = await c.fetch_open_orders(sym)
        active_pos = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
        total_pos += len(active_pos)
        total_orders += len(orders)
        print(f"\n--- {sym} ---")
        for p in active_pos:
            side = p.get('positionSide')
            amt = float(p.get('positionAmt'))
            entry = float(p.get('entryPrice'))
            matching = [o for o in orders if o.get('positionSide') == side]
            if matching:
                ord_strs = [f"ID:{o.get('orderId')} ({o.get('side')} {o.get('origQty')} @ {o.get('price')})" for o in matching]
                ord_info = ", ".join(ord_strs)
                ord_vol = sum(float(o.get('origQty')) for o in matching)
                status = "OK" if abs(ord_vol - abs(amt)) < 1e-4 else f"MISMATCH (pos={abs(amt)}, ord={ord_vol})"
            else:
                ord_info = "NONE (MISSING!)"
                status = "CRITICAL_MISSING"
            print(f"  [{status}] {side} POS: {amt} (Entry: {entry}) -> TP ORDER: {ord_info}")
            
    print(f"\nTOTAL POSITIONS: {total_pos} | TOTAL OPEN ORDERS: {total_orders}")
    print("============================================================")

if __name__ == '__main__':
    asyncio.run(query_binance())
