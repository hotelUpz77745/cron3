# CORE/SERVICES/per_coin_stop_loss.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.per_coin_stop_loss
ROLE: Сервис индивидуального аварийного стоп-лосса монет (Per-Coin Stop-Loss)
      с рыночной ликвидацией, отменой сетки, полной выгрузкой монеты из пула
      (app.json, рантайм, диск) и Telegram-уведомлением оператора.
==============================================================================
"""

import asyncio
import time
from typing import Dict, Any, Optional, List, Tuple
from c_log import UnifiedLogger
from consts import FLOAT_ZERO_EPSILON

logger = UnifiedLogger("PerCoinStopLoss")


class PerCoinStopLossManager:
    """
    Институциональный сервис мониторинга индивидуального стоп-лосса монет.
    
    Принцип работы:
    1. Рассчитывает неблагоприятное отклонение цены (adverse move %) СТРОГО от начальной
       точки входа L0 (initial_entry_price):
       - Для LONG:  (entry0 - cur_price) / entry0 * 100.0 >= max_adverse_pct
       - Для SHORT: (cur_price - entry0) / entry0 * 100.0 >= max_adverse_pct
    2. При превышении порога:
       - Мгновенная ликвидация позиции по рынку (MARKET).
       - Отмена всех отложенных ордеров сетки по символу.
       - Сброс FSM-состояния и AvgManager.
       - Полное исключение монеты из обоймы бота (delete_symbol):
         удаление из app.json, удаление из памяти, закрытие стримов и удаление
         файлов кэша с жесткого диска.
       - Отправка детального алерта оператору в Telegram.
    """

    @staticmethod
    def normalize_threshold(raw_val: Any) -> Optional[float]:
        """
        Нормализует значение порога стоп-лосса.
        Поддерживает:
        - None / False / 0 -> None (отключен)
        - Числовое значение (положительное или отрицательное) -> abs(float(val))
        """
        if raw_val is None or raw_val is False:
            return None
        try:
            val = abs(float(raw_val))
            return val if val > 0.0 else None
        except (ValueError, TypeError):
            return None

    @classmethod
    def extract_config(cls, strat_cfg: Any) -> Tuple[Optional[float], Optional[float], bool]:
        """
        Извлекает и нормализует параметры per_coin_stop_loss из конфигурации стратегии.
        Возвращает: (max_adverse_short_pct, max_adverse_long_pct, notify_telegram).
        """
        if not strat_cfg:
            return None, None, True

        cfg_dict: Dict[str, Any] = {}
        # Проверяем возможные места хранения в StrategyConfig / raw_meta / overrides
        if hasattr(strat_cfg, "per_coin_stop_loss_cfg") and isinstance(strat_cfg.per_coin_stop_loss_cfg, dict):
            cfg_dict = strat_cfg.per_coin_stop_loss_cfg
        elif hasattr(strat_cfg, "overrides") and isinstance(strat_cfg.overrides, dict) and "per_coin_stop_loss" in strat_cfg.overrides:
            cfg_dict = strat_cfg.overrides["per_coin_stop_loss"]
        elif hasattr(strat_cfg, "raw_meta") and isinstance(strat_cfg.raw_meta, dict) and "per_coin_stop_loss" in strat_cfg.raw_meta:
            cfg_dict = strat_cfg.raw_meta["per_coin_stop_loss"]
        elif isinstance(strat_cfg, dict):
            cfg_dict = strat_cfg.get("per_coin_stop_loss") or strat_cfg

        if not isinstance(cfg_dict, dict):
            return None, None, True

        raw_legacy = cfg_dict.get("max_adverse_from_entry_pct")
        raw_short = cfg_dict.get("max_adverse_short_pct", raw_legacy)
        raw_long = cfg_dict.get("max_adverse_long_pct")

        short_thresh = cls.normalize_threshold(raw_short)
        long_thresh = cls.normalize_threshold(raw_long)

        # Fallback для legacy max_adverse_from_entry_pct:
        # Для LONG он применим только если явно < 100% (так как падение более 100% невозможно)
        if long_thresh is None and raw_legacy is not None:
            norm_leg = cls.normalize_threshold(raw_legacy)
            if norm_leg is not None and norm_leg < 100.0:
                long_thresh = norm_leg

        notify = bool(cfg_dict.get("notify_telegram", True))
        return short_thresh, long_thresh, notify

    @classmethod
    def evaluate_symbol(
        cls,
        symbol: str,
        fsm_state: Any,
        cur_price: float,
        threshold_pct: float
    ) -> Tuple[bool, float, float]:
        """
        Оценивает неблагоприятное движение цены относительно начальной точки входа L0.
        
        Возвращает:
            (is_triggered, adverse_pct, entry0_price)
        """
        if not fsm_state or not getattr(fsm_state, "in_position", False):
            return False, 0.0, 0.0

        if cur_price <= 0.0:
            return False, 0.0, 0.0

        # Точка входа L0: initial_entry_price, либо fallback на avg_entry_price
        entry0 = float(getattr(fsm_state, "initial_entry_price", 0.0) or 0.0)
        if entry0 <= 0.0:
            entry0 = float(getattr(fsm_state, "avg_entry_price", 0.0) or 0.0)
        if entry0 <= 0.0:
            return False, 0.0, 0.0

        side = (getattr(fsm_state, "side", "") or "").upper()
        if side == "LONG":
            adverse_pct = (entry0 - cur_price) / entry0 * 100.0
        elif side == "SHORT":
            adverse_pct = (cur_price - entry0) / entry0 * 100.0
        else:
            return False, 0.0, entry0

        is_triggered = adverse_pct >= threshold_pct
        return is_triggered, adverse_pct, entry0

    @classmethod
    async def execute_stop_loss(
        cls,
        bot_core: Any,
        symbol: str,
        side: str,
        adverse_pct: float,
        threshold_pct: float,
        entry0_price: float,
        cur_price: float,
        strat_id: str,
        strat_name: str,
        notify_telegram: bool = True
    ) -> bool:
        """
        Выполняет полную ликвидацию монеты и ее бесповоротное удаление из пула бота.
        """
        sym = symbol.upper()
        logger.critical(
            f"🛑 [PER_COIN_SL] [{sym}] TRIGGERED! Adverse move from L0: +{adverse_pct:.2f}% "
            f"(threshold: {threshold_pct:.1f}%). Entry0: {entry0_price}, Current: {cur_price}. "
            f"Liquidating position and purging coin from universe!"
        )

        sym_client = bot_core.get_client_for_symbol(sym) if hasattr(bot_core, "get_client_for_symbol") else getattr(bot_core, "client", None)

        # 1. Отмена всех активных лимитных ордеров
        try:
            if hasattr(bot_core, "_get_symbol_lock"):
                async with bot_core._get_symbol_lock(sym):
                    await sym_client.cancel_all_orders(sym)
            else:
                await sym_client.cancel_all_orders(sym)
            logger.info(f"[PER_COIN_SL][{sym}] All limit orders canceled.")
        except Exception as e:
            logger.error(f"[PER_COIN_SL][{sym}] Error canceling orders: {e}")

        # 2. Мгновенная ликвидация открытых позиций по рынку
        closed_amt = 0.0
        try:
            positions = await sym_client.fetch_positions()
            for p in positions:
                p_sym = (p.get("symbol") or "").upper()
                if p_sym != sym:
                    continue
                amt = float(p.get("positionAmt", 0.0))
                if abs(amt) > FLOAT_ZERO_EPSILON:
                    pos_side = p.get("positionSide")
                    order_side = "SELL" if pos_side == "LONG" else "BUY"
                    logger.critical(f"[PER_COIN_SL][{sym}] Market closing {pos_side} pos: Qty {abs(amt)}")
                    if hasattr(bot_core, "_get_symbol_lock"):
                        async with bot_core._get_symbol_lock(sym):
                            await sym_client.make_order(
                                symbol=sym,
                                qty=abs(amt),
                                side=order_side,
                                position_side=pos_side,
                                market_type="MARKET"
                            )
                    else:
                        await sym_client.make_order(
                            symbol=sym,
                            qty=abs(amt),
                            side=order_side,
                            position_side=pos_side,
                            market_type="MARKET"
                        )
                    closed_amt += abs(amt)
        except Exception as e:
            logger.error(f"[PER_COIN_SL][{sym}] Error liquidating position: {e}")

        # 3. Сброс FSM стейтов и AvgManager для снятия блокировки delete_symbol
        if hasattr(bot_core, "fsm_states") and sym in bot_core.fsm_states:
            for s_side in ("LONG", "SHORT"):
                if s_side in bot_core.fsm_states[sym]:
                    st = bot_core.fsm_states[sym][s_side]
                    if hasattr(bot_core, "runtime_manager") and bot_core.runtime_manager:
                        bot_core.runtime_manager.reset_state_to_default(sym, s_side, st)
                    else:
                        st.in_position = False
                        st.total_volume = 0.0
                        st.avg_entry_price = 0.0
                        st.initial_entry_price = 0.0
                    if hasattr(bot_core, "avg_manager") and bot_core.avg_manager:
                        bot_core.avg_manager.reset(sym, s_side)

        # 4. Удаление монеты из обоймы бота (app.json, дисковые кэши, FSM, стримы)
        deleted = False
        try:
            if hasattr(bot_core, "delete_symbol"):
                deleted = await bot_core.delete_symbol(sym)
            elif hasattr(bot_core, "symbol_lifecycle_manager") and bot_core.symbol_lifecycle_manager:
                deleted = await bot_core.symbol_lifecycle_manager.delete_symbol(sym)
            logger.info(f"[PER_COIN_SL][{sym}] Symbol purged from BotCore: {deleted}")
        except Exception as e:
            logger.error(f"[PER_COIN_SL][{sym}] Error purging symbol: {e}")

        # 5. Оперативное Telegram-уведомление оператору
        if notify_telegram:
            msg = (
                f"🚨 <b>[STOP-LOSS PER COIN] Монета {sym} ликвидирована и удалена из пула!</b>\n\n"
                f"• Купол: <b>{strat_name}</b> (<code>{strat_id}</code>)\n"
                f"• Сторона: <b>{side}</b>\n"
                f"• Точка входа L0: <code>{entry0_price:.6f}</code>\n"
                f"• Цена ликвидации: <code>{cur_price:.6f}</code>\n"
                f"• Неблагоприятное отклонение: <b>+{adverse_pct:.2f}%</b> (порог: <b>{threshold_pct:.1f}%</b>)\n"
                f"• Исполнение: <b>Рыночная ликвидация MARKET</b>, все лимитки сняты.\n"
                f"• Пул: <b>Символ полностью удален</b> из app.json, кэш очищен с диска, квота освобождена."
            )
            try:
                if hasattr(bot_core, "notifier") and bot_core.notifier and hasattr(bot_core.notifier, "send_alert"):
                    await bot_core.notifier.send_alert(msg)
            except Exception as ne:
                logger.error(f"[PER_COIN_SL][{sym}] Error sending Telegram alert: {ne}")

        return True

    @classmethod
    async def check_per_coin_stop_loss(cls, bot_core: Any) -> List[str]:
        """
        Основной цикл мониторинга: проверяет все активные стратегии и монеты.
        Возвращает список ликвидированных символов.
        """
        if not bot_core:
            return []

        active_strats = {}
        if hasattr(bot_core, "strategy_registry") and bot_core.strategy_registry:
            active_strats = bot_core.strategy_registry.active_strategies

        liquidated_symbols = []

        for strat_id, strat_cfg in active_strats.items():
            short_thresh, long_thresh, notify = cls.extract_config(strat_cfg)
            if short_thresh is None and long_thresh is None:
                continue

            symbols = getattr(strat_cfg, "symbols", []) or []
            strat_name = getattr(strat_cfg, "name", strat_id)

            for sym in list(symbols):
                sym_u = sym.upper()
                cur_price_tuple = bot_core.prices.get(sym_u) if hasattr(bot_core, "prices") else None
                cur_price = cur_price_tuple[0] if (cur_price_tuple and isinstance(cur_price_tuple, tuple)) else 0.0
                if cur_price <= 0.0:
                    continue

                if not hasattr(bot_core, "fsm_states") or sym_u not in bot_core.fsm_states:
                    continue

                for side in ("LONG", "SHORT"):
                    fsm_state = bot_core.fsm_states[sym_u].get(side)
                    if not fsm_state or not getattr(fsm_state, "in_position", False):
                        continue

                    side_threshold = long_thresh if side == "LONG" else short_thresh
                    if side_threshold is None or side_threshold <= 0.0:
                        continue

                    is_triggered, adv_pct, entry0 = cls.evaluate_symbol(
                        symbol=sym_u,
                        fsm_state=fsm_state,
                        cur_price=cur_price,
                        threshold_pct=side_threshold
                    )

                    if is_triggered:
                        success = await cls.execute_stop_loss(
                            bot_core=bot_core,
                            symbol=sym_u,
                            side=side,
                            adverse_pct=adv_pct,
                            threshold_pct=side_threshold,
                            entry0_price=entry0,
                            cur_price=cur_price,
                            strat_id=strat_id,
                            strat_name=strat_name,
                            notify_telegram=notify
                        )
                        if success:
                            liquidated_symbols.append(sym_u)
                        break  # Прерываем цикл сторон для данного символа

        return liquidated_symbols

