# ==============================================================================
# Path: tools/restore_missing_tp_orders.py
# Role: Восстановление отсутствующих TP ордеров для открытых позиций на Binance
# ==============================================================================

import asyncio
import os
import sys
from pathlib import Path

# Setup paths
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(root_dir.parent))

from consts import API_KEY, API_SECRET
from API.BINANCE.client_prod import BinanceClient
from API.BINANCE.public import BinancePublic
from CORE._utils import TradeMath, Utils, RiskCalculatingUtils
from RUNTIME_FSM.runtime_manager import RuntimeFsmManager

async def restore_missing_tp():
    print("=" * 80)
    print("[RESTORE_TP] Проверка и восстановление TP ордеров на Binance Futures...")
    print("=" * 80)

    client = BinanceClient(api_key=API_KEY, api_secret=API_SECRET)
    
    # 1. Загрузка спецификаций биржи
    instruments = await BinancePublic.get_instruments()
    if not instruments:
        print("[ERR] Не удалось загрузить спецификации биржи через BinancePublic!")
        await client.close()
        return
    spec_data = {"symbols": instruments}

    # 2. Получение аккаунта и открытых позиций
    acc_res = await client.fetch_account_info()
    if not acc_res.success or not isinstance(acc_res.data, dict):
        print(f"[ERR] Ошибка получения аккаунта: {acc_res}")
        await client.close()
        return

    positions = acc_res.data.get("positions", [])
    active_positions = [
        p for p in positions
        if abs(float(p.get("positionAmt", 0.0))) > 1e-8
    ]
    print(f"[*] Активных позиций на бирже: {len(active_positions)}")

    # 3. Получение открытых ордеров
    orders_res = await client._request(
        "GET",
        "https://fapi.binance.com/fapi/v1/openOrders",
        params={"recvWindow": 20000},
        signed=True,
    )
    if not orders_res.success or not isinstance(orders_res.data, list):
        print(f"[ERR] Ошибка получения открытых ордеров: {orders_res}")
        await client.close()
        return

    open_orders = orders_res.data
    print(f"[*] Открытых ордеров на бирже: {len(open_orders)}")

    # Группировка ордеров по (symbol, positionSide)
    orders_by_pos = {}
    for o in open_orders:
        key = (o["symbol"], o["positionSide"])
        orders_by_pos.setdefault(key, []).append(o)

    # 4. Поиск позиций без TP
    runtime_mgr = RuntimeFsmManager()
    symbols_list = list(set(p["symbol"] for p in active_positions))
    runtime_mgr.load_initial_caches(symbols_list)

    restored_count = 0

    for p in active_positions:
        sym = p["symbol"]
        side = p["positionSide"]
        amt = float(p.get("positionAmt", 0.0))
        entry = float(p.get("entryPrice", 0.0))
        key = (sym, side)

        existing_orders = orders_by_pos.get(key, [])
        if existing_orders:
            print(f"  [OK] {sym} {side}: Позиция {amt} @ {entry} -> Найдено ордеров: {len(existing_orders)} (ID: {existing_orders[0]['orderId']} @ {existing_orders[0]['price']})")
            continue

        print(f"\n  [MISSING!] {sym} {side}: Позиция {amt} @ {entry} НЕ ИМЕЕТ ТЕЙК-ПРОФИТ ОРДЕРА!")
        
        # Получаем параметры TP из рантайм кэша
        cache = runtime_mgr.caches.get(sym, {}).get(side, {})
        tp_map = cache.get("tp_map", {})
        
        grid = cache.get("grid", {})
        level = RiskCalculatingUtils.get_current_grid_level(grid)
        if level not in tp_map and tp_map:
            level = "0"
            
        tp_cfg = tp_map.get(level, {})
        indent = float(tp_cfg.get("indent", 2.0))
        print(f"   -> Уровень сетки: {level}, TP indent: {indent}%")

        # Расчет цены TP от цены входа
        tp_price = TradeMath.calculate_take_profit_price(entry, indent, side, spec_data, sym)
        vol_float = abs(amt)
        volume = TradeMath.round_qty(vol_float, spec_data, sym)
        order_side = "SELL" if side == "LONG" else "BUY"

        print(f"   -> Выставляем LIMIT TP: {order_side} {volume} @ {tp_price}...")
        
        res = await client.make_order(
            symbol=sym,
            qty=volume,
            side=order_side,
            position_side=side,
            market_type="LIMIT",
            price=tp_price
        )

        if res.success and res.data:
            order_id = res.data.get("orderId")
            print(f"   [SUCCESS] Тейк-профит ордер успешно выставлен! Order ID: {order_id}")
            if level in tp_map:
                cache["tp_map"][level]["is_active"] = True
            await runtime_mgr.save_cache(sym)
            restored_count += 1
        else:
            print(f"   [ERROR] Ошибка выставления ордера: {res.error_msg}")

    print("\n" + "=" * 80)
    print(f"[ИТОГ] Проверено позиций: {len(active_positions)}, Восстановлено TP ордеров: {restored_count}")
    print("=" * 80)

    await client.close()

if __name__ == "__main__":
    asyncio.run(restore_missing_tp())
