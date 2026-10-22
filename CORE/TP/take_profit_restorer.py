# ==============================================================================
# Path: CORE/TP/take_profit_restorer.py
# Role: Автономный ревизор и восстановитель Take Profit лимитных ордеров при запуске и в рантайме
# ==============================================================================

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from consts import API_KEY, API_SECRET
from c_log import UnifiedLogger
from CORE._utils import RiskCalculatingUtils, TradeMath, Utils
from API.BINANCE.client_prod import BinanceClient
from API.BINANCE.public import BinancePublic
from RUNTIME_FSM.runtime_manager import RuntimeFsmManager

if TYPE_CHECKING:
    from CORE.bot import BotCore

logger = UnifiedLogger("TakeProfitRestorer")


class TakeProfitRestorer:
    """
    Утилита и подсистема гарантированного восстановления Take Profit ордеров.
    
    При старте бота и периодически в фоновом режиме:
    1. Сканирует все открытые позиции на Binance.
    2. Сверяет каждую открытую позицию с активными биржевыми лимитными ордерами.
    3. Если ордер отсутствует или объем не совпадает — немедленно выставляет
       корректный лимитный ордер закрытия строго от средней цены входа (avg_entry_price).
    """

    def __init__(self, bot_core: Optional[BotCore] = None):
        self.bot_core = bot_core
        self._last_run_ts: float = 0.0

    async def verify_and_restore_all(self, force: bool = False) -> Dict[str, int]:
        """
        Основной метод аудита и восстановления ордеров для BotCore.
        Возвращает статистику: {'checked': N, 'confirmed': N, 'restored': N, 'mismatch': N}.
        """
        if not self.bot_core:
            logger.warning("[TP_RESTORER] BotCore instance not provided.")
            return {"checked": 0, "confirmed": 0, "restored": 0, "mismatch": 0}

        now = time.time()
        if not force and (now - self._last_run_ts < 15.0):
            return {"checked": 0, "confirmed": 0, "restored": 0, "mismatch": 0}
        self._last_run_ts = now

        stats = {"checked": 0, "confirmed": 0, "restored": 0, "mismatch": 0}

        try:
            # 1. Запрашиваем все открытые ордера по аккаунту одним пакетным запросом
            try:
                open_orders = await self.bot_core.client.fetch_open_orders()
            except Exception as e:
                err_str = str(e)
                is_transient = any(
                    kw in err_str.lower()
                    for kw in ("502", "503", "504", "bad gateway", "gateway timeout", "timeout", "connection", "network", "temporarily unavailable")
                ) or isinstance(e, (asyncio.TimeoutError, ConnectionError, OSError))

                if is_transient:
                    logger.warning(f"[TP_RESTORER] ⚠️ Временная недоступность Binance API при получении открытых ордеров ({e}). Пропуск аудита.")
                    return stats
                raise

            orders_map: Dict[tuple[str, str], List[dict]] = {}
            for o in open_orders:
                sym = o.get("symbol", "").upper()
                pos_side = o.get("positionSide", "").upper()
                orders_map.setdefault((sym, pos_side), []).append(o)

            # 2. Обходим все активные инструменты бота
            for symbol in self.bot_core.symbols:
                states = self.bot_core.fsm_states.get(symbol, {})
                for side in ("LONG", "SHORT"):
                    state = states.get(side)
                    if not state:
                        continue

                    # Проверяем наличие активной позиции
                    pos_vol = float(getattr(state, "total_volume", 0.0))
                    in_pos = bool(getattr(state, "in_position", False))

                    if not in_pos or abs(pos_vol) <= 1e-8:
                        continue

                    # Игнорируем позиции в процессе закрытия, завершения, усреднения или если уже идет выставление TP
                    if (
                        getattr(state, "is_closing", False) is True
                        or getattr(state, "is_finished", False) is True
                        or getattr(state, "pending_avg", False) is True
                        or getattr(state, "pending_rolling_tp", False) is True
                        or symbol in getattr(self.bot_core, "_closing_symbols", set())
                    ):
                        continue

                    if hasattr(state, "can_place_tp") and callable(state.can_place_tp):
                        if state.can_place_tp() is False:
                            continue

                    stats["checked"] += 1
                    key = (symbol.upper(), side.upper())
                    matching_orders = orders_map.get(key, [])
                    sym_client = self.bot_core.get_client_for_symbol(symbol)
                    opposite_state = states.get("SHORT" if side == "LONG" else "LONG")

                    price_data = self.bot_core.prices.get(symbol)
                    current_p = price_data[0] if isinstance(price_data, tuple) else (price_data or state.avg_entry_price)

                    # СЛУЧАЙ 1: Ордер полностью отсутствует на бирже
                    if not matching_orders:
                        logger.warning(
                            f"[TP_RESTORER] ⚠️ [{symbol}] {side}: Позиция {pos_vol} @ {state.avg_entry_price} "
                            f"НЕ ИМЕЕТ ордера Take Profit на Binance! Немедленное восстановление..."
                        )
                        success = await self.bot_core.tp_manager.place_take_profit(
                            sym_client, symbol, side, current_p, self.bot_core.spec_data, state, opposite_state
                        )
                        if success:
                            stats["restored"] += 1
                            logger.info(f"[TP_RESTORER] ✅ [{symbol}] {side}: Take Profit успешно восстановлен.")
                        else:
                            logger.error(f"[TP_RESTORER] ❌ [{symbol}] {side}: Не удалось выставить восстанавливающий TP ордер.")
                        continue

                    # СЛУЧАЙ 2: Ордер есть, проверяем совпадение объема
                    total_ord_vol = sum(float(o.get("origQty", 0.0)) for o in matching_orders)
                    if abs(total_ord_vol - abs(pos_vol)) > 1e-4:
                        logger.warning(
                            f"[TP_RESTORER] ⚠️ [{symbol}] {side}: Несоответствие объема TP! "
                            f"Позиция: {abs(pos_vol)}, Ордера: {total_ord_vol}. Перевыставляем..."
                        )
                        stats["mismatch"] += 1
                        success = await self.bot_core.tp_manager.place_take_profit(
                            sym_client, symbol, side, current_p, self.bot_core.spec_data, state, opposite_state
                        )
                        if success:
                            stats["restored"] += 1
                            logger.info(f"[TP_RESTORER] ✅ [{symbol}] {side}: Объем TP успешно синхронизирован.")
                    else:
                        stats["confirmed"] += 1
                        o_first = matching_orders[0]
                        logger.debug(
                            f"[TP_RESTORER] [{symbol}] {side}: TP ордер ID:{o_first.get('orderId')} "
                            f"@ {o_first.get('price')} (vol={total_ord_vol}) подтвержден."
                        )

            if stats["restored"] > 0:
                logger.info(
                    f"[TP_RESTORER] Итог аудита: Проверено {stats['checked']} поз., "
                    f"Подтверждено {stats['confirmed']}, Восстановлено {stats['restored']} TP ордеров."
                )

        except Exception as e:
            err_str = str(e)
            is_transient = any(
                kw in err_str.lower()
                for kw in ("502", "503", "504", "bad gateway", "gateway timeout", "timeout", "connection", "network", "temporarily unavailable")
            ) or isinstance(e, (asyncio.TimeoutError, ConnectionError, OSError))

            if is_transient:
                logger.warning(f"[TP_RESTORER] ⚠️ Временная ошибка сети/API при аудите TP ({e}). Пропуск тика.")
            else:
                logger.error(f"[TP_RESTORER] Ошибка во время аудита TP ордеров: {e}", exc_info=True)

        return stats


