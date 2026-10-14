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
import secrets
from datetime import datetime, timezone
from pathlib import Path

from consts import (
    _CFG, DATA_DIR, ANALYTICS_DIR, CACHE_DIR, TIME_SLACK_SEC, SPEC_TTL_SEC, STREAMER_PORT, STREAMER_LOCK_NAME,
    DEFAULT_TIMEFRAME, DEFAULT_SMART_GRACE
)
from API.BINANCE.public import BinancePublic
from API.BINANCE.price_stream import HotPriceTick
from API.IPC.streamer_hub import CrossProcessHotPriceStream
from CORE.ENTRY.signal import TimeControl
from CORE.ENTRY.leverage_manager import LeverageManager
from CORE._utils import TradeMath
from API.BINANCE.client_prod import BinanceClient
from POS_FSM.models import PositionState
from c_utils import Utils

from c_log import UnifiedLogger
from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
from CORE.notifier import NotifierManager
from CORE.TP.tp_manager import TakeProfitManager
from CORE.GRID.avg_manager import AverageManager
from CORE.TP.fallback_tp_manager import FallbackTpManager
from CORE._utils import SpecManager
from ANALYTICS.analytics import AnalyticsManager
from consts import TG_ENABLED, BACKUP_ENABLED
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
from POS_FSM.pos_stream_monitor import PositionMonitor, BinanceWsInterpreter
from POS_FSM.pos_stream import PositionStream
from CORE.auto_closer import AutoCloser
from consts import API_KEY
from API.BINANCE.public import BinancePublic
from CORE.ADVANCED.volatility_manager import VolatilityManager
from consts import _CFG
from CORE.STRATEGY.strategy_registry import StrategyRegistry
from CORE.STRATEGY.momentum_engine import MomentumEngine
logger = UnifiedLogger("BotCore")

BLOCK_ENTRY = False  # Глобальный флаг блокировки входа в позиции (для отладки)

