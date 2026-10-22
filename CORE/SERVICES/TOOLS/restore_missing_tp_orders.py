# CORE/SERVICES/TOOLS/restore_missing_tp_orders.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.TOOLS.restore_missing_tp_orders
ROLE: Восстановление отсутствующих TP ордеров для открытых позиций на Binance Futures
==============================================================================
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Setup paths
root_dir = Path(__file__).resolve().parent.parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from consts import API_KEY, API_SECRET
from c_log import UnifiedLogger
from API.BINANCE.client_prod import BinanceClient
from API.BINANCE.public import BinancePublic
from CORE._utils import TradeMath, Utils, RiskCalculatingUtils
from RUNTIME_FSM.runtime_manager import RuntimeFsmManager

logger = UnifiedLogger("RestoreMissingTP")


class MissingTPRestorer:
    """
    Институциональный сервис проверки и автоматического восстановления
    отсутствующих TP (Take Profit) ордеров для открытых позиций.
    """

    def __init__(
        self,
        api_key: str = API_KEY,
        api_secret: str = API_SECRET,
        client: Optional[BinanceClient] = None,
        runtime_mgr: Optional[RuntimeFsmManager] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self._client = client
        self._runtime_mgr = runtime_mgr

    async def run(self, client: Optional[BinanceClient] = None) -> int:
        """Выполняет аудит позиций и восстанавливает недостающие TP ордера."""
        logger.info("=" * 80)
        logger.info("[RESTORE_TP] Проверка и восстановление TP ордеров на Binance Futures...")
        logger.info("=" * 80)

        should_close = False
        active_client = client or self._client
        if active_client is None:
            active_client = BinanceClient(api_key=self.api_key, api_secret=self.api_secret)
            should_close = True

        restored_count = 0
        try:
            # 1. Загрузка спецификаций биржи
            instruments = await BinancePublic.get_instruments()
            if not instruments:
                logger.error("[ERR] Не удалось загрузить спецификации биржи через BinancePublic!")
                return 0
            spec_data = {"symbols": instruments}

            # 2. Получение аккаунта и открытых позиций
            acc_res = await active_client.fetch_account_info()
            if not acc_res.success or not isinstance(acc_res.data, dict):
                logger.error(f"[ERR] Ошибка получения аккаунта: {acc_res.error_msg}")
                return 0

            positions = acc_res.data.get("positions", [])
            active_positions = [
                p for p in positions
                if abs(float(p.get("positionAmt", 0.0))) > 1e-8
            ]
            logger.info(f"[*] Активных позиций на бирже: {len(active_positions)}")

            # 3. Получение открытых ордеров через официальный метод адаптера
            open_orders = await active_client.fetch_open_orders()

            orders_by_key: Dict[tuple, list] = {}
            for o in open_orders:
                key = (o["symbol"], o["positionSide"])
                orders_by_key.setdefault(key, []).append(o)

            symbols_to_check = list({p["symbol"] for p in active_positions})
            runtime_mgr = self._runtime_mgr or RuntimeFsmManager()
            runtime_mgr.load_initial_caches(symbols_to_check)

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

                logger.warning(
                    f"[MISSING TP] Обнаружена позиция без TP ордера: {sym} {pside} (Объем: {pos_amt}, Вход: {entry_price})"
                )

                state = runtime_mgr.get_state(sym, pside)
                if not state:
                    logger.warning(f"   [WARN] FSM State не найден в runtime для {sym} {pside}! Пропуск.")
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
                    logger.info(f"   [*] Рассчитан fallback TP: {tp_price:.6f} (индент {indent}%)")

                tp_price = TradeMath.round_price(tp_price, spec_data, sym)
                tp_qty = TradeMath.round_qty(pos_amt, spec_data, sym)
                order_side = "SELL" if pside == "LONG" else "BUY"

                logger.info(
                    f"   [ACTION] Выставление восстановительного TP: {sym} {pside} | {order_side} {tp_qty} @ {tp_price}"
                )
                place_res = await active_client.make_order(
                    symbol=sym,
                    qty=tp_qty,
                    side=order_side,
                    position_side=pside,
                    price=tp_price,
                    market_type="LIMIT",
                )

                if place_res.success:
                    logger.info(f"   [SUCCESS] TP успешно выставлен! Order ID: {place_res.order_id}")
                    state.tp_price = tp_price
                    runtime_mgr.mark_dirty()
                    restored_count += 1
                else:
                    logger.error(f"   [FAILED] Не удалось выставить TP: {place_res.error_msg}")

            logger.info("-" * 80)
            logger.info(f"[COMPLETED] Восстановлено недостающих TP: {restored_count}")
            logger.info("=" * 80)
            return restored_count

        finally:
            if should_close:
                await active_client.close()


# Модульная функция для обратной совместимости
async def restore_missing_tp(client: Optional[BinanceClient] = None) -> int:
    restorer = MissingTPRestorer()
    return await restorer.run(client=client)


if __name__ == "__main__":
    asyncio.run(restore_missing_tp())