async def standalone_restore():
    """Точка входа для запуска утилиты из консоли: python -m CORE.TP.take_profit_restorer"""
    print("=" * 80)
    print("🚀 [STANDALONE TP RESTORER] Аудит и гарантированное восстановление TP ордеров")
    print("=" * 80)

    client = BinanceClient(api_key=API_KEY, api_secret=API_SECRET)
    instruments = await BinancePublic.get_instruments()
    if not instruments:
        print("❌ Ошибка загрузки спецификаций через BinancePublic")
        await client.shutdown()
        return
    spec_data = {"symbols": instruments}

    acc_res = await client.fetch_account_info()
    if not acc_res.success or not isinstance(acc_res.data, dict):
        print(f"❌ Ошибка запроса аккаунта: {acc_res}")
        await client.shutdown()
        return

    positions = [
        p for p in acc_res.data.get("positions", [])
        if abs(float(p.get("positionAmt", 0.0))) > 1e-8
    ]
    print(f"[*] Открытых позиций на аккаунте: {len(positions)}")

    open_orders = await client.fetch_open_orders()
    print(f"[*] Активных ордеров на аккаунте: {len(open_orders)}")

    orders_map: Dict[tuple[str, str], List[dict]] = {}
    for o in open_orders:
        orders_map.setdefault((o["symbol"].upper(), o["positionSide"].upper()), []).append(o)

    runtime_mgr = RuntimeFsmManager()
    symbols_list = list(set(p["symbol"] for p in positions))
    runtime_mgr.load_initial_caches(symbols_list)

    restored = 0
    confirmed = 0

    for p in positions:
        sym = p["symbol"].upper()
        side = p["positionSide"].upper()
        amt = float(p.get("positionAmt", 0.0))
        entry = float(p.get("entryPrice", 0.0))
        key = (sym, side)

        matching = orders_map.get(key, [])
        if matching:
            tot_vol = sum(float(o.get("origQty", 0.0)) for o in matching)
            if abs(tot_vol - abs(amt)) <= 1e-4:
                print(f"  ✅ [OK] {sym} {side}: Позиция {amt} @ {entry} -> Ордер ID:{matching[0]['orderId']} @ {matching[0]['price']} (Vol: {tot_vol})")
                confirmed += 1
                continue
            else:
                print(f"  ⚠️ [MISMATCH] {sym} {side}: Объем ордера {tot_vol} != объем позиции {abs(amt)}")

        print(f"\n  🚨 [ТРЕБУЕТСЯ ВОССТАНОВЛЕНИЕ] {sym} {side}: Позиция {amt} @ {entry}")
        cache = runtime_mgr.caches.get(sym, {}).get(side, {})
        tp_map = cache.get("tp_map", {})
        grid = cache.get("grid", {})
        level = RiskCalculatingUtils.get_current_grid_level(grid)
        if level not in tp_map and tp_map:
            level = "0"

        tp_cfg = tp_map.get(level, {})
        indent = float(tp_cfg.get("indent") or tp_cfg.get("fallback_indent") or 2.0)
        tp_price = TradeMath.calculate_take_profit_price(entry, indent, side, spec_data, sym)
        vol_float = abs(amt)
        volume = TradeMath.round_qty(vol_float, spec_data, sym)
        order_side = "SELL" if side == "LONG" else "BUY"

        print(f"     -> Выставляем лимитный TP строго от средней цены {entry}: {order_side} {volume} @ {tp_price} (уровень {level}, отступ {indent}%)...")
        res = await client.make_order(
            symbol=sym,
            qty=volume,
            side=order_side,
            position_side=side,
            market_type="LIMIT",
            price=tp_price,
        )
        if res.success and res.data:
            print(f"     ✅ [SUCCESS] TP ордер выставлен! Order ID: {res.data.get('orderId')}")
            restored += 1
            if level in tp_map:
                cache["tp_map"][level]["is_active"] = True
            await runtime_mgr.save_cache(sym)
        else:
            print(f"     ❌ [ERROR] Ошибка выставления: {res.error_msg}")

    print("=" * 80)
    print(f"📊 [ИТОГ]: Позиций: {len(positions)} | Подтверждено: {confirmed} | Восстановлено: {restored}")
    print("=" * 80)

    await client.shutdown()


if __name__ == "__main__":
    asyncio.run(standalone_restore())
