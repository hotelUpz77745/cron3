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
logger = UnifiedLogger("BotCore")

BLOCK_ENTRY = False  # Глобальный флаг блокировки входа в позиции (для отладки)

class BotCore:
    def __init__(self):
        self.is_running = False
        # Работаем только с теми символами, которые прописаны в конфигах .app.json в разделе symbols
        self.symbols = _CFG.get("symbols", [])
        self.prices = {}   # Структура для хранения цен
        from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
        self.runtime_manager = RuntimeFsmManager()
        self.runtime_configs = self.runtime_manager.caches # Кеш рантаймов
        
        # Флаги готовности стримов
        self.price_stream_synced = asyncio.Event()
        self.pos_stream_synced = asyncio.Event()
        
        # Получаем таймфрейм из конфига app.json (секция signal)
        signal_cfg = _CFG.get("signal", {})
        timeframe = signal_cfg.get("timeframe", "5m")
        self.time_control = TimeControl(interval=timeframe)
        
        # 2. Менеджеры
        from CORE.ENTRY.leverage_manager import LeverageManager
        self.leverage_manager = LeverageManager()
        
        from CORE.TP.tp_manager import TakeProfitManager
        self.tp_manager = TakeProfitManager(self.runtime_manager)
        
        from CORE.GRID.avg_manager import AverageManager
        self.avg_manager = AverageManager()
        
        from CORE.TP.fallback_tp_manager import FallbackTpManager
        self.fallback_tp_manager = FallbackTpManager()
        
        from ANALYTICS.analytics import AnalyticsManager
        self.analytics = AnalyticsManager()
        
        from consts import TG_ENABLED
        auto_start = _CFG.get("app", {}).get("auto_start", False)
        if TG_ENABLED:
            self.is_paused = not auto_start
        else:
            self.is_paused = False
        
        self.spec_data = {}
        
        from consts import API_KEY, API_SECRET
        self.client = BinanceClient(api_key=API_KEY, api_secret=API_SECRET)
        
        # Кеш FSM-состояний в формате {symbol: {"LONG": PositionState, "SHORT": PositionState}}
        self.fsm_states = {}
        
        # Сетевые адаптеры и стримы
        self.price_stream = BinanceHotPriceStream(self.symbols)

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
            
        self.price_stream = BinanceHotPriceStream(self.symbols)
        self.price_stream_synced.clear()
        asyncio.create_task(self.price_stream.run(self._on_tick))
        
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
            self.price_stream = BinanceHotPriceStream(self.symbols)
            self.price_stream_synced.clear()
            asyncio.create_task(self.price_stream.run(self._on_tick))
        else:
            self.price_stream = None
            
        logger.info(f"[{symbol}] Successfully deleted from BotCore.")

    async def _specification_task(self):
        """Фоновая задача: Обновление спецификации."""
        try:
            while self.is_running:
                data = await BinancePublic.get_instruments()
                if data:
                    self.spec_data = {"symbols": data}

                await asyncio.sleep(SPEC_TTL_SEC) 
        except asyncio.CancelledError:
            pass
            
    async def _on_tick(self, tick: HotPriceTick):
        """Коллбэк для стрима горячих цен."""
        self.prices[tick.symbol] = tick.price
        if not self.price_stream_synced.is_set():
            self.price_stream_synced.set()

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
            from CORE._utils import TradeMath
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
                
            current_time_ms = int(time.time() * 1000)
            state.open_time = current_time_ms
            side_cfg["open_time"] = current_time_ms

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
            await self.tp_manager.place_take_profit(self.client, symbol, side, current_price, self.spec_data, state)
            
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

    async def _process_symbol_loop(self, symbol: str, is_signal: bool):
        runtime_cfg = self.runtime_configs.get(symbol, {})
        states = self.fsm_states[symbol]
        current_price = self.prices.get(symbol)
        
        if not current_price:
            try:
                from API.BINANCE.public import BinancePublic
                price = await BinancePublic.get_last_price(symbol)
                if price:
                    current_price = price
                    self.prices[symbol] = price
            except Exception:
                pass
        
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
            
            if not current_price:
                continue
            
            if not state.in_position and not state.in_position_papper:
                if is_signal and not BLOCK_ENTRY:
                    logger.info(f"[{symbol}] {side}: Signal is TRUE! Entering position...")
                    # Ставим временный флаг идемпотентности
                    state.in_position_papper = True
                    signal_tasks.append(self._process_signal(symbol, side, side_cfg, current_price, concurrent_mode=is_concurrent))
                # pass            
            
            else:
                # Позиция уже открыта (или в процессе in_position_papper)
                await self.avg_manager.process(self.client, self.runtime_manager, symbol, side, state, current_price, self.spec_data, self.tp_manager)
                await self.fallback_tp_manager.process(self.client, self.runtime_manager, symbol, side, state, current_price, self.spec_data)

        if signal_tasks:
            await asyncio.gather(*signal_tasks)
            
        # --- REST Failsafe ---
        from consts import REST_FAILSAFE_SEC
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

        # В конце итерации по символу проверяем закрытие позиций
        await self._check_and_reset_finished_positions(symbol, states, runtime_cfg)

    async def _game_loop(self):
        """Главный цикл торгового ядра."""
        self.is_running = True
        
        # ШАГ 1. Сборка и проверка рантайм кешей (создание отсутствующих JSON)
        from RUNTIME_FSM.runtime_builder import build_runtime_caches, prompt_runtime_check
        from consts import TG_ENABLED
        created_new = build_runtime_caches()
        if created_new and not TG_ENABLED:
            prompt_runtime_check()
        
        self.runtime_manager.load_initial_caches(self.symbols)
        self.runtime_configs = self.runtime_manager.caches

        # ШАГ 2. Инициализация стримов и фоновых задач
        spec_task = asyncio.create_task(self._specification_task())
        price_task = asyncio.create_task(self.price_stream.run(self._on_tick))
        
        from POS_FSM.pos_stream_monitor import PositionMonitor
        from POS_FSM.pos_stream import PositionStream
        
        self.pos_monitor = PositionMonitor(states_cache=self.fsm_states, target_symbols=self.symbols)
        
        # ПОДЧИНЯЕМ PositionState загруженному рантайм-кешу
        self.runtime_manager.populate_fsm_from_cache(self.fsm_states)
        
        from consts import API_KEY
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
        from API.BINANCE.public import BinancePublic
        if self.symbols:
            initial_prices = await BinancePublic.get_prices_bulk(self.symbols)
            for sym, p in initial_prices.items():
                self.prices[sym] = p
                
        logger.info("Waiting for price streams to connect...")
        try:
            await asyncio.wait_for(self.price_stream_synced.wait(), timeout=3.0)
            logger.info("Price streams connected successfully.")
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for price streams! Relying on REST prefetch.")

        
        # Строгая гарантия: забираем начальный стейт позиций по REST
        await self.pos_monitor.sync_from_rest(self.client, self.symbols)
        
        # ШАГ 3. Синхронизация рантаймов с реальностью FSM
        logger.info("Streams and REST synced! Running initial RuntimeFSM Sync...")
        await self.runtime_manager.sync_with_fsm(self.fsm_states)

        while self.is_running:
            try:
                if self.is_paused:
                    await asyncio.sleep(1.0)
                    continue
                
                # # ===== ОПОРНАЯ ТОЧКА ДЛЯ ТЕСТИРОВАНИЯ =====               
                # logger.info(f"DEBUG LOOP: Prices snapshot: {list(self.prices.items())[:3]}...")
                # # Скипаем дальнейший проход для отладкыи
                # await asyncio.sleep(TIME_SLACK_SEC)
                # # continue
                # ==========================================

                # Источник сигнала для позиции, которая не в позиции
                is_signal = self.time_control.is_new_interval()

                tasks = [self._process_symbol_loop(symbol, is_signal) for symbol in self.symbols]
                await asyncio.gather(*tasks)
                
                # Синхронизация рантаймов при изменениях в PositionState (постоянный контроль)
                await self.runtime_manager.sync_with_fsm(self.fsm_states)

                # Предотвращение блокировки event loop
                await asyncio.sleep(TIME_SLACK_SEC)

            except asyncio.CancelledError:
                logger.info("_game_loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in _game_loop: {e}", exc_info=True)
                await asyncio.sleep(TIME_SLACK_SEC)
                
        self.is_running = False
        spec_task.cancel()
        self.price_stream.stop()
        price_task.cancel()
        pos_task.cancel()
        # Попытка быстрого сохранения стейтов при нормальном завершении
        await self.runtime_manager.sync_with_fsm(self.fsm_states, force_save=True)
        await self.client.shutdown()
        await asyncio.gather(spec_task, price_task, pos_task, return_exceptions=True)

    async def start(self):
        """Запуск бота."""
        if self.is_paused:
            logger.info("BotCore started in PAUSED state (waiting for TG Start).")
        else:
            logger.info("BotCore started in ACTIVE state (auto_start enabled). Trading loops are running.")
        self.analytics.start_realtime_tracker(self.client)
        
        logger.info("Running initial deep sync analytics on startup...")
        await self.analytics.deep_sync_analytics(self.client)
        
        from CORE.ADVANCED.volatility_manager import VolatilityManager
        self.volatility_manager = VolatilityManager(self)
        self.volatility_manager.start()
        
        await self._game_loop()

    def stop(self):
        """Остановка бота."""
        self.is_running = False
        if hasattr(self, 'volatility_manager'):
            self.volatility_manager.stop()

    async def shutdown(self):
        """Гарантированное сохранение рантайма (последний чих) и закрытие сессий."""
        logger.info("Executing graceful BotCore shutdown...")
        self.is_running = False
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
