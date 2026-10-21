# ============================================================
# FILE: CORE/TP/tp_backoff_manager.py
# ROLE: Evaluates TP order timeout and applies backoff tightening to market/base indent
# ============================================================

import time
from typing import Optional, Dict, Any

from consts import _BASE_CFG
from c_log import UnifiedLogger

logger = UnifiedLogger("TakeProfitBackoffManager")


class TakeProfitBackoffManager:
    """
    Механика тайм-аута тейк-профита (TP Backoff):
    Если выставленный лимитный ордер TP не исполнился за timeout_sec (например 1800s = 30 мин),
    переставляет ордер ближе к рынку на target_indent_pct (например 1.0% или 1.2%),
    ускоряя выход из позиции и исключая зависание на завышенных ожиданиях.
    """

    def __init__(self, runtime_manager=None):
        self.runtime_manager = runtime_manager

    async def check_and_apply_backoff(
        self,
        client,
        runtime_manager,
        symbol: str,
        side: str,
        state,
        current_price: float,
        spec_data: dict,
        tp_manager,
        opposite_state=None,
    ) -> bool:
        """
        Проверяет условия тайм-аута TP и при необходимости переставляет лимитный ордер на target_indent_pct.
        Возвращает True если backoff был применен, иначе False.
        """
        if not getattr(state, "in_position", False) or getattr(state, "is_finished", False):
            return False
        if getattr(state, "is_closing", False) or getattr(state, "pending_rolling_tp", False):
            return False
        if getattr(state, "tp_placed_at", None) is None:
            return False
        if getattr(state, "tp_backoff_applied", False):
            return False

        rm = runtime_manager or self.runtime_manager
        strat = None
        if hasattr(rm, "strategy_registry") and rm.strategy_registry:
            try:
                strat = rm.strategy_registry.get_strategy_for_symbol(symbol)
            except Exception:
                strat = None

        backoff_cfg = None
        if strat:
            backoff_cfg = getattr(strat, "tp_backoff_cfg", None)
            if not backoff_cfg and hasattr(strat, "raw_meta") and isinstance(strat.raw_meta, dict):
                backoff_cfg = strat.raw_meta.get("tp_backoff")
            if not backoff_cfg and hasattr(strat, "overrides") and isinstance(strat.overrides, dict):
                backoff_cfg = strat.overrides.get("tp_backoff")
        if not backoff_cfg:
            base_cfg = getattr(rm, "base_cfg", None) or {}
            backoff_cfg = base_cfg.get("tp_backoff") or _BASE_CFG.get("tp_backoff")

        if not backoff_cfg or not isinstance(backoff_cfg, dict) or not backoff_cfg.get("enabled", False):
            return False

        bot = getattr(rm, "bot_core", None)
        now = bot.get_current_time() if (bot and hasattr(bot, "get_current_time")) else (getattr(client, "current_sim_time", None) or time.time())
        timeout_sec = float(backoff_cfg.get("timeout_sec", 1800))
        elapsed_sec = now - state.tp_placed_at
        if elapsed_sec < timeout_sec:
            return False

        # Определение целевого отступа (target_indent_pct / fallback_indent / indent)
        raw_target = (
            backoff_cfg.get("target_indent_pct")
            or backoff_cfg.get("fallback_indent")
            or backoff_cfg.get("indent")
        )
        if raw_target is None:
            raw_target = (state.tp_map.get("0") or {}).get("indent", 1.0)
        target_indent = float(raw_target)

        # Проверка минимального пола стратегии / базы (min_tp_indent)
        min_tp_floor = None
        if strat and getattr(strat, "min_tp_indent", None) is not None:
            min_tp_floor = float(strat.min_tp_indent)
        elif "min_tp_indent" in _BASE_CFG and _BASE_CFG["min_tp_indent"] is not None:
            min_tp_floor = float(_BASE_CFG["min_tp_indent"])
        if min_tp_floor is not None:
            target_indent = max(min_tp_floor, target_indent)

        # Если текущий тейк уже равен или меньше целевого, перестановка не требуется
        cur_indent = getattr(state, "tp_current_indent", None)
        if cur_indent is not None and cur_indent <= target_indent:
            state.tp_backoff_applied = True
            return False

        cur_indent_str = f"{cur_indent:.2f}%" if cur_indent is not None else "N/A"
        logger.warning(
            f"[{symbol}] {side} ⏱️ [TP BACKOFF TRIGGERED] Order active for {elapsed_sec:.0f}s >= {timeout_sec:.0f}s. "
            f"Tightening TP indent: {cur_indent_str} -> {target_indent:.2f}%."
        )

        success = await tp_manager.place_take_profit(
            client=client,
            symbol=symbol,
            side=side,
            current_price=current_price,
            spec_data=spec_data,
            state=state,
            opposite_state=opposite_state,
            override_indent=target_indent,
        )

        if success:
            state.tp_backoff_applied = True
            state.tp_placed_at = now
            state.tp_current_indent = target_indent
            logger.info(
                f"[{symbol}] {side} ✅ [TP BACKOFF] Successfully tightened TP order to {target_indent:.2f}%."
            )
            return True
        else:
            logger.error(
                f"[{symbol}] {side} ❌ [TP BACKOFF] Failed to tighten TP order to {target_indent:.2f}%."
            )
            return False