class BotCore:
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
        if hasattr(self, "strategy_registry") and self.strategy_registry and getattr(self.strategy_registry, "active_strategies", None):
            first_strat = next(iter(self.strategy_registry.active_strategies.values()), None)
            if first_strat:
                timeframe = getattr(first_strat, "heartbeat_tfr", getattr(first_strat, "timeframe", DEFAULT_TIMEFRAME))
        self.time_control = TimeControl(interval=timeframe)
        
        # 2. Менеджеры
        self.leverage_manager = LeverageManager()
        
        self.tp_manager = TakeProfitManager(self.runtime_manager)
        from CORE.TP.take_profit_restorer import TakeProfitRestorer
        self.tp_restorer = TakeProfitRestorer(self)
        
        self.avg_manager = AverageManager()
        
        self.fallback_tp_manager = FallbackTpManager()
        
        self.spec_manager = SpecManager(self)
        
        self.analytics = AnalyticsManager(strategy_registry=self.strategy_registry)
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
        stream_symbols = list(self.symbols) + ["BNBUSDT"] if "BNBUSDT" not in self.symbols else list(self.symbols)
        self.price_stream = CrossProcessHotPriceStream(stream_symbols, port=STREAMER_PORT, lock_name=STREAMER_LOCK_NAME)
        self._price_task = None
        self._pos_task = None
        self._watchdog_task = None

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
        from CORE.radar_navigator import RadarNavigator
        self.radar_navigator = RadarNavigator(self)

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
        if hasattr(self, "strategy_registry") and self.strategy_registry:
            s_id = self.strategy_registry.get_strategy_id_for_symbol(symbol)
            if s_id and s_id in self.clients:
                return self.clients[s_id]
        return self.client

    async def _restart_price_stream(self):
        """Безопасный перезапуск price_stream с сохранением сильной ссылки на Task."""
        if hasattr(self, '_price_task') and self._price_task and not self._price_task.done():
            if hasattr(self, 'price_stream') and self.price_stream:
                await self.price_stream.aclose()
            self._price_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._price_task
            self._price_task = None
        elif hasattr(self, 'price_stream') and self.price_stream:
            await self.price_stream.aclose()

        for task in asyncio.all_tasks():
            if task.get_name() == "price_stream" and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        await asyncio.sleep(0.2)

        if self.symbols:
            stream_symbols = list(self.symbols) + ["BNBUSDT"] if "BNBUSDT" not in self.symbols else list(self.symbols)
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
                if hasattr(self, "strategy_registry") and self.strategy_registry:
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
                        "start_balance_usdt": u_meta.get("start_balance_usdt", 50.0)
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
        if hasattr(self, "strategy_registry") and self.strategy_registry:
            try:
                self.strategy_registry.reload(active_strategies)
                self._init_clients()
            except Exception as e:
                logger.error(f"Error reloading StrategyRegistry: {e}")

        # 1c. Build isolated runtime config using StrategyApplicator if needed
        try:
            from CORE.STRATEGY.strategy_applicator import StrategyApplicator
            if hasattr(self, "strategy_registry") and self.strategy_registry.is_symbol_active(symbol):
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
        if hasattr(self, 'pos_stream') and hasattr(self.pos_stream, 'target_symbols'):
            self.pos_stream.target_symbols.add(symbol)
        if hasattr(self, 'ws_interpreter') and hasattr(self.ws_interpreter, 'target_symbols') and self.ws_interpreter.target_symbols is not None:
            self.ws_interpreter.target_symbols.add(symbol)
        if hasattr(self, 'pos_monitor') and hasattr(self.pos_monitor, 'target_symbols') and self.pos_monitor.target_symbols is not None:
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
        if hasattr(self, "strategy_registry") and self.strategy_registry:
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
        if hasattr(self, 'pos_stream') and hasattr(self.pos_stream, 'target_symbols') and symbol in self.pos_stream.target_symbols:
            self.pos_stream.target_symbols.remove(symbol)
        if hasattr(self, 'ws_interpreter') and hasattr(self.ws_interpreter, 'target_symbols') and self.ws_interpreter.target_symbols is not None:
            self.ws_interpreter.target_symbols.discard(symbol)
        if hasattr(self, 'pos_monitor') and hasattr(self.pos_monitor, 'target_symbols') and self.pos_monitor.target_symbols is not None:
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

    async def _process_signal(self, symbol: str, side: str, side_cfg: dict, current_price: float, concurrent_mode: bool = False):
        """Обработка сигнала входа для символа и стороны."""
        sym_upper = symbol.upper()

        if getattr(self, 'is_paused', False) is True:
            logger.warning(f"[{symbol}] {side}: Entry aborted — Bot is paused.")
            return

        if getattr(self, 'entry_blocked', False) is True:
            logger.warning(f"[{symbol}] {side}: Entry aborted — Close All block is currently active.")
            return

        if sym_upper in getattr(self, "_closing_symbols", set()):
            logger.warning(f"[{symbol}] {side}: Entry aborted — symbol is currently in closing sequence.")
            return

        block_ts = getattr(self, 'block_until_timestamp', 0)
        if isinstance(block_ts, (int, float)) and block_ts > time.time():
            logger.warning(f"[{symbol}] {side}: Entry aborted — Close All cooldown is currently active.")
            return

        if hasattr(self, "radar_navigator") and self.radar_navigator:
            is_pending = False
            if hasattr(self.radar_navigator, "is_symbol_pending_rotation"):
                is_pending = bool(self.radar_navigator.is_symbol_pending_rotation(symbol))
            elif symbol in getattr(self.radar_navigator, "symbols_pending_rotation", set()):
                is_pending = True
            if is_pending:
                logger.info(f"[{symbol}] Вход в позицию отложен: монета ожидает решения по ротации Штурмана (advisory timeout).")
                return

        state = self.fsm_states[symbol][side]
        if getattr(state, "is_closing", False) is True:
            logger.warning(f"[{symbol}] {side}: Entry aborted — position state is marked as is_closing.")
            return

        try:
            logger.info(f"[{symbol}] Signal triggered for {side}.")

            strat = self.strategy_registry.get_strategy_for_symbol(symbol)

            # Актуализация закрытий стороны в пределах окна (по умолчанию 300с)
            now = time.time()
            eval_w = 300.0
            if strat and getattr(strat, "smart_grace_cfg", None) and strat.smart_grace_cfg.get("eval_window_sec"):
                eval_w = float(strat.smart_grace_cfg["eval_window_sec"])
            elif strat and getattr(strat, "smart_size_cfg", None) and strat.smart_size_cfg.get("eval_window_sec"):
                eval_w = float(strat.smart_size_cfg["eval_window_sec"])
            state.recent_closes = [t for t in getattr(state, "recent_closes", []) if now - t <= eval_w]
            closed_count = len(state.recent_closes)

            # Броуновский шум применяется ИСКЛЮЧИТЕЛЬНО между ордерами сильной руки (когда в окне закрылся хотя бы 1 ордер).
            # Первый сигнальный ордер (closed_count == 0) открывается строго по штатному таймингу без задержки!
            if strat and getattr(strat, "smart_grace_cfg", None) and closed_count >= 1:
                bn_factor = (
                    strat.smart_grace_cfg.get("brownian_noise_factor")
                    or strat.smart_grace_cfg.get("white_noise_factor")
                )
                if isinstance(bn_factor, (list, tuple)) and len(bn_factor) == 2:
                    low = float(min(bn_factor))
                    high = float(max(bn_factor))
                    if high > 0 and high >= low:
                        # Дробный джиттер броуновского шума через аппаратную энтропию secrets
                        noise_sleep = round(secrets.SystemRandom().uniform(low, high), 2)
                        logger.info(
                            f"[{symbol}] {side} Brownian Noise Factor: sleeping {noise_sleep}s (range [{low}..{high}]s) "
                            f"between strong-hand orders (closed orders in window: {closed_count})."
                        )
                        await asyncio.sleep(noise_sleep)
                        if (
                            getattr(self, 'is_paused', False) is True 
                            or getattr(self, 'entry_blocked', False) is True
                            or sym_upper in getattr(self, "_closing_symbols", set())
                            or getattr(state, "is_closing", False) is True
                            or getattr(state, "in_position", False) is True
                        ):
                            logger.warning(f"[{symbol}] {side}: Entry aborted after brownian noise sleep.")
                            state.in_position_papper = False
                            return
                        p_fresh = await BinancePublic.get_last_price(symbol)
                        if p_fresh and p_fresh > 0:
                            current_price = p_fresh
            
            sym_client = self.get_client_for_symbol(symbol)
            # 1. Установка плеча и маржи (использует кеш, 0 наносекунд если уже установлено)
            await self.leverage_manager.set_leverage_and_margin(sym_client, symbol, side_cfg)

            # 1.5. Открытие позиции по маркету
            base_invest = float(side_cfg["invest_size"])
            strat = self.strategy_registry.get_strategy_for_symbol(symbol)
            if strat and getattr(strat, "trend_size_cfg", {}).get("enabled"):
                from CORE.STRATEGY.trend_size_depender import TrendSizeDepender
                eff = await TrendSizeDepender.get_effective_invest_size(
                    sym_client, symbol, side, base_invest, strat.trend_size_cfg
                )
                if eff > 0:
                    base_invest = eff
            state.eff_invest = base_invest
            invest_size = base_invest

            if strat and strat.smart_size_cfg:

                trend_state = None
                trend_determ = strat.smart_size_cfg.get("trend_determ") or strat.smart_size_cfg.get("dynamic_sizing_mode")
                if trend_determ:
                    ema_cfg = strat.smart_size_cfg.get("ema_trend")
                    trend_state = await MomentumEngine.get_ema_trend_state(
                        symbol=symbol,
                        ema_cfg=ema_cfg,
                        client=sym_client
                    )

                size_mult = MomentumEngine.calculate_smart_size_multiplier(
                    closed_count=closed_count,
                    smart_size_cfg=strat.smart_size_cfg,
                    side=side,
                    trend_state=trend_state
                )
                if size_mult != 1.0:
                    invest_size *= size_mult
                    trend_tag = f" [Trend: {trend_state}]" if trend_state else ""
                    logger.info(f"[{symbol}][{strat.strategy_id}] {side} Smart Size applied: x{size_mult:.2f}{trend_tag} -> invest_size={invest_size:.2f} USDT (closes: {closed_count})")

            grid_0 = side_cfg["grid"]["0"]
            volume_pct = float(grid_0.get("volume") or 0.0)

            # Проверка динамического авто-режима по старшему таймфрейму (auto_ema)
            regime_cfg = (
                getattr(strat, "active_settings", {}).get("first_order_weight_regime")
                or getattr(strat, "overrides", {}).get("first_order_weight_regime")
                or getattr(strat, "raw_meta", {}).get("first_order_weight_regime")
            ) if strat else None

            if regime_cfg:
                mode_val = regime_cfg.get("mode") if isinstance(regime_cfg, dict) else regime_cfg
                if mode_val in ("auto", "auto_ema"):
                    from CORE.STRATEGY.strategy_applicator import StrategyApplicator
                    ema_trend_cfg = regime_cfg.get("auto_ema") if isinstance(regime_cfg, dict) else None
                    if not ema_trend_cfg and strat and strat.smart_size_cfg:
                        ema_trend_cfg = strat.smart_size_cfg.get("ema_trend")
                    trend_st = await MomentumEngine.get_ema_trend_state(
                        symbol=symbol,
                        ema_cfg=ema_trend_cfg,
                        client=sym_client
                    )
                    # UP -> bullish (12/7), DOWN -> bearish (7/12), FLAT/UNSTABLE -> neutral (9/9)
                    if trend_st == "UP":
                        resolved_mode = "bullish"
                    elif trend_st == "DOWN":
                        resolved_mode = "bearish"
                    else:
                        resolved_mode = "neutral"

                    dyn_vol = StrategyApplicator.resolve_first_order_volume(
                        regime_cfg={"mode": resolved_mode, "presets": regime_cfg.get("presets", {})} if isinstance(regime_cfg, dict) else resolved_mode,
                        side=side,
                        default_vol=volume_pct
                    )
                    if dyn_vol != volume_pct:
                        logger.info(f"[{symbol}][{strat.strategy_id}] {side} Grid 0 Auto-EMA regime [{trend_st} -> {resolved_mode}]: volume_pct {volume_pct}% -> {dyn_vol}%")
                        volume_pct = dyn_vol

            volume = TradeMath.calculate_order_volume(invest_size, volume_pct, current_price, self.spec_data, symbol)
            
            order_side = "BUY" if side == "LONG" else "SELL"
            
            # Критическая секция под локером символа: полная защита от гонки пакетов и параллельного закрытия
            sym_lock = BotCore._get_symbol_lock(self, symbol)
            async with sym_lock:
                # Защита от race condition: проверка непосредственно перед отправкой рыночного ордера
                if (
                    getattr(self, 'is_paused', False) is True 
                    or getattr(self, 'entry_blocked', False) is True
                    or sym_upper in getattr(self, "_closing_symbols", set())
                    or getattr(state, "is_closing", False) is True
                ):
                    logger.warning(f"[{symbol}] {side}: Entry aborted right before market order — bot paused/entry blocked/is closing.")
                    state.in_position_papper = False
                    return

                block_ts = getattr(self, 'block_until_timestamp', 0)
                if isinstance(block_ts, (int, float)) and block_ts > time.time():
                    logger.warning(f"[{symbol}] {side}: Entry aborted right before market order — Close All cooldown is active.")
                    state.in_position_papper = False
                    return

                logger.info(f"[{symbol}] Opening {side} market order. Volume: {volume}")
                res = await sym_client.make_order(
                    symbol=symbol,
                    qty=volume,
                    side=order_side,
                    position_side=side,
                    market_type="MARKET",
                    concurrent_mode=concurrent_mode
                )
                if not res.success:
                    logger.error(f"[{symbol}] Failed to open {side} position: {res.error_msg}")
                    state.in_position_papper = False
                    state.last_failed_entry_ts = time.time()
                    return
                    
                # БЕЗУСЛОВНАЯ фиксация входа в позицию (ИДЕМПОТЕНТНОСТЬ)
                # Если биржа приняла ордер, мы УЖЕ в позиции. Флаг спасет от двойного входа при отвале REST/WS.
                state.set_in_position(True)
                
            current_time_ms = int(time.time() * 1000)
            state.open_time = current_time_ms
            side_cfg["open_time"] = current_time_ms
            if "0" in state.grid:
                state.grid["0"]["is_active"] = True
                state.grid["0"]["activated_at"] = current_time_ms / 1000.0
                state.grid["0"]["timestamp"] = current_time_ms

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
                    positions = await sym_client.fetch_positions(symbol)
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
            await self.tp_manager.place_take_profit(sym_client, symbol, side, current_price, self.spec_data, state, opposite_state)
            
        finally:
            # ВСЕГДА сбрасываем временный флаг защиты от двойного входа
            state.in_position_papper = False
            # 3. Сохраняем стейт в рантайм кэш
            if hasattr(self, "runtime_manager") and self.runtime_manager:
                if symbol in self.runtime_manager.caches and side in self.runtime_manager.caches[symbol]:
                    self.runtime_manager.caches[symbol][side]["eff_invest"] = getattr(state, "eff_invest", 0.0)
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

                # Рециркуляция прибыли (Cross-Side Profit Recycling), если закрытие в плюс
                closed_vol = getattr(states[side], "last_closed_volume", 0.0)
                closed_entry = getattr(states[side], "last_closed_avg_price", 0.0)
                price_data = self.prices.get(symbol)
                cur_price = price_data[0] if isinstance(price_data, tuple) else price_data
                if (not cur_price or cur_price <= 0) and getattr(states[side], "tp_price", None):
                    cur_price = states[side].tp_price

                if closed_vol > 0 and closed_entry > 0 and cur_price and cur_price > 0:
                    realized_pnl = (cur_price - closed_entry) * closed_vol if side == "LONG" else (closed_entry - cur_price) * closed_vol
                    if realized_pnl > 0:
                        try:
                            from CORE.HEDGE.cross_recycler import CrossSideProfitRecycler
                            await CrossSideProfitRecycler.maybe_recycle(
                                universe=self,
                                symbol=symbol,
                                winning_side=side,
                                realized_profit=realized_pnl,
                                current_price=cur_price,
                                spec_data=self.spec_data,
                            )
                        except Exception as e:
                            logger.error(f"[{symbol}] Error during cross-side recycling: {e}")

                # Записываем метку времени для smart_grace
                states[side].recent_closes.append(time.time())
                
                # Вызов аналитики
                open_time = runtime_cfg.get(side, {}).get("open_time", 0)
                close_time = int(time.time() * 1000)
                sym_client = self.get_client_for_symbol(symbol)
                self.analytics.record_finished_position(sym_client, symbol, side, open_time, close_time)
                # Снимаем все тейк-профит ордера этой стороны
                logger.info(f"[{symbol}] {side} Canceling all limit orders for this side")
                await sym_client.cancel_orders_for_side(symbol, side)
                
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
                if time.time() - ts < 15.0:  # PRICE_STALE_SEC
                    current_price = p
            else:
                current_price = price_data  # safe fallback
                
        if not current_price:
            return  # safely skip loop iteration if we couldn't get a price even after fallback
        
        signal_tasks = []
        
        # Предварительно определяем, будут ли открыты обе стороны
        def _is_side_active(cfg: Optional[dict]) -> bool:
            if not cfg or not isinstance(cfg, dict):
                return False
            if cfg.get("enable") is False or cfg.get("enabled") is False:
                return False
            return bool(cfg.get("enable", cfg.get("enabled", True)))

        sides_to_open = []
        for side in ("LONG", "SHORT"):
            state = states[side]
            side_cfg = runtime_cfg.get(side)
            if side_cfg and _is_side_active(side_cfg) and not state.in_position and not state.in_position_papper:
                sides_to_open.append(side)

        is_concurrent = len(sides_to_open) > 1
        
        for side in ("LONG", "SHORT"):
            state = states[side]
            side_cfg = runtime_cfg.get(side)
            
            if not side_cfg or not _is_side_active(side_cfg):
                continue
            
            if not state.in_position and not state.in_position_papper:
                # Получаем стратегию для символа
                strat = self.strategy_registry.get_strategy_for_symbol(symbol)
                smart_grace_cfg = strat.smart_grace_cfg if strat else DEFAULT_SMART_GRACE
                smart_size_cfg = strat.smart_size_cfg if strat else None
                eval_window = smart_grace_cfg["eval_window_sec"]

                now = time.time()
                state.recent_closes = [t for t in state.recent_closes if now - t <= eval_window]
                closed_count = len(state.recent_closes)

                # Проверка Knife Blocker (запрет входа страдающей стороне)
                opposite_side = "SHORT" if side == "LONG" else "LONG"
                opposite_state = states[opposite_side]
                opposite_state.recent_closes = [t for t in opposite_state.recent_closes if now - t <= eval_window]
                opposite_closed_count = len(opposite_state.recent_closes)

                if MomentumEngine.is_knife_blocked(opposite_closed_count, smart_size_cfg):
                    continue

                # Защита от спама: если на текущей свече попытка входа завершилась ошибкой биржи,
                # выдерживаем кулдаун 60 секунд (до закрытия окна grace period этой свечи)
                failed_ts = getattr(state, "last_failed_entry_ts", 0.0)
                if time.time() - failed_ts < 60.0:
                    continue

                is_signal, current_grace = self.time_control.is_new_interval(closed_count, smart_grace_cfg)

                # Логируем изменения окна благодати
                if state.last_grace_period != current_grace:
                    if state.last_grace_period != 0.0:
                        strat_tag = f"[{strat.strategy_id}] " if strat else ""
                        logger.info(f"[{symbol}] {strat_tag}{side} Smart Grace Window changed: {state.last_grace_period}s -> {current_grace}s (Recent closes: {closed_count})")
                    state.last_grace_period = current_grace

                if is_signal and not BLOCK_ENTRY and getattr(self, 'entry_blocked', False) is not True and not getattr(self, 'is_paused', False) and symbol not in getattr(self, "_closing_symbols", set()) and getattr(state, "is_closing", False) is not True:
                    block_ts = getattr(self, 'block_until_timestamp', 0)
                    now_ts = time.time()
                    if isinstance(block_ts, (int, float)) and block_ts > now_ts:
                        if not hasattr(self, '_last_blocked_log'):
                            self._last_blocked_log = {}
                        last_log = self._last_blocked_log.get(symbol, {}).get(side, 0.0)
                        if now_ts - last_log >= 30.0:
                            self._last_blocked_log.setdefault(symbol, {})[side] = now_ts
                            rem_s = int(block_ts - now_ts)
                            logger.info(f"[{symbol}] {side}: Signal is TRUE but entry is blocked due to Close All cooldown ({rem_s}s remaining).")
                    else:
                        strat_tag = f"[{strat.strategy_id}] " if strat else ""
                        logger.info(f"[{symbol}] {strat_tag}{side}: Signal is TRUE! Entering position...")
                        # Ставим временный флаг идемпотентности
                        state.in_position_papper = True
                        signal_tasks.append(self._process_signal(symbol, side, side_cfg, current_price, concurrent_mode=is_concurrent))
            
            else:
                # Позиция уже открыта (или в процессе in_position_papper)
                if getattr(state, "is_closing", False) is True or symbol in getattr(self, "_closing_symbols", set()):
                    continue
                opposite_state = states["SHORT"] if side == "LONG" else states["LONG"]
                
                sym_client = self.get_client_for_symbol(symbol)
                # LIFO Subgrid Peeling (U14 & U16 support)
                has_peel = any(isinstance(v, dict) and v.get("peel_tp_pct") for v in getattr(state, "grid", {}).values())
                peeled = False
                if has_peel and hasattr(self.avg_manager, "process_peel"):
                    peeled = await self.avg_manager.process_peel(
                        sym_client, self.runtime_manager, symbol, side, state, current_price, self.spec_data, self.tp_manager
                    )
                if not peeled:
                    await self.avg_manager.process(sym_client, self.runtime_manager, symbol, side, state, opposite_state, current_price, self.spec_data, self.tp_manager)
                await self.fallback_tp_manager.process(sym_client, self.runtime_manager, symbol, side, state, current_price, self.spec_data, opposite_state=opposite_state)

        if signal_tasks:
            await asyncio.gather(*signal_tasks)

    async def _game_loop(self):
        """Главный цикл торгового ядра."""
        self.is_running = True
        
        # ШАГ 0. ХОЛОСТОЙ ХОД И НАЧАЛЬНЫЙ АВТОКОМПЛИТ ШТУРМАНА
        if hasattr(self, "radar_navigator") and self.radar_navigator.is_enabled():
            await self.radar_navigator.wait_radar_warmup_and_init()

        # ШАГ 0.1. ПРОВЕРКА НА МОНЕТНОСТЬ
        while self.is_running and not self.symbols:
            logger.warning("[BotCore] Symbol list is empty. Waiting for symbols to be configured in app.json...")
            await asyncio.sleep(5.0)
            self.strategy_registry = StrategyRegistry()
            active_syms = self.strategy_registry.get_all_active_symbols()
            if active_syms:
                self.symbols = active_syms

        if not self.is_running:
            return

        # ШАГ 1. Сборка и проверка рантайм кешей (создание отсутствующих JSON)
        created_new = build_runtime_caches(self.symbols, registry=self.strategy_registry)
        if created_new and not TG_ENABLED:
            prompt_runtime_check()
            
        # ШАГ 1.5. ПЕРВЫЙ РАСЧЕТ ВОЛАТИЛЬНОСТИ
        if hasattr(self, 'volatility_manager'):
            try:
                await self.volatility_manager.process_all()
            except Exception as e:
                logger.error(f"Error during initial VolatilityManager process_all: {e}")
            self.volatility_manager.start()
        
        if hasattr(self, 'strategy_registry') and self.strategy_registry:
            try:
                from CORE.STRATEGY.strategy_applicator import StrategyApplicator
                StrategyApplicator().build_all_runtimes(self.strategy_registry)
            except Exception as e:
                logger.warning(f"[BotCore] Startup StrategyApplicator sync warning: {e}")

        self.runtime_manager.load_initial_caches(self.symbols)
        self.runtime_configs = self.runtime_manager.caches

        # ШАГ 2. Инициализация стримов и фоновых задач
        self.spec_manager.start()
        self._price_task = asyncio.create_task(self.price_stream.run(self._on_tick), name="price_stream")
        
        
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

        # Дожидаемся инициализации цен по всем монетам от стрима (таймаут 30 секунд)
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
            
        # Строгая гарантия: забираем начальный стейт позиций по REST всегда (WS не шлет стейт при старте)
        await self.pos_monitor.sync_from_rest(self.client, self.symbols)

        # Синхронизируем уровни сетки с реальными биржевыми объемами для всех активных позиций
        for sym in self.symbols:
            strat = self.strategy_registry.get_strategy_for_symbol(sym) if hasattr(self, "strategy_registry") and self.strategy_registry else None
            for s_side in ("LONG", "SHORT"):
                st = self.fsm_states.get(sym, {}).get(s_side)
                if st and st.in_position and abs(st.total_volume) > 1e-6:
                    from CORE.STRATEGY.strategy_applicator import StrategyApplicator
                    cur_p = self.prices.get(sym, (0.0, 0))[0] if sym in self.prices else None
                    StrategyApplicator.resolve_position_state_grid(st, strategy=strat, current_price=cur_p)

        # ШАГ 3. Синхронизация рантаймов с реальностью FSM
        logger.info("Streams and REST synced! Running initial RuntimeFSM Sync...")
        await self.runtime_manager.sync_with_fsm(self.fsm_states)

        logger.info("Waiting for exchange specifications to load...")
        while not self.spec_data.get("symbols") and self.is_running:
            await asyncio.sleep(0.5)
        logger.info("Exchange specifications loaded.")

        # Предварительная установка плеч и маржи для всех активных монет (один раз при старте)
        logger.info(f"[BotCore] Pre-warming leverage and margin settings for {len(self.symbols)} active symbols...")
        for sym in self.symbols:
            cfg = self.runtime_configs.get(sym, {})
            side_cfg = cfg.get("LONG") or cfg.get("SHORT")
            if side_cfg:
                try:
                    await self.leverage_manager.set_leverage_and_margin(self.client, sym, side_cfg)
                except Exception as e:
                    logger.warning(f"[{sym}] Leverage pre-warm warning: {e}")

        # Первоначальный аудит и гарантированное восстановление TP ордеров сразу после синхронизации FSM
        await self._reconcile_open_orders(force=True)

        # Запуск фонового контроллера Watchdog
        # Запускаем Notifier
        await self.notifier.start()

        # Запускаем watchdog
        self._watchdog_task = asyncio.create_task(self.watchdog.start(), name="watchdog")

        # ФИНАЛЬНАЯ ТОЧКА ПРОГРЕВА: Запуск аналитики только ПОСЛЕ полной готовности цен, FSM-стейтов и спецификаций
        logger.info("[BotCore] All systems & prices fully warmed up. Running initial sync and starting analytics tracker...")
        await self.analytics.deep_sync_analytics(self.client, self.prices)
        self.analytics.start_realtime_tracker(self.client)

        while self.is_running:
            self._last_tick = time.time()
            self._tick_count += 1
            
            try:

                if not self.symbols:
                    await asyncio.sleep(1.0)
                    continue

                if self.is_paused:
                    await asyncio.sleep(1.0)
                    continue

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

                # Автоматический перезапуск стрима цен при полной стагнации WS (> 25s)
                if len(stale_symbols) == len(self.symbols) and len(self.symbols) > 0:
                    oldest_ts = min([self.prices[s][1] for s in self.symbols if s in self.prices and isinstance(self.prices[s], tuple)] or [0.0])
                    if now - oldest_ts > 25.0 and (now - getattr(self, "_last_stream_restart_ts", 0.0) >= 30.0):
                        self._last_stream_restart_ts = now
                        logger.warning(f"[STREAM_WATCHDOG] All {len(self.symbols)} symbol prices stale for {now - oldest_ts:.1f}s! Auto-restarting price stream...")
                        asyncio.create_task(self._restart_price_stream())

                tasks = [self._process_symbol_loop(symbol) for symbol in self.symbols]
                await asyncio.gather(*tasks)
                
                # Синхронизация рантаймов при изменениях в PositionState (постоянный контроль)
                await self.runtime_manager.sync_with_fsm(self.fsm_states)

                # Периодический аудит и восстановление потерянных TP ордеров
                await self._reconcile_open_orders()
                


                # Автоматическое закрытие по триггеру профита
                if hasattr(self, 'auto_closer'):
                    await self.auto_closer.check()

                # Предотвращение блокировки event loop
                await asyncio.sleep(TIME_SLACK_SEC)                


            except asyncio.CancelledError:
                logger.info("_game_loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in _game_loop: {e}", exc_info=True)
                await asyncio.sleep(TIME_SLACK_SEC)
                
        self.is_running = False
        await self.notifier.stop()
        self.watchdog.stop()
        if hasattr(self, '_watchdog_task') and self._watchdog_task:
            self._watchdog_task.cancel()
        self.spec_manager.stop()
        if hasattr(self, 'price_stream') and self.price_stream:
            self.price_stream.stop()
        if hasattr(self, '_price_task') and self._price_task:
            self._price_task.cancel()
        if hasattr(self, '_pos_task') and self._pos_task:
            self._pos_task.cancel()
        # Попытка быстрого сохранения стейтов при нормальном завершении
        # Закрытие всех подключений мультиаккаунтного пула клиентов
        for creds, cl in getattr(self, '_key_clients', {}).items():
            with contextlib.suppress(Exception):
                await cl.shutdown()
        tasks = [t for t in (getattr(self, '_price_task', None), getattr(self, '_pos_task', None), getattr(self, '_watchdog_task', None)) if t]
        if hasattr(self, '_pos_tasks'):
            tasks.extend([t for t in self._pos_tasks if t])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
        if hasattr(self, 'watchdog'):
            self.watchdog.stop()
        self.spec_manager.stop()
        if hasattr(self, 'volatility_manager'):
            self.volatility_manager.stop()

    async def reset_strategy_iteration(self, strategy_id: str) -> dict:
        """
        Принудительный сброс итерации стратегии и переход на новый цикл.
        Синхронизирует stats, cache, leaderboard и fsm независимо от правил stagnation_card.
        """
        now_ms = int(time.time() * 1000)
        strat_stats_path = ANALYTICS_DIR / "STATS" / f"analytics_{strategy_id}.json"
        strat_analytics = {}
        if strat_stats_path.exists():
            strat_analytics = Utils.read_json_file(strat_stats_path) or {}
        elif hasattr(self, "analytics"):
            strat_analytics = self.analytics._read_data() or {}

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

        # Сброс recent_closes у монет стратегии
        if hasattr(self, "fsm_states") and hasattr(self, "strategy_registry"):
            strat_cfg = self.strategy_registry.active_strategies.get(strategy_id)
            if strat_cfg and hasattr(strat_cfg, "symbols"):
                for sym in strat_cfg.symbols:
                    if sym in self.fsm_states:
                        for s_side in ("LONG", "SHORT"):
                            if s_side in self.fsm_states[sym]:
                                self.fsm_states[sym][s_side].recent_closes = []

        # Кулдаун авто-закрытия
        if hasattr(self, "auto_closer") and self.auto_closer:
            self.auto_closer._last_close_ts[strategy_id] = time.time()

        logger.info(
            f"[{strategy_id}] Manual iteration reset: Cycle #{cur_iter_idx} -> #{next_iter_idx}. "
            f"Base Net: {new_iter_start_net:+.2f} USDT, Iter Net reset to 0.0."
        )

        return {
            "strategy_id": strategy_id,
            "prev_iteration": cur_iter_idx,
            "new_iteration": next_iter_idx,
            "base_net": new_iter_start_net,
            "base_realized": new_iter_start_real
        }

    async def close_all_positions(self, reset_iterations: bool = True) -> dict:
        """Экстренное закрытие всех позиций, отмена лимитных ордеров и сброс итераций СТРОГО для текущей обоймы монет."""
        if False:
            raise Exception("Действие заблокировано: Эта нода сейчас в пассивном режиме (РЕЗЕРВ)!")
            
        symbols = self.symbols if hasattr(self, "symbols") and self.symbols else (
            self.strategy_registry.get_all_active_symbols() if hasattr(self, "strategy_registry") else []
        )
        if not symbols:
            logger.warning("[CLOSE_ALL] No active symbols to close.")
            return {}

        symbols_set = {s.upper() for s in symbols}

        async with self._close_lock:
            # 1. Мгновенная разметка флагов состояний
            self._closing_symbols.update(symbols_set)
            if hasattr(self, "fsm_states"):
                for sym in symbols_set:
                    if sym in self.fsm_states:
                        for s_side in ("LONG", "SHORT"):
                            if s_side in self.fsm_states[sym]:
                                self.fsm_states[sym][s_side].is_closing = True

            now = time.time()
            interval = getattr(self.time_control, "interval_seconds", 300)
            self.entry_blocked = True
            self.block_until_timestamp = max(getattr(self, 'block_until_timestamp', 0), ((now // interval) + 1) * interval + 60.0, now + interval)
            logger.warning(f"🛑 [CLOSE_ALL] Entry block activated until timestamp {self.block_until_timestamp} for symbols: {symbols}")

            try:
                # 2. Помонетная отмена ордеров и закрытие позиций строго по обойме
                for sym in symbols_set:
                    async with self._get_symbol_lock(sym):
                        try:
                            await self.client.cancel_all_orders(sym)
                            logger.info(f"[{sym}] Canceled all limit orders.")
                        except Exception as e:
                            logger.error(f"[{sym}] Failed to cancel orders: {e}")

                # 2.1 Закрытие открытых позиций по рынку (строго sym in symbols_set)
                try:
                    positions = await self.client.fetch_positions()
                    for p in positions:
                        sym = (p.get("symbol") or "").upper()
                        if sym not in symbols_set:
                            continue
                        amt = float(p.get("positionAmt", 0.0))
                        if abs(amt) > 0:
                            pos_side = p.get("positionSide") # "LONG" or "SHORT"
                            side = "SELL" if pos_side == "LONG" else "BUY"
                            async with self._get_symbol_lock(sym):
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

                # 2.5 Контрольный ре-чек с задержкой 0.3с
                await asyncio.sleep(0.3)
                try:
                    positions_recheck = await self.client.fetch_positions()
                    for p in positions_recheck:
                        sym = (p.get("symbol") or "").upper()
                        if sym not in symbols_set:
                            continue
                        amt = float(p.get("positionAmt", 0.0))
                        if abs(amt) > 0:
                            pos_side = p.get("positionSide")
                            side = "SELL" if pos_side == "LONG" else "BUY"
                            async with self._get_symbol_lock(sym):
                                logger.critical(f"[{sym}] [RE-CHECK] Emergency closing hanging {pos_side} position! Volume: {abs(amt)}")
                                await self.client.make_order(
                                    symbol=sym,
                                    qty=abs(amt),
                                    side=side,
                                    position_side=pos_side,
                                    market_type="MARKET"
                                )
                                await self.client.cancel_all_orders(sym)
                except Exception as e:
                    logger.error(f"Error during close_all re-check: {e}")

                # 2.6 Сброс in-memory FSM стейтов и avg_manager строго для монет обоймы
                if hasattr(self, "fsm_states"):
                    for sym in symbols_set:
                        if sym in self.fsm_states:
                            for s_side in ("LONG", "SHORT"):
                                if s_side in self.fsm_states[sym]:
                                    st = self.fsm_states[sym][s_side]
                                    st.reset()
                                    if hasattr(self, "avg_manager"):
                                        self.avg_manager.reset(sym, s_side)
            finally:
                self._closing_symbols.difference_update(symbols_set)
                if not getattr(self, "is_paused", False):
                    self.entry_blocked = False

        # 3. Синхронизация итераций и лидерборда
        reset_report = {}
        if reset_iterations:
            if hasattr(self, "strategy_registry"):
                for strat_id in self.strategy_registry.active_strategies.keys():
                    try:
                        res = await self.reset_strategy_iteration(strat_id)
                        reset_report[strat_id] = res
                    except Exception as re:
                        logger.error(f"[{strat_id}] Error resetting strategy iteration: {re}")

            # Сброс глобальной итерации портфеля
            try:
                g_path = ANALYTICS_DIR / "analytics.json"
                if g_path.exists():
                    g_data = Utils.read_json_file(g_path) or {}
                    g_net = float(g_data.get("net_profit_usdt", 0.0))
                    g_idx = int(g_data.get("iteration_index", 1))
                    next_g_idx = g_idx + 1
                    now_ms = int(time.time() * 1000)

                    g_data["iteration_index"] = next_g_idx
                    g_data["iteration_start_net_profit_usdt"] = round(g_net, 4)
                    g_data["iteration_net_profit_usdt"] = 0.0
                    g_data["iteration_start_ts"] = now_ms
                    g_data["iteration_peak_net_profit_usdt"] = 0.0
                    g_data["iteration_peak_ts"] = now_ms
                    g_data["iteration_peak_age_hours"] = 0.0
                    g_data["iteration_net_velocity"] = 0.0
                    Utils.atomic_write_json(g_path, g_data, indent=4)

                    c_file = CACHE_DIR / "iteration_state.json"
                    c_dict = {}
                    if c_file.exists():
                        try:
                            c_dict = json.loads(c_file.read_text(encoding="utf-8"))
                        except Exception:
                            c_dict = {}
                    c_dict["__global__"] = {
                        "iteration_index": next_g_idx,
                        "iteration_start_net_profit_usdt": round(g_net, 4),
                        "iteration_start_ts": now_ms,
                        "iteration_peak_net_profit_usdt": 0.0,
                        "iteration_peak_ts": now_ms,
                        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    }
                    Utils.atomic_write_json(c_file, c_dict, indent=4)
                    if hasattr(self, "auto_closer") and self.auto_closer:
                        self.auto_closer._last_close_ts["__global__"] = time.time()
            except Exception as ge:
                logger.error(f"Error resetting global portfolio iteration: {ge}")

            # Синхронизация Лидерборда
            try:
                if hasattr(self, "analytics") and hasattr(self.analytics, "leaderboard_engine") and hasattr(self, "strategy_registry"):
                    universes_meta = {
                        sid: {**getattr(scfg, "raw_meta", {}), "name": scfg.name, "enabled": True}
                        for sid, scfg in self.strategy_registry.active_strategies.items()
                    }
                    entries = self.analytics.leaderboard_engine.generate_leaderboard(universes_meta)
                    evaluated_coins = len(self.strategy_registry.get_all_active_symbols())
                    self.analytics.leaderboard_engine.save_portfolio(entries, evaluated_coins_count=evaluated_coins)
                    logger.info(f"[LEADERBOARD] Synchronized leaderboard on Close All. Active: {len(entries)}")
            except Exception as le:
                logger.error(f"Error syncing leaderboard on Close All: {le}")

        return reset_report

    async def close_strategy_positions(self, strategy_id: str):
        """Штатное закрытие позиций и отмена ордеров СТРОГО для монет одной конкретной стратегии."""
        if not hasattr(self, "strategy_registry"):
            return
        strat = self.strategy_registry.active_strategies.get(strategy_id)
        if not strat or not strat.symbols:
            return

        symbols_set = {s.upper() for s in strat.symbols}
        logger.info(f"[{strategy_id}] Closing positions and orders for strategy symbols: {symbols_set}")

        async with self._close_lock:
            self._closing_strategies.add(strategy_id)
            self._closing_symbols.update(symbols_set)

            # 1. Мгновенная разметка флагов состояний FSM
            if hasattr(self, "fsm_states"):
                for sym in symbols_set:
                    if sym in self.fsm_states:
                        for s_side in ("LONG", "SHORT"):
                            if s_side in self.fsm_states[sym]:
                                self.fsm_states[sym][s_side].is_closing = True

            try:
                # 2. Отмена ордеров только для монет этой стратегии
                for sym in symbols_set:
                    async with self._get_symbol_lock(sym):
                        try:
                            await self.client.cancel_all_orders(sym)
                            logger.info(f"[{strategy_id}][{sym}] Canceled limit orders.")
                        except Exception as e:
                            logger.error(f"[{strategy_id}][{sym}] Failed to cancel orders: {e}")

                # 3. Закрытие открытых позиций СТРОГО по обойме этой стратегии
                try:
                    positions = await self.client.fetch_positions()
                    for p in positions:
                        sym = (p.get("symbol") or "").upper()
                        if sym not in symbols_set:
                            continue
                        amt = float(p.get("positionAmt", 0.0))
                        if abs(amt) > 0:
                            pos_side = p.get("positionSide")
                            side = "SELL" if pos_side == "LONG" else "BUY"
                            async with self._get_symbol_lock(sym):
                                logger.info(f"[{strategy_id}][{sym}] Closing {pos_side} pos. Qty: {abs(amt)}")
                                await self.client.make_order(
                                    symbol=sym,
                                    qty=abs(amt),
                                    side=side,
                                    position_side=pos_side,
                                    market_type="MARKET"
                                )
                except Exception as e:
                    logger.error(f"[{strategy_id}] Error closing positions: {e}")

                # 4. Контрольный ре-чек (защита от race condition и зависших сделок)
                await asyncio.sleep(0.3)
                try:
                    positions_recheck = await self.client.fetch_positions()
                    for p in positions_recheck:
                        sym = (p.get("symbol") or "").upper()
                        if sym not in symbols_set:
                            continue
                        amt = float(p.get("positionAmt", 0.0))
                        if abs(amt) > 0:
                            pos_side = p.get("positionSide")
                            side = "SELL" if pos_side == "LONG" else "BUY"
                            async with self._get_symbol_lock(sym):
                                logger.info(f"[{strategy_id}][{sym}] [RE-CHECK] Closing remaining {pos_side} pos. Qty: {abs(amt)}")
                                await self.client.make_order(
                                    symbol=sym,
                                    qty=abs(amt),
                                    side=side,
                                    position_side=pos_side,
                                    market_type="MARKET"
                                )
                                await self.client.cancel_all_orders(sym)
                except Exception as e:
                    logger.error(f"[{strategy_id}] Error in re-check closing positions: {e}")

                # 5. Сброс FSM стейтов и avg_manager строго для закрытых монет стратегии
                if hasattr(self, "fsm_states"):
                    for sym in symbols_set:
                        if sym in self.fsm_states:
                            for s_side in ("LONG", "SHORT"):
                                if s_side in self.fsm_states[sym]:
                                    st = self.fsm_states[sym][s_side]
                                    st.reset()
                                    if hasattr(self, "avg_manager"):
                                        self.avg_manager.reset(sym, s_side)
            finally:
                self._closing_symbols.difference_update(symbols_set)
                self._closing_strategies.discard(strategy_id)

    async def shutdown(self):
        """Гарантированное сохранение рантайма (последний чих) и закрытие сессий."""
        logger.info("Executing graceful BotCore shutdown...")
        self.is_running = False
        if hasattr(self, 'watchdog'):
            self.watchdog.stop()
            try:
                await self.watchdog.aclose()
            except Exception:
                pass
        if hasattr(self, 'pos_stream') and self.pos_stream:
            self.pos_stream._external_stop = True
            try:
                await self.pos_stream.stop()
            except Exception:
                pass
        if hasattr(self, '_pos_task') and self._pos_task:
            self._pos_task.cancel()
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
