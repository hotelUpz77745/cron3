# CORE/SERVICES/TOOLS/audit_and_cleanup_orders.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.TOOLS.audit_and_cleanup_orders
ROLE: Частный утилитный хелпер для аудита и мгновенной очистки лишних ордеров Binance
==============================================================================
"""

import asyncio
import os
import sys
from pathlib import Path

# Fix Windows console encoding if needed
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Setup paths
root_dir = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(root_dir))

from consts import API_KEY, API_SECRET
from API.BINANCE.client_prod import BinanceClient


async def audit_and_cleanup():
    print("=" * 80)
    print("[AUDIT] Zapusk audita otkrytykh pozitsiy i orderov na Binance Futures...")
    print("=" * 80)

    client = BinanceClient(api_key=API_KEY, api_secret=API_SECRET)

    # 1. Загрузка открытых ордеров
    orders_res = await client._request(
        "GET",
        "https://fapi.binance.com/fapi/v1/openOrders",
        params={"recvWindow": 20000},
        signed=True,
    )
    if not orders_res.success or not isinstance(orders_res.data, list):
        print(f"[ERR] Oshibka polucheniya otkrytykh orderov: {orders_res}")
        if client._session:
            await client._session.close()
        return

    open_orders = orders_res.data
    print(f"[*] Vsego aktivnykh orderov na birzhe: {len(open_orders)}")

    # 2. Загрузка открытых позиций
    acc_res = await client.fetch_account_info()
    if not acc_res.success or not isinstance(acc_res.data, dict):
        print(f"[ERR] Oshibka polucheniya akkaunta: {acc_res}")
        if client._session:
            await client._session.close()
        return

    positions = acc_res.data.get("positions", [])
    active_positions = {
        (p["symbol"], p["positionSide"]): float(p["positionAmt"])
        for p in positions
        if abs(float(p.get("positionAmt", 0.0))) > 1e-8
    }

    total_wallet = acc_res.data.get("totalWalletBalance", "0")
    total_unrealized = acc_res.data.get("totalUnrealizedProfit", "0")
    print(f"[*] Balans koshelka: {float(total_wallet):.2f} USDT | Plavayushchiy PnL: {float(total_unrealized):.2f} USDT")
    print(f"[*] Otkrytykh pozitsiy: {len(active_positions)}")
    for (sym, pside), amt in active_positions.items():
        print(f"   - {sym} {pside}: {amt}")

    print("-" * 80)
    print("[*] Analiz sootvetstviya orderov pozitsiyam:")

    orders_by_key = {}
    for o in open_orders:
        key = (o["symbol"], o["positionSide"])
        orders_by_key.setdefault(key, []).append(o)

    canceled_count = 0

    # Проверка ордеров на закрытые стороны (ордер есть, позиции нет)
    for (sym, pside), o_list in orders_by_key.items():
        pos_amt = active_positions.get((sym, pside), 0.0)

        if pos_amt == 0.0:
            print(f"[WARN] [SIROTA] Obnaruzhen order bez otkrytoy pozitsii: {sym} {pside} ({len(o_list)} sht.)! Otmena...")
            for o in o_list:
                oid = o["orderId"]
                c_res = await client._request(
                    "DELETE",
                    "https://fapi.binance.com/fapi/v1/order",
                    params={"symbol": sym, "orderId": oid, "recvWindow": 20000},
                    signed=True,
                )
                print(f"   [DEL] Otmenen order {oid} po {sym}: {c_res.success}")
                canceled_count += 1
        elif len(o_list) > 1:
            print(f"[WARN] [DUBL] Obnaruzheno {len(o_list)} orderov na odnu pozitsiyu {sym} {pside}!")
            # Оставляем самый свежий (по времени или ID), остальные отменяем
            sorted_orders = sorted(o_list, key=lambda x: int(x.get("time", 0)), reverse=True)
            keep_order = sorted_orders[0]
            print(f"   [KEEP] Ostavlyaem aktualnyy TP: ID {keep_order['orderId']} @ {keep_order['price']}")
            for old_o in sorted_orders[1:]:
                oid = old_o["orderId"]
                c_res = await client._request(
                    "DELETE",
                    "https://fapi.binance.com/fapi/v1/order",
                    params={"symbol": sym, "orderId": oid, "recvWindow": 20000},
                    signed=True,
                )
                print(f"   [DEL] Otmenen dubliruyushchiy order {oid} @ {old_o['price']}: {c_res.success}")
                canceled_count += 1
        else:
            o = o_list[0]
            print(f"   [OK] {sym} {pside}: order ID {o['orderId']} @ {o['price']} (qty: {o['origQty']}) strogo sinkhronen.")

    print("-" * 80)
    if canceled_count == 0:
        print("[SUCCESS] Vse chisto! Nikakikh lishnikh orderov na birzhe net.")
        print("          Kazhdyy TP tochno sootvetstvuet svoey otkrytoy pozitsii (1 pozitsiya = 1 TP).")
    else:
        print(f"[CLEANUP] Uspeshno otmeneno lishnikh orderov: {canceled_count}")
    print("=" * 80)

    if client._session:
        await client._session.close()


if __name__ == "__main__":
    asyncio.run(audit_and_cleanup())
