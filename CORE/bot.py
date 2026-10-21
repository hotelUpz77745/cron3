# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\bot.py
# Role: bot.py module

# ==============================================================================
# Path: CORE/bot.py
# Role: Торговое ядро и основная логика
# ==============================================================================

import asyncio
import contextlib
import os
import time
import json
import random
from typing import Dict, Any, Optional, Tuple, List, Union
import secrets
from datetime import datetime, timezone
from pathlib import Path

from consts import (
    _CFG, DATA_DIR, ANALYTICS_DIR, CACHE_DIR, TIME_SLACK_SEC, SPEC_TTL_SEC,
    STREAMER_PORT, STREAMER_LOCK_NAME, DEFAULT_TIMEFRAME, DEFAULT_HEARTBEAT_TFR,
    COOLDOWN_POST_CANDLE_BUFFER_SEC, COOLDOWN_MIN_HEARTBEAT_SEC,
    DEFAULT_SMART_GRACE, TG_ENABLED, BACKUP_ENABLED, API_KEY, API_SECRET,
    WATCHDOG_TIMEOUT_SEC, WATCHDOG_CHECK_INTERVAL_SEC,
    WATCHDOG_HEARTBEAT_INTERVAL_SEC, WATCHDOG_HEARTBEAT_AUTODELETE_SEC,
    REST_FAILSAFE_SEC
)

from c_log import UnifiedLogger
from c_utils import Utils
from watchdog import LoopWatchdog

from API.BINANCE.public import BinancePublic
from API.BINANCE.price_stream import HotPriceTick
from API.BINANCE.client_prod import BinanceClient
from API.IPC.streamer_hub import CrossProcessHotPriceStream

from POS_FSM.models import PositionState
from POS_FSM.pos_stream import PositionStream
from POS_FSM.pos_stream_monitor import PositionMonitor, BinanceWsInterpreter

from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
from RUNTIME_FSM.runtime_builder import build_runtime_caches, prompt_runtime_check

from CORE._utils import TradeMath, SpecManager
from CORE.SERVICES.auto_closer import AutoCloser
from CORE.SERVICES.notifier import NotifierManager
from CORE.SERVICES.leverage_adapter import LeverageAdapter, LeverageManager
from CORE.SERVICES.VOLATILITY.volatility_manager import VolatilityManager
from CORE.SERVICES.emergency_closer import EmergencyCloserService
from CORE.SERVICES.symbol_lifecycle_manager import SymbolLifecycleManager
from CORE.SERVICES.bot_bootstrap import BotBootstrapService
from CORE.ENTRY.signal import TimeControl
from CORE.GRID.avg_manager import AverageManager
from CORE.TP.tp_manager import TakeProfitManager
from CORE.TP.fallback_tp_manager import FallbackTpManager
from CORE.TP.tp_backoff_manager import TakeProfitBackoffManager
from CORE.TP.take_profit_restorer import TakeProfitRestorer
from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager
from CORE.STRATEGY.peeling_manager import PeelingManager
from CORE.STRATEGY.brownian_noise import BrownianNoiseController
from CORE.strategy_registry import StrategyRegistry
from CORE.strategy_applicator import StrategyApplicator
from CORE.trade_cycle_manager import TradeCycleManager
from ANALYTICS.analytics import AnalyticsManager
logger = UnifiedLogger("BotCore")

BLOCK_ENTRY = False  # Глобальный флаг блокировки входа в позиции (для отладки)

