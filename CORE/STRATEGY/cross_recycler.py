# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\STRATEGY\cross_recycler.py
# Role: cross_recycler.py module

# ==============================================================================
# Path: CORE/STRATEGY/cross_recycler.py
# Role: Модуль рециркуляции прибыли (Cross-Side Profit Recycling) для хедж-позиций
#       Доменная сущность паспорта стратегии (cross_side_recycling)
# ==============================================================================

import asyncio
from typing import Optional, Dict, Any, Tuple
from consts import DEFAULT_TAKER_FEE_RATE
from c_log import UnifiedLogger
from CORE._utils import TradeMath, RiskCalculatingUtils

logger = UnifiedLogger("CrossRecycler")


class CrossSideProfitRecycler:
    """
    Модуль рециркуляции прибыли (Cross-Side Profit Recycling).
    Доменная сущность паспорта стратегии: cross_side_recycling.

    При закрытии прибыльного тейка сильной стороны направляет долю прибыли
    на рыночное сокращение токсичного объема противоположной страдающей стороны.

    Защита от 'переливания из пустого в порожнее':
    1. min_burn_level: рециркуляция работает ТОЛЬКО если страдающая сторона набрала сетку >= min_burn_level (например L2+).
    2. min_drawdown_pct: просадка страдающей стороны должна быть реальной (>= min_drawdown_pct, например >= 3.0%).
    3. max_slice_pct: за один цикл сжигается не более max_slice_pct объема страдающей стороны (например <= 25%).
    4. recycle_ratio: расходуется только часть прибыли (например 50%), гарантируя чистый плюс на балансе.
    """

    @staticmethod
    def resolve_recycle_ratio(recycling_cfg: Dict[str, Any], level: int) -> float:
        """
        Каноническое дискретное определение доли рециркуляции прибыли (burn_ratio).
        Поддерживает:
          - диапазоны: "2-4": 0.50, "2..4": 0.50
          - операторы: ">=5": 0.75, ">4": 0.75, "<=2": 0.30
          - плюс-нотацию: "5+": 0.75
          - прямые номера: "2": 0.50
          - скалярный ключ: 'recycle_ratio'
        """
        if not recycling_cfg or not recycling_cfg.get("enabled", True):
            return 0.0

        fallback = float(recycling_cfg["recycle_ratio"]) if ("recycle_ratio" in recycling_cfg and recycling_cfg["recycle_ratio"] is not None) else None
        tiers = (
            recycling_cfg.get("recycle_ratio_tiers")
            or recycling_cfg.get("recycle_ratio_map")
            or recycling_cfg.get("step_map")
        )
        if not tiers or not isinstance(tiers, dict):
            return fallback if fallback is not None else 0.50

        step_str = str(level)
        if step_str in tiers:
            v = tiers[step_str]
            return float(v.get("burn_ratio", v)) if isinstance(v, dict) else float(v)

        for pattern, val in tiers.items():
            pat = str(pattern).strip()
            v = float(val.get("burn_ratio", val)) if isinstance(val, dict) else float(val)

            if "-" in pat or ".." in pat:
                delim = ".." if ".." in pat else "-"
                parts = pat.split(delim)
                if len(parts) == 2:
                    try:
                        low, high = int(parts[0].strip()), int(parts[1].strip())
                        if low <= level <= high:
                            return v
                    except ValueError:
                        pass
            elif pat.endswith("+"):
                try:
                    thresh = int(pat[:-1].strip())
                    if level >= thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith(">="):
                try:
                    thresh = int(pat[2:].strip())
                    if level >= thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith(">"):
                try:
                    thresh = int(pat[1:].strip())
                    if level > thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith("<="):
                try:
                    thresh = int(pat[2:].strip())
                    if level <= thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith("<"):
                try:
                    thresh = int(pat[1:].strip())
                    if level < thresh:
                        return v
                except ValueError:
                    pass
            elif pat.startswith("=="):
                try:
                    thresh = int(pat[2:].strip())
                    if level == thresh:
                        return v
                except ValueError:
                    pass

        return fallback if fallback is not None else 0.0

    @staticmethod
    def resolve_min_burn_level(recycling_cfg: Optional[Dict[str, Any]]) -> int:
        """Определяет минимальный уровень сетки для начала сжигания объема."""
        if not recycling_cfg or not recycling_cfg.get("enabled", True):
            return 999
        if "min_burn_level" in recycling_cfg and recycling_cfg["min_burn_level"] is not None:
            return int(recycling_cfg["min_burn_level"])

        tiers = recycling_cfg.get("recycle_ratio_tiers") or recycling_cfg.get("recycle_ratio_map")
        if tiers and isinstance(tiers, dict):
            levels = []
            for pat in tiers.keys():
                p = str(pat).strip()
                if "-" in p or ".." in p:
                    delim = ".." if ".." in p else "-"
                    parts = p.split(delim)
                    try:
                        levels.append(int(parts[0].strip()))
                    except ValueError:
                        pass
                elif p.endswith("+"):
                    try:
                        levels.append(int(p[:-1].strip()))
                    except ValueError:
                        pass
                elif p.startswith(">="):
                    try:
                        levels.append(int(p[2:].strip()))
                    except ValueError:
                        pass
                elif p.startswith(">"):
                    try:
                        levels.append(int(p[1:].strip()) + 1)
                    except ValueError:
                        pass
                elif p.isdigit():
                    levels.append(int(p))
            if levels:
                return min(levels)

        return 2

    @classmethod
    def calculate_burn_slice(
        cls,
        net_profit: float,
        opp_side: str,
        opp_avg_price: float,
        opp_vol_qty: float,
        opp_cur_step: int,
        current_price: float,
        recycling_cfg: Optional[Dict[str, Any]],
        taker_fee: Optional[float] = None
    ) -> Tuple[float, float, float]:
        """
        Канонический математический расчет объема и затрат на срезку:
        Возвращает: (burned_qty, spent_profit, burned_notional).
        Строгое прямое чтение параметров без скрытых дефолтов (Правило 2).
        """
        if not recycling_cfg or not recycling_cfg.get("enabled", True) or net_profit <= 0:
            return 0.0, 0.0, 0.0

        min_lvl = cls.resolve_min_burn_level(recycling_cfg)
        if opp_cur_step < min_lvl or opp_vol_qty <= 0.0:
            return 0.0, 0.0, 0.0

        burn_ratio = cls.resolve_recycle_ratio(recycling_cfg, opp_cur_step)
        if burn_ratio <= 0.0:
            return 0.0, 0.0, 0.0

        loss_per_unit = (opp_avg_price - current_price) if opp_side == "LONG" else (current_price - opp_avg_price)
        if loss_per_unit <= 0.0:
            return 0.0, 0.0, 0.0

        # Прямое чтение параметров конфига (Правило 2: никаких скрытых дефолтов!)
        min_dd = float(recycling_cfg["min_drawdown_pct"])
        dd_pct = (loss_per_unit / opp_avg_price) * 100.0 if opp_avg_price > 0 else 0.0
        if dd_pct < min_dd:
            return 0.0, 0.0, 0.0

        fee_rate = taker_fee if taker_fee is not None else DEFAULT_TAKER_FEE_RATE
        loss_with_fee = loss_per_unit + (current_price * fee_rate)
        profit_budget = net_profit * burn_ratio
        max_burn_qty = profit_budget / loss_with_fee

        max_slice_pct = float(recycling_cfg["max_slice_pct"]) / 100.0
        allowed_slice_qty = opp_vol_qty * max_slice_pct

        burn_qty = min(max_burn_qty, allowed_slice_qty, opp_vol_qty)
        if burn_qty <= 0.0:
            return 0.0, 0.0, 0.0

        spent = burn_qty * loss_with_fee
        burned_notional = burn_qty * current_price

        return burn_qty, spent, burned_notional

    @classmethod
    async def maybe_recycle(
        cls,
        universe,
        symbol: str,
        winning_side: str,
        realized_profit: float,
        current_price: float,
        spec_data: dict,
    ) -> Optional[Dict[str, Any]]:
        """
        Проверяет условия и при необходимости исполняет срезку объема страдающей стороны.
        Возвращает словарь с деталями операции или None.
        """
        if realized_profit <= 0 or current_price <= 0:
            return None

        # Разрешение конфигурации для CronUniverse (paper) и BotCore (live)
        recycling_cfg = None
        strat_id = getattr(universe, "universe_id", None)

        if hasattr(universe, "cross_recycling_cfg"):
            raw_cfg = getattr(universe, "cross_recycling_cfg")
            if isinstance(raw_cfg, dict):
                recycling_cfg = raw_cfg

        if not recycling_cfg and hasattr(universe, "runtime_manager") and getattr(universe.runtime_manager, "strategy_registry", None):
            strat = universe.runtime_manager.strategy_registry.get_strategy_for_symbol(symbol)
            if strat:
                recycling_cfg = getattr(strat, "cross_recycling_cfg", None) or getattr(strat, "overrides", {}).get("cross_side_recycling")
                if not strat_id:
                    strat_id = getattr(strat, "strategy_id", None)

        if not recycling_cfg and hasattr(universe, "overrides") and isinstance(universe.overrides, dict):
            recycling_cfg = universe.overrides.get("cross_side_recycling")

        if not recycling_cfg or not isinstance(recycling_cfg, dict) or not recycling_cfg.get("enabled", False):
            return None

        if not strat_id:
            strat_id = "BOT"

        # Строгое соблюдение Протокола: единый источник истины (дискретная карта или скаляр)
        if (
            "recycle_ratio" not in recycling_cfg
            and "recycle_ratio_tiers" not in recycling_cfg
            and "recycle_ratio_map" not in recycling_cfg
        ):
            raise KeyError(
                f"[{strat_id}][{symbol}] Missing recycle ratio definition: "
                f"either 'recycle_ratio_tiers' (карта) or 'recycle_ratio' (скаляр) must be defined"
            )
        min_burn_level = cls.resolve_min_burn_level(recycling_cfg)
        min_drawdown_pct = float(recycling_cfg["min_drawdown_pct"])
        max_slice_pct = float(recycling_cfg["max_slice_pct"])

        losing_side = "SHORT" if winning_side == "LONG" else "LONG"
        fsm_states = getattr(universe, "fsm_states", {}) or {}
        losing_state = fsm_states.get(symbol, {}).get(losing_side)
        if not losing_state or not losing_state.in_position:
            return None

        pos_vol = abs(float(getattr(losing_state, "total_volume", 0.0)))
        if pos_vol <= 1e-8:
            return None

        cur_level = int(RiskCalculatingUtils.get_current_grid_level(losing_state.grid))
        if cur_level < min_burn_level:
            logger.debug(
                f"[{strat_id}][{symbol}] Cross-side recycling skipped: {losing_side} level {cur_level} < min_burn_level {min_burn_level}"
            )
            return None

        if losing_side == "LONG":
            drawdown_pct = ((losing_state.avg_entry_price - current_price) / losing_state.avg_entry_price) * 100.0
            loss_per_unit = losing_state.avg_entry_price - current_price
        else:
            drawdown_pct = ((current_price - losing_state.avg_entry_price) / losing_state.avg_entry_price) * 100.0
            loss_per_unit = current_price - losing_state.avg_entry_price

        if drawdown_pct < min_drawdown_pct or loss_per_unit <= 0:
            logger.debug(
                f"[{strat_id}][{symbol}] Cross-side recycling skipped: {losing_side} DD {drawdown_pct:.1f}% < min_dd {min_drawdown_pct}% or loss_per_unit <= 0"
            )
            return None

        effective_ratio = cls.resolve_recycle_ratio(recycling_cfg, cur_level)
        if effective_ratio <= 0.0:
            return None
        burn_budget = realized_profit * effective_ratio
        raw_qty = burn_budget / loss_per_unit
        max_allowed_qty = pos_vol * (max_slice_pct / 100.0)
        actual_qty = min(raw_qty, max_allowed_qty)

        rounded_qty = TradeMath.round_qty(actual_qty, spec_data, symbol)
        if rounded_qty <= 0 or rounded_qty > pos_vol:
            return None

        order_side = "SELL" if losing_side == "LONG" else "BUY"
        actual_burn_loss = rounded_qty * loss_per_unit

        logger.info(
            f"[{strat_id}][{symbol}] [CROSS-SIDE RECYCLING] "
            f"Winning {winning_side} (+${realized_profit:.2f}) sponsors burn of {rounded_qty} {losing_side} "
            f"(Ratio: {effective_ratio:.2f}, Budget: ${burn_budget:.2f}, Slice loss: -${actual_burn_loss:.2f}, Lvl {cur_level}, DD: -{drawdown_pct:.1f}%)"
        )

        client = None
        if hasattr(type(universe), "get_client_for_symbol") or "get_client_for_symbol" in universe.__dict__:
            try:
                client = universe.get_client_for_symbol(symbol)
            except Exception:
                pass
        if client is None:
            client = getattr(universe, "client", None)

        res = client.make_order(
            symbol=symbol,
            qty=rounded_qty,
            side=order_side,
            position_side=losing_side,
            market_type="MARKET",
        )
        if asyncio.iscoroutine(res):
            res = await res

        if res.success:
            # Синхронизируем фактический объем позиции с биржи/эмулятора без слепой ручной арифметики
            synced = False
            try:
                positions = client.fetch_positions(symbol)
                if asyncio.iscoroutine(positions):
                    positions = await positions
                side_pos = next((p for p in positions if p.get("positionSide") == losing_side), None)
                if side_pos:
                    remote_amt = float(side_pos.get("positionAmt", 0.0))
                    # Сохраняем знак объема для SHORT в live/эмуляторе
                    losing_state.total_volume = remote_amt if losing_state.total_volume < 0 else abs(remote_amt)
                    synced = True
            except Exception as e:
                logger.warning(f"[{strat_id}][{symbol}] Failed to sync position after burn: {e}")

            if not synced:
                new_abs = max(0.0, pos_vol - rounded_qty)
                losing_state.total_volume = -new_abs if losing_state.total_volume < 0 else new_abs

            if abs(losing_state.total_volume) <= 1e-8:
                losing_state.total_volume = 0.0
                losing_state.set_in_position(False)
                losing_state.is_finished = True

            # Уведомляем рантайм о сохранении стейта
            if hasattr(universe, "runtime_manager") and hasattr(universe.runtime_manager, "save_cache"):
                try:
                    res_save = universe.runtime_manager.save_cache(symbol)
                    if asyncio.iscoroutine(res_save):
                        await res_save
                except Exception:
                    pass
            elif hasattr(universe, "mark_dirty") and callable(universe.mark_dirty):
                try:
                    universe.mark_dirty()
                except Exception:
                    pass

            # Если позиция осталась активна, актуализируем лимитный TP ордер под новый уменьшенный объем
            if losing_state.in_position and abs(losing_state.total_volume) > 1e-8:
                if hasattr(universe, "tp_manager") and hasattr(universe.tp_manager, "place_take_profit"):
                    try:
                        opposite_state = fsm_states.get(symbol, {}).get(winning_side)
                        res_tp = universe.tp_manager.place_take_profit(
                            client=client,
                            symbol=symbol,
                            side=losing_side,
                            current_price=current_price,
                            spec_data=spec_data,
                            state=losing_state,
                            opposite_state=opposite_state,
                        )
                        if asyncio.iscoroutine(res_tp):
                            await res_tp
                    except Exception as e:
                        logger.warning(f"[{strat_id}][{symbol}] Failed to refresh TP limit order after burn: {e}")
            else:
                # Позиция полностью сожжена: отменяем старые лимитные ордера этой стороны на бирже
                try:
                    if hasattr(client, "cancel_orders_for_side"):
                        res_c = client.cancel_orders_for_side(symbol, losing_side)
                        if asyncio.iscoroutine(res_c):
                            await res_c
                except Exception as e:
                    logger.warning(f"[{strat_id}][{symbol}] Failed to cancel orders for fully burned side: {e}")

            # Учет сжигания Cross-Side Profit Recycling в бухгалтерии аналитики
            try:
                analytics = getattr(universe, "analytics", None)
                if not analytics and hasattr(universe, "runtime_manager"):
                    analytics = getattr(universe.runtime_manager, "analytics", None)
                if not analytics and hasattr(universe, "bot_core"):
                    analytics = getattr(universe.bot_core, "analytics", None)

                if analytics and hasattr(analytics, "record_cross_recycling"):
                    analytics.record_cross_recycling(
                        strategy_id=strat_id or "default",
                        symbol=symbol,
                        winning_side=winning_side,
                        realized_profit=realized_profit,
                        losing_side=losing_side,
                        losing_level=cur_level,
                        losing_dd_pct=drawdown_pct,
                        burn_qty=rounded_qty,
                        current_price=current_price,
                        actual_burn_loss=actual_burn_loss,
                        burn_budget=burn_budget,
                        rem_pos_qty=abs(losing_state.total_volume),
                    )
            except Exception as e:
                logger.warning(f"[{strat_id}][{symbol}] Failed to record cross-recycling to analytics: {e}")

            return {
                "symbol": symbol,
                "winning_side": winning_side,
                "losing_side": losing_side,
                "burned_qty": rounded_qty,
                "burn_loss": actual_burn_loss,
                "burn_budget": burn_budget,
                "remaining_vol": losing_state.total_volume,
            }
        else:
            logger.warning(
                f"[{strat_id}][{symbol}] Cross-side recycling order failed: {res.error_msg}"
            )
            return None


CrossRecycler = CrossSideProfitRecycler
