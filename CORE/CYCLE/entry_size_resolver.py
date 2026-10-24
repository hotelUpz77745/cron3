# CORE/CYCLE/entry_size_resolver.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.CYCLE.entry_size_resolver
ROLE: Доменный расчет объема первого ордера L0 (Base Invest, Smart Size, Кулак Сильной Руки)
==============================================================================
"""

from typing import Any, Dict, Optional, Tuple
from c_log import UnifiedLogger
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE._utils import TradeMath

logger = UnifiedLogger("EntrySizeResolver")


class EntrySizeResolver:
    """
    Канонический резолвер объема и параметров входа:
    - Настройка плеча и маржи (LeverageManager)
    - Базовая инвестиция (base_invest)
    - Мультипликатор Smart Size (MomentumEngine)
    - Процент первого ордера «Правило кулака Сильной руки» (FirstOrderRegimeManager)
    - Динамический фильтр микро-ступени (Micro-Step Filter)
    - Окончательное квантование объема (TradeMath)
    """

    @staticmethod
    async def resolve_entry_order(
        bot: Any,
        symbol: str,
        side: str,
        side_cfg: dict,
        state: Any,
        current_price: float,
        strat: Optional[Any],
        closed_count: int,
    ) -> Tuple[float, float]:
        """
        Разрешает и возвращает: (volume: float, base_invest: float)
        """
        sym_client = bot.get_client_for_symbol(symbol)

        # 1. Настройка плеча
        await bot.leverage_manager.set_leverage_and_margin(sym_client, symbol, side_cfg)

        # 2. Расчет базы инвестиций
        if "invest_size" not in side_cfg or side_cfg["invest_size"] is None:
            raise ValueError(f"Missing required 'invest_size' in side_cfg for {symbol} ({side})")
        base_invest = float(side_cfg["invest_size"])
        state.eff_invest = base_invest

        # Определение состояния противоположной стороны (для Smart Size и триггера стагнации Кулака)
        opp_side = "SHORT" if side == "LONG" else "LONG"
        opp_state = None
        if hasattr(bot, "fsm_states") and bot.fsm_states:
            opp_state = bot.fsm_states.get(symbol, {}).get(opp_side)
        opp_in_pos = bool(opp_state and (getattr(opp_state, "in_position", False) or getattr(opp_state, "in_position_papper", False)))
        opp_step = int(getattr(opp_state, "cur_step", 0)) if opp_state else 0

        # 3. Smart Size
        smart_size_mult = 1.0
        if strat and getattr(strat, "smart_size_cfg", None):
            trend_state = None
            trend_determ = getattr(strat, "smart_size_trend_determ", None)
            if trend_determ:
                ema_cfg = strat.smart_size_cfg.get("ema_trend")
                if ema_cfg:
                    trend_state = await MomentumEngine.get_ema_trend_state(
                        symbol=symbol,
                        ema_cfg=ema_cfg,
                        client=sym_client,
                    )

            size_mult = MomentumEngine.calculate_smart_size_multiplier(
                closed_count=closed_count,
                smart_size_cfg=strat.smart_size_cfg,
                side=side,
                trend_state=trend_state,
                opp_in_pos=opp_in_pos,
                opp_step=opp_step,
            )
            if size_mult != 1.0:
                smart_size_mult = size_mult
                trend_tag = f" [Trend: {trend_state}]" if trend_state else ""
                logger.info(f"[{symbol}][{strat.strategy_id}] {side} Smart Size applied to grid_0: x{size_mult:.2f}{trend_tag} (closes: {closed_count})")

        # ВАЖНО: eff_invest остается строго base_invest (сетка L1+ не раздувается)
        state.eff_invest = base_invest
        side_cfg["eff_invest"] = base_invest

        grid_0 = side_cfg.get("grid", {}).get("0", {})
        volume_pct = float(grid_0.get("volume") or 12.0)

        # 4. Режим веса первого ордера («Правило кулака»)
        regime_cfg = (
            getattr(strat, "active_settings", {}).get("first_order_weight_regime")
            or getattr(strat, "overrides", {}).get("first_order_weight_regime")
            or getattr(strat, "raw_meta", {}).get("first_order_weight_regime")
        ) if strat else None

        if regime_cfg and hasattr(bot, "first_order_regime") and hasattr(bot.first_order_regime, "evaluate_and_resolve_volume"):
            volume_pct = await bot.first_order_regime.evaluate_and_resolve_volume(
                symbol=symbol,
                side=side,
                regime_cfg=regime_cfg,
                default_vol=volume_pct,
                client=sym_client,
                strategy_id=strat.strategy_id if strat else "",
                opp_in_pos=opp_in_pos,
                opp_step=opp_step,
            )

        # 5. Динамический фильтр микро-ступени (Micro-Step Filter)
        ms_filter = getattr(strat, "micro_step_filter_cfg", None)
        if ms_filter and ms_filter.get("enabled"):
            target_lvl = str(ms_filter["target_level"])
            max_atr = float(ms_filter["max_atr_pct"])
            if hasattr(bot, "get_atr_for_symbol"):
                cur_atr = bot.get_atr_for_symbol(symbol)
                if cur_atr is not None and isinstance(cur_atr, (int, float)):
                    is_active = cur_atr <= max_atr
                    if target_lvl in getattr(state, "grid", {}):
                        state.grid[target_lvl]["enabled"] = is_active
                    if target_lvl in side_cfg.get("grid", {}):
                        side_cfg["grid"][target_lvl]["enabled"] = is_active

        # 6. Квантование объема
        grid_0_invest = base_invest * smart_size_mult
        volume = TradeMath.calculate_order_volume(grid_0_invest, volume_pct, current_price, bot.spec_data, symbol)
        return volume, base_invest
