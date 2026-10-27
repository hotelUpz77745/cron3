# ==============================================================================
# Path: CORE/SERVICES/notional_validator.py
# Role: Пре-трейд валидатор достаточности капитала (invest_size >= 7.0 USDT min notional)
# ==============================================================================

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from consts import (
    CHECK_MIN_NOTIONAL,
    MIN_ORDER_NOTIONAL_USDT,
    FLOAT_ZERO_EPSILON,
)
from c_log import UnifiedLogger
from CORE.STRATEGY.cross_recycler import CrossSideProfitRecycler

logger = UnifiedLogger("NotionalValidator")


class NotionalCapacityValidator:
    """
    Институциональный валидатор достаточности торгового капитала (invest_size).
    Колаборирует напрямую с паспортом стратегии (raw_meta / CFG/universes/*.json)
    и капиталами из app.active_strategies (active_settings).

    Проверяет ДО начала торговой деятельности, что номинал ЛЮБОГО возможного ордера
    (сетка усреднения, LIFO-пилинг, Cross-Side Recycling) на Binance Futures
    строго превышает минимальный порог (min_order_notional_usdt = 7.0 USDT).

    Если капитал недостаточен:
    1. Формирует подробный отчет со всеми проблемными монетами, сторонами и ступенями.
    2. Отправляет фатальный алерт в Telegram.
    3. Логирует критическую ошибку.
    4. Вызывает ValueError, останавливая запуск бота до вступления в торговлю.
    """

    @classmethod
    def resolve_l0_min_volume(cls, regime_cfg: Optional[Dict[str, Any]], side: str) -> Optional[float]:
        """
        Разрешает минимальный процент объема L0 из паспорта стратегии (first_order_weight_regime).
        Берет минимальный вес среди всех доступных пресетов (bullish, neutral, bearish) для заданной стороны.
        Строгое прямое чтение без скрытых дефолтов (Правило 2 и 3 Самсонова).
        """
        if not regime_cfg or not regime_cfg.get("enabled", True):
            return None
        presets = regime_cfg.get("presets")
        if not presets or not isinstance(presets, dict):
            return None
        side_key = side.upper().strip()
        vols = []
        for p_name, p_val in presets.items():
            if isinstance(p_val, dict) and side_key in p_val:
                raw_v = p_val[side_key]
                if raw_v is not None:
                    vols.append(float(raw_v))
        return min(vols) if vols else None

    @classmethod
    def validate_side_grid(
        cls,
        strat_id: str,
        symbol: str,
        side: str,
        invest_size: float,
        grid_cfg: Dict[str, Any],
        regime_cfg: Optional[Dict[str, Any]],
        min_notional: float = 7.0,
    ) -> List[str]:
        """
        Проверяет, что каждое активное колено сетки и каждый LIFO-пилинг
        формируют ордер номиналом >= min_notional USDT.
        """
        errors: List[str] = []
        if invest_size <= 0:
            errors.append(
                f"• <b>[{strat_id}] {symbol} {side}</b>: invest_size={invest_size:.2f} USDT <= 0.0!"
            )
            return errors

        sorted_keys = sorted(grid_cfg.keys(), key=lambda x: int(x) if str(x).isdigit() else 999)

        for lvl_key in sorted_keys:
            lvl = grid_cfg[lvl_key]
            if not isinstance(lvl, dict):
                continue
            if lvl.get("enabled", True) is False:
                continue

            # 1. Расчет объема колена (в процентах от invest_size)
            raw_vol = lvl.get("volume")
            if raw_vol is None and str(lvl_key) == "0":
                vol_pct = cls.resolve_l0_min_volume(regime_cfg, side)
                if vol_pct is None:
                    errors.append(
                        f"• <b>[{strat_id}] {symbol} {side} L0</b>: в паспорте volume=null и отсутствует "
                        f"пер-пресетная конфигурация first_order_weight_regime.presets!"
                    )
                    continue
            else:
                vol_pct = float(raw_vol or 0.0)

            if vol_pct <= 0:
                continue

            order_notional = invest_size * (vol_pct / 100.0)
            if order_notional < (min_notional - FLOAT_ZERO_EPSILON):
                min_req_invest = min_notional / (vol_pct / 100.0)
                errors.append(
                    f"• <b>[{strat_id}] {symbol} {side} L{lvl_key}</b> (Ордер сетки): "
                    f"объем {vol_pct:.1f}% дает номинал <b>{order_notional:.2f} USDT</b> < {min_notional:.2f} USDT! "
                    f"Требуется invest_size >= <b>{min_req_invest:.2f} USDT</b> (текущий: {invest_size:.2f} USDT)."
                )

            # 2. Расчет LIFO-пилинга (если настроен peel_tp_pct)
            peel_tp = lvl.get("peel_tp_pct")
            if peel_tp is not None and float(peel_tp) > 0:
                raw_ratio = lvl.get("peel_ratio")
                peel_ratio = float(raw_ratio) if raw_ratio is not None else 1.0
                peel_notional = order_notional * peel_ratio

                if peel_notional < (min_notional - FLOAT_ZERO_EPSILON):
                    min_req_invest_peel = min_notional / ((vol_pct / 100.0) * peel_ratio)
                    errors.append(
                        f"• <b>[{strat_id}] {symbol} {side} L{lvl_key}</b> (LIFO-пилинг): "
                        f"срезка {peel_ratio * 100:.0f}% от колена {vol_pct:.1f}% дает номинал <b>{peel_notional:.2f} USDT</b> < {min_notional:.2f} USDT! "
                        f"Требуется invest_size >= <b>{min_req_invest_peel:.2f} USDT</b> (текущий: {invest_size:.2f} USDT)."
                    )

        return errors

    @classmethod
    def validate_cross_recycling(
        cls,
        strat_id: str,
        symbol: str,
        raw_meta: Dict[str, Any],
        active_settings: Dict[str, Any],
        strategy: Any,
        min_notional: float = 7.0,
    ) -> List[str]:
        """
        Проверяет, что параметры Cross-Side Recycling из паспорта стратегии (cross_side_recycling)
        совместно с капиталами из app.active_strategies обеспечивают номинал сжигания >= min_notional USDT
        на пороге min_drawdown_pct.
        """
        errors: List[str] = []
        cross_cfg = raw_meta.get("cross_side_recycling")
        if not cross_cfg or not isinstance(cross_cfg, dict) or not cross_cfg.get("enabled", True):
            return errors

        # Прямое чтение параметров паспорта (Правило 2)
        min_burn_level = CrossSideProfitRecycler.resolve_min_burn_level(cross_cfg)
        max_slice_pct = float(cross_cfg["max_slice_pct"])
        min_drawdown_pct = float(cross_cfg["min_drawdown_pct"])
        regime_cfg = raw_meta.get("first_order_weight_regime")

        for side in ("LONG", "SHORT"):
            opp_side = "SHORT" if side == "LONG" else "LONG"

            # Проверка, включены ли обе стороны в active_settings
            side_enabled = bool((active_settings.get(side) or {}).get("enabled", True))
            opp_enabled = bool((active_settings.get(opp_side) or {}).get("enabled", True))
            if not (side_enabled and opp_enabled):
                continue

            opp_invest = float(strategy.get_invest_size(opp_side, symbol) or 0.0)
            opp_grid = (raw_meta.get(opp_side) or {}).get("grid", {})
            if not opp_grid or opp_invest <= 0:
                continue

            # Расчет кумулятивного объема противоположной стороны до min_burn_level
            sorted_opp_keys = sorted(opp_grid.keys(), key=lambda x: int(x) if str(x).isdigit() else 999)
            cum_vol_pct = 0.0
            for k in sorted_opp_keys:
                if str(k).isdigit() and int(k) <= min_burn_level:
                    lvl_data = opp_grid[k]
                    if isinstance(lvl_data, dict) and lvl_data.get("enabled", True) is not False:
                        r_vol = lvl_data.get("volume")
                        if r_vol is None and str(k) == "0":
                            v_l0 = cls.resolve_l0_min_volume(regime_cfg, opp_side)
                            if v_l0 is not None:
                                cum_vol_pct += v_l0
                        else:
                            cum_vol_pct += float(r_vol or 0.0)

            if cum_vol_pct <= 0:
                continue

            # 1. Проверка емкости срезки страдающей стороны
            slice_capacity = opp_invest * (cum_vol_pct / 100.0) * (max_slice_pct / 100.0)
            if slice_capacity < (min_notional - FLOAT_ZERO_EPSILON):
                req_opp_invest = min_notional / ((cum_vol_pct / 100.0) * (max_slice_pct / 100.0))
                errors.append(
                    f"• <b>[{strat_id}] {symbol}</b> (Cross-Recycling {opp_side} срезка): "
                    f"на уровне L{min_burn_level} (объем {cum_vol_pct:.1f}%) срезка {max_slice_pct:.0f}% составит "
                    f"<b>{slice_capacity:.2f} USDT</b> < {min_notional:.2f} USDT (при min_drawdown {min_drawdown_pct:.1f}%)! "
                    f"Требуется {opp_side} invest_size >= <b>{req_opp_invest:.2f} USDT</b> (текущий: {opp_invest:.2f} USDT)."
                )

        return errors

    @classmethod
    def validate_strategy(
        cls,
        strategy: Any,
        min_notional: float = 7.0,
    ) -> List[str]:
        """
        Валидирует стратегию: сопоставляет паспорт (strategy.raw_meta)
        с капиталами из настроек app.active_strategies (strategy.active_settings).
        """
        errors: List[str] = []
        strat_id = getattr(strategy, "strategy_id", "UNKNOWN")
        raw_meta = getattr(strategy, "raw_meta", {})
        active_settings = getattr(strategy, "active_settings", {})
        symbols = getattr(strategy, "symbols", [])

        if not symbols or not raw_meta:
            return errors

        regime_cfg = raw_meta.get("first_order_weight_regime")

        for sym in symbols:
            for side in ("LONG", "SHORT"):
                # Проверяем активность стороны в active_settings
                side_act = active_settings.get(side) or {}
                if side_act.get("enabled", True) is False:
                    continue

                passport_side = raw_meta.get(side) or {}
                grid_cfg = passport_side.get("grid") or {}
                if not grid_cfg:
                    continue

                invest_size = float(strategy.get_invest_size(side, sym) or 0.0)
                grid_errs = cls.validate_side_grid(
                    strat_id=strat_id,
                    symbol=sym,
                    side=side,
                    invest_size=invest_size,
                    grid_cfg=grid_cfg,
                    regime_cfg=regime_cfg,
                    min_notional=min_notional,
                )
                errors.extend(grid_errs)

            # Проверка рециркуляции для пары сторон символа
            rec_errs = cls.validate_cross_recycling(
                strat_id=strat_id,
                symbol=sym,
                raw_meta=raw_meta,
                active_settings=active_settings,
                strategy=strategy,
                min_notional=min_notional,
            )
            errors.extend(rec_errs)

        return errors

    @classmethod
    def validate_all(
        cls,
        registry: Any,
        min_notional: float = 7.0,
    ) -> List[str]:
        """Валидирует все активные стратегии в реестре."""
        errors: List[str] = []
        if not registry:
            return errors

        active_strats = registry.get_active_strategies() if hasattr(registry, "get_active_strategies") else getattr(registry, "active_strategies", {})
        for strat in active_strats.values():
            s_errs = cls.validate_strategy(strat, min_notional=min_notional)
            errors.extend(s_errs)

        return errors

    @classmethod
    def format_compact_report(cls, errors: List[str], threshold: float) -> str:
        """
        Группирует длинный список нарушений по стратегиям и символам,
        формируя компактный, лаконичный и понятный отчет (по 1 строке на символ).
        """
        import re
        grouped = {}
        for err in errors:
            m = re.search(r'\[(.*?)\]\s+([A-Z0-9]+).*?Требуется.*?invest_size >= <b>([\d\.]+) USDT</b> \(текущий: ([\d\.]+) USDT\)', err)
            if m:
                strat_id, sym, req_str, curr_str = m.groups()
                req_val = float(req_str)
                curr_val = float(curr_str)
                key = (strat_id, sym)
                bottleneck = "Ордер сетки"
                if "LIFO-пилинг" in err:
                    bottleneck = "LIFO-пилинг"
                elif "Cross-Recycling" in err:
                    bottleneck = "Cross-Recycling срезка"
                elif " L" in err:
                    lvl_m = re.search(r'(LONG|SHORT)\s+L(\d+)', err)
                    if lvl_m:
                        bottleneck = f"{lvl_m.group(1)} L{lvl_m.group(2)}"

                if key not in grouped or req_val > grouped[key]["req"]:
                    grouped[key] = {
                        "curr": curr_val,
                        "req": req_val,
                        "bottleneck": bottleneck
                    }

        if not grouped:
            err_lines = "\n".join(errors[:10])
            overflow_note = f"\n...и еще {len(errors) - 10} нарушений." if len(errors) > 10 else ""
            return err_lines + overflow_note

        lines = []
        overall_max_req = 0.0
        for (strat_id, sym), data in sorted(grouped.items()):
            if data["req"] > overall_max_req:
                overall_max_req = data["req"]
            lines.append(
                f"• <b>[{strat_id}] {sym}</b>: текущий invest_size = <b>{data['curr']:.2f} USDT</b> ➔ "
                f"требуется минимум <b>{data['req']:.2f} USDT</b> (узкое место: {data['bottleneck']})"
            )

        remedy = f"\n💡 <b>Решение:</b> Установите <code>default_invest_size</code> >= <b>{overall_max_req:.2f} USDT</b> в <code>CFG/app.json</code> (или выставите <code>\"check_min_notional\": false</code>)."
        return "\n".join(lines) + "\n" + remedy

    @classmethod
    def validate_or_abort(
        cls,
        registry: Any,
        min_notional: Optional[float] = None,
        is_enabled: Optional[bool] = None,
        bot_core: Optional[Any] = None,
    ) -> None:
        """
        Главная точка входа пре-трейд валидации.
        Если валидация включена и обнаружены нарушения:
        - Формирует информативное сообщение;
        - Отправляет фатальный алерт в Telegram;
        - Логирует критическую ошибку;
        - Вызывает ValueError с прерыванием запуска.
        """
        enabled = is_enabled if is_enabled is not None else CHECK_MIN_NOTIONAL
        if not enabled:
            logger.info("[NOTIONAL_VALIDATOR] Пре-трейд проверка минимального номинала отключена (check_min_notional=False).")
            return

        threshold = float(min_notional if min_notional is not None else MIN_ORDER_NOTIONAL_USDT)
        logger.info(f"[NOTIONAL_VALIDATOR] 🛡️ Запуск пре-трейд проверки капитала (порог min_order_notional_usdt = {threshold:.2f} USDT)...")

        errors = cls.validate_all(registry, min_notional=threshold)
        if not errors:
            logger.info(f"[NOTIONAL_VALIDATOR] ✅ Все активные стратегии и символы соответствуют минимальному номиналу {threshold:.2f} USDT!")
            return

        # Формирование компактного структурированного отчета
        err_count = len(errors)
        compact_summary = cls.format_compact_report(errors, threshold=threshold)

        fatal_msg = (
            f"🚨 <b>FATAL CONFIG ERROR: INSUFFICIENT CAPITAL FOR MIN NOTIONAL</b>\n\n"
            f"Капитал (<code>invest_size</code>) не обеспечивает минимальный биржевой номинал ордеров Binance Futures (<b>{threshold:.2f} USDT</b>):\n\n"
            f"{compact_summary}\n\n"
            f"🛑 <b>Запуск бота заблокирован! Торговля остановлена до исправления конфигурации.</b>"
        )

        logger.critical(f"[NOTIONAL_VALIDATOR] 🛑 ФАТАЛЬНАЯ ОШИБКА КОНФИГУРАЦИИ:\n{fatal_msg}")

        # Отправка в Telegram только если передан работающий bot_core и мы не в тестах
        if bot_core and hasattr(bot_core, "notifier") and bot_core.notifier:
            try:
                from TG.tg_adapter import TelegramAdapter
                if not TelegramAdapter._is_testing_or_backtest():
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(bot_core.notifier.send_alert(fatal_msg))
            except Exception:
                pass

        raise ValueError(
            f"INSUFFICIENT CAPITAL FOR MIN NOTIONAL ({threshold:.2f} USDT): "
            f"Обнаружено {err_count} несоответствий в invest_size! Проверьте логи и Telegram."
        )