class BotCore:
    first_order_regime: FirstOrderRegimeManager
    brownian_noise: BrownianNoiseController
    trade_cycle: TradeCycleManager
    volatility_manager: VolatilityManager

    def __init__(self):
        self.is_running = False
        # Инициализация реестра стратегий и мультивселенных
        self.strategy_registry = StrategyRegistry()
        active_syms = self.strategy_registry.get_all_active_symbols()
        self.symbols = active_syms
        self.prices = {}   # Структура для хранения цен
        self.runtime_manager = RuntimeFsmManager(strategy_registry=self.strategy_registry)
        
        self.notifier = NotifierManager(bot_core=self)
        
        self.runtime_configs = self.runtime_manager.caches # Кеш рантаймов
        
        # Флаги готовности стримов
        self.pos_stream_synced = asyncio.Event()
        self.price_stream_synced = asyncio.Event()
        
        # Получаем таймфрейм/хартбит из стратегий или дефолтный 5m
        timeframe = DEFAULT_TIMEFRAME
        if self.strategy_registry.active_strategies:
            first_strat = next(iter(self.strategy_registry.active_strategies.values()), None)
            if first_strat:
                timeframe = getattr(first_strat, "heartbeat_tfr", getattr(first_strat, "timeframe", DEFAULT_TIMEFRAME))
        self.time_control = TimeControl(interval=timeframe)
        
        # 2. Менеджеры
        self.leverage_manager = LeverageManager()
        self.brownian_noise = BrownianNoiseController()
        self.first_order_regime = FirstOrderRegimeManager()
        
        self.tp_manager = TakeProfitManager(self.runtime_manager)
        self.tp_restorer = TakeProfitRestorer(self)
        
        self.avg_manager = AverageManager()
        self.peeling_manager = PeelingManager()
        
        self.fallback_tp_manager = FallbackTpManager()
        self.tp_backoff_manager = TakeProfitBackoffManager(self.runtime_manager)
        self.spec_manager = SpecManager(self)
        
        self.analytics = AnalyticsManager(strategy_registry=self.strategy_registry)
        self.trade_cycle = TradeCycleManager(self)
        self.backup_manager = None
        if BACKUP_ENABLED:
            logger.info(
                "ℹ️ [BACKUP] Информационная подсветка (backup.enabled=true): "
                "Фоновый спам zip-архивами упразднен в архитектуре под корень. Рантайм непрерывно пишется в быстрый бинарный .pkl с нулевой нагрузкой. "
                "Для экспорта рантайма в JSON используйте On-Demand сервис: команда /backup в Telegram или CLI 'python -m CORE.SERVICES.runtime_backup_service'."
            )
            
        self.was_passive = False
        
        auto_start = _CFG["app"]["auto_start"]
        if TG_ENABLED:
            self.is_paused = not auto_start
        else:
            self.is_paused = False
            
        self.entry_blocked = False
        self.block_until_timestamp = 0.0
        self.symbol_blocked_until: Dict[str, float] = {}
        self.strategy_blocked_until: Dict[str, float] = {}
        self.current_sim_time: Optional[float] = None
        self._last_blocked_log: Dict[str, Dict[str, float]] = {}
        self._last_rest_syncs: Dict[str, float] = {}
        
        spec_cache_path = CACHE_DIR / "specifications.json"
        self.spec_data = Utils.read_json_file(spec_cache_path) if spec_cache_path.exists() else {}

        self.client = BinanceClient(api_key=API_KEY, api_secret=API_SECRET)
        
        # Мультиаккаунтный пул клиентов биржи: {strat_id: BinanceClient}
        self.clients: Dict[str, BinanceClient] = {}
        # Словарь уникальных клиентов по паре ключей: {(api_key, api_secret): BinanceClient}
        self._key_clients: Dict[tuple[str, str], BinanceClient] = {(API_KEY, API_SECRET): self.client}
        self._init_clients()
        
        # Кеш FSM-состояний в формате {symbol: {"LONG": PositionState, "SHORT": PositionState}}
        self.fsm_states = {
            s: {
                "LONG": PositionState(symbol=s, side="LONG"),
                "SHORT": PositionState(symbol=s, side="SHORT")
            } for s in self.symbols
        }
        if self.symbols:
            self.runtime_manager.load_initial_caches(self.symbols)
            self.runtime_configs = self.runtime_manager.caches
            self.runtime_manager.populate_fsm_from_cache(self.fsm_states)
        
        # Конкурентные локеры и флаги состояний закрытия
        self._closing_symbols: set[str] = set()
        self._closing_strategies: set[str] = set()
        self._close_lock = asyncio.Lock()
        self._symbol_locks: dict[str, asyncio.Lock] = {}

        # Сетевые адаптеры и стримы
        stream_symbols = list(self.symbols)
        self.price_stream = CrossProcessHotPriceStream(stream_symbols, port=STREAMER_PORT, lock_name=STREAMER_LOCK_NAME)
        self.pos_stream = None
        self.pos_monitor = None
        self.ws_interpreter = None
        self._price_task = None
        self._pos_task = None
        self._watchdog_task = None

        # Контроль главный петли (Watchdog)
        self._last_tick = time.time()
        self._tick_count = 0
        self.logger = logger
        self.server_name = _CFG["app"].get("server_name", "HronBot")

        self.watchdog = LoopWatchdog(
            self,
            timeout_sec=WATCHDOG_TIMEOUT_SEC,
            check_interval_sec=WATCHDOG_CHECK_INTERVAL_SEC,
            heartbeat_interval_sec=WATCHDOG_HEARTBEAT_INTERVAL_SEC,
            heartbeat_autodelete_sec=WATCHDOG_HEARTBEAT_AUTODELETE_SEC
        )
        
        self.volatility_manager = VolatilityManager(self)
        self.auto_closer = AutoCloser(self)

    def _init_clients(self):
        """
        Инициализирует BinanceClient для каждой активной стратегии в соответствии с её ключами в .env.
        Если разные стратегии используют один набор ключей, инстанс клиента переиспользуется.
        """
        if not hasattr(self, "strategy_registry") or not self.strategy_registry:
            return
            
        for strat_id, strat_cfg in self.strategy_registry.active_strategies.items():
            strat_key, strat_sec = strat_cfg.get_api_credentials()
            cred_tuple = (strat_key, strat_sec)
            
            if cred_tuple not in self._key_clients:
                logger.info(f"[BotCore] Инициализирован независимый BinanceClient для стратегии '{strat_id}' (key: {strat_key[:6]}...)")
                self._key_clients[cred_tuple] = BinanceClient(api_key=strat_key, api_secret=strat_sec)
                
            self.clients[strat_id] = self._key_clients[cred_tuple]

    def get_client_for_strategy(self, strat_id: str) -> BinanceClient:
        """Возвращает BinanceClient для конкретной стратегии (или дефолтный self.client)."""
        if strat_id in self.clients:
            return self.clients[strat_id]
        return self.client

    def get_client_for_symbol(self, symbol: str, strat_id: Optional[str] = None) -> BinanceClient:
        """Возвращает BinanceClient, привязанный к символу или стратегии."""
        if strat_id and strat_id in self.clients:
            return self.clients[strat_id]
        if self.strategy_registry:
            s_id = self.strategy_registry.get_strategy_id_for_symbol(symbol)
            if s_id and s_id in self.clients:
                return self.clients[s_id]
        return self.client

    async def _restart_price_stream(self):
        """Безопасный перезапуск price_stream с сохранением сильной ссылки на Task."""
        if self._price_task and not self._price_task.done():
            if self.price_stream:
                await self.price_stream.aclose()
            self._price_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._price_task
            self._price_task = None
        elif self.price_stream:
            await self.price_stream.aclose()

        for task in asyncio.all_tasks():
            if task.get_name() == "price_stream" and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        await asyncio.sleep(0.2)

        if self.symbols:
            stream_symbols = list(self.symbols)
            self.price_stream = CrossProcessHotPriceStream(stream_symbols, port=STREAMER_PORT, lock_name=STREAMER_LOCK_NAME)
            self.price_stream_synced.clear()
            self._price_task = asyncio.create_task(self.price_stream.run(self._on_tick), name="price_stream")
        else:
            self.price_stream = None
            self._price_task = None

    async def _reconcile_open_orders(self, force: bool = False) -> Dict[str, int]:
        """
        Гарантирует наличие Take Profit ордеров на бирже для всех открытых позиций.
        Если биржа потеряла ордер или бот перезагрузился в позиции без TP, немедленно выставляет ордер.
        """
        if hasattr(self, "tp_restorer") and self.tp_restorer:
            return await self.tp_restorer.verify_and_restore_all(force=force)
        return {"checked": 0, "confirmed": 0, "restored": 0, "mismatch": 0}

    async def add_symbol(self, symbol: str, strategy_id: Optional[str] = None) -> bool:
        """
        Динамическое добавление торгового символа в обойму бота.
        Делегирует исполнение в SymbolLifecycleManager.
        """
        sm = getattr(self, "symbol_manager", None)
        if not isinstance(sm, SymbolLifecycleManager):
            sm = SymbolLifecycleManager(self)
        return await sm.add_symbol(symbol, strategy_id=strategy_id)

    async def delete_symbol(self, symbol: str) -> bool:
        """
        Динамическое удаление торгового символа из обоймы бота.
        Делегирует исполнение в SymbolLifecycleManager.
        """
        sm = getattr(self, "symbol_manager", None)
        if not isinstance(sm, SymbolLifecycleManager):
            sm = SymbolLifecycleManager(self)
        return await sm.delete_symbol(symbol)

    async def _on_tick(self, tick: HotPriceTick):
        """Коллбэк для стрима горячих цен."""
        self.prices[tick.symbol] = (tick.price, time.time())

    def _get_symbol_lock(self, symbol: str) -> asyncio.Lock:
        """Потокобезопасное получение/создание asyncio.Lock для символа."""
        sym = symbol.upper()
        if not hasattr(self, "_symbol_locks") or not isinstance(self._symbol_locks, dict):
            self._symbol_locks = {}
        if sym not in self._symbol_locks:
            self._symbol_locks[sym] = asyncio.Lock()
        return self._symbol_locks[sym]

    @staticmethod
    def calc_heartbeat_window_cooldown(now: float, interval_sec: Any) -> float:
        """
        Вычисляет временную метку окончания кулдауна до открытия следующего окна свечи
        согласно персональному таймфрейму пульса стратегии (signal_heartbeat_tfr).
        Гарантирует защиту от преждевременного переоткрытия позиций (идемпотентность).
        """
        interval = int(interval_sec)
        if interval <= 0:
            raise ValueError(f"[BotCore] interval_sec must be positive integer, got: {interval_sec}")
        now_f = float(now)
        next_candle_start = ((int(now_f) // interval) + 1) * interval
        return max(next_candle_start + COOLDOWN_POST_CANDLE_BUFFER_SEC, now_f + min(COOLDOWN_MIN_HEARTBEAT_SEC, float(interval)))

    def get_strategy_heartbeat_seconds(self, strategy_id: Optional[str] = None, symbol: Optional[str] = None) -> int:
        """
        Возвращает интервал пульса стратегии в секундах (signal_heartbeat_tfr),
        строго извлекая его из объекта активной стратегии (StrategyConfig) без скрытых магических констант.
        """
        strat_obj = None
        if strategy_id:
            strat_obj = self.strategy_registry.get_strategy(strategy_id)
        elif symbol:
            strat_obj = self.strategy_registry.get_strategy_for_symbol(symbol)
        elif self.strategy_registry.active_strategies:
            strat_obj = next(iter(self.strategy_registry.active_strategies.values()), None)

        if strat_obj is not None:
            if strat_obj.heartbeat_seconds is not None:
                return int(strat_obj.heartbeat_seconds)
            if strat_obj.signal_heartbeat_tfr:
                return TimeControl.interval_to_seconds(strat_obj.signal_heartbeat_tfr)

        if self.time_control and self.time_control.interval_seconds:
            return int(self.time_control.interval_seconds)

        return TimeControl.interval_to_seconds(DEFAULT_HEARTBEAT_TFR)

    def get_current_time(self) -> float:
        """Возвращает текущее время: виртуальное время симуляции/бэктеста или системное time.time()."""
        return Utils.get_current_time(self.current_sim_time)

    def check_entry_block(self, symbol: str, strategy_id: Optional[str] = None) -> tuple[bool, str, float]:
        """Проверяет блокировку входа через торговую машину состояний TradeCycleManager."""
        tc = getattr(self, "trade_cycle", None)
        if not isinstance(tc, TradeCycleManager):
            tc = TradeCycleManager(self)
        return tc.check_entry_block(symbol, strategy_id)

    async def _process_signal(self, symbol: str, side: str, side_cfg: dict, current_price: float, concurrent_mode: bool = False):
        """Обработка сигнала входа для символа и стороны."""
        tc = getattr(self, "trade_cycle", None)
        if not isinstance(tc, TradeCycleManager):
            tc = TradeCycleManager(self)
        return await tc.process_signal(symbol, side, side_cfg, current_price, concurrent_mode=concurrent_mode)

    async def _check_and_reset_finished_positions(self, symbol: str, states: dict, runtime_cfg: dict):
        """Проверяет флаги is_finished, пишет аналитику, отменяет ордера и сбрасывает рантайм."""
        tc = getattr(self, "trade_cycle", None)
        if not isinstance(tc, TradeCycleManager):
            tc = TradeCycleManager(self)
        return await tc.check_and_reset_finished_positions(symbol, states, runtime_cfg)

    async def _process_symbol_loop(self, symbol: str):
        """Обработка одной итерации торгового цикла для символа."""
        tc = getattr(self, "trade_cycle", None)
        if not isinstance(tc, TradeCycleManager):
            tc = TradeCycleManager(self)
        return await tc.process_symbol_loop(symbol)

    async def _prepare_game_loop(self) -> bool:
        """ШАГ 0 и 1: Валидация списка монет, прогрев кэшей и волатильности (делегировано в BotBootstrapService)."""
        return await BotBootstrapService.prepare_game_loop(self)

    async def _init_streams_and_fsm(self) -> None:
        """ШАГ 2 и 3: Запуск спецификаций, стримов цен/позиций, REST-синхронизация и прогрев (делегировано в BotBootstrapService)."""
        return await BotBootstrapService.init_streams_and_fsm(self)

    async def _run_loop_iteration(self) -> None:
        """Одна итерация главного цикла торгового ядра."""
        self._last_tick = time.time()
        self._tick_count += 1

        try:
            if not self.symbols or self.is_paused:
                await asyncio.sleep(1.0)
                return

            # Pre-flight bulk price update & Stream Watchdog
            stale_symbols = []
            now = time.time()
            for sym in self.symbols:
                price_data = self.prices.get(sym)
                if not price_data or not isinstance(price_data, tuple) or (now - price_data[1] > 10.0):
                    stale_symbols.append(sym)

            if stale_symbols and (now - getattr(self, "_last_bulk_price_fetch", 0.0) >= 5.0):
                self._last_bulk_price_fetch = now
                try:
                    bulk_prices = await BinancePublic.get_prices_bulk(stale_symbols)
                    fetch_now = time.time()
                    for sym, p in bulk_prices.items():
                        self.prices[sym] = (p, fetch_now)
                except Exception as e:
                    logger.warning(f"[REST_FAILSAFE] Bulk price fetch failed: {e}")

            if len(stale_symbols) == len(self.symbols) and len(self.symbols) > 0:
                oldest_ts = min([self.prices[s][1] for s in self.symbols if s in self.prices and isinstance(self.prices[s], tuple)] or [0.0])
                if now - oldest_ts > 25.0 and (now - getattr(self, "_last_stream_restart_ts", 0.0) >= 30.0):
                    self._last_stream_restart_ts = now
                    logger.warning(f"[STREAM_WATCHDOG] All {len(self.symbols)} symbol prices stale for {now - oldest_ts:.1f}s! Auto-restarting price stream...")
                    asyncio.create_task(self._restart_price_stream())

            tasks = [self._process_symbol_loop(symbol) for symbol in self.symbols]
            await asyncio.gather(*tasks)

            # Синхронизация рантаймов при изменениях в PositionState
            await self.runtime_manager.sync_with_fsm(self.fsm_states)

            # Периодический аудит и восстановление TP ордеров
            await self._reconcile_open_orders()

            # Автоматическое закрытие по триггеру профита
            if self.auto_closer:
                await self.auto_closer.check()

            await asyncio.sleep(TIME_SLACK_SEC)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error in _run_loop_iteration: {e}", exc_info=True)
            await asyncio.sleep(TIME_SLACK_SEC)

    async def _shutdown_game_loop(self) -> None:
        """Корректная остановка задач и стримов игрового цикла (делегировано в BotBootstrapService)."""
        return await BotBootstrapService.shutdown_game_loop(self)

    async def _game_loop(self):
        """Главный цикл торгового ядра (дирижер)."""
        self.is_running = True
        try:
            if not await self._prepare_game_loop():
                return
            await self._init_streams_and_fsm()
            while self.is_running:
                await self._run_loop_iteration()
        except asyncio.CancelledError:
            logger.info("_game_loop cancelled.")
        finally:
            await self._shutdown_game_loop()

    async def start(self):
        """Запуск бота."""
        if self.is_paused:
            logger.info("BotCore started in PAUSED state (waiting for TG Start).")
        else:
            logger.info("BotCore started in ACTIVE state (auto_start enabled). Trading loops are running.")
            
        self.volatility_manager = VolatilityManager(self)
        
        await self._game_loop()

    def stop(self):
        """Остановка бота."""
        self.is_running = False
        if self.watchdog:
            self.watchdog.stop()
        if self.spec_manager:
            self.spec_manager.stop()
        if self.volatility_manager:
            self.volatility_manager.stop()

    async def reset_strategy_iteration(self, strategy_id: str) -> dict:
        """
        Принудительный сброс итерации стратегии и переход на новый цикл.
        Делегирует исполнение в EmergencyCloserService.
        """
        return await EmergencyCloserService.reset_strategy_iteration(self, strategy_id)

    async def close_all_positions(self, reset_iterations: bool = True) -> dict:
        """Экстренное закрытие всех позиций, отмена лимитных ордеров и сброс итераций СТРОГО для текущей обоймы монет."""
        return await EmergencyCloserService.close_all(self, reset_iterations=reset_iterations)

    async def close_strategy_positions(self, strategy_id: str) -> None:
        """Штатное закрытие позиций и отмена ордеров СТРОГО для монет одной конкретной стратегии."""
        return await EmergencyCloserService.close_strategy(self, strategy_id)

    async def shutdown(self):
        """Гарантированное сохранение рантайма (последний чих) и закрытие сессий."""
        logger.info("Executing graceful BotCore shutdown...")
        self.is_running = False
        if self.watchdog:
            self.watchdog.stop()
            try:
                await self.watchdog.aclose()
            except Exception:
                pass
        if self.pos_stream:
            self.pos_stream._external_stop = True
            try:
                await self.pos_stream.stop()
            except Exception:
                pass
        if self._pos_task:
            self._pos_task.cancel()
        try:
            # Принудительно дампим стейты
            if self.fsm_states and self.runtime_manager:
                await self.runtime_manager.sync_with_fsm(self.fsm_states, force_save=True)
                logger.info("Final FSM snapshot saved to runtime cache.")
        except Exception as e:
            logger.error(f"Error saving FSM snapshot during shutdown: {e}")

        try:
            if self.client:
                await self.client.shutdown()
        except Exception:
            pass
