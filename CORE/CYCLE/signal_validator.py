# CORE/CYCLE/signal_validator.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.CYCLE.signal_validator
ROLE: Валидация возможности входа в позицию (кулдауны, пауза бота, блокировки)
==============================================================================
"""

from typing import Tuple, Optional, Any
from c_log import UnifiedLogger
from c_utils import Utils

logger = UnifiedLogger("SignalValidator")


class SignalValidator:
    """
    Изолированный валидатор входных условий и блокировок:
    1. Глобальная пауза бота (is_paused).
    2. Процедура закрытия монеты (_closing_symbols, state.is_closing).
    3. Символьный, стратегический и глобальный кулдауны.
    """

    @staticmethod
    def check_entry_block(bot: Any, symbol: str, strategy_id: Optional[str] = None) -> Tuple[bool, str, float]:
        """
        Проверяет блокировку входа для монеты/стратегии.
        Возвращает (is_blocked, reason, remaining_seconds).
        """
        now_ts = Utils.get_current_time(getattr(bot, "current_sim_time", None))
        sym_upper = symbol.upper()

        if getattr(bot, "is_paused", False) is True:
            return True, "Bot is paused", 0.0

        if sym_upper in getattr(bot, "_closing_symbols", set()):
            return True, "Symbol is in closing sequence", 0.0

        sym_blocked = 0.0
        s_blocked_dict = getattr(bot, "symbol_blocked_until", None)
        if isinstance(s_blocked_dict, dict):
            sym_blocked = s_blocked_dict.get(sym_upper, 0.0)

        strat_blocked = 0.0
        if not strategy_id and getattr(bot, "strategy_registry", None):
            try:
                s_obj = bot.strategy_registry.get_strategy_for_symbol(symbol)
                if s_obj:
                    strategy_id = s_obj.strategy_id
            except Exception:
                strategy_id = None

        st_blocked_dict = getattr(bot, "strategy_blocked_until", None)
        if strategy_id and isinstance(st_blocked_dict, dict):
            strat_blocked = st_blocked_dict.get(strategy_id, 0.0)

        global_blocked = getattr(bot, "block_until_timestamp", 0.0)
        global_blocked = global_blocked if isinstance(global_blocked, (int, float)) else 0.0
        sym_blocked = sym_blocked if isinstance(sym_blocked, (int, float)) else 0.0
        strat_blocked = strat_blocked if isinstance(strat_blocked, (int, float)) else 0.0
        effective_until = max(global_blocked, sym_blocked, strat_blocked)

        if effective_until > now_ts:
            rem = effective_until - now_ts
            tfr_desc = "heartbeat"
            if getattr(bot, "strategy_registry", None):
                try:
                    s_obj = bot.strategy_registry.get_strategy(strategy_id) if strategy_id else bot.strategy_registry.get_strategy_for_symbol(symbol)
                    if s_obj and hasattr(s_obj, "signal_heartbeat_tfr"):
                        tfr_desc = s_obj.signal_heartbeat_tfr
                except Exception:
                    pass
            return True, f"Heartbeat ({tfr_desc}) candle window cooldown active ({int(rem)}s remaining)", rem

        if getattr(bot, "entry_blocked", False) is True:
            if global_blocked > 0 and now_ts >= global_blocked:
                bot.entry_blocked = False
            else:
                return True, "Entry blocked flag is set", 0.0

        return False, "", 0.0

    @classmethod
    def can_process_signal(cls, bot: Any, symbol: str, side: str, state: Any) -> Tuple[bool, str]:
        """
        Проверяет базовые пререквизиты перед началом расчета объемов ордера.
        """
        is_blocked, reason, _ = cls.check_entry_block(bot, symbol)
        if is_blocked:
            return False, reason

        if getattr(state, "is_closing", False) is True:
            return False, "position state is marked as is_closing"

        return True, ""
