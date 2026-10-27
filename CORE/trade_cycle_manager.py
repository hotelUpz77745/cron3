# CORE/trade_cycle_manager.py
# -*- coding: utf-8 -*-
"""
=============================================================================
MODULE: CORE.trade_cycle_manager
ROLE: Машина состояний торгового цикла (Signal Processing, Entry, Position Cycle)
=============================================================================
"""

import asyncio
import inspect
import time
from typing import Dict, Any, Optional, Tuple, List

from c_log import UnifiedLogger
from c_utils import Utils
from consts import (
    REST_FAILSAFE_SEC,
    DEFAULT_SMART_GRACE,
    TIME_SLACK_SEC,
)
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE.STRATEGY.directional_trend_gate import DirectionalTrendGate
from CORE.STRATEGY.cross_recycler import CrossSideProfitRecycler
from CORE.STRATEGY.brownian_noise import BrownianNoiseController
from CORE.CYCLE.signal_validator import SignalValidator
from CORE.CYCLE.entry_size_resolver import EntrySizeResolver
from CORE.CYCLE.order_submitter import OrderSubmitter
from CORE.CYCLE.open_position_lifecycle import OpenPositionLifecycleHandler
from CORE.CYCLE.finished_position_handler import FinishedPositionHandler

logger = UnifiedLogger("TradeCycleManager")


