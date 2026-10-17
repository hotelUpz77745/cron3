# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\bot.py
# Role: bot.py module

# ==============================================================================
# Path: CORE/bot.py
# Role: Торговое ядро и основная логика
# ==============================================================================

import asyncio
import contextlib
import logging
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
from CORE.ENTRY.signal import TimeControl
from CORE.GRID.avg_manager import AverageManager
from CORE.TP.tp_manager import TakeProfitManager
from CORE.TP.fallback_tp_manager import FallbackTpManager
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

    async def add_symbol(self, symbol: str, strategy_id: Optional[str] = None):
        symbol = symbol.upper()
        if symbol in self.symbols:
            logger.warning(f"[{symbol}] Symbol is already active in BotCore.")
            return
            
        # 1. Update app.json
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path)
        active_strategies = app_data.get("active_strategies", {})
        
        target_strat = strategy_id
        if not target_strat:
            for sid, sdata in active_strategies.items():
                if sdata.get("enabled", False):
                    target_strat = sid
                    break

        if target_strat:
            if target_strat in active_strategies:
                active_strategies[target_strat].setdefault("symbols", []).append(symbol)
                active_strategies[target_strat]["enabled"] = True
            else:
                if self.strategy_registry:
                    u_meta = self.strategy_registry.universes_db.get(target_strat, {})
                else:
                    univ_path = DATA_DIR / "universes" / f"{target_strat}.json"
                    u_meta = Utils.read_json_file(univ_path) if univ_path.exists() else {}
                active_strategies[target_strat] = {
                    "enabled": True,
                    "symbols": [symbol],
                    "super_grid": {
                        "enabled": bool((u_meta.get("overrides", {}).get("super_grid") or u_meta.get("super_grid") or {}).get("enabled", False)),
                        "timeframe": "1w",
                        "window": 12,
                        "multiplier": 2.0,
                        "min_volatility_pct": 15.0,
                        "max_volatility_pct": None,
                        "update_interval_hours": 24
                    },
                    "analytics": {
                        "start_balance_usdt": u_meta.get("start_balance_usdt", 449.0)
                    },
                    "notifications": {
                        "enabled": True,
                        "negative_threshold": -20.0,
                        "positive_threshold": 20.0
                    },
                    "auto_closing": {
                        "negative": {"threshold": None, "threshold_increment": None},
                        "positive": {"threshold": 100, "threshold_increment": 100}
                    }
                }
            Utils.write_json_file(app_json_path, app_data)
            logger.info(f"[{symbol}] Added to active_strategy '{target_strat}' in app.json.")

        # 1b. Reload StrategyRegistry
        if self.strategy_registry:
            try:
                self.strategy_registry.reload(active_strategies)
                self._init_clients()
            except Exception as e:
                logger.error(f"Error reloading StrategyRegistry: {e}")

        # 1c. Build isolated runtime config using StrategyApplicator if needed
        try:
            if self.strategy_registry and self.strategy_registry.is_symbol_active(symbol):
                strat_cfg = self.strategy_registry.get_strategy_for_symbol(symbol)
                StrategyApplicator().build_runtime_for_symbol(symbol, strat_cfg)
        except Exception as e:
            logger.warning(f"[{symbol}] StrategyApplicator build notice: {e}")
            
        # 2. Update bot lists and FSM states
        self.symbols.append(symbol)
        
        if symbol not in self.fsm_states:
            self.fsm_states[symbol] = {
                "LONG": PositionState(symbol=symbol, side="LONG"),
                "SHORT": PositionState(symbol=symbol, side="SHORT")
            }
            
        # 3. Add to monitors and streams
        if self.pos_stream and self.pos_stream.target_symbols is not None:
            self.pos_stream.target_symbols.add(symbol)
        if self.ws_interpreter and self.ws_interpreter.target_symbols is not None:
            self.ws_interpreter.target_symbols.add(symbol)
        if self.pos_monitor and self.pos_monitor.target_symbols is not None:
            if symbol not in self.pos_monitor.target_symbols:
                self.pos_monitor.target_symbols.append(symbol)

        # 4. Immediate REST price prefetch
        try:
            p = await BinancePublic.get_price(symbol)
            if p and p > 0:
                self.prices[symbol] = (p, time.time())
                logger.info(f"[{symbol}] Initial REST price fetched: {p}")
        except Exception as e:
            logger.warning(f"[{symbol}] Failed initial REST price fetch: {e}")
            
        # 5. Reload caches
        self.runtime_manager.load_initial_caches(self.symbols)
        self.runtime_configs = self.runtime_manager.caches
        self.runtime_manager.populate_fsm_from_cache(self.fsm_states)
        
        # 5.5 Pre-warm leverage for the new symbol (sets leverage & margin once)
        try:
            cfg = self.runtime_configs.get(symbol, {})
            side_cfg = cfg.get("LONG") or cfg.get("SHORT")
            if side_cfg:
                await self.leverage_manager.set_leverage_and_margin(self.client, symbol, side_cfg)
        except Exception as e:
            logger.warning(f"[{symbol}] Leverage pre-warm warning on add_symbol: {e}")
        
        # 6. Restart price stream
        logger.info(f"Restarting price stream to include new symbol: {symbol}")
        await self._restart_price_stream()
        
        # 7. Trigger VolatilityManager to compute super_indent for the new symbol
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
        active_strategies = app_data.get("active_strategies", {})
        changed = False
        if active_strategies:
            for sdata in active_strategies.values():
                if "symbols" in sdata and symbol in sdata["symbols"]:
                    sdata["symbols"].remove(symbol)
                    changed = True
        if symbol in app_data.get("symbols", []):
            app_data["symbols"].remove(symbol)
            changed = True
        if changed:
            Utils.write_json_file(app_json_path, app_data)
            logger.info(f"[{symbol}] Removed from app.json.")
            
        # 1b. Reload StrategyRegistry
        if self.strategy_registry:
            try:
                self.strategy_registry.reload(active_strategies)
                self._init_clients()
            except Exception as e:
                logger.error(f"Error reloading StrategyRegistry on delete: {e}")
            
        # 2. Update bot lists and FSM states
        self.symbols.remove(symbol)
        
        if symbol in self.fsm_states:
            del self.fsm_states[symbol]
            
        if symbol in self.runtime_configs:
            del self.runtime_configs[symbol]
            
        if symbol in self.prices:
            del self.prices[symbol]
            
        # 3. Remove from monitors and streams
        if self.pos_stream and self.pos_stream.target_symbols and symbol in self.pos_stream.target_symbols:
            self.pos_stream.target_symbols.remove(symbol)
        if self.ws_interpreter and self.ws_interpreter.target_symbols is not None:
            self.ws_interpreter.target_symbols.discard(symbol)
        if self.pos_monitor and self.pos_monitor.target_symbols is not None:
            self.pos_monitor.target_symbols = [s for s in self.pos_monitor.target_symbols if s != symbol]
            
        # 4. Delete the runtime JSON cache
        runtime_path = self.runtime_manager.get_runtime_path(symbol)
        if runtime_path.exists():
            try:
                runtime_path.unlink()
                logger.info(f"[{symbol}] Deleted runtime cache file: {runtime_path}.")
            except Exception as e:
                logger.error(f"[{symbol}] Failed to delete runtime cache: {e}")
        root_path = self.runtime_manager.runtime_dir / f"{symbol.lower()}.json"
        if root_path.exists():
            try:
                root_path.unlink()
            except Exception:
                pass
                
        # 5. Restart price stream
        logger.info(f"Restarting price stream to exclude deleted symbol: {symbol}")
        await self._restart_price_stream()
            
        logger.info(f"[{symbol}] Successfully deleted from BotCore.")

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
        """ШАГ 0 и 1: Валидация списка монет, прогрев кэшей и волатильности."""
        while self.is_running and not self.symbols:
            logger.warning("[BotCore] Symbol list is empty. Waiting for symbols to be configured in app.json...")
            await asyncio.sleep(5.0)
            self.strategy_registry = StrategyRegistry()
            active_syms = self.strategy_registry.get_all_active_symbols()
            if active_syms:
                self.symbols = active_syms

        if not self.is_running:
            return False

        # Сборка и проверка рантайм кешей (создание отсутствующих JSON)
        created_new = build_runtime_caches(self.symbols, registry=self.strategy_registry)
        if created_new and not TG_ENABLED:
            prompt_runtime_check()

        # Первый расчет волатильности
        if self.volatility_manager:
            try:
                await self.volatility_manager.process_all()
            except Exception as e:
                logger.error(f"Error during initial VolatilityManager process_all: {e}")
            self.volatility_manager.start()

        if self.strategy_registry:
            try:
                StrategyApplicator().build_all_runtimes(self.strategy_registry)
            except Exception as e:
                logger.warning(f"[BotCore] Startup StrategyApplicator sync warning: {e}")

        self.runtime_manager.load_initial_caches(self.symbols)
        self.runtime_configs = self.runtime_manager.caches
        return True

    async def _init_streams_and_fsm(self) -> None:
        """ШАГ 2 и 3: Запуск спецификаций, стримов цен/позиций, REST-синхронизация и прогрев."""
        self.spec_manager.start()
        self._price_task = asyncio.create_task(self.price_stream.run(self._on_tick), name="price_stream")

        self.pos_monitor = PositionMonitor(states_cache=self.fsm_states, target_symbols=self.symbols)
        self.runtime_manager.populate_fsm_from_cache(self.fsm_states)

        self.pos_stream = PositionStream(
            api_key=API_KEY,
            stop_flag=lambda: not self.is_running,
            monitor=self.pos_monitor,
            target_symbols=set(self.symbols),
            client=self.client
        )

        self.ws_interpreter = BinanceWsInterpreter(
            monitor=self.pos_monitor, 
            target_symbols=set(self.symbols)
        )
        self.pos_stream.register_callback(self.ws_interpreter.process_message)
        self._pos_task = asyncio.create_task(self.pos_stream.start(), name="pos_stream")

        logger.info("Main _game_loop started. Specifications and price streams are running.")
        logger.info("Waiting for price streams to connect and snapshot...")
        try:
            await asyncio.wait_for(self.price_stream.ready.wait(), timeout=10.0)
            if hasattr(self.price_stream, "snapshot_received"):
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.price_stream.snapshot_received.wait(), timeout=5.0)
            logger.info("Price streams connected and snapshot received.")
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for price streams! Waiting for prices...")

        # Дожидаемся цен от стрима (до 30с) с фолбеком на bulk REST
        logger.info(f"Waiting for price initialization for all {len(self.symbols)} symbols (up to 30s)...")
        wait_start = time.time()
        while time.time() - wait_start < 30.0 and self.is_running:
            missing = [s for s in self.symbols if not self.prices.get(s) or (isinstance(self.prices.get(s), tuple) and self.prices[s][0] <= 0)]
            if not missing:
                logger.info(f"All {len(self.symbols)} symbol prices successfully initialized from stream in {time.time() - wait_start:.1f}s!")
                break
            await asyncio.sleep(0.5)
        else:
            missing = [s for s in self.symbols if not self.prices.get(s) or (isinstance(self.prices.get(s), tuple) and self.prices[s][0] <= 0)]
            if missing:
                logger.warning(
                    f"[PRICE_INIT] Price init timeout (30s): {len(missing)}/{len(self.symbols)} symbols missing. "
                    f"Executing abnormal bulk REST fallback for all symbols..."
                )
                try:
                    bulk_prices = await BinancePublic.get_prices_bulk(self.symbols)
                    now_ts = time.time()
                    for sym, p in bulk_prices.items():
                        self.prices[sym] = (p, now_ts)
                    logger.info(f"[PRICE_INIT] Bulk REST fallback populated {len(bulk_prices)}/{len(self.symbols)} symbols.")
                except Exception as e:
                    logger.warning(f"[PRICE_INIT] Bulk REST fallback failed: {e}")

        logger.info("Waiting for Position Stream to connect...")
        wait_cycles = 0
        while not self.pos_stream.ready and wait_cycles < 50:
            await asyncio.sleep(0.1)
            wait_cycles += 1

        if not self.pos_stream.ready:
            logger.warning("Position Stream failed to connect in time! Relying on REST failsafe.")
        else:
            logger.info("Position Stream connected successfully.")

        # Начальный стейт позиций по REST
        await self.pos_monitor.sync_from_rest(self.client, self.symbols)

        # Синхронизируем уровни сетки с реальными биржевыми объемами
        for sym in self.symbols:
            strat = self.strategy_registry.get_strategy_for_symbol(sym) if self.strategy_registry else None
            for s_side in ("LONG", "SHORT"):
                st = self.fsm_states.get(sym, {}).get(s_side)
                if st and st.in_position and abs(st.total_volume) > 1e-6:
                    cur_p = self.prices.get(sym, (0.0, 0))[0] if sym in self.prices else None
                    StrategyApplicator.resolve_position_state_grid(st, strategy=strat, current_price=cur_p)

        logger.info("Streams and REST synced! Running initial RuntimeFSM Sync...")
        await self.runtime_manager.sync_with_fsm(self.fsm_states)

        logger.info("Waiting for exchange specifications to load...")
        while not self.spec_data.get("symbols") and self.is_running:
            await asyncio.sleep(0.5)
        logger.info("Exchange specifications loaded.")

        # Прогрев плеча и маржи
        logger.info(f"[BotCore] Pre-warming leverage and margin settings for {len(self.symbols)} active symbols...")
        for sym in self.symbols:
            cfg = self.runtime_configs.get(sym, {})
            side_cfg = cfg.get("LONG") or cfg.get("SHORT")
            if side_cfg:
                try:
                    await self.leverage_manager.set_leverage_and_margin(self.client, sym, side_cfg)
                except Exception as e:
                    logger.warning(f"[{sym}] Leverage pre-warm warning: {e}")

        # Первоначальный аудит и восстановление TP ордеров
        await self._reconcile_open_orders(force=True)

        # Notifier & Watchdog
        await self.notifier.start()
        self._watchdog_task = asyncio.create_task(self.watchdog.start(), name="watchdog")

        # Аналитика
        logger.info("[BotCore] All systems & prices fully warmed up. Running initial sync and starting analytics tracker...")
        await self.analytics.deep_sync_analytics(self.client, self.prices)
        self.analytics.start_realtime_tracker(self.client)

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
        """Корректная остановка задач и стримов игрового цикла."""
        self.is_running = False
        await self.notifier.stop()
        if self.watchdog:
            self.watchdog.stop()
        if self._watchdog_task:
            self._watchdog_task.cancel()
        self.spec_manager.stop()
        if self.price_stream:
            self.price_stream.stop()
        if self._price_task:
            self._price_task.cancel()
        if self._pos_task:
            self._pos_task.cancel()

        for creds, cl in getattr(self, '_key_clients', {}).items():
            with contextlib.suppress(Exception):
                await cl.shutdown()

        tasks = [t for t in (self._price_task, self._pos_task, self._watchdog_task) if t]
        if hasattr(self, '_pos_tasks'):
            tasks.extend([t for t in self._pos_tasks if t])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
        Делегирует расчет и запись аналитики в AnalyticsManager, сбрасывает recent_closes и ставит кулдаун.
        """
        from unittest.mock import MagicMock

        # 1. Запись аналитики через сервисный метод (с поддержкой фолбека для тестов с моками)
        res = None
        if hasattr(self, "analytics") and hasattr(self.analytics, "reset_strategy_iteration") and not isinstance(self.analytics, MagicMock):
            res = self.analytics.reset_strategy_iteration(strategy_id)
        if not isinstance(res, dict):
            now_ms = int(time.time() * 1000)
            strat_stats_path = ANALYTICS_DIR / "STATS" / f"analytics_{strategy_id}.json"
            strat_analytics = {}
            if strat_stats_path.exists():
                strat_analytics = Utils.read_json_file(strat_stats_path) or {}
            strat_net = float(strat_analytics.get("net_profit_usdt", 0.0))
            cur_iter_idx = int(strat_analytics.get("iteration_index", 1))
            next_iter_idx = cur_iter_idx + 1
            new_iter_start_net = round(strat_net, 4)
            new_iter_start_real = round(float(strat_analytics.get("realized_pnl_net_usdt", strat_analytics.get("realized_pnl_usdt", strat_net))), 4)

            strat_analytics["iteration_index"] = next_iter_idx
            strat_analytics["iteration_start_net_profit_usdt"] = new_iter_start_net
            strat_analytics["iteration_net_profit_usdt"] = 0.0
            strat_analytics["iteration_start_realized_pnl_usdt"] = new_iter_start_real
            strat_analytics["iteration_realized_pnl_usdt"] = 0.0
            strat_analytics["iteration_start_ts"] = now_ms
            strat_analytics["iteration_peak_net_profit_usdt"] = 0.0
            strat_analytics["iteration_peak_ts"] = now_ms
            strat_analytics["iteration_peak_age_hours"] = 0.0
            strat_analytics["iteration_net_velocity"] = 0.0

            if strat_stats_path.exists():
                Utils.atomic_write_json(strat_stats_path, strat_analytics, indent=4)

            # Синхронизация CACHE/iteration_state.json
            try:
                c_file = CACHE_DIR / "iteration_state.json"
                c_dict = {}
                if c_file.exists():
                    try:
                        c_dict = json.loads(c_file.read_text(encoding="utf-8"))
                    except Exception:
                        c_dict = {}
                prev_entry = c_dict.get(strategy_id, {})
                c_dict[strategy_id] = {
                    "iteration_index": next_iter_idx,
                    "iteration_start_net_profit_usdt": new_iter_start_net,
                    "iteration_start_realized_pnl_usdt": new_iter_start_real,
                    "iteration_realized_pnl_usdt": 0.0,
                    "iteration_start_ts": now_ms,
                    "iteration_peak_net_profit_usdt": 0.0,
                    "iteration_peak_ts": now_ms,
                    "historical_peak_net_profit_usdt": prev_entry.get("historical_peak_net_profit_usdt", strat_net),
                    "historical_peak_net_ts": prev_entry.get("historical_peak_net_ts", now_ms),
                    "historical_max_drawdown_usdt": prev_entry.get("historical_max_drawdown_usdt", 0.0),
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                }
                Utils.atomic_write_json(c_file, c_dict, indent=4)
            except Exception as ce:
                logger.error(f"[{strategy_id}] Error saving iteration cache on manual reset: {ce}")

            res = {
                "strategy_id": strategy_id,
                "prev_iteration": cur_iter_idx,
                "new_iteration": next_iter_idx,
                "base_net": new_iter_start_net,
                "base_realized": new_iter_start_real
            }

        cur_iter_idx = res.get("prev_iteration", 1)
        next_iter_idx = res.get("new_iteration", 2)
        new_iter_start_net = res.get("base_net", 0.0)

        # 2. Сброс recent_closes у монет стратегии
        strat_cfg = None
        if hasattr(self, "strategy_registry") and self.strategy_registry:
            if hasattr(self.strategy_registry, "active_strategies") and isinstance(self.strategy_registry.active_strategies, dict):
                strat_cfg = self.strategy_registry.active_strategies.get(strategy_id)
            if not strat_cfg and hasattr(self.strategy_registry, "get_strategy") and not isinstance(self.strategy_registry.get_strategy, MagicMock):
                try:
                    strat_cfg = self.strategy_registry.get_strategy(strategy_id)
                except Exception:
                    strat_cfg = None
        if strat_cfg and getattr(strat_cfg, "symbols", None):
            for sym in strat_cfg.symbols:
                if hasattr(self, "fsm_states") and sym in self.fsm_states:
                    for s_side in ("LONG", "SHORT"):
                        if s_side in self.fsm_states[sym]:
                            self.fsm_states[sym][s_side].recent_closes = []

        # 3. Кулдаун авто-закрытия и блокировка входов до открытия новой свечи хартбита
        now = time.time()
        heartbeat_sec = 300
        if hasattr(self, "get_strategy_heartbeat_seconds") and not isinstance(getattr(self, "get_strategy_heartbeat_seconds", None), MagicMock):
            try:
                heartbeat_sec = int(self.get_strategy_heartbeat_seconds(strategy_id=strategy_id))
            except (TypeError, ValueError):
                heartbeat_sec = 300
        cooldown_until = BotCore.calc_heartbeat_window_cooldown(now, heartbeat_sec)
        if not isinstance(cooldown_until, (int, float)):
            cooldown_until = now + heartbeat_sec

        if not isinstance(getattr(self, "strategy_blocked_until", None), dict):
            self.strategy_blocked_until = {}
        prev_st = self.strategy_blocked_until.get(strategy_id, 0.0)
        prev_st = prev_st if isinstance(prev_st, (int, float)) else 0.0
        self.strategy_blocked_until[strategy_id] = max(prev_st, cooldown_until)

        if not isinstance(getattr(self, "symbol_blocked_until", None), dict):
            self.symbol_blocked_until = {}
        if strat_cfg and getattr(strat_cfg, "symbols", None):
            for sym in strat_cfg.symbols:
                prev_sym = self.symbol_blocked_until.get(sym.upper(), 0.0)
                prev_sym = prev_sym if isinstance(prev_sym, (int, float)) else 0.0
                self.symbol_blocked_until[sym.upper()] = max(prev_sym, cooldown_until)

        all_active_syms = set()
        if hasattr(self, "strategy_registry") and self.strategy_registry and hasattr(self.strategy_registry, "get_all_active_symbols") and not isinstance(self.strategy_registry.get_all_active_symbols, MagicMock):
            try:
                all_active_syms = {s.upper() for s in self.strategy_registry.get_all_active_symbols()}
            except Exception:
                all_active_syms = set()
        strat_syms = {s.upper() for s in strat_cfg.symbols} if (strat_cfg and getattr(strat_cfg, "symbols", None)) else set()
        if not all_active_syms or strat_syms >= all_active_syms:
            prev_block = getattr(self, "block_until_timestamp", 0.0)
            prev_block = prev_block if isinstance(prev_block, (int, float)) else 0.0
            self.block_until_timestamp = max(prev_block, cooldown_until)
            self.entry_blocked = True

        if hasattr(self, "auto_closer") and self.auto_closer and hasattr(self.auto_closer, "_last_close_ts") and isinstance(self.auto_closer._last_close_ts, dict):
            self.auto_closer._last_close_ts[strategy_id] = time.time()

        rem_s = int(cooldown_until - now)
        raw_tfr = getattr(strat_cfg, "signal_heartbeat_tfr", None)
        tfr_label = str(raw_tfr) if isinstance(raw_tfr, str) and raw_tfr else f"{heartbeat_sec}s"
        logger.info(
            f"[{strategy_id}] Manual iteration reset: Cycle #{cur_iter_idx} -> #{next_iter_idx}. "
            f"Base Net: {new_iter_start_net:+.2f} USDT. Idempotent cooldown locked until {cooldown_until} ({rem_s}s until next {tfr_label} window)."
        )

        return res

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
