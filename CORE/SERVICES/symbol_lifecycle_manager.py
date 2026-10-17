# CORE/SERVICES/symbol_lifecycle_manager.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: CORE/SERVICES/symbol_lifecycle_manager.py
Role: Сервис динамического управления жизненным циклом торговых символов (добавление, удаление, синхронизация)
==============================================================================
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Optional, Dict, List

from c_log import UnifiedLogger
from c_utils import Utils
from consts import DATA_DIR
from API.BINANCE.public import BinancePublic
from POS_FSM.models import PositionState
from CORE.strategy_applicator import StrategyApplicator

logger = UnifiedLogger("SymbolLifecycleManager")


class SymbolLifecycleManager:
    """
    Высокоуровневый сервис динамического жизненного цикла торговых символов:
    1. Регистрация нового символа: обновление app.json, синхронизация StrategyRegistry,
       генерация FSM-состояний, подписка на стримы и мониторы, предвыборка цен и прогрев маржи.
    2. Удаление символа: очистка app.json, удаление из FSM-состояний, мониторов и стримов,
       удаление кэша рантайма и безопасный перезапуск price stream.
    """

    def __init__(self, bot_core: Any):
        self.bot = bot_core

    async def add_symbol(self, symbol: str, strategy_id: Optional[str] = None) -> bool:
        """
        Динамическое добавление торгового символа в обойму бота.
        Возвращает True, если символ успешно добавлен, False если уже активен или произошла ошибка.
        """
        symbol = symbol.upper()
        if symbol in self.bot.symbols:
            logger.warning(f"[{symbol}] Symbol is already active in BotCore.")
            return False

        # 1. Update app.json
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) if app_json_path.exists() else {}
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
                if getattr(self.bot, "strategy_registry", None):
                    u_meta = self.bot.strategy_registry.universes_db.get(target_strat, {})
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
                        "update_interval_hours": 24,
                    },
                    "analytics": {
                        "start_balance_usdt": u_meta.get("start_balance_usdt", 449.0)
                    },
                    "notifications": {
                        "enabled": True,
                        "negative_threshold": -20.0,
                        "positive_threshold": 20.0,
                    },
                    "auto_closing": {
                        "negative": {"threshold": None, "threshold_increment": None},
                        "positive": {"threshold": 100, "threshold_increment": 100},
                    },
                }
            Utils.write_json_file(app_json_path, app_data)
            logger.info(f"[{symbol}] Added to active_strategy '{target_strat}' in app.json.")

        # 1b. Reload StrategyRegistry
        if getattr(self.bot, "strategy_registry", None):
            try:
                self.bot.strategy_registry.reload(active_strategies)
                if hasattr(self.bot, "_init_clients"):
                    self.bot._init_clients()
            except Exception as e:
                logger.error(f"Error reloading StrategyRegistry: {e}")

        # 1c. Build isolated runtime config using StrategyApplicator if needed
        try:
            if getattr(self.bot, "strategy_registry", None) and self.bot.strategy_registry.is_symbol_active(symbol):
                strat_cfg = self.bot.strategy_registry.get_strategy_for_symbol(symbol)
                StrategyApplicator().build_runtime_for_symbol(symbol, strat_cfg)
        except Exception as e:
            logger.warning(f"[{symbol}] StrategyApplicator build notice: {e}")

        # 2. Update bot lists and FSM states
        self.bot.symbols.append(symbol)

        if symbol not in self.bot.fsm_states:
            self.bot.fsm_states[symbol] = {
                "LONG": PositionState(symbol=symbol, side="LONG"),
                "SHORT": PositionState(symbol=symbol, side="SHORT"),
            }

        # 3. Add to monitors and streams
        if getattr(self.bot, "pos_stream", None) and self.bot.pos_stream.target_symbols is not None:
            self.bot.pos_stream.target_symbols.add(symbol)
        if getattr(self.bot, "ws_interpreter", None) and self.bot.ws_interpreter.target_symbols is not None:
            self.bot.ws_interpreter.target_symbols.add(symbol)
        if getattr(self.bot, "pos_monitor", None) and self.bot.pos_monitor.target_symbols is not None:
            if symbol not in self.bot.pos_monitor.target_symbols:
                self.bot.pos_monitor.target_symbols.append(symbol)

        # 4. Immediate REST price prefetch
        try:
            p = await BinancePublic.get_last_price(symbol)
            if p and p > 0:
                self.bot.prices[symbol] = (p, time.time())
                logger.info(f"[{symbol}] Initial REST price fetched: {p}")
        except Exception as e:
            logger.warning(f"[{symbol}] Failed initial REST price fetch: {e}")

        # 5. Reload caches
        if getattr(self.bot, "runtime_manager", None):
            self.bot.runtime_manager.load_initial_caches(self.bot.symbols)
            self.bot.runtime_configs = self.bot.runtime_manager.caches
            self.bot.runtime_manager.populate_fsm_from_cache(self.bot.fsm_states)

        # 5.5 Pre-warm leverage for the new symbol (sets leverage & margin once)
        try:
            cfg = getattr(self.bot, "runtime_configs", {}).get(symbol, {})
            side_cfg = cfg.get("LONG") or cfg.get("SHORT")
            if side_cfg and getattr(self.bot, "leverage_manager", None):
                target_client = self.bot.get_client_for_symbol(symbol) if hasattr(self.bot, "get_client_for_symbol") else getattr(self.bot, "client", None)
                await self.bot.leverage_manager.set_leverage_and_margin(target_client, symbol, side_cfg)
        except Exception as e:
            logger.warning(f"[{symbol}] Leverage pre-warm warning on add_symbol: {e}")

        # 6. Restart price stream
        if hasattr(self.bot, "_restart_price_stream"):
            logger.info(f"Restarting price stream to include new symbol: {symbol}")
            await self.bot._restart_price_stream()

        # 7. Trigger VolatilityManager to compute super_indent for the new symbol
        if getattr(self.bot, "volatility_manager", None) and getattr(self.bot.volatility_manager, "is_running", False):
            asyncio.create_task(self.bot.volatility_manager.process_all())

        logger.info(f"[{symbol}] Dynamically added to BotCore.")
        return True

    async def delete_symbol(self, symbol: str) -> bool:
        """
        Динамическое удаление торгового символа из обоймы бота.
        Возвращает True, если символ успешно удален, False если символа не было в обойме.
        """
        symbol = symbol.upper()
        if symbol not in self.bot.symbols:
            return False

        logger.info(f"[{symbol}] Dynamically deleting symbol from BotCore...")

        # 1. Remove from app.json
        app_json_path = DATA_DIR / "app.json"
        app_data = Utils.read_json_file(app_json_path) if app_json_path.exists() else {}
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
        if getattr(self.bot, "strategy_registry", None):
            try:
                self.bot.strategy_registry.reload(active_strategies)
                if hasattr(self.bot, "_init_clients"):
                    self.bot._init_clients()
            except Exception as e:
                logger.error(f"Error reloading StrategyRegistry on delete: {e}")

        # 2. Update bot lists and FSM states
        self.bot.symbols.remove(symbol)

        if symbol in self.bot.fsm_states:
            del self.bot.fsm_states[symbol]

        if hasattr(self.bot, "runtime_configs") and symbol in self.bot.runtime_configs:
            del self.bot.runtime_configs[symbol]

        if hasattr(self.bot, "prices") and symbol in self.bot.prices:
            del self.bot.prices[symbol]

        # 3. Remove from monitors and streams
        if getattr(self.bot, "pos_stream", None) and self.bot.pos_stream.target_symbols and symbol in self.bot.pos_stream.target_symbols:
            self.bot.pos_stream.target_symbols.remove(symbol)
        if getattr(self.bot, "ws_interpreter", None) and self.bot.ws_interpreter.target_symbols is not None:
            self.bot.ws_interpreter.target_symbols.discard(symbol)
        if getattr(self.bot, "pos_monitor", None) and self.bot.pos_monitor.target_symbols is not None:
            self.bot.pos_monitor.target_symbols = [s for s in self.bot.pos_monitor.target_symbols if s != symbol]

        # 4. Delete the runtime JSON cache
        if getattr(self.bot, "runtime_manager", None):
            runtime_path = self.bot.runtime_manager.get_runtime_path(symbol)
            if runtime_path.exists():
                try:
                    runtime_path.unlink()
                    logger.info(f"[{symbol}] Deleted runtime cache file: {runtime_path}.")
                except Exception as e:
                    logger.error(f"[{symbol}] Failed to delete runtime cache: {e}")

            root_path = self.bot.runtime_manager.runtime_dir / f"{symbol.lower()}.json"
            if root_path.exists():
                try:
                    root_path.unlink()
                except Exception:
                    pass

        # 5. Restart price stream
        if hasattr(self.bot, "_restart_price_stream"):
            logger.info(f"Restarting price stream to exclude deleted symbol: {symbol}")
            await self.bot._restart_price_stream()

        logger.info(f"[{symbol}] Successfully deleted from BotCore.")
        return True