class TradeCycleManager:
    """
    Машина состояний торгового цикла (Оркестратор доменных компонентов):
    1. Идемпотентная валидация входов и окон благодати (SignalValidator, Smart Grace, Knife Blocker).
    2. Расчет объемов и плеча (EntrySizeResolver).
    3. Размещение первого ордера и синхронизация FSM с биржей (OrderSubmitter).
    4. Управление открытой позицией (OpenPositionLifecycleHandler: Averaging, LIFO Peeling, Fallback TP).
    5. Фиксация завершенных сделок и рециркуляция прибыли (FinishedPositionHandler).
    """

    def __init__(self, bot_core: Any):
        self.bot = bot_core

    def check_entry_block(self, symbol: str, strategy_id: Optional[str] = None) -> Tuple[bool, str, float]:
        """
        Проверяет блокировку входа для монеты/стратегии.
        Возвращает (is_blocked, reason, remaining_seconds).
        """
        return SignalValidator.check_entry_block(self.bot, symbol, strategy_id)

    async def process_signal(
        self,
        symbol: str,
        side: str,
        side_cfg: dict,
        current_price: float,
        concurrent_mode: bool = False
    ) -> None:
        """Обработка сигнала входа для символа и стороны."""
        can_proceed, reason = SignalValidator.can_process_signal(
            self.bot, symbol, side, self.bot.fsm_states[symbol][side]
        )
        if not can_proceed:
            logger.warning(f"[{symbol}] {side}: Entry aborted — {reason}.")
            return

        state = self.bot.fsm_states[symbol][side]

        try:
            logger.info(f"[{symbol}] Signal triggered for {side}.")

            strat = None
            if hasattr(self.bot, "strategy_registry") and self.bot.strategy_registry:
                try:
                    strat = self.bot.strategy_registry.get_strategy_for_symbol(symbol)
                except Exception:
                    active = getattr(self.bot.strategy_registry, "active_strategies", {})
                    if active and isinstance(active, dict):
                        strat = next(iter(active.values()), None)

            # Свечное окно закрытий (строго в пределах текущей свечи сердцебиения)
            now = Utils.get_current_time(getattr(self.bot, "current_sim_time", None))
            interval = None
            if strat and getattr(strat, "heartbeat_seconds", None):
                interval = int(strat.heartbeat_seconds)
            elif hasattr(self.bot, "get_strategy_heartbeat_seconds"):
                interval = self.bot.get_strategy_heartbeat_seconds(symbol=symbol)
            elif hasattr(self.bot, "get_heartbeat_interval_for_symbol"):
                interval = self.bot.get_heartbeat_interval_for_symbol(symbol)
            elif hasattr(self.bot, "time_control") and getattr(self.bot.time_control, "interval_seconds", None):
                interval = int(self.bot.time_control.interval_seconds)
            if not interval or interval <= 0:
                raise ValueError(f"[TradeCycleManager] Could not determine heartbeat interval for {symbol}")

            candle_id = int(now // interval)
            candle_start = candle_id * interval
            state.recent_closes = [t for t in getattr(state, "recent_closes", []) if t >= candle_start]
            closed_count = len(state.recent_closes)

            # Броуновский шум применяется только между ордерами сильной руки (closed_count >= 1).
            # Первый вход (closed_count == 0) открывается строго без задержки!
            if closed_count >= 1:
                brownian_ctrl = getattr(strat, "brownian_controller", None) if strat else None
                if not brownian_ctrl and hasattr(self.bot, "brownian_noise"):
                    brownian_ctrl = self.bot.brownian_noise

                if brownian_ctrl and brownian_ctrl.should_apply(closed_count):
                    res = brownian_ctrl.apply_noise_if_needed(
                        bot_core=self.bot,
                        symbol=symbol,
                        side=side,
                        state=state,
                        closed_count=closed_count,
                        current_price=current_price,
                    )
                    fresh_price = await res if inspect.isawaitable(res) else res
                    if fresh_price is None:
                        return
                    current_price = fresh_price

            # 1. Разрешение объемов и плеча (EntrySizeResolver)
            volume, _ = await EntrySizeResolver.resolve_entry_order(
                bot=self.bot,
                symbol=symbol,
                side=side,
                side_cfg=side_cfg,
                state=state,
                current_price=current_price,
                strat=strat,
                closed_count=closed_count,
            )

            # 2. Идемпотентная отправка ордера и установка TP (OrderSubmitter)
            await OrderSubmitter.submit_market_entry(
                bot=self.bot,
                symbol=symbol,
                side=side,
                side_cfg=side_cfg,
                state=state,
                volume=volume,
                current_price=current_price,
                concurrent_mode=concurrent_mode,
                wait_for_sync_fn=Utils.wait_for_fsm_sync,
            )

        finally:
            state.in_position_papper = False
            if hasattr(self.bot, "runtime_manager") and self.bot.runtime_manager:
                if hasattr(self.bot.runtime_manager, "caches") and symbol in self.bot.runtime_manager.caches and side in self.bot.runtime_manager.caches[symbol]:
                    self.bot.runtime_manager.caches[symbol][side]["eff_invest"] = getattr(state, "eff_invest", 0.0)
                if hasattr(self.bot.runtime_manager, "save_cache"):
                    s_res = self.bot.runtime_manager.save_cache(symbol)
                    if inspect.isawaitable(s_res):
                        await s_res

    async def check_and_reset_finished_positions(
        self,
        symbol: str,
        states: Dict[str, Any],
        runtime_cfg: dict
    ) -> None:
        """Проверяет флаги is_finished, начисляет аналитику, проводит сжигание токсина и сбрасывает рантайм."""
        await FinishedPositionHandler.process_finished_positions(
            bot=self.bot,
            symbol=symbol,
            states=states,
            runtime_cfg=runtime_cfg,
            recycler_cls=CrossSideProfitRecycler,
        )

    async def process_symbol_loop(
        self,
        symbol: str,
        states: Optional[Dict[str, Any]] = None,
        runtime_cfg: Optional[dict] = None
    ) -> None:
        """Главный тик-цикл символа: оценка сигналов для входа либо обслуживание открытых позиций."""
        if states is None:
            states = self.bot.fsm_states.get(symbol)
        if runtime_cfg is None:
            runtime_cfg = self.bot.runtime_configs.get(symbol)
        if not states or not runtime_cfg:
            return

        await self.check_and_reset_finished_positions(symbol, states, runtime_cfg)

        current_price = None
        price_data = self.bot.prices.get(symbol)
        if price_data:
            if isinstance(price_data, tuple):
                p, ts = price_data
                now_ts = Utils.get_current_time(getattr(self.bot, "current_sim_time", None))
                if abs(now_ts - ts) < 15.0:
                    current_price = p
            else:
                current_price = price_data

        if not current_price:
            return

        def _is_side_active(cfg: Optional[dict]) -> bool:
            if not cfg or not isinstance(cfg, dict):
                return False
            return bool(cfg.get("enable", cfg.get("enabled", True)))

        sides_to_open = []
        for side in ("LONG", "SHORT"):
            state = states[side]
            side_cfg = runtime_cfg.get(side)
            if side_cfg and _is_side_active(side_cfg) and not state.in_position and not state.in_position_papper:
                sides_to_open.append(side)

        is_concurrent = len(sides_to_open) > 1
        signal_tasks = []

        for side in ("LONG", "SHORT"):
            state = states[side]
            side_cfg = runtime_cfg.get(side)
            if not side_cfg or not _is_side_active(side_cfg):
                continue

            if not state.in_position and not state.in_position_papper:
                strat = self.bot.strategy_registry.get_strategy_for_symbol(symbol) if self.bot.strategy_registry else None
                smart_grace_cfg = strat.smart_grace_cfg if strat else DEFAULT_SMART_GRACE
                smart_size_cfg = strat.smart_size_cfg if strat else None

                now = self.bot.get_current_time()
                interval = None
                if strat and getattr(strat, "heartbeat_seconds", None):
                    interval = int(strat.heartbeat_seconds)
                elif hasattr(self.bot, "get_strategy_heartbeat_seconds"):
                    interval = self.bot.get_strategy_heartbeat_seconds(symbol=symbol)
                elif hasattr(self.bot, "get_heartbeat_interval_for_symbol"):
                    interval = self.bot.get_heartbeat_interval_for_symbol(symbol)
                elif hasattr(self.bot, "time_control") and getattr(self.bot.time_control, "interval_seconds", None):
                    interval = int(self.bot.time_control.interval_seconds)
                if not interval or interval <= 0:
                    raise ValueError(f"[TradeCycleManager] Could not determine heartbeat interval for {symbol}")

                candle_id = int(now // interval)
                candle_start = candle_id * interval

                state.recent_closes = [t for t in getattr(state, "recent_closes", []) if t >= candle_start]
                closed_count = len(state.recent_closes)

                opposite_side = "SHORT" if side == "LONG" else "LONG"
                opposite_state = states[opposite_side]
                opposite_state.recent_closes = [t for t in getattr(opposite_state, "recent_closes", []) if t >= candle_start]
                opposite_closed_count = len(opposite_state.recent_closes)

                if MomentumEngine.is_knife_blocked(opposite_closed_count, smart_size_cfg):
                    continue

                # Directional Trend-Gate (Вектор 1: Однонаправленный макро-шлюз по 1d EMA)
                gate_cfg = getattr(strat, "directional_trend_gate", None) if strat else None
                if gate_cfg and gate_cfg.get("enabled"):
                    client_obj = self.bot.get_client_for_symbol(symbol) if hasattr(self.bot, "get_client_for_symbol") else getattr(self.bot, "client", None)
                    is_allowed, gate_reason = await DirectionalTrendGate.is_side_allowed(
                        symbol=symbol,
                        side=side,
                        gate_cfg=gate_cfg,
                        client=client_obj,
                        strategy_id=strat.strategy_id if strat else None,
                    )
                    if not is_allowed:
                        continue

                failed_ts = getattr(state, "last_failed_entry_ts", 0.0)
                if now - failed_ts < 60.0:
                    continue

                is_signal, current_grace = self.bot.time_control.is_new_interval(
                    closed_count, smart_grace_cfg, current_timestamp=now
                )

                if state.last_grace_period != current_grace:
                    if state.last_grace_period != 0.0:
                        strat_tag = f"[{strat.strategy_id}] " if strat else ""
                        logger.info(f"[{symbol}] {strat_tag}{side} Smart Grace: {state.last_grace_period}s -> {current_grace}s")
                    state.last_grace_period = current_grace

                is_blocked, reason, rem = self.check_entry_block(symbol, strat.strategy_id if strat else None)
                if is_blocked:
                    last_log = self.bot._last_blocked_log.get(symbol, {}).get(side, 0.0)
                    now_ts = Utils.get_current_time(getattr(self.bot, "current_sim_time", None))
                    if now_ts - last_log >= 30.0:
                        self.bot._last_blocked_log.setdefault(symbol, {})[side] = now_ts
                        logger.info(f"[{symbol}] {side}: Signal is TRUE but entry is blocked: {reason}.")
                    continue

                if is_signal and not self.bot.is_paused and not getattr(state, "is_closing", False):
                    strat_tag = f"[{strat.strategy_id}] " if strat else ""
                    logger.info(f"[{symbol}] {strat_tag}{side}: Signal is TRUE! Entering position...")
                    state.in_position_papper = True
                    signal_tasks.append(
                        self.process_signal(symbol, side, side_cfg, current_price, concurrent_mode=is_concurrent)
                    )
            else:
                opposite_state = states["SHORT"] if side == "LONG" else states["LONG"]
                await OpenPositionLifecycleHandler.process_open_position(
                    bot=self.bot,
                    symbol=symbol,
                    side=side,
                    state=state,
                    opposite_state=opposite_state,
                    current_price=current_price,
                )

        if signal_tasks:
            await asyncio.gather(*signal_tasks)
