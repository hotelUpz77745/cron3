# ============================================================
# FILE: CORE/GRID/fsm_grid_resolver.py
# ROLE: FSM Position State Grid Level Resolution and Reconciliation
# ============================================================

from typing import Any, Dict, Optional

from consts import FLOAT_ZERO_EPSILON, DEFAULT_GRID_VOLUME_TOLERANCE_PCT, _CFG
from c_log import UnifiedLogger
from c_utils import Utils
from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager

logger = UnifiedLogger("FSMGridResolver")

MAX_ENTRY_PRICE_DIVERGENCE: float = 0.40
DEFAULT_VOLUME_TOLERANCE_PCT: float = DEFAULT_GRID_VOLUME_TOLERANCE_PCT
DEFAULT_FIRST_ORDER_VOL_LONG: float = float(FirstOrderRegimeManager.CANONICAL_PRESETS["bullish"]["LONG"])
DEFAULT_FIRST_ORDER_VOL_SHORT: float = float(FirstOrderRegimeManager.CANONICAL_PRESETS["bullish"]["SHORT"])


class FSMGridResolver:
    """
    Универсальный FSM-резолвер уровней сетки позиций.
    Синхронизирует сетку PositionState с реальной биржевой позицией,
    помечает отработанные уровни (is_active=True) по накопленному биржевому объему
    и сбрасывает устаревшие/ложные курки активации при перезапусках.
    """

    @staticmethod
    def get_fsm_resolution_cfg(strategy: Optional[Any] = None) -> dict:
        """
        Получает настройки fsm_level_resolution:
        1. Из конфига стратегии/вселенной (raw_meta), если заданы индивидуально.
        2. Из глобального CFG/app.json ('fsm_level_resolution').
        3. Безопасные дефолты (enabled=True, mode='cumulative_volume_threshold', volume_tolerance_pct=10.0).
        """
        raw_meta = getattr(strategy, "raw_meta", {}) if hasattr(strategy, "raw_meta") and strategy else {}
        cfg = raw_meta.get("fsm_level_resolution")
        if not cfg or not isinstance(cfg, dict):
            cfg = _CFG.get("fsm_level_resolution") if isinstance(_CFG, dict) else None
        if not cfg or not isinstance(cfg, dict):
            cfg = {
                "enabled": True,
                "mode": "cumulative_volume_threshold",
                "volume_tolerance_pct": DEFAULT_VOLUME_TOLERANCE_PCT,
            }
        return cfg

    @staticmethod
    def resolve_first_order_volume(regime_cfg: Any, side: str, default_vol: float) -> float:
        """
        Делегат к каноническому менеджеру FirstOrderRegimeManager (Single Source of Truth).
        Исключает дублирование пресетов и скрытые дефолты (Правило 2, 3 Протокола).
        """
        return FirstOrderRegimeManager.resolve_static_volume(regime_cfg, side, default_vol)

    @classmethod
    def resolve_executed_grid_levels(
        cls,
        strategy: Any,
        symbol: str,
        side: str,
        new_side: dict,
        exist_side: dict
    ) -> None:
        """
        Универсально определяет исполненные уровни сетки по FSM-состоянию позиции и конфигу стратегии/app.json.
        Все правила читаются из 'fsm_level_resolution' (app.json / strategy).
        """
        exist_vol = Utils.safe_float(exist_side.get("total_volume"), 0.0)
        if not (exist_side.get("in_position", False) or abs(exist_vol) > FLOAT_ZERO_EPSILON):
            return

        resolution_cfg = cls.get_fsm_resolution_cfg(strategy)
        if not resolution_cfg.get("enabled", True):
            return

        tolerance_pct = Utils.safe_float(resolution_cfg.get("volume_tolerance_pct"), DEFAULT_VOLUME_TOLERANCE_PCT)
        tolerance_factor = 1.0 - (tolerance_pct / 100.0)

        current_vol = abs(exist_vol)
        entry_price = Utils.safe_float(exist_side.get("avg_entry_price"), 0.0)
        current_notional = current_vol * entry_price
        invest_size = Utils.safe_float(new_side.get("invest_size"), 0.0)
        if invest_size <= 0:
            return

        grid = new_side.get("grid", {})
        exist_grid = exist_side.get("grid", {})
        trailing_state = new_side.get("trailing_grid_state", {})
        armed_dict = trailing_state.get("armed_levels", {}) if isinstance(trailing_state, dict) else {}

        # Сортируем уровни по числовому индексу (0, 1, 2, ...)
        sorted_level_keys = sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else 999)

        cumulative_planned_usdt = 0.0
        for lvl_key in sorted_level_keys:
            lvl_cfg = grid[lvl_key]
            if not isinstance(lvl_cfg, dict):
                continue
            raw_vol = lvl_cfg.get("volume")
            if raw_vol is None and lvl_key == "0":
                regime_cfg = (
                    getattr(strategy, "active_settings", {}).get("first_order_weight_regime")
                    or (getattr(strategy, "overrides", {}) or {}).get("first_order_weight_regime")
                    or (getattr(strategy, "raw_meta", {}) or {}).get("first_order_weight_regime")
                )
                vol_pct = FirstOrderRegimeManager.resolve_static_volume(
                    regime_cfg=regime_cfg,
                    side=side,
                    default_vol=DEFAULT_FIRST_ORDER_VOL_LONG if side.upper() == "LONG" else DEFAULT_FIRST_ORDER_VOL_SHORT
                )
            else:
                vol_pct = Utils.safe_float(raw_vol, 0.0)

            planned_lvl_usdt = invest_size * (vol_pct / 100.0)
            cumulative_planned_usdt += planned_lvl_usdt

            # Уровень считается исполненным, если:
            # 1. Он уже был помечен is_active=True в предыдущем FSM
            # 2. Либо накопленный объем позиции покрывает этот уровень по сетке с учетом допуска
            was_active = bool(exist_grid.get(lvl_key, {}).get("is_active", False)) or bool(lvl_cfg.get("is_active", False))
            covered_by_volume = current_notional >= (cumulative_planned_usdt * tolerance_factor)

            if was_active or covered_by_volume:
                lvl_cfg["is_active"] = True
                lvl_cfg["is_armed"] = False
                lvl_cfg.pop("armed_at_drawdown", None)
                lvl_cfg.pop("armed_timestamp", None)
                if isinstance(armed_dict, dict) and lvl_key in armed_dict:
                    armed_dict.pop(lvl_key, None)
            else:
                # Последующие неисполненные ступени
                lvl_cfg["is_active"] = False
                lvl_cfg["is_armed"] = False

    @classmethod
    def resolve_position_state_grid(
        cls,
        state: Any,
        strategy: Optional[Any] = None,
        invest_size: Optional[float] = None,
        tolerance_pct: Optional[float] = None,
        current_price: Optional[float] = None,
    ) -> bool:
        """
        Универсально синхронизирует сетку PositionState с реальной позицией на бирже.
        Помечает исполненными (is_active=True) все ступени сетки, которые покрыты
        текущим объемом позиции с учетом допустимого отклонения (tolerance_pct).
        Предотвращает повторные и ложные срабатывания усреднений при перезапусках,
        восстановлениях и сбоях стрима.
        Возвращает True, если хотя бы один уровень был обновлен.
        """
        if not getattr(state, "in_position", False) and abs(float(getattr(state, "total_volume", 0.0) or 0.0)) <= FLOAT_ZERO_EPSILON:
            state._last_resolved_vol = None
            state._last_resolved_price = None
            return False

        grid = getattr(state, "grid", None)
        if not grid or not isinstance(grid, dict):
            return False

        resolution_cfg = cls.get_fsm_resolution_cfg(strategy)
        if resolution_cfg.get("enabled", True) is False:
            return False

        tol_pct = tolerance_pct
        if tol_pct is None:
            tol_pct = float(resolution_cfg.get("volume_tolerance_pct", DEFAULT_VOLUME_TOLERANCE_PCT))
        tolerance_factor = 1.0 - (tol_pct / 100.0)

        eff_invest = invest_size
        if eff_invest is None or eff_invest <= 0:
            eff_invest = float(getattr(state, "eff_invest", 0.0) or 0.0)
        if (eff_invest is None or eff_invest <= 0) and strategy:
            if hasattr(strategy, "get_invest_size"):
                try:
                    eff_invest = float(strategy.get_invest_size(getattr(state, "side", ""), getattr(state, "symbol", None)))
                except Exception:
                    pass
            elif hasattr(strategy, "sides") and isinstance(strategy.sides, dict):
                side_cfg = strategy.sides.get(getattr(state, "side", ""), {})
                eff_invest = float(side_cfg.get("invest_size", 0.0) or 0.0)
        if eff_invest and eff_invest > 0 and getattr(state, "eff_invest", 0.0) <= 0:
            state.eff_invest = eff_invest
        if eff_invest is None or eff_invest <= 0:
            return False

        current_vol = abs(float(getattr(state, "total_volume", 0.0) or 0.0))
        entry_price = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
        if entry_price <= 0:
            entry_price = float(getattr(state, "initial_entry_price", 0.0) or 0.0)
        if entry_price <= 0 and current_price and current_price > 0:
            entry_price = float(current_price)

        if entry_price <= 0:
            return False

        sym_name = getattr(state, "symbol", "") or "UNKNOWN"
        side_name = getattr(state, "side", "") or "UNKNOWN"
        updated = False

        # Проверка критического рассинхрона initial_entry_price со средней ценой биржи
        cached_init_p = float(getattr(state, "initial_entry_price", 0.0) or 0.0)
        if cached_init_p > 0 and entry_price > 0:
            divergence = abs(cached_init_p - entry_price) / entry_price
            if divergence > MAX_ENTRY_PRICE_DIVERGENCE:
                logger.warning(
                    f"[POSITION_RESTORER] [{sym_name}] {side_name}: ⚠️ Обнаружен критический рассинхрон initial_entry_price! "
                    f"Кэш: {cached_init_p}, Биржа (avg): {entry_price} (расхождение {divergence * 100:.1f}% > {MAX_ENTRY_PRICE_DIVERGENCE * 100:.0f}%). "
                    f"Синхронизируем initial_entry_price с биржевой ценой!"
                )
                state.initial_entry_price = entry_price
                for l_k, l_v in grid.items():
                    if isinstance(l_v, dict) and not l_v.get("is_active"):
                        l_v["price"] = None
                        l_v["activation_price"] = None
                updated = True

        current_notional = current_vol * entry_price
        trailing_state = getattr(state, "trailing_grid_state", {})
        armed_dict = trailing_state.get("armed_levels", {}) if isinstance(trailing_state, dict) else {}

        # Расчет текущей просадки для санитизации ложно взведенных курков неисполненных ступеней
        cur_dd_pct = 0.0
        check_p = float(current_price) if (current_price and float(current_price) > 0) else entry_price
        base_p = float(getattr(state, "initial_entry_price", 0.0) or entry_price)
        if base_p > 0 and check_p > 0:
            if side_name.upper() == "LONG":
                cur_dd_pct = max(0.0, (base_p - check_p) / base_p * 100.0)
            else:
                cur_dd_pct = max(0.0, (check_p - base_p) / base_p * 100.0)

        sorted_keys = sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else 999)
        cumulative_planned_usdt = 0.0

        for lvl_key in sorted_keys:
            lvl_cfg = grid[lvl_key]
            if not isinstance(lvl_cfg, dict):
                continue
            raw_vol = lvl_cfg.get("volume")
            if raw_vol is None and lvl_key == "0":
                regime_cfg = (
                    getattr(strategy, "active_settings", {}).get("first_order_weight_regime")
                    or getattr(strategy, "raw_meta", {}).get("first_order_weight_regime")
                ) if strategy else None
                vol_pct = FirstOrderRegimeManager.resolve_static_volume(
                    regime_cfg=regime_cfg,
                    side=getattr(state, "side", "LONG"),
                    default_vol=DEFAULT_FIRST_ORDER_VOL_LONG if getattr(state, "side", "").upper() == "LONG" else DEFAULT_FIRST_ORDER_VOL_SHORT
                )
            else:
                vol_pct = Utils.safe_float(raw_vol, 0.0)
            planned_lvl_usdt = eff_invest * (vol_pct / 100.0)
            cumulative_planned_usdt += planned_lvl_usdt

            was_active = bool(lvl_cfg.get("is_active", False))
            covered_by_volume = current_notional >= (cumulative_planned_usdt * tolerance_factor)

            if was_active or covered_by_volume:
                if not was_active:
                    lvl_cfg["is_active"] = True
                    logger.info(
                        f"[POSITION_RESTORER] [{sym_name}] {side_name}: Уровень {lvl_key} отмечен как ИСПОЛНЕННЫЙ (is_active=True). "
                        f"Покрыт биржевым объемом: notional {current_notional:.2f} >= planned {cumulative_planned_usdt * tolerance_factor:.2f} USDT"
                    )
                    updated = True
                lvl_cfg["is_armed"] = False
                lvl_cfg.pop("armed_at_drawdown", None)
                lvl_cfg.pop("armed_timestamp", None)
                if isinstance(armed_dict, dict) and lvl_key in armed_dict:
                    armed_dict.pop(lvl_key, None)
                    updated = True
            else:
                # Для неисполненных уровней: если текущая просадка МЕНЬШЕ курка активации,
                # сбрасываем залипшие старые флаги взведения (только если передана реальная рыночная цена)
                raw_act = lvl_cfg.get("activation")
                if raw_act is not None and current_price is not None and float(current_price) > 0:
                    act_thresh = abs(float(raw_act))
                    if cur_dd_pct < act_thresh:
                        if lvl_cfg.get("is_armed") or (isinstance(armed_dict, dict) and lvl_key in armed_dict):
                            lvl_cfg["is_armed"] = False
                            lvl_cfg.pop("armed_at_drawdown", None)
                            lvl_cfg.pop("armed_timestamp", None)
                            if isinstance(armed_dict, dict):
                                armed_dict.pop(lvl_key, None)
                            logger.info(
                                f"[POSITION_RESTORER] [{sym_name}] {side_name}: Сброшен ложный/устаревший курок уровня {lvl_key} "
                                f"(Текущая просадка {cur_dd_pct:.2f}% < порога {act_thresh:.2f}%)"
                            )
                            updated = True

        active_lvls = [k for k in sorted_keys if isinstance(grid.get(k), dict) and grid[k].get("is_active")]
        unexecuted_lvls = [k for k in sorted_keys if isinstance(grid.get(k), dict) and not grid[k].get("is_active")]
        if updated:
            logger.info(
                f"[POSITION_RESTORER] [{sym_name}] {side_name}: Синхронизация сетки завершена! "
                f"Исполнено: {active_lvls}, В ожидании: {unexecuted_lvls}, Notional: {current_notional:.2f} USDT, Base Price: {base_p}"
            )
        else:
            logger.debug(
                f"[POSITION_RESTORER] [{sym_name}] {side_name}: Синхронизация сетки (без изменений). "
                f"Исполнено: {active_lvls}, В ожидании: {unexecuted_lvls}"
            )

        state._last_resolved_vol = state.total_volume
        state._last_resolved_price = state.avg_entry_price
        return updated
