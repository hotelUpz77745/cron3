# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\bot.py
# Role: bot.py module

# ==============================================================================
# Path: CORE/bot.py
# Role: Торговое ядро и основная логика
# ==============================================================================

import asyncio
import logging
import os
import time
from pathlib import Path

from consts import _CFG, DATA_DIR, TIME_SLACK_SEC, SPEC_TTL_SEC
from API.BINANCE.public import BinancePublic
from API.BINANCE.price_stream import BinanceHotPriceStream, HotPriceTick
from CORE.ENTRY.signal import TimeControl
from CORE.ENTRY.leverage_manager import LeverageManager
from CORE._utils import TradeMath
from API.BINANCE.client import BinanceClient
from POS_FSM.models import PositionState
from c_utils import Utils

from c_log import UnifiedLogger
from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
from consts import BACKUP_ENABLED, BACKUP_DEBOUNCE_SEC, BACKUP_MAX_INTERVAL_SEC
from CORE.runtime_backup import RuntimeBackupManager
from CORE.notifier import NotifierManager
from CORE.ENTRY.leverage_manager import LeverageManager
from CORE.TP.tp_manager import TakeProfitManager
from CORE.GRID.avg_manager import AverageManager
from CORE.TP.fallback_tp_manager import FallbackTpManager
from CORE._utils import SpecManager
from ANALYTICS.analytics import AnalyticsManager
from consts import TG_ENABLED
from consts import API_KEY, API_SECRET
from watchdog import LoopWatchdog
from consts import (
    WATCHDOG_TIMEOUT_SEC,
    WATCHDOG_CHECK_INTERVAL_SEC,
    WATCHDOG_HEARTBEAT_INTERVAL_SEC,
    WATCHDOG_HEARTBEAT_AUTODELETE_SEC
)
from CORE._utils import TradeMath
from consts import REST_FAILSAFE_SEC
from RUNTIME_FSM.runtime_builder import build_runtime_caches, prompt_runtime_check
from POS_FSM.pos_stream_monitor import PositionMonitor
from POS_FSM.pos_stream import PositionStream
from CORE.auto_closer import AutoCloser
from consts import API_KEY
from API.BINANCE.public import BinancePublic
from CORE.ADVANCED.volatility_manager import VolatilityManager
from consts import _CFG
logger = UnifiedLogger("BotCore")

BLOCK_ENTRY = False  # Глобальный флаг блокировки входа в позиции (для отладки)

