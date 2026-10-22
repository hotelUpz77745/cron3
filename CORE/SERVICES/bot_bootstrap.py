# ==============================================================================
# Path: CORE/SERVICES/bot_bootstrap.py
# Role: Сервис инициализации, прогрева стримов и корректного завершения торгового ядра (BotCore Bootstrap)
# ==============================================================================

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, List, Dict

from consts import (
    API_KEY,
    TG_ENABLED,
    PRICE_INIT_TIMEOUT_SEC,
    PRICE_STREAM_READY_TIMEOUT_SEC,
    PRICE_STREAM_SNAPSHOT_TIMEOUT_SEC,
    POS_STREAM_CONNECT_TIMEOUT_SEC,
)
from c_log import UnifiedLogger
from API.BINANCE.public import BinancePublic
from POS_FSM.pos_stream import PositionStream
from POS_FSM.pos_stream_monitor import PositionMonitor, BinanceWsInterpreter
from RUNTIME_FSM.runtime_builder import RuntimeBuilder, build_runtime_caches, prompt_runtime_check
from CORE.strategy_registry import StrategyRegistry
from CORE.strategy_applicator import StrategyApplicator

if TYPE_CHECKING:
    from CORE.bot import BotCore

logger = UnifiedLogger("BotBootstrap")


