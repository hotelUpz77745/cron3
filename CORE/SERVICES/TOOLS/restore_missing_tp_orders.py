# CORE/SERVICES/TOOLS/restore_missing_tp_orders.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.TOOLS.restore_missing_tp_orders
ROLE: Восстановление отсутствующих TP ордеров для открытых позиций на Binance
==============================================================================
"""

import asyncio
import os
import sys
from pathlib import Path

# Setup paths
root_dir = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(root_dir))

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
    orders_by_key = {}
    for o in open_orders:
        key = (o["symbol"], o["positionSide"])
        orders_by_key.setdefault(key, []).append(o)

    symbols_to_check = list({p["symbol"] for p in active_positions})
    runtime_mgr = RuntimeFsmManager()
    runtime_mgr.load_initial_caches(symbols_to_check)

    restored_count = 0

    for p in active_positions:
        sym = p["symbol"]
        pside = p["positionSide"]
        pos_amt = abs(float(p["positionAmt"]))
        entry_price = float(p.get("entryPrice", 0.0))

        if pos_amt <= 1e-8:
            continue

        orders = orders_by_key.get((sym, pside), [])
        if orders:
            continue

        print(f"[MISSING TP] Обнаружена позиция без TP ордера: {sym} {pside} (Объем: {pos_amt}, Вход: {entry_price})")

        state = runtime_mgr.get_state(sym, pside)
        if not state:
            print(f"   [WARN] FSM State не найден в runtime для {sym} {pside}! Пропуск.")
            continue

        tp_price = getattr(state, "tp_price", None)
        if not tp_price or tp_price <= 0:
            grid = getattr(state, "grid", {})
            first_lvl = grid.get("0", {}) if isinstance(grid, dict) else {}
            indent = float(first_lvl.get("indent") or 1.0)
            if pside == "LONG":
                tp_price = entry_price * (1.0 + indent / 100.0)
            else:
                tp_price = entry_price * (1.0 - indent / 100.0)
            print(f"   [*] Рассчитан fallback TP: {tp_price:.6f} (индент {indent}%)")

        tp_price = TradeMath.round_price(tp_price, spec_data, sym)
        tp_qty = TradeMath.round_qty(pos_amt, spec_data, sym)
        order_side = "SELL" if pside == "LONG" else "BUY"

        print(f"   [ACTION] Выставление восстановительного TP: {sym} {pside} | {order_side} {tp_qty} @ {tp_price}")
        place_res = await client.make_order(
            symbol=sym,
            qty=tp_qty,
            side=order_side,
            position_side=pside,
            price=tp_price,
            market_type="LIMIT",
        )

        if place_res.success:
            print(f"   [SUCCESS] TP успешно выставлен! Order ID: {place_res.order_id}")
            state.tp_price = tp_price
            runtime_mgr.mark_dirty()
            restored_count += 1
        else:
            print(f"   [FAILED] Не удалось выставить TP: {place_res.error_msg}")

    print("-" * 80)
    print(f"[COMPLETED] Восстановлено недостающих TP: {restored_count}")
    print("=" * 80)

    if client._session:
        await client._session.close()


if __name__ == "__main__":
    asyncio.run(restore_missing_tp())
