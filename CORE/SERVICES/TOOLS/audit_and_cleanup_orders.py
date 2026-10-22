# CORE/SERVICES/TOOLS/audit_and_cleanup_orders.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.TOOLS.audit_and_cleanup_orders
ROLE: Аудит и очистка лишних/сиротских ордеров Binance Futures через API-адаптер
==============================================================================
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Fix Windows console encoding if needed
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Setup paths
root_dir = Path(__file__).resolve().parent.parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from consts import API_KEY, API_SECRET
from c_log import UnifiedLogger
from API.BINANCE.client_prod import BinanceClient

logger = UnifiedLogger("AuditCleanup")


class OrderAuditor:
    """
    Институциональный сервис аудита и санитарной очистки ордеров.
    Сравнивает открытые позиции на бирже со списком активных ордеров,
    выявляет и отменяет сиротские ордера (позиция закрыта, ордер висит)
    и дублирующие TP ордера (оставляя самый свежий).
    """

    def __init__(self, api_key: str = API_KEY, api_secret: str = API_SECRET, client: Optional[BinanceClient] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self._client = client

    async def run(self, client: Optional[BinanceClient] = None) -> Dict[str, Any]:
        """Запускает полный цикл аудита ордеров и позиций."""
        logger.info("=" * 80)
        logger.info("[AUDIT] Запуск аудита открытых позиций и ордеров на Binance Futures...")
        logger.info("=" * 80)

        should_close = False
        active_client = client or self._client
        if active_client is None:
            active_client = BinanceClient(api_key=self.api_key, api_secret=self.api_secret)
            should_close = True

        canceled_count = 0
        try:
            # 1. Загрузка открытых ордеров через официальный метод адаптера
            open_orders = await active_client.fetch_open_orders()
            logger.info(f"[*] Всего активных ордеров на бирже: {len(open_orders)}")

            # 2. Загрузка открытых позиций через адаптер
            acc_res = await active_client.fetch_account_info()
            if not acc_res.success or not isinstance(acc_res.data, dict):
                logger.error(f"[ERR] Ошибка получения аккаунта: {acc_res.error_msg}")
                return {
                    "success": False,
                    "error": acc_res.error_msg,
                    "canceled_count": 0,
                }

            positions = acc_res.data.get("positions", [])
            active_positions = {
                (p["symbol"], p["positionSide"]): float(p["positionAmt"])
                for p in positions
                if abs(float(p.get("positionAmt", 0.0))) > 1e-8
            }

            total_wallet = acc_res.data.get("totalWalletBalance", "0")
            total_unrealized = acc_res.data.get("totalUnrealizedProfit", "0")
            logger.info(
                f"[*] Баланс кошелька: {float(total_wallet):.2f} USDT | Плавающий PnL: {float(total_unrealized):.2f} USDT"
            )
            logger.info(f"[*] Открытых позиций: {len(active_positions)}")
            for (sym, pside), amt in active_positions.items():
                logger.info(f"   - {sym} {pside}: {amt}")

            logger.info("-" * 80)
            logger.info("[*] Анализ соответствия ордеров позициям:")

            orders_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
            for o in open_orders:
                key = (o["symbol"], o["positionSide"])
                orders_by_key.setdefault(key, []).append(o)

            # Проверка ордеров на закрытые стороны (ордер есть, позиции нет)
            for (sym, pside), o_list in orders_by_key.items():
                pos_amt = active_positions.get((sym, pside), 0.0)

                if pos_amt == 0.0:
                    logger.warning(
                        f"[SIROTA] Обнаружен ордер без открытой позиции: {sym} {pside} ({len(o_list)} шт.)! Отмена..."
                    )
                    for o in o_list:
                        oid = o["orderId"]
                        c_res = await active_client.cancel_order(symbol=sym, order_id=oid)
                        logger.info(f"   [DEL] Отменен ордер {oid} по {sym}: {c_res.success}")
                        canceled_count += 1
                elif len(o_list) > 1:
                    logger.warning(f"[DUBL] Обнаружено {len(o_list)} ордеров на одну позицию {sym} {pside}!")
                    # Оставляем самый свежий (по времени или ID), остальные отменяем
                    sorted_orders = sorted(o_list, key=lambda x: int(x.get("time", 0)), reverse=True)
                    keep_order = sorted_orders[0]
                    logger.info(f"   [KEEP] Оставляем актуальный TP: ID {keep_order['orderId']} @ {keep_order.get('price')}")
                    for old_o in sorted_orders[1:]:
                        oid = old_o["orderId"]
                        c_res = await active_client.cancel_order(symbol=sym, order_id=oid)
                        logger.info(f"   [DEL] Отменен дублирующий ордер {oid} @ {old_o.get('price')}: {c_res.success}")
                        canceled_count += 1
                else:
                    o = o_list[0]
                    logger.info(f"   [OK] {sym} {pside}: ордер ID {o['orderId']} @ {o.get('price')} (qty: {o.get('origQty')}) синхронен.")

            logger.info("-" * 80)
            if canceled_count == 0:
                logger.info("[SUCCESS] Все чисто! Лишних ордеров на бирже нет.")
                logger.info("          Каждый TP соответствует своей открытой позиции (1 позиция = 1 TP).")
            else:
                logger.info(f"[CLEANUP] Успешно отменено лишних ордеров: {canceled_count}")
            logger.info("=" * 80)

            return {
                "success": True,
                "open_orders": len(open_orders),
                "active_positions": len(active_positions),
                "canceled_count": canceled_count,
            }

        finally:
            if should_close:
                await active_client.close()


# Модульная функция для обратной совместимости
async def audit_and_cleanup(client: Optional[BinanceClient] = None) -> Dict[str, Any]:
    auditor = OrderAuditor()
    return await auditor.run(client=client)


if __name__ == "__main__":
    asyncio.run(audit_and_cleanup())