class BotBootstrapService:
    """
    Сервис управления жизненным циклом запуска, прогрева и остановки торгового ядра.
    Инкапсулирует валидацию обоймы символов, создание рантаймов, прогрев волатильности,
    запуск стримов (цен и позиций), REST-синхронизацию и корректный shutdown.
    """

    @classmethod
    async def prepare_game_loop(cls, bot_core: "BotCore") -> bool:
        """ШАГ 0 и 1: Валидация списка монет, прогрев кэшей и волатильности."""
        while bot_core.is_running and not bot_core.symbols:
            logger.warning("[BotBootstrap] Symbol list is empty. Waiting for symbols to be configured in app.json...")
            await asyncio.sleep(5.0)
            bot_core.strategy_registry = StrategyRegistry()
            active_syms = bot_core.strategy_registry.get_all_active_symbols()
            if active_syms:
                bot_core.symbols = active_syms

        if not bot_core.is_running:
            return False

        # Сборка и проверка рантайм кешей (создание отсутствующих JSON)
        created_new = build_runtime_caches(bot_core.symbols, registry=bot_core.strategy_registry)
        if created_new and not TG_ENABLED:
            prompt_runtime_check()

        # Первый расчет волатильности
        if bot_core.volatility_manager:
            try:
                await bot_core.volatility_manager.process_all()
            except Exception as e:
                logger.error(f"Error during initial VolatilityManager process_all: {e}")
            bot_core.volatility_manager.start()

        if bot_core.strategy_registry:
            try:
                StrategyApplicator().build_all_runtimes(bot_core.strategy_registry)
            except Exception as e:
                logger.warning(f"[BotBootstrap] Startup StrategyApplicator sync warning: {e}")

        bot_core.runtime_manager.load_initial_caches(bot_core.symbols)
        bot_core.runtime_configs = bot_core.runtime_manager.caches
        return True

    @classmethod
    async def init_streams_and_fsm(cls, bot_core: "BotCore") -> None:
        """ШАГ 2 и 3: Запуск спецификаций, стримов цен/позиций, REST-синхронизация и прогрев."""
        bot_core.spec_manager.start()
        bot_core._price_task = asyncio.create_task(bot_core.price_stream.run(bot_core._on_tick), name="price_stream")

        bot_core.pos_monitor = PositionMonitor(
            states_cache=bot_core.fsm_states,
            target_symbols=bot_core.symbols,
            strategy_registry=bot_core.strategy_registry,
        )
        bot_core.runtime_manager.populate_fsm_from_cache(bot_core.fsm_states)

        bot_core.pos_stream = PositionStream(
            api_key=API_KEY,
            stop_flag=lambda: not bot_core.is_running,
            monitor=bot_core.pos_monitor,
            target_symbols=set(bot_core.symbols),
            client=bot_core.client
        )

        bot_core.ws_interpreter = BinanceWsInterpreter(
            monitor=bot_core.pos_monitor, 
            target_symbols=set(bot_core.symbols)
        )
        bot_core.pos_stream.register_callback(bot_core.ws_interpreter.process_message)
        bot_core._pos_task = asyncio.create_task(bot_core.pos_stream.start(), name="pos_stream")

        logger.info("Main _game_loop started. Specifications and price streams are running.")
        logger.info("Waiting for price streams to connect and snapshot...")
        try:
            await asyncio.wait_for(bot_core.price_stream.ready.wait(), timeout=PRICE_STREAM_READY_TIMEOUT_SEC)
            if hasattr(bot_core.price_stream, "snapshot_received"):
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(bot_core.price_stream.snapshot_received.wait(), timeout=PRICE_STREAM_SNAPSHOT_TIMEOUT_SEC)
            logger.info("Price streams connected and snapshot received.")
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for price streams! Waiting for prices...")

        # Дожидаемся цен от стрима с фолбеком на bulk REST
        logger.info(f"Waiting for price initialization for all {len(bot_core.symbols)} symbols (up to {int(PRICE_INIT_TIMEOUT_SEC)}s)...")
        wait_start = time.time()
        while time.time() - wait_start < PRICE_INIT_TIMEOUT_SEC and bot_core.is_running:
            missing = [s for s in bot_core.symbols if not bot_core.prices.get(s) or (isinstance(bot_core.prices.get(s), tuple) and bot_core.prices[s][0] <= 0)]
            if not missing:
                logger.info(f"All {len(bot_core.symbols)} symbol prices successfully initialized from stream in {time.time() - wait_start:.1f}s!")
                break
            await asyncio.sleep(0.5)
        else:
            missing = [s for s in bot_core.symbols if not bot_core.prices.get(s) or (isinstance(bot_core.prices.get(s), tuple) and bot_core.prices[s][0] <= 0)]
            if missing:
                logger.warning(
                    f"[PRICE_INIT] Price init timeout ({int(PRICE_INIT_TIMEOUT_SEC)}s): {len(missing)}/{len(bot_core.symbols)} symbols missing. "
                    f"Executing abnormal bulk REST fallback for all symbols..."
                )
                try:
                    bulk_prices = await BinancePublic.get_prices_bulk(bot_core.symbols)
                    now_ts = time.time()
                    for sym, p in bulk_prices.items():
                        bot_core.prices[sym] = (p, now_ts)
                    logger.info(f"[PRICE_INIT] Bulk REST fallback populated {len(bulk_prices)}/{len(bot_core.symbols)} symbols.")
                except Exception as e:
                    logger.warning(f"[PRICE_INIT] Bulk REST fallback failed: {e}")

        logger.info("Waiting for Position Stream to connect...")
        pos_wait_start = time.time()
        while not bot_core.pos_stream.ready and (time.time() - pos_wait_start) < POS_STREAM_CONNECT_TIMEOUT_SEC:
            await asyncio.sleep(0.1)

        if not bot_core.pos_stream.ready:
            logger.warning("Position Stream failed to connect in time! Relying on REST failsafe.")
        else:
            logger.info("Position Stream connected successfully.")

        # Начальный стейт позиций по REST
        await bot_core.pos_monitor.sync_from_rest(bot_core.client, bot_core.symbols)

        # Синхронизируем уровни сетки с реальными биржевыми объемами
        for sym in bot_core.symbols:
            strat = bot_core.strategy_registry.get_strategy_for_symbol(sym) if bot_core.strategy_registry else None
            for s_side in ("LONG", "SHORT"):
                st = bot_core.fsm_states.get(sym, {}).get(s_side)
                if st and st.in_position and abs(st.total_volume) > 1e-6:
                    cur_p = bot_core.prices.get(sym, (0.0, 0))[0] if sym in bot_core.prices else None
                    StrategyApplicator.resolve_position_state_grid(st, strategy=strat, current_price=cur_p)

        logger.info("Streams and REST synced! Running initial RuntimeFSM Sync...")
        await bot_core.runtime_manager.sync_with_fsm(bot_core.fsm_states)

        logger.info("Waiting for exchange specifications to load...")
        while not bot_core.spec_data.get("symbols") and bot_core.is_running:
            await asyncio.sleep(0.5)
        logger.info("Exchange specifications loaded.")

        # Прогрев плеча и маржи
        logger.info(f"[BotBootstrap] Pre-warming leverage and margin settings for {len(bot_core.symbols)} active symbols...")
        for sym in bot_core.symbols:
            cfg = bot_core.runtime_configs.get(sym, {})
            side_cfg = cfg.get("LONG") or cfg.get("SHORT")
            if side_cfg:
                try:
                    await bot_core.leverage_manager.set_leverage_and_margin(bot_core.client, sym, side_cfg)
                except Exception as e:
                    logger.warning(f"[{sym}] Leverage pre-warm warning: {e}")

        # Первоначальный аудит и восстановление TP ордеров
        await bot_core._reconcile_open_orders(force=True)

        # Notifier & Watchdog
        await bot_core.notifier.start()
        bot_core._watchdog_task = asyncio.create_task(bot_core.watchdog.start(), name="watchdog")

        # Аналитика
        logger.info("[BotBootstrap] All systems & prices fully warmed up. Running initial sync and starting analytics tracker...")
        await bot_core.analytics.deep_sync_analytics(bot_core.client, bot_core.prices)
        bot_core.analytics.start_realtime_tracker(bot_core.client)

    @classmethod
    async def shutdown_game_loop(cls, bot_core: "BotCore") -> None:
        """Корректная остановка задач и стримов игрового цикла."""
        bot_core.is_running = False
        await bot_core.notifier.stop()
        if bot_core.watchdog:
            bot_core.watchdog.stop()
        if bot_core._watchdog_task:
            bot_core._watchdog_task.cancel()
        bot_core.spec_manager.stop()
        if bot_core.price_stream:
            bot_core.price_stream.stop()
        if bot_core._price_task:
            bot_core._price_task.cancel()
        if bot_core._pos_task:
            bot_core._pos_task.cancel()

        for creds, cl in getattr(bot_core, '_key_clients', {}).items():
            with contextlib.suppress(Exception):
                await cl.shutdown()

        tasks = [t for t in (bot_core._price_task, bot_core._pos_task, bot_core._watchdog_task) if t]
        if hasattr(bot_core, '_pos_tasks'):
            tasks.extend([t for t in bot_core._pos_tasks if t])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
