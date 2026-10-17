# CORE/SERVICES/emergency_closer.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.emergency_closer
ROLE: Сервис экстренного закрытия позиций и отмены ордеров на бирже
==============================================================================
"""

import asyncio
import inspect
import time
from typing import Dict, Any, List, Set, Optional
from c_log import UnifiedLogger
from c_utils import Utils
from consts import (
    ANALYTICS_DIR,
    CACHE_DIR,
    COOLDOWN_POST_CANDLE_BUFFER_SEC,
    COOLDOWN_MIN_HEARTBEAT_SEC,
)

logger = UnifiedLogger("EmergencyCloser")


class EmergencyCloserService:
    """
    Высокоуровневый сервис экстренной ликвидации позиций и отмены лимитных ордеров.
    Инкапсулирует идемпотентные блокировки, рыночные ордера закрытия и синхронизацию FSM.
    """

    @staticmethod
    def calc_cooldown(now: float, heartbeat_sec: int) -> float:
        hb = max(int(heartbeat_sec), COOLDOWN_MIN_HEARTBEAT_SEC)
        candle_boundary = ((int(now) // hb) + 1) * hb
        target = float(candle_boundary) + COOLDOWN_POST_CANDLE_BUFFER_SEC
        min_cooldown = now + COOLDOWN_MIN_HEARTBEAT_SEC
        return max(target, min_cooldown)

    @classmethod
    async def close_all(cls, bot_core: Any, reset_iterations: bool = True) -> Dict[str, Any]:
        """Экстренное закрытие всех позиций и отмена лимитных ордеров для текущей обоймы монет."""
        symbols = bot_core.symbols if bot_core.symbols else (
            bot_core.strategy_registry.get_all_active_symbols() if bot_core.strategy_registry else []
        )
        if not symbols:
            logger.warning("[CLOSE_ALL] No active symbols to close.")
            return {}

        symbols_set = {s.upper() for s in symbols}

        async with bot_core._close_lock:
            # 1. Мгновенная разметка флагов состояний
            bot_core._closing_symbols.update(symbols_set)
            for sym in symbols_set:
                if sym in bot_core.fsm_states:
                    for s_side in ("LONG", "SHORT"):
                        if s_side in bot_core.fsm_states[sym]:
                            bot_core.fsm_states[sym][s_side].is_closing = True

            now = time.time()
            raw_hb = bot_core.get_strategy_heartbeat_seconds()
            heartbeat_sec = int(raw_hb) if isinstance(raw_hb, (int, float)) else 300
            if hasattr(bot_core, "calc_heartbeat_window_cooldown"):
                try:
                    res = bot_core.calc_heartbeat_window_cooldown(now, heartbeat_sec)
                    cooldown_until = float(res) if isinstance(res, (int, float)) else cls.calc_cooldown(now, heartbeat_sec)
                except Exception:
                    cooldown_until = cls.calc_cooldown(now, heartbeat_sec)
            else:
                cooldown_until = cls.calc_cooldown(now, heartbeat_sec)

            prev_block = getattr(bot_core, 'block_until_timestamp', 0.0)
            prev_block = prev_block if isinstance(prev_block, (int, float)) else 0.0
            bot_core.entry_blocked = True
            bot_core.block_until_timestamp = max(prev_block, cooldown_until)

            if not isinstance(getattr(bot_core, "symbol_blocked_until", None), dict):
                bot_core.symbol_blocked_until = {}
            if not isinstance(getattr(bot_core, "strategy_blocked_until", None), dict):
                bot_core.strategy_blocked_until = {}

            for sym in symbols_set:
                prev_s = bot_core.symbol_blocked_until.get(sym, 0.0)
                prev_s = prev_s if isinstance(prev_s, (int, float)) else 0.0
                bot_core.symbol_blocked_until[sym] = max(prev_s, cooldown_until)

            if bot_core.strategy_registry and bot_core.strategy_registry.active_strategies:
                for sid in bot_core.strategy_registry.active_strategies.keys():
                    prev_st = bot_core.strategy_blocked_until.get(sid, 0.0)
                    prev_st = prev_st if isinstance(prev_st, (int, float)) else 0.0
                    bot_core.strategy_blocked_until[sid] = max(prev_st, cooldown_until)

            rem_s = int(cooldown_until - now)
            logger.warning(
                f"🛑 [CLOSE_ALL] Idempotent entry cooldown activated until {cooldown_until} "
                f"({rem_s}s remaining) for symbols: {symbols}"
            )

            try:
                # 2. Отмена всех активных лимитных ордеров
                for sym in symbols_set:
                    async with bot_core._get_symbol_lock(sym):
                        sym_client = bot_core.get_client_for_symbol(sym)
                        try:
                            await sym_client.cancel_all_orders(sym)
                            logger.info(f"[{sym}] Canceled limit orders.")
                        except Exception as e:
                            logger.error(f"[{sym}] Failed to cancel orders: {e}")

                # 3. Закрытие всех открытых позиций по рынку
                try:
                    positions = await bot_core.client.fetch_positions()
                    for p in positions:
                        sym = (p.get("symbol") or "").upper()
                        if sym not in symbols_set:
                            continue
                        amt = float(p.get("positionAmt", 0.0))
                        if abs(amt) > 0:
                            pos_side = p.get("positionSide")
                            side = "SELL" if pos_side == "LONG" else "BUY"
                            sym_client = bot_core.get_client_for_symbol(sym)
                            async with bot_core._get_symbol_lock(sym):
                                logger.info(f"[{sym}] Closing {pos_side} pos. Qty: {abs(amt)}")
                                await sym_client.make_order(
                                    symbol=sym,
                                    qty=abs(amt),
                                    side=side,
                                    position_side=pos_side,
                                    market_type="MARKET"
                                )
                except Exception as e:
                    logger.error(f"[CLOSE_ALL] Error closing positions on exchange: {e}")

                # 3.5 Контрольный ре-чек с задержкой 0.3с
                await asyncio.sleep(0.3)
                try:
                    positions_recheck = await bot_core.client.fetch_positions()
                    for p in positions_recheck:
                        sym = (p.get("symbol") or "").upper()
                        if sym not in symbols_set:
                            continue
                        amt = float(p.get("positionAmt", 0.0))
                        if abs(amt) > 0:
                            pos_side = p.get("positionSide")
                            side = "SELL" if pos_side == "LONG" else "BUY"
                            sym_client = bot_core.get_client_for_symbol(sym)
                            async with bot_core._get_symbol_lock(sym):
                                logger.critical(f"[{sym}] [RE-CHECK] Emergency closing hanging {pos_side} pos! Qty: {abs(amt)}")
                                await sym_client.make_order(
                                    symbol=sym,
                                    qty=abs(amt),
                                    side=side,
                                    position_side=pos_side,
                                    market_type="MARKET"
                                )
                                await sym_client.cancel_all_orders(sym)
                except Exception as e:
                    logger.error(f"[CLOSE_ALL] Error during re-check: {e}")

                # 4. Сброс стейтов и рантайм-кэшей
                for sym in symbols_set:
                    if sym in bot_core.fsm_states:
                        for side in ("LONG", "SHORT"):
                            state = bot_core.fsm_states[sym][side]
                            bot_core.runtime_manager.reset_state_to_default(sym, side, state)
                            bot_core.avg_manager.reset(sym, side)
                    if hasattr(bot_core, "runtime_manager") and bot_core.runtime_manager:
                        save_fn = getattr(bot_core.runtime_manager, "save_cache", None)
                        if callable(save_fn):
                            res = save_fn(sym)
                            if inspect.isawaitable(res):
                                await res
            finally:
                # 5. Очистка блокировок закрытия
                bot_core._closing_symbols.difference_update(symbols_set)

            # 6. Сброс итераций и синхронизация лидерборда, если запрошено
            reset_report = {}
            if reset_iterations:
                if bot_core.strategy_registry and bot_core.strategy_registry.active_strategies:
                    for sid in bot_core.strategy_registry.active_strategies.keys():
                        try:
                            reset_report[sid] = await bot_core.reset_strategy_iteration(sid)
                        except Exception as re:
                            logger.error(f"[{sid}] Error resetting strategy iteration: {re}")

                # Сброс глобальной итерации портфеля
                try:
                    bot_core.analytics.reset_global_iteration()
                except Exception as ge:
                    logger.error(f"[CLOSE_ALL] Error resetting global portfolio iteration: {ge}")

                # Синхронизация Лидерборда
                try:
                    if hasattr(bot_core.analytics, "leaderboard_engine") and bot_core.strategy_registry:
                        universes_meta = {
                            sid: {**getattr(scfg, "raw_meta", {}), "name": scfg.name, "enabled": True}
                            for sid, scfg in bot_core.strategy_registry.active_strategies.items()
                        }
                        entries = bot_core.analytics.leaderboard_engine.generate_leaderboard(universes_meta)
                        evaluated_coins = len(bot_core.strategy_registry.get_all_active_symbols())
                        bot_core.analytics.leaderboard_engine.save_portfolio(entries, evaluated_coins_count=evaluated_coins)
                        logger.info(f"[LEADERBOARD] Synchronized leaderboard on Close All. Active: {len(entries)}")
                except Exception as le:
                    logger.error(f"[CLOSE_ALL] Error syncing leaderboard on Close All: {le}")

            logger.info("🛑 [CLOSE_ALL] All positions closed, limit orders canceled, runtimes reset.")
            return reset_report

    @classmethod
    async def close_strategy(cls, bot_core: Any, strategy_id: str) -> None:
        """Штатное закрытие позиций и отмена ордеров СТРОГО для монет одной стратегии."""
        if not bot_core.strategy_registry:
            return
        strat = bot_core.strategy_registry.active_strategies.get(strategy_id)
        if not strat or not strat.symbols:
            return

        symbols_set = {s.upper() for s in strat.symbols}
        logger.info(f"[{strategy_id}] Closing positions and orders for strategy symbols: {symbols_set}")

        now = time.time()
        raw_hb = bot_core.get_strategy_heartbeat_seconds(strategy_id=strategy_id)
        heartbeat_sec = int(raw_hb) if isinstance(raw_hb, (int, float)) else 300
        if hasattr(bot_core, "calc_heartbeat_window_cooldown"):
            try:
                res = bot_core.calc_heartbeat_window_cooldown(now, heartbeat_sec)
                cooldown_until = float(res) if isinstance(res, (int, float)) else cls.calc_cooldown(now, heartbeat_sec)
            except Exception:
                cooldown_until = cls.calc_cooldown(now, heartbeat_sec)
        else:
            cooldown_until = cls.calc_cooldown(now, heartbeat_sec)

        if not isinstance(getattr(bot_core, "strategy_blocked_until", None), dict):
            bot_core.strategy_blocked_until = {}
        if not isinstance(getattr(bot_core, "symbol_blocked_until", None), dict):
            bot_core.symbol_blocked_until = {}

        prev_st = bot_core.strategy_blocked_until.get(strategy_id, 0.0)
        prev_st = prev_st if isinstance(prev_st, (int, float)) else 0.0
        bot_core.strategy_blocked_until[strategy_id] = max(prev_st, cooldown_until)

        for sym in symbols_set:
            prev_s = bot_core.symbol_blocked_until.get(sym, 0.0)
            prev_s = prev_s if isinstance(prev_s, (int, float)) else 0.0
            bot_core.symbol_blocked_until[sym] = max(prev_s, cooldown_until)

        all_active_syms = set()
        if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
            try:
                all_active_syms = {s.upper() for s in bot_core.strategy_registry.get_all_active_symbols()}
            except Exception:
                all_active_syms = set()
        if not all_active_syms or symbols_set >= all_active_syms:
            prev_block = getattr(bot_core, 'block_until_timestamp', 0.0)
            prev_block = prev_block if isinstance(prev_block, (int, float)) else 0.0
            bot_core.block_until_timestamp = max(prev_block, cooldown_until)
            bot_core.entry_blocked = True

        async with bot_core._close_lock:
            bot_core._closing_strategies.add(strategy_id)
            bot_core._closing_symbols.update(symbols_set)

            for sym in symbols_set:
                if sym in bot_core.fsm_states:
                    for s_side in ("LONG", "SHORT"):
                        if s_side in bot_core.fsm_states[sym]:
                            bot_core.fsm_states[sym][s_side].is_closing = True

            strat_client = bot_core.get_client_for_strategy(strategy_id)
            # Отмена ордеров
            for sym in symbols_set:
                async with bot_core._get_symbol_lock(sym):
                    try:
                        await strat_client.cancel_all_orders(sym)
                        logger.info(f"[{strategy_id}][{sym}] Canceled limit orders.")
                    except Exception as e:
                        logger.error(f"[{strategy_id}][{sym}] Failed to cancel orders: {e}")

            # Закрытие позиций
            try:
                positions = await strat_client.fetch_positions()
                for p in positions:
                    sym = (p.get("symbol") or "").upper()
                    if sym not in symbols_set:
                        continue
                    amt = float(p.get("positionAmt", 0.0))
                    if abs(amt) > 0:
                        pos_side = p.get("positionSide")
                        side = "SELL" if pos_side == "LONG" else "BUY"
                        async with bot_core._get_symbol_lock(sym):
                            logger.info(f"[{strategy_id}][{sym}] Closing {pos_side} pos. Qty: {abs(amt)}")
                            await strat_client.make_order(
                                symbol=sym,
                                qty=abs(amt),
                                side=side,
                                position_side=pos_side,
                                market_type="MARKET"
                            )
            except Exception as e:
                logger.error(f"[{strategy_id}] Error closing positions: {e}")

            # Сброс стейтов
            for sym in symbols_set:
                if sym in bot_core.fsm_states:
                    for side in ("LONG", "SHORT"):
                        state = bot_core.fsm_states[sym][side]
                        bot_core.runtime_manager.reset_state_to_default(sym, side, state)
                        bot_core.avg_manager.reset(sym, side)
                if hasattr(bot_core, "runtime_manager") and bot_core.runtime_manager:
                    save_fn = getattr(bot_core.runtime_manager, "save_cache", None)
                    if callable(save_fn):
                        res = save_fn(sym)
                        if inspect.isawaitable(res):
                            await res

            bot_core._closing_strategies.discard(strategy_id)
            bot_core._closing_symbols.difference_update(symbols_set)