class BotCore:
    def __init__(self):
        self.is_running = False
        # Работаем только с теми символами, которые прописаны в конфигах .app.json в разделе symbols
        self.symbols = _CFG["symbols"]
        self.prices = {}   # Структура для хранения цен
        self.runtime_manager = RuntimeFsmManager()
        

            
        self.notifier = NotifierManager()
        
        self.runtime_configs = self.runtime_manager.caches # Кеш рантаймов
        
        # Флаги готовности стримов
        self.pos_stream_synced = asyncio.Event()
        self.price_stream_synced = asyncio.Event()
        
        # Получаем таймфрейм из конфига app.json (секция signal)
        signal_cfg = _CFG["signal"]
        timeframe = signal_cfg.get("timeframe", "5m")
        self.time_control = TimeControl(interval=timeframe)
        
        # 2. Менеджеры
        self.leverage_manager = LeverageManager()
        
        self.tp_manager = TakeProfitManager(self.runtime_manager)
        
        self.avg_manager = AverageManager()
        
        self.fallback_tp_manager = FallbackTpManager()
        
        self.spec_manager = SpecManager(self)
        
        self.analytics = AnalyticsManager()
        
        if BACKUP_ENABLED:
            self.backup_manager = RuntimeBackupManager(
                debounce_sec=BACKUP_DEBOUNCE_SEC,
                max_interval_sec=BACKUP_MAX_INTERVAL_SEC
            )
            self.runtime_manager.backup_manager = self.backup_manager
            self.analytics.backup_manager = self.backup_manager
        else:
            self.backup_manager = None
            
        from CORE.redis_manager import RedisManager
        self.redis_manager = RedisManager(debounce_sec=1.0)
        self.runtime_manager.redis_manager = self.redis_manager
        self.analytics.redis_manager = self.redis_manager
        
        from consts import SEMAPHORE_ENABLED, SEMAPHORE_BOT_NAME, SEMAPHORE_SERVER_NAME, SEMAPHORE_ARBITER_IP, SEMAPHORE_PULL_PORT, SEMAPHORE_PUB_PORT
        if SEMAPHORE_ENABLED:
            from semaphore import NodeSemaphore
            self.semaphore = NodeSemaphore(
                bot_name=SEMAPHORE_BOT_NAME,
                server_name=SEMAPHORE_SERVER_NAME,
                arbiter_ip=SEMAPHORE_ARBITER_IP,
                pull_port=SEMAPHORE_PULL_PORT,
                pub_port=SEMAPHORE_PUB_PORT
            )
            # Принудительно ставим True, чтобы при первом входе в АКТИВНЫЙ режим 
            # (даже при старте) бот скачал свежий стейт из Redis.
            self.was_passive = True
        else:
            self.semaphore = None
            self.was_passive = False
        
        auto_start = _CFG["app"]["auto_start"]
        if TG_ENABLED:
            self.is_paused = not auto_start
        else:
            self.is_paused = False
            
        self.entry_blocked = False
        
        spec_cache_path = DATA_DIR / "CACHE" / "specifications.json"
        self.spec_data = Utils.read_json_file(spec_cache_path)
        

        self.client = BinanceClient(api_key=API_KEY, api_secret=API_SECRET)
        
        # Кеш FSM-состояний в формате {symbol: {"LONG": PositionState, "SHORT": PositionState}}
        self.fsm_states = {}
        
        # Сетевые адаптеры и стримы
        stream_symbols = list(self.symbols) + ["BNBUSDT"] if "BNBUSDT" not in self.symbols else list(self.symbols)
        self.price_stream = BinanceHotPriceStream(stream_symbols)

        # Контроль главный петли (Watchdog)
        self._last_tick = time.time()
        self._tick_count = 0
        self.logger = logger
        # server_name is purely cosmetic for Telegram, so it's genuinely optional
        self.server_name = _CFG["app"].get("server_name", "HronBot")


        self.watchdog = LoopWatchdog(
            self,
            timeout_sec=WATCHDOG_TIMEOUT_SEC,
            check_interval_sec=WATCHDOG_CHECK_INTERVAL_SEC,
            heartbeat_interval_sec=WATCHDOG_HEARTBEAT_INTERVAL_SEC,
            heartbeat_autodelete_sec=WATCHDOG_HEARTBEAT_AUTODELETE_SEC
        )
        
        self.auto_closer = AutoCloser(self)

    async def add_symbol(self, symbol: str):
        symbol = symbol.upper()
        if symbol in self.symbols:
            return
            
        # 1. Update app.json
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        if symbol not in app_data.get("symbols", []):
            if "symbols" not in app_data:
                app_data["symbols"] = []
            app_data["symbols"].append(symbol)
            Utils.write_json_file(app_json_path, app_data)
            logger.info(f"[{symbol}] Added to app.json.")
            
        # 2. Update bot lists and FSM states
        self.symbols.append(symbol)
        
        if symbol not in self.fsm_states:
            self.fsm_states[symbol] = {
                "LONG": PositionState(symbol=symbol, side="LONG"),
                "SHORT": PositionState(symbol=symbol, side="SHORT")
            }
            
        # 3. Add to monitors and streams
        if hasattr(self, 'pos_stream'):
            self.pos_stream.target_symbols.add(symbol)
            
        # 4. Reload caches
        self.runtime_manager.load_initial_caches(self.symbols)
        self.runtime_configs = self.runtime_manager.caches
        self.runtime_manager.populate_fsm_from_cache(self.fsm_states)
        
        # 5. Restart price stream
        logger.info(f"Restarting price stream to include new symbol: {symbol}")
        if hasattr(self, 'price_stream') and self.price_stream:
            await self.price_stream.aclose()
            
        # Завершаем текущую таску price_stream
        for task in asyncio.all_tasks():
            if task.get_name() == "price_stream":
                task.cancel()
                
        stream_symbols = list(self.symbols) + ["BNBUSDT"] if "BNBUSDT" not in self.symbols else list(self.symbols)
        self.price_stream = BinanceHotPriceStream(stream_symbols)
        self.price_stream_synced.clear()
        asyncio.create_task(self.price_stream.run(self._on_tick), name="price_stream")
        
        # 6. Trigger VolatilityManager to compute super_indent for the new symbol
        if hasattr(self, 'volatility_manager') and self.volatility_manager.is_running:
            asyncio.create_task(self.volatility_manager.process_all())
        
        logger.info(f"[{symbol}] Dynamically added to BotCore.")

    async def delete_symbol(self, symbol: str):
        symbol = symbol.upper()
        if symbol not in self.symbols:
            return
            
        logger.info(f"[{symbol}] Dynamically deleting symbol from BotCore...")
        
        # 1. Remove from app.json
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        if symbol in app_data.get("symbols", []):
            app_data["symbols"].remove(symbol)
            Utils.write_json_file(app_json_path, app_data)
            logger.info(f"[{symbol}] Removed from app.json.")
            
        # 2. Update bot lists and FSM states
        self.symbols.remove(symbol)
        
        if symbol in self.fsm_states:
            del self.fsm_states[symbol]
            
        if symbol in self.runtime_configs:
            del self.runtime_configs[symbol]
            
        if symbol in self.prices:
            del self.prices[symbol]
            
        # 3. Remove from monitors and streams
        if hasattr(self, 'pos_stream') and symbol in self.pos_stream.target_symbols:
            self.pos_stream.target_symbols.remove(symbol)
            
        # 4. Delete the runtime JSON cache
        runtime_path = self.runtime_manager.runtime_dir / f"{symbol.lower()}.json"
        if runtime_path.exists():
            try:
                runtime_path.unlink()
                logger.info(f"[{symbol}] Deleted runtime cache file.")
            except Exception as e:
                logger.error(f"[{symbol}] Failed to delete runtime cache: {e}")
                
        # 5. Restart price stream
        logger.info(f"Restarting price stream to exclude deleted symbol: {symbol}")
        if hasattr(self, 'price_stream') and self.price_stream:
            await self.price_stream.aclose()
            
        if self.symbols:
            stream_symbols = list(self.symbols) + ["BNBUSDT"] if "BNBUSDT" not in self.symbols else list(self.symbols)
            self.price_stream = BinanceHotPriceStream(stream_symbols)
            asyncio.create_task(self.price_stream.run(self._on_tick))
        else:
            self.price_stream = None
            
        logger.info(f"[{symbol}] Successfully deleted from BotCore.")

    async def _on_tick(self, tick: HotPriceTick):
        """Коллбэк для стрима горячих цен."""
        self.prices[tick.symbol] = (tick.price, time.time())

    async def _process_signal(self, symbol: str, side: str, side_cfg: dict, current_price: float, concurrent_mode: bool = False):
        """Обработка сигнала входа для символа и стороны."""
        state = self.fsm_states[symbol][side]
        try:
            logger.info(f"[{symbol}] Signal triggered for {side}.")
            
            # 1. Установка плеча и маржи
            await self.leverage_manager.set_leverage_and_margin(self.client, symbol, side_cfg)

            # 1.5. Открытие позиции по маркету
            invest_size = side_cfg["invest_size"]
            grid_0 = side_cfg["grid"]["0"]
            volume_pct = grid_0["volume"]
            volume = TradeMath.calculate_order_volume(invest_size, volume_pct, current_price, self.spec_data, symbol)
            
            order_side = "BUY" if side == "LONG" else "SELL"
            
            logger.info(f"[{symbol}] Opening {side} market order. Volume: {volume}")
            res = await self.client.make_order(
                symbol=symbol,
                qty=volume,
                side=order_side,
                position_side=side,
                market_type="MARKET",
                concurrent_mode=concurrent_mode
            )
            if not res.success:
                logger.error(f"[{symbol}] Failed to open {side} position: {res.error_msg}")
                return
                
            # БЕЗУСЛОВНАЯ фиксация входа в позицию (ИДЕМПОТЕНТНОСТЬ)
            # Если биржа приняла ордер, мы УЖЕ в позиции. Флаг спасет от двойного входа при отвале REST/WS.
            state.set_in_position(True)
                
            current_time_ms = int(time.time() * 1000)
            state.open_time = current_time_ms
            side_cfg["open_time"] = current_time_ms

            # Очищаем кэши от прошлых сделок, чтобы они не стрельнули ложным фолбеком
            state.fallback_price = None
            state.next_avg_price = None
            state.initial_entry_price = 0.0
            
            # Дожидаемся обновления avg_entry_price от вебсокета после входа
            state.pre_avg_price = 0.0
            sync_success = await Utils.wait_for_fsm_sync(state, timeout_sec=3.0, poll_interval=0.01)
            
            if not sync_success:
                logger.warning(f"[{symbol}] {side} WS did not update avg_entry_price in time at start! Forcing REST fallback...")
                try:
                    positions = await self.client.fetch_positions(symbol)
                    for p in positions:
                        if p.get("positionSide") == side:
                            new_avg = float(p.get("entryPrice", 0.0))
                            new_vol = float(p.get("positionAmt", 0.0))
                            if new_avg > 0:
                                state.avg_entry_price = new_avg
                                state.total_volume = new_vol
                                state.set_in_position(True)
                    logger.info(f"[{symbol}] {side} REST fallback applied! New avg_entry_price: {state.avg_entry_price}")
                except Exception as e:
                    logger.error(f"[{symbol}] {side} REST fallback failed: {e}")
            else:
                logger.info(f"[{symbol}] {side} FSM synced at start! New avg_entry_price: {state.avg_entry_price}")

            # 2. Математика расчета объема и TP (Используем реальный объем из стрима)
            opposite_state = self.fsm_states[symbol]["SHORT"] if side == "LONG" else self.fsm_states[symbol]["LONG"]
            await self.tp_manager.place_take_profit(self.client, symbol, side, current_price, self.spec_data, state, opposite_state)
            
        finally:
            # ВСЕГДА сбрасываем временный флаг защиты от двойного входа
            state.in_position_papper = False
            # 3. Сохраняем стейт в рантайм кэш
            await self.runtime_manager.save_cache(symbol)

    async def _check_and_reset_finished_positions(self, symbol: str, states: dict, runtime_cfg: dict):
        """Проверяет флаги is_finished, пишет аналитику, отменяет ордера и сбрасывает рантайм.""" 
        # не вижу сброса PositionState
        sides_to_reset = []
        for side in ("LONG", "SHORT"):
            if states[side].is_finished:
                sides_to_reset.append(side)

        if sides_to_reset:
            for idx, side in enumerate(sides_to_reset):
                logger.info(f"[{symbol}] {side} is_finished. Running analytics and resetting runtime cache...")
                
                # Записываем метку времени для smart_grace
                states[side].recent_closes.append(time.time())
                
                # Вызов аналитики
                open_time = runtime_cfg.get(side, {}).get("open_time", 0)
                close_time = int(time.time() * 1000)
                self.analytics.record_finished_position(self.client, symbol, side, open_time, close_time)
                # Снимаем все тейк-профит ордера этой стороны
                logger.info(f"[{symbol}] {side} Canceling all limit orders for this side")
                await self.client.cancel_orders_for_side(symbol, side)
                
                # Сбрасываем рантайм-кеш в памяти и восстанавливаем стейт до дефолта
                self.runtime_manager.reset_state_to_default(symbol, side, states[side])
                
                # Сбрасываем флаг калькуляции сетки в avg_manager
                self.avg_manager.reset(symbol, side)
                
                # Если позиций две, то сбрасываем их с задержкой, чтобы биржа не забанила
                if idx < len(sides_to_reset) - 1:
                    await asyncio.sleep(0.1)

            # Сохраняем рантайм (сразу за обе стороны, если их было две)
            await self.runtime_manager.save_cache(symbol)

    async def _process_symbol_loop(self, symbol: str):
        runtime_cfg = self.runtime_configs.get(symbol, {})
        states = self.fsm_states[symbol]
        
        # --- REST Failsafe (MUST run even if price is missing) ---
        current_time = time.monotonic()
        if not hasattr(self, "_last_rest_syncs"):
            self._last_rest_syncs = {}
            
        last_sync = self._last_rest_syncs.get(symbol, current_time)
        if current_time - last_sync > REST_FAILSAFE_SEC or symbol not in self._last_rest_syncs:
            self._last_rest_syncs[symbol] = current_time
            any_open = any(s.in_position for s in states.values())
            if any_open:
                try:
                    await self.pos_monitor.sync_from_rest(self.client, [symbol])
                except Exception as e:
                    logger.error(f"[{symbol}] Periodic REST sync failed: {e}")

        # Проверяем закрытие позиций (ДО ПРОВЕРКИ ЦЕНЫ)
        await self._check_and_reset_finished_positions(symbol, states, runtime_cfg)
        
        current_price = None
        price_data = self.prices.get(symbol)
        if price_data:
            if isinstance(price_data, tuple):
                p, ts = price_data
                # We trust the pre-flight bulk fetch to have refreshed it if needed
                if time.time() - ts < 5.0:  # PRICE_STALE_SEC
                    current_price = p
            else:
                current_price = price_data  # safe fallback
                
        if not current_price:
            return  # safely skip loop iteration if we couldn't get a price even after fallback
        
        signal_tasks = []
        
        # Предварительно определяем, будут ли открыты обе стороны
        sides_to_open = []
        for side in ("LONG", "SHORT"):
            state = states[side]
            side_cfg = runtime_cfg.get(side)
            if side_cfg and side_cfg.get("enable") and not state.in_position and not state.in_position_papper:
                sides_to_open.append(side)

        is_concurrent = len(sides_to_open) > 1
        
        for side in ("LONG", "SHORT"):
            state = states[side]
            side_cfg = runtime_cfg.get(side)
            
            if not side_cfg or not side_cfg.get("enable"):
                continue
            
            if not state.in_position and not state.in_position_papper:
                # Calculate closed_count for smart_grace
                now = time.time()
                smart_grace_cfg = _CFG.get("signal", {}).get("smart_grace", {})
                eval_window = smart_grace_cfg.get("eval_window_sec", 300)
                state.recent_closes = [t for t in state.recent_closes if now - t <= eval_window]
                closed_count = len(state.recent_closes)
                
                is_signal, current_grace = self.time_control.is_new_interval(closed_count, smart_grace_cfg)
                
                # Логируем изменения окна благодати
                if state.last_grace_period != current_grace:
                    if state.last_grace_period != 0.0:
                        logger.info(f"[{symbol}] {side} Smart Grace Window changed: {state.last_grace_period}s -> {current_grace}s (Recent closes: {closed_count})")
                    state.last_grace_period = current_grace
                    
                if is_signal and not BLOCK_ENTRY and not getattr(self, 'entry_blocked', False):
                    logger.info(f"[{symbol}] {side}: Signal is TRUE! Entering position...")
                    # Ставим временный флаг идемпотентности
                    state.in_position_papper = True
                    signal_tasks.append(self._process_signal(symbol, side, side_cfg, current_price, concurrent_mode=is_concurrent))
            
            else:
                # Позиция уже открыта (или в процессе in_position_papper)
                opposite_state = states["SHORT"] if side == "LONG" else states["LONG"]
                await self.avg_manager.process(self.client, self.runtime_manager, symbol, side, state, opposite_state, current_price, self.spec_data, self.tp_manager)
                await self.fallback_tp_manager.process(self.client, self.runtime_manager, symbol, side, state, current_price, self.spec_data)

        if signal_tasks:
            await asyncio.gather(*signal_tasks)

    async def _game_loop(self):
        """Главный цикл торгового ядра."""
        self.is_running = True
        
        # ШАГ 1. Сборка и проверка рантайм кешей (создание отсутствующих JSON)
        created_new = build_runtime_caches()
        if created_new and not TG_ENABLED:
            prompt_runtime_check()
            
        # ШАГ 1.5. ПЕРВЫЙ РАСЧЕТ ВОЛАТИЛЬНОСТИ
        if hasattr(self, 'volatility_manager'):
            try:
                await self.volatility_manager.process_all()
            except Exception as e:
                logger.error(f"Error during initial VolatilityManager process_all: {e}")
            self.volatility_manager.start()
        
        self.runtime_manager.load_initial_caches(self.symbols)
        self.runtime_configs = self.runtime_manager.caches

        # ШАГ 2. Инициализация стримов и фоновых задач
        self.spec_manager.start()
        price_task = asyncio.create_task(self.price_stream.run(self._on_tick))
        
        
        self.pos_monitor = PositionMonitor(states_cache=self.fsm_states, target_symbols=self.symbols)
        
        # ПОДЧИНЯЕМ PositionState загруженному рантайм-кешу
        self.runtime_manager.populate_fsm_from_cache(self.fsm_states)
        
        self.pos_stream = PositionStream(
            api_key=API_KEY,
            stop_flag=lambda: not self.is_running,
            monitor=self.pos_monitor,
            target_symbols=set(self.symbols),
            client=self.client
        )
        pos_task = asyncio.create_task(self.pos_stream.start())
        
        logger.info("Main _game_loop started. Specifications and price streams are running.")
        
        # Ожидание готовности прайс-стримов и первоначальная загрузка по REST
        logger.info("Prefetching initial prices via REST...")
        if self.symbols:
            initial_prices = await BinancePublic.get_prices_bulk(self.symbols)
            for sym, p in initial_prices.items():
                self.prices[sym] = (p, time.time())
                
        logger.info("Waiting for price streams to connect...")
        try:
            await asyncio.wait_for(self.price_stream.ready.wait(), timeout=3.0)
            logger.info("Price streams connected successfully.")
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for price streams! Relying on REST prefetch.")

        logger.info("Waiting for Position Stream to connect...")
        wait_cycles = 0
        while not self.pos_stream.ready and wait_cycles < 50:
            await asyncio.sleep(0.1)
            wait_cycles += 1
            
        if not self.pos_stream.ready:
            logger.warning("Position Stream failed to connect in time! Relying on REST failsafe.")
        else:
            logger.info("Position Stream connected successfully.")
            
        # Строгая гарантия: забираем начальный стейт позиций по REST всегда (WS не шлет стейт при старте)
        await self.pos_monitor.sync_from_rest(self.client, self.symbols)

        
        # ШАГ 3. Синхронизация рантаймов с реальностью FSM
        logger.info("Streams and REST synced! Running initial RuntimeFSM Sync...")
        await self.runtime_manager.sync_with_fsm(self.fsm_states)

        logger.info("Waiting for exchange specifications to load...")
        while not self.spec_data.get("symbols") and self.is_running:
            await asyncio.sleep(0.5)
        logger.info("Exchange specifications loaded.")

        # Запуск фонового контроллера Watchdog
        # Запускаем Notifier
        await self.notifier.start()

        # Запускаем watchdog
        watchdog_task = asyncio.create_task(self.watchdog.start())

        while self.is_running:
            self._last_tick = time.time()
            self._tick_count += 1
            
            try:
                if self.semaphore:
                    self.semaphore.tick("start")
                    if not self.semaphore.is_active:
                        if not self.was_passive or getattr(self, '_first_passive_log', True):
                            from consts import SEMAPHORE_SERVER_NAME
                            logger.info(f"[{SEMAPHORE_SERVER_NAME}] 🟡 Я РЕЗЕРВ. Сплю, жду отвала основного сервера...")
                            self.was_passive = True
                            self._first_passive_log = False
                        await asyncio.sleep(1.0)
                        self.semaphore.tick("end")
                        continue
                    else:
                        if self.was_passive:
                            from consts import SEMAPHORE_SERVER_NAME
                            logger.info(f"[{SEMAPHORE_SERVER_NAME}] 🟢 Я АКТИВЕН. Кручу торговую логику бота...")
                            self.was_passive = False
                            
                            if self.redis_manager:
                                success = await self.redis_manager.pull_failover_data()
                                if success:
                                    try:
                                        from c_utils import Utils
                                        from consts import CFG_PATH
                                        new_app = Utils.read_json_file(CFG_PATH)
                                        if "symbols" in new_app:
                                            new_symbols = new_app["symbols"]
                                            if isinstance(new_symbols, dict):
                                                new_symbols = list(new_symbols.keys())
                                            else:
                                                new_symbols = list(new_symbols)
                                                
                                            to_add = set(new_symbols) - set(self.symbols)
                                            to_remove = set(self.symbols) - set(new_symbols)
                                            
                                            for sym in to_add:
                                                await self.add_symbol(sym)
                                            for sym in to_remove:
                                                await self.delete_symbol(sym)
                                    except Exception as e:
                                        logger.error(f"Error dynamically syncing symbols after failover: {e}")
                                
                                    self.runtime_manager.load_initial_caches(self.symbols)
                                    self.runtime_manager.populate_fsm_from_cache(self.fsm_states)
                                    await self.pos_monitor.sync_from_rest(self.client, self.symbols)
                                    logger.info("Failover sync complete. Resuming trading.")

                if self.is_paused:
                    await asyncio.sleep(1.0)
                    if self.semaphore:
                        self.semaphore.tick("end")
                    continue

                # Pre-flight bulk price update
                stale_symbols = []
                now = time.time()
                for sym in self.symbols:
                    price_data = self.prices.get(sym)
                    if not price_data or not isinstance(price_data, tuple) or (now - price_data[1] > 5.0):
                        stale_symbols.append(sym)
                
                if stale_symbols:
                    try:
                        bulk_prices = await BinancePublic.get_prices_bulk(stale_symbols)
                        for sym, p in bulk_prices.items():
                            self.prices[sym] = (p, time.time())
                    except Exception as e:
                        logger.error(f"Bulk price fetch failed: {e}")

                tasks = [self._process_symbol_loop(symbol) for symbol in self.symbols]
                await asyncio.gather(*tasks)
                
                # Синхронизация рантаймов при изменениях в PositionState (постоянный контроль)
                await self.runtime_manager.sync_with_fsm(self.fsm_states)

                if getattr(self, 'backup_manager', None):
                    await self.backup_manager.check_and_backup()
                
                if getattr(self, 'redis_manager', None):
                    await self.redis_manager.check_and_backup()

                # Автоматическое закрытие по триггеру профита
                if hasattr(self, 'auto_closer'):
                    await self.auto_closer.check()

                # Предотвращение блокировки event loop
                await asyncio.sleep(TIME_SLACK_SEC)
                
                if self.semaphore:
                    self.semaphore.tick("end")

            except asyncio.CancelledError:
                logger.info("_game_loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in _game_loop: {e}", exc_info=True)
                await asyncio.sleep(TIME_SLACK_SEC)
                
        self.is_running = False
        await self.notifier.stop()
        self.watchdog.stop()
        watchdog_task.cancel()
        self.spec_manager.stop()
        self.price_stream.stop()
        price_task.cancel()
        pos_task.cancel()
        # Попытка быстрого сохранения стейтов при нормальном завершении
        await self.runtime_manager.sync_with_fsm(self.fsm_states, force_save=True)
        await self.client.shutdown()
        await asyncio.gather(price_task, pos_task, watchdog_task, return_exceptions=True)

    async def start(self):
        """Запуск бота."""
        if self.is_paused:
            logger.info("BotCore started in PAUSED state (waiting for TG Start).")
        else:
            logger.info("BotCore started in ACTIVE state (auto_start enabled). Trading loops are running.")
            
        logger.info("[BotCore] Running initial deep sync of analytics...")
        await self.analytics.deep_sync_analytics(self.client, self.prices)
            
        self.analytics.start_realtime_tracker(self.client)

        self.volatility_manager = VolatilityManager(self)
        
        await self._game_loop()

    def stop(self):
        """Остановка бота."""
        self.is_running = False
        if hasattr(self, 'watchdog'):
            self.watchdog.stop()
        self.spec_manager.stop()
        if hasattr(self, 'volatility_manager'):
            self.volatility_manager.stop()

    async def close_all_positions(self):
        """Экстренное закрытие всех позиций и отмена лимитных ордеров для активных монет."""
        if getattr(self, 'semaphore', None) and not self.semaphore.is_active:
            raise Exception("Действие заблокировано: Эта нода сейчас в пассивном режиме (РЕЗЕРВ)!")
            
        symbols = _CFG["symbols"]
        
        # 1. Отмена лимитных ордеров
        for sym in symbols:
            try:
                await self.client.cancel_all_orders(sym)
                logger.info(f"[{sym}] Canceled all limit orders.")
            except Exception as e:
                logger.error(f"[{sym}] Failed to cancel orders: {e}")
            
        # 2. Получение и закрытие позиций по рынку
        try:
            positions = await self.client.fetch_positions()
            for p in positions:
                sym = p.get("symbol")
                if sym not in symbols:
                    continue
                amt = float(p.get("positionAmt", 0.0))
                if abs(amt) > 0:
                    pos_side = p.get("positionSide") # "LONG" or "SHORT"
                    side = "SELL" if pos_side == "LONG" else "BUY"
                    
                    logger.warning(f"[{sym}] Emergency closing {pos_side} position. Volume: {abs(amt)}")
                    await self.client.make_order(
                        symbol=sym,
                        qty=abs(amt),
                        side=side,
                        position_side=pos_side,
                        market_type="MARKET"
                    )
        except Exception as e:
            logger.error(f"Error while fetching/closing positions: {e}")

    async def shutdown(self):
        """Гарантированное сохранение рантайма (последний чих) и закрытие сессий."""
        logger.info("Executing graceful BotCore shutdown...")
        self.is_running = False
        if hasattr(self, 'watchdog'):
            self.watchdog.stop()
        try:
            # Принудительно дампим стейты
            if hasattr(self, 'fsm_states') and hasattr(self, 'runtime_manager'):
                await self.runtime_manager.sync_with_fsm(self.fsm_states, force_save=True)
                logger.info("Final FSM snapshot saved to runtime cache.")
        except Exception as e:
            logger.error(f"Error saving FSM snapshot during shutdown: {e}")
            
        try:
            if hasattr(self, 'client'):
                await self.client.shutdown()
        except Exception:
            pass
