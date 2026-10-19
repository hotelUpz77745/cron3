# CORE/trade_cycle_manager.py
# -*- coding: utf-8 -*-
"""
=============================================================================
MODULE: CORE.trade_cycle_manager
ROLE: Машина состояний торгового цикла (Signal Processing, Entry, Position Cycle)
=============================================================================
"""

import asyncio
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
from CORE.STRATEGY.cross_recycler import CrossSideProfitRecycler

logger = UnifiedLogger("TradeCycleManager")


class TradeCycleManager:
    """
    Машина состояний торгового цикла:
    1. Идемпотентная валидация входов и окон благодати (Smart Grace, Knife Blocker).
    2. Размещение первого ордера и синхронизация FSM с биржей.
    3. Управление открытой позицией (Averaging, LIFO Peeling, Take Profit).
    4. Фиксация завершенных сделок, рециркуляция прибыли (Cross-Side Profit Recycling), сброс рантайма.
    """

    def __init__(self, bot_core: Any):
        self.bot = bot_core

    def check_entry_block(self, symbol: str, strategy_id: Optional[str] = None) -> Tuple[bool, str, float]:
        """
        Проверяет блокировку входа для монеты/стратегии.
        Возвращает (is_blocked, reason, remaining_seconds).
        """
        now_ts = Utils.get_current_time(getattr(self.bot, "current_sim_time", None))
        sym_upper = symbol.upper()

        if getattr(self.bot, 'is_paused', False) is True:
            return True, "Bot is paused", 0.0

        if sym_upper in getattr(self.bot, "_closing_symbols", set()):
            return True, "Symbol is in closing sequence", 0.0

        sym_blocked = 0.0
        s_blocked_dict = getattr(self.bot, 'symbol_blocked_until', None)
        if isinstance(s_blocked_dict, dict):
            sym_blocked = s_blocked_dict.get(sym_upper, 0.0)

        strat_blocked = 0.0
        if not strategy_id and getattr(self.bot, "strategy_registry", None):
            try:
                s_obj = self.bot.strategy_registry.get_strategy_for_symbol(symbol)
                if s_obj:
                    strategy_id = s_obj.strategy_id
            except Exception:
                strategy_id = None

        st_blocked_dict = getattr(self.bot, 'strategy_blocked_until', None)
        if strategy_id and isinstance(st_blocked_dict, dict):
            strat_blocked = st_blocked_dict.get(strategy_id, 0.0)

        global_blocked = getattr(self.bot, 'block_until_timestamp', 0.0)
        global_blocked = global_blocked if isinstance(global_blocked, (int, float)) else 0.0
        sym_blocked = sym_blocked if isinstance(sym_blocked, (int, float)) else 0.0
        strat_blocked = strat_blocked if isinstance(strat_blocked, (int, float)) else 0.0
        effective_until = max(global_blocked, sym_blocked, strat_blocked)

        if effective_until > now_ts:
            rem = effective_until - now_ts
            tfr_desc = "heartbeat"
            if getattr(self.bot, "strategy_registry", None):
                try:
                    s_obj = self.bot.strategy_registry.get_strategy(strategy_id) if strategy_id else self.bot.strategy_registry.get_strategy_for_symbol(symbol)
                    if s_obj and hasattr(s_obj, "signal_heartbeat_tfr"):
                        tfr_desc = s_obj.signal_heartbeat_tfr
                except Exception:
                    pass
            return True, f"Heartbeat ({tfr_desc}) candle window cooldown active ({int(rem)}s remaining)", rem

        if getattr(self.bot, 'entry_blocked', False) is True:
            if global_blocked > 0 and now_ts >= global_blocked:
                self.bot.entry_blocked = False
            else:
                return True, "Entry blocked flag is set", 0.0

        return False, "", 0.0

    async def process_signal(
        self,
        symbol: str,
        side: str,
        side_cfg: dict,
        current_price: float,
        concurrent_mode: bool = False
    ) -> None:
        """Обработка сигнала входа для символа и стороны."""
        is_blocked, reason, _ = self.check_entry_block(symbol)
        if is_blocked:
            logger.warning(f"[{symbol}] {side}: Entry aborted — {reason}.")
            return

        state = self.bot.fsm_states[symbol][side]
        if getattr(state, "is_closing", False) is True:
            logger.warning(f"[{symbol}] {side}: Entry aborted — position state is marked as is_closing.")
            return

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
            interval = 300
            if hasattr(self.bot, "get_heartbeat_interval_for_symbol"):
                try:
                    interval = self.bot.get_heartbeat_interval_for_symbol(symbol)
                except Exception:
                    interval = 300
            elif hasattr(self.bot, "time_control") and getattr(self.bot.time_control, "interval_seconds", None):
                interval = int(self.bot.time_control.interval_seconds)

            candle_id = int(now // interval)
            candle_start = candle_id * interval
            state.recent_closes = [t for t in getattr(state, "recent_closes", []) if t >= candle_start]
            closed_count = len(state.recent_closes)

            # Броуновский шум применяется только между ордерами сильной руки (closed_count >= 1).
            # Первый вход (closed_count == 0) открывается строго без задержки!
            if strat and getattr(strat, "smart_grace_cfg", None) and closed_count >= 1:
                bn_factor = (
                    strat.smart_grace_cfg.get("brownian_noise_factor")
                    or strat.smart_grace_cfg.get("white_noise_factor")
                )
                if isinstance(bn_factor, (list, tuple)) and len(bn_factor) == 2:
                    low = float(min(bn_factor))
                    high = float(max(bn_factor))
                    if high > 0 and high >= low and getattr(self.bot, "current_sim_time", None) is None:
                        import CORE.bot as _bot_mod
                        noise_sleep = round(_bot_mod.secrets.SystemRandom().uniform(low, high), 2)
                        logger.info(
                            f"[{symbol}] {side} Brownian Noise Factor: sleeping {noise_sleep}s (range [{low}..{high}]s) "
                            f"between strong-hand orders (closed orders in window: {closed_count})."
                        )
                        await _bot_mod.asyncio.sleep(noise_sleep)
                        if (
                            getattr(self.bot, 'is_paused', False) is True 
                            or getattr(self.bot, 'entry_blocked', False) is True
                            or (getattr(self.bot, "_closing_symbols", None) and symbol.upper() in self.bot._closing_symbols)
                            or getattr(state, "is_closing", False) is True
                            or getattr(state, "in_position", False) is True
                        ):
                            logger.warning(f"[{symbol}] {side}: Entry aborted after brownian noise sleep.")
                            state.in_position_papper = False
                            return
                        p_fresh = await _bot_mod.BinancePublic.get_last_price(symbol)
                        if p_fresh and p_fresh > 0:
                            current_price = float(p_fresh)

            sym_client = self.bot.get_client_for_symbol(symbol)
            # 1. Настройка плеча
            await self.bot.leverage_manager.set_leverage_and_margin(sym_client, symbol, side_cfg)

            # 1.5. Расчет базы инвестиций
            if "invest_size" not in side_cfg or side_cfg["invest_size"] is None:
                raise ValueError(f"Missing required 'invest_size' in side_cfg for {symbol} ({side})")
            base_invest = float(side_cfg["invest_size"])
            if not strat and hasattr(self.bot, "strategy_registry") and self.bot.strategy_registry:
                try:
                    strat = self.bot.strategy_registry.get_strategy_for_symbol(symbol)
                except Exception:
                    active = getattr(self.bot.strategy_registry, "active_strategies", {})
                    if active and isinstance(active, dict):
                        strat = next(iter(active.values()), None)

            if strat and getattr(strat, "trend_size_cfg", None) and getattr(strat.trend_size_cfg, "get", lambda k: None)("enabled"):
                from CORE.STRATEGY.trend_size_depender import TrendSizeDepender
                eff = await TrendSizeDepender.get_effective_invest_size(
                    sym_client, symbol, side, base_invest, strat.trend_size_cfg
                )
                if eff > 0:
                    base_invest = eff
            state.eff_invest = base_invest
            invest_size = base_invest

            smart_size_mult = 1.0
            if strat and getattr(strat, "smart_size_cfg", None):
                trend_state = None
                trend_determ = strat.smart_size_cfg.get("trend_determ") or strat.smart_size_cfg.get("dynamic_sizing_mode")
                if trend_determ:
                    if str(trend_determ).startswith("adx"):
                        adx_cfg = strat.smart_size_cfg.get("adx_trend") or {"timeframe": "1d", "period": 14, "adx_threshold": 25.0}
                        trend_state = await MomentumEngine.get_adx_trend_state(
                            symbol=symbol,
                            adx_cfg=adx_cfg,
                            client=sym_client
                        )
                    else:
                        ema_cfg = strat.smart_size_cfg.get("ema_trend")
                        trend_state = await MomentumEngine.get_ema_trend_state(
                            symbol=symbol,
                            ema_cfg=ema_cfg,
                            client=sym_client
                        )

                opp_side = "SHORT" if side == "LONG" else "LONG"
                opp_state = None
                if hasattr(self.bot, "fsm_states") and self.bot.fsm_states:
                    opp_state = self.bot.fsm_states.get(symbol, {}).get(opp_side)
                opp_in_pos = bool(opp_state and (getattr(opp_state, "in_position", False) or getattr(opp_state, "in_position_papper", False)))
                opp_step = int(getattr(opp_state, "cur_step", 0)) if opp_state else 0

                size_mult = MomentumEngine.calculate_smart_size_multiplier(
                    closed_count=closed_count,
                    smart_size_cfg=strat.smart_size_cfg,
                    side=side,
                    trend_state=trend_state,
                    opp_in_pos=opp_in_pos,
                    opp_step=opp_step
                )
                if size_mult != 1.0:
                    smart_size_mult = size_mult
                    trend_tag = f" [Trend: {trend_state}]" if trend_state else ""
                    logger.info(f"[{symbol}][{strat.strategy_id}] {side} Smart Size applied to grid_0: x{size_mult:.2f}{trend_tag} (closes: {closed_count})")

            # ВАЖНО: state.eff_invest и side_cfg["eff_invest"] остаются строго base_invest.
            # Колена усреднения сетки (L1..L7) не перегружают депозит и рассчитываются по базовому капиталу.
            state.eff_invest = base_invest
            side_cfg["eff_invest"] = base_invest

            grid_0 = side_cfg.get("grid", {}).get("0", {})
            volume_pct = float(grid_0.get("volume") or 12.0)

            # Режим веса первого ордера («Правило кулака»)
            regime_cfg = (
                getattr(strat, "active_settings", {}).get("first_order_weight_regime")
                or getattr(strat, "overrides", {}).get("first_order_weight_regime")
                or getattr(strat, "raw_meta", {}).get("first_order_weight_regime")
            ) if strat else None

            if regime_cfg and hasattr(self.bot, "first_order_regime") and hasattr(self.bot.first_order_regime, "evaluate_and_resolve_volume"):
                volume_pct = await self.bot.first_order_regime.evaluate_and_resolve_volume(
                    symbol=symbol,
                    side=side,
                    regime_cfg=regime_cfg,
                    default_vol=volume_pct,
                    client=sym_client,
                    strategy_id=strat.strategy_id if strat else "",
                )

            # Динамический фильтр микро-ступени волатильности (Micro-Step Filter)
            ms_filter = getattr(strat, "micro_step_filter_cfg", None)
            if ms_filter and ms_filter.get("enabled"):
                target_lvl = str(ms_filter["target_level"])
                max_atr = float(ms_filter["max_atr_pct"])
                if hasattr(self.bot, "get_atr_for_symbol"):
                    cur_atr = self.bot.get_atr_for_symbol(symbol)
                    if cur_atr is not None and isinstance(cur_atr, (int, float)):
                        is_active = bool(cur_atr <= max_atr)
                        if target_lvl in getattr(state, "grid", {}):
                            state.grid[target_lvl]["enabled"] = is_active
                        if target_lvl in side_cfg.get("grid", {}):
                            side_cfg["grid"][target_lvl]["enabled"] = is_active

            grid_0_invest = base_invest * smart_size_mult
            from CORE._utils import TradeMath
            volume = TradeMath.calculate_order_volume(grid_0_invest, volume_pct, current_price, self.bot.spec_data, symbol)
            
            order_side = "BUY" if side == "LONG" else "SELL"

            # Асинхронный лок монеты
            sym_lock = self.bot._get_symbol_lock(symbol) if hasattr(self.bot, "_get_symbol_lock") else None
            async def _do_open():
                is_blocked, reason, _ = self.check_entry_block(symbol)
                if is_blocked or getattr(state, "is_closing", False) is True:
                    logger.warning(f"[{symbol}] {side}: Entry aborted right before market order — {reason or 'state is closing'}.")
                    state.in_position_papper = False
                    return False

                logger.info(f"[{symbol}] Opening {side} market order. Volume: {volume}")
                res = await sym_client.make_order(
                    symbol=symbol,
                    qty=volume,
                    side=order_side,
                    position_side=side,
                    market_type="MARKET",
                    concurrent_mode=concurrent_mode
                )
                if not res or not res.success:
                    err = getattr(res, "error_msg", "Unknown error")
                    logger.error(f"[{symbol}] Failed to open {side} position: {err}")
                    state.in_position_papper = False
                    state.last_failed_entry_ts = Utils.get_current_time(getattr(self.bot, "current_sim_time", None))
                    return False

                state.set_in_position(True)
                return True

            if sym_lock:
                async with sym_lock:
                    if not await _do_open():
                        return
            else:
                if not await _do_open():
                    return

            current_time_ms = int(Utils.get_current_time(getattr(self.bot, "current_sim_time", None)) * 1000)
            state.open_time = current_time_ms
            side_cfg["open_time"] = current_time_ms
            if "0" in state.grid:
                state.grid["0"]["is_active"] = True
                state.grid["0"]["activated_at"] = current_time_ms / 1000.0
                state.grid["0"]["timestamp"] = current_time_ms

            state.fallback_price = None
            state.next_avg_price = None
            state.initial_entry_price = 0.0
            state.pre_avg_price = 0.0

            sync_success = await Utils.wait_for_fsm_sync(state, timeout_sec=3.0, poll_interval=0.01)
            if not sync_success:
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
                except Exception as e:
                    logger.error(f"[{symbol}] {side} REST fallback failed: {e}")

            # Размещение Take Profit ордера
            opposite_state = self.bot.fsm_states[symbol]["SHORT"] if side == "LONG" else self.bot.fsm_states[symbol]["LONG"]
            await self.bot.tp_manager.place_take_profit(
                sym_client, symbol, side, current_price, self.bot.spec_data, state, opposite_state
            )

        finally:
            state.in_position_papper = False
            if hasattr(self.bot, "runtime_manager") and self.bot.runtime_manager:
                if hasattr(self.bot.runtime_manager, "caches") and symbol in self.bot.runtime_manager.caches and side in self.bot.runtime_manager.caches[symbol]:
                    self.bot.runtime_manager.caches[symbol][side]["eff_invest"] = getattr(state, "eff_invest", 0.0)
                if hasattr(self.bot.runtime_manager, "save_cache"):
                    s_res = self.bot.runtime_manager.save_cache(symbol)
                    import inspect
                    if inspect.isawaitable(s_res):
                        await s_res

    async def check_and_reset_finished_positions(
        self,
        symbol: str,
        states: Dict[str, Any],
        runtime_cfg: dict
    ) -> None:
        """Проверяет флаги is_finished, начисляет аналитику, проводит сжигание токсина и сбрасывает рантайм."""
        sides_to_reset = [side for side in ("LONG", "SHORT") if states[side].is_finished]
        if not sides_to_reset:
            return

        for idx, side in enumerate(sides_to_reset):
            logger.info(f"[{symbol}] {side} is_finished. Running analytics and resetting runtime cache...")

            # Cross-Side Profit Recycling
            closed_vol = getattr(states[side], "last_closed_volume", 0.0)
            closed_entry = getattr(states[side], "last_closed_avg_price", 0.0)
            price_data = self.bot.prices.get(symbol)
            cur_price = price_data[0] if isinstance(price_data, tuple) else price_data
            if (not cur_price or cur_price <= 0) and getattr(states[side], "tp_price", None):
                cur_price = states[side].tp_price

            if closed_vol > 0 and closed_entry > 0 and cur_price and cur_price > 0:
                realized_pnl = (cur_price - closed_entry) * closed_vol if side == "LONG" else (closed_entry - cur_price) * closed_vol
                if realized_pnl > 0:
                    try:
                        await CrossSideProfitRecycler.maybe_recycle(
                            universe=self.bot,
                            symbol=symbol,
                            winning_side=side,
                            realized_profit=realized_pnl,
                            current_price=cur_price,
                            spec_data=self.bot.spec_data,
                        )
                    except Exception as e:
                        logger.error(f"[{symbol}] Error during cross-side recycling: {e}")

            now = self.bot.get_current_time()
            states[side].recent_closes.append(now)

            open_time = runtime_cfg.get(side, {}).get("open_time", 0)
            close_time = int(now * 1000)
            sym_client = self.bot.get_client_for_symbol(symbol)
            self.bot.analytics.record_finished_position(sym_client, symbol, side, open_time, close_time)

            logger.info(f"[{symbol}] {side} Canceling all limit orders for this side")
            await sym_client.cancel_orders_for_side(symbol, side)

            self.bot.runtime_manager.reset_state_to_default(symbol, side, states[side])
            self.bot.avg_manager.reset(symbol, side)

            if idx < len(sides_to_reset) - 1 and getattr(self.bot, "current_sim_time", None) is None:
                await asyncio.sleep(0.1)

        if hasattr(self.bot, "runtime_manager") and hasattr(self.bot.runtime_manager, "save_cache"):
            s_res = self.bot.runtime_manager.save_cache(symbol)
            import inspect
            if inspect.isawaitable(s_res):
                await s_res

    async def process_symbol_loop(self, symbol: str) -> None:
        """Основной цикл обработки одного торгового символа."""
        runtime_cfg = self.bot.runtime_configs.get(symbol, {})
        states = self.bot.fsm_states[symbol]

        # REST Failsafe
        current_time = time.monotonic()
        last_sync = self.bot._last_rest_syncs.get(symbol, current_time)
        if current_time - last_sync > REST_FAILSAFE_SEC or symbol not in self.bot._last_rest_syncs:
            self.bot._last_rest_syncs[symbol] = current_time
            any_open = any(s.in_position for s in states.values())
            if any_open and hasattr(self.bot, "pos_monitor") and self.bot.pos_monitor:
                try:
                    await self.bot.pos_monitor.sync_from_rest(self.bot.client, [symbol])
                except Exception as e:
                    logger.error(f"[{symbol}] Periodic REST sync failed: {e}")

        # Проверка закрытия позиций
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
                interval = 300
                if hasattr(self.bot, "get_heartbeat_interval_for_symbol"):
                    try:
                        interval = self.bot.get_heartbeat_interval_for_symbol(symbol)
                    except Exception:
                        interval = 300
                elif hasattr(self.bot, "time_control") and getattr(self.bot.time_control, "interval_seconds", None):
                    interval = int(self.bot.time_control.interval_seconds)

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
                if getattr(state, "is_closing", False) is True or symbol in self.bot._closing_symbols:
                    continue
                opposite_state = states["SHORT"] if side == "LONG" else states["LONG"]
                sym_client = self.bot.get_client_for_symbol(symbol)

                has_peel = any(isinstance(v, dict) and v.get("peel_tp_pct") for v in getattr(state, "grid", {}).values())
                peeled = False
                if has_peel and hasattr(self.bot.avg_manager, "process_peel"):
                    peeled = await self.bot.avg_manager.process_peel(
                        sym_client, self.bot.runtime_manager, symbol, side, state, current_price, self.bot.spec_data, self.bot.tp_manager
                    )
                if not peeled:
                    await self.bot.avg_manager.process(
                        sym_client, self.bot.runtime_manager, symbol, side, state, opposite_state, current_price, self.bot.spec_data, self.bot.tp_manager
                    )
                await self.bot.fallback_tp_manager.process(
                    sym_client, self.bot.runtime_manager, symbol, side, state, current_price, self.bot.spec_data, opposite_state=opposite_state
                )

        if signal_tasks:
            await asyncio.gather(*signal_tasks)
