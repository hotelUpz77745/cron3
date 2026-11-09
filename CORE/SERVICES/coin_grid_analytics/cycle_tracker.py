# ==============================================================================
# Path: CORE/SERVICES/coin_grid_analytics/cycle_tracker.py
# Role: Ядро трекинга активных и завершенных сеточных циклов, расчета просадок
#       и регистрации исполнений ордеров (CoinGridRiskAnalytics).
# ==============================================================================

from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple, Set

from .grid_schema import RiskThresholds, load_level_metadata, create_empty_coin_record
from .summary_metrics import build_summary
from .merge_service import merge_analytics
from .table_formatters import (
    format_grid_distribution_table,
    format_per_coin_table,
    format_worst_drawdowns_table,
)


class CoinGridRiskAnalytics:
    """
    Институциональный сервис сбора, аналитики и визуализации поведения сеточных ступеней,
    глубины усреднений и риска неблагоприятного движения цены (Adverse Price Excursion) per-coin.
    """

    def __init__(self, strategy_id: str = ""):
        self.strategy_id = strategy_id
        self._next_cycle_id: int = 1

        # Открытые в данный момент циклы: (symbol, side) -> cycle_data
        self.active_cycles: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.open_symbols: Set[str] = set()

        # Завершенные циклы
        self.completed_cycles: List[Dict[str, Any]] = []

        # Счетчики ордеров по уровням
        self.level_fills_count: Dict[str, int] = defaultdict(int)
        self.level_fills_notional: Dict[str, float] = defaultdict(float)

        # Распределение максимальной глубины циклов: "L0", "L1", ... -> count
        self.cycle_depth_counts: Dict[str, int] = defaultdict(int)

        # Per-coin накопительные данные
        self.per_coin_stats: Dict[str, Dict[str, Any]] = {}

        # Описания ступеней сетки (L0, L1, ...) с динамической загрузкой из конфигурации стратегии
        self.level_metadata: Dict[str, Dict[str, Any]] = load_level_metadata(strategy_id)

    # =========================================================================
    # EVENT RECORDING
    # =========================================================================

    def record_fill(
        self,
        symbol: str,
        side: str,
        fill_price: float,
        qty: float,
        is_opening: bool,
        pos_qty_after: float,
        pos_entry_after: float,
        fee: float = 0.0,
        level_idx: Optional[int] = None,
        state: Optional[Any] = None,
        timestamp: float = 0.0,
        close_reason: str = "TP",
    ) -> None:
        """Регистрирует исполнение ордера (вход, усреднение, частичный срез или полное закрытие)."""
        sym_u = symbol.upper().strip()
        side_u = side.upper().strip()
        key = (sym_u, side_u)
        notional = float(qty) * float(fill_price)

        if is_opening:
            # 1. Позиция открывается или добирается (L0 или L1..L8)
            if key not in self.active_cycles:
                # Начало нового торгового цикла (L0)
                cid = self._next_cycle_id
                self._next_cycle_id += 1
                lvl = 0 if level_idx is None else int(level_idx)
                lvl_key = f"L{lvl}"

                cycle = {
                    "cycle_id": cid,
                    "symbol": sym_u,
                    "side": side_u,
                    "start_time": timestamp,
                    "entry0_price": float(fill_price),
                    "avg_entry_price": float(pos_entry_after if pos_entry_after > 0 else fill_price),
                    "current_qty": float(pos_qty_after if pos_qty_after > 0 else qty),
                    "max_level_reached": lvl,
                    "fills_count": 1,
                    "notional_invested": notional,
                    "max_adverse_from_entry0_pct": 0.0,
                    "max_adverse_from_avg_pct": 0.0,
                    "max_floating_dd_usdt": 0.0,
                    "realized_pnl": 0.0,
                    "fees_paid": float(fee),
                }
                self.active_cycles[key] = cycle
                self.open_symbols.add(sym_u)

                self.level_fills_count[lvl_key] += 1
                self.level_fills_notional[lvl_key] += notional
            else:
                # Усредняющий добор в существующий цикл (L1..L8)
                cycle = self.active_cycles[key]
                if level_idx is not None:
                    lvl = int(level_idx)
                elif state and getattr(state, "pending_avg", False) and hasattr(state, "grid"):
                    from CORE._utils import RiskCalculatingUtils
                    active_lvl_str = RiskCalculatingUtils.get_current_grid_level(state.grid)
                    lvl = int(active_lvl_str) if str(active_lvl_str).isdigit() else (cycle["max_level_reached"] + 1)
                else:
                    lvl = cycle["max_level_reached"] + 1

                lvl_key = f"L{lvl}"
                cycle["max_level_reached"] = max(cycle["max_level_reached"], lvl)
                cycle["avg_entry_price"] = float(pos_entry_after if pos_entry_after > 0 else fill_price)
                cycle["current_qty"] = float(pos_qty_after if pos_qty_after > 0 else (cycle["current_qty"] + qty))
                cycle["fills_count"] += 1
                cycle["notional_invested"] += notional
                cycle["fees_paid"] += float(fee)

                self.level_fills_count[lvl_key] += 1
                self.level_fills_notional[lvl_key] += notional
        else:
            # 2. Позиция сокращается или полностью закрывается (Take-Profit, AutoCloser, Peeling, Stop-Loss)
            if key in self.active_cycles:
                cycle = self.active_cycles[key]
                cycle["fees_paid"] += float(fee)
                close_qty = min(cycle["current_qty"], float(qty))

                # Реализованный PnL закрываемого объема
                if side_u == "LONG":
                    realized = (float(fill_price) - cycle["avg_entry_price"]) * close_qty
                else:
                    realized = (cycle["avg_entry_price"] - float(fill_price)) * close_qty

                cycle["realized_pnl"] += realized
                cycle["current_qty"] = max(0.0, cycle["current_qty"] - close_qty)

                # Если позиция полностью закрыта (или остаток микроскопический)
                if pos_qty_after <= RiskThresholds.EPSILON or cycle["current_qty"] <= RiskThresholds.EPSILON:
                    self._finalize_cycle(key, fill_price=float(fill_price), timestamp=timestamp, close_reason=close_reason)

    def update_price(
        self,
        symbol: str,
        price: float,
        positions: Optional[Dict[Tuple[str, str], Any]] = None,
        timestamp: float = 0.0,
    ) -> None:
        """
        Высокоскоростной расчет текущей неблагоприятной экскурсии (adverse excursion)
        и плавающей просадки для всех активных позиций инструмента.
        """
        sym_u = symbol.upper().strip()
        if sym_u not in self.open_symbols:
            return

        price_f = float(price)
        for side in ("LONG", "SHORT"):
            key = (sym_u, side)
            if key in self.active_cycles:
                cycle = self.active_cycles[key]
                e0 = cycle["entry0_price"]
                avg_p = cycle["avg_entry_price"]
                cur_qty = cycle["current_qty"]

                if cur_qty <= RiskThresholds.EPSILON or avg_p <= 0 or e0 <= 0:
                    continue

                if side == "LONG":
                    # Для лонга неблагоприятное движение — падение цены ниже входа
                    adv_e0_pct = max(0.0, (e0 - price_f) / e0 * 100.0)
                    adv_avg_pct = max(0.0, (avg_p - price_f) / avg_p * 100.0)
                    floating_dd = max(0.0, (avg_p - price_f) * cur_qty)
                else:
                    # Для шорта неблагоприятное движение — взлет цены выше входа
                    adv_e0_pct = max(0.0, (price_f - e0) / e0 * 100.0)
                    adv_avg_pct = max(0.0, (price_f - avg_p) / avg_p * 100.0)
                    floating_dd = max(0.0, (price_f - avg_p) * cur_qty)

                if adv_e0_pct > cycle["max_adverse_from_entry0_pct"]:
                    cycle["max_adverse_from_entry0_pct"] = adv_e0_pct
                if adv_avg_pct > cycle["max_adverse_from_avg_pct"]:
                    cycle["max_adverse_from_avg_pct"] = adv_avg_pct
                if floating_dd > cycle["max_floating_dd_usdt"]:
                    cycle["max_floating_dd_usdt"] = floating_dd

    def _finalize_cycle(
        self,
        key: Tuple[str, str],
        fill_price: float,
        timestamp: float,
        close_reason: str = "TP",
    ) -> None:
        """Переводит активный цикл в категорию завершенных и обновляет накопительные метрики."""
        cycle = self.active_cycles.pop(key, None)
        if not cycle:
            return

        sym_u, side_u = key
        # Проверяем, остались ли другие стороны открытыми по этой монете
        if not any(k[0] == sym_u for k in self.active_cycles.keys()):
            self.open_symbols.discard(sym_u)

        start_t = cycle.get("start_time", 0.0)
        end_t = timestamp if timestamp > 0 else start_t
        duration_h = max(0.0, (end_t - start_t) / RiskThresholds.SECONDS_PER_HOUR) if start_t > 0 else 0.0

        max_lvl = int(cycle["max_level_reached"])
        lvl_key = f"L{max_lvl}"
        self.cycle_depth_counts[lvl_key] += 1

        net_pnl = cycle["realized_pnl"] - cycle["fees_paid"]
        is_win = net_pnl > 0.0

        finished_record = {
            "cycle_id": cycle["cycle_id"],
            "symbol": sym_u,
            "side": side_u,
            "start_time": start_t,
            "end_time": end_t,
            "duration_hours": round(duration_h, 2),
            "max_level_reached": max_lvl,
            "fills_count": cycle["fills_count"],
            "entry0_price": round(cycle["entry0_price"], 6),
            "close_price": round(fill_price, 6),
            "max_adverse_from_entry0_pct": round(cycle["max_adverse_from_entry0_pct"], 2),
            "max_adverse_from_avg_pct": round(cycle["max_adverse_from_avg_pct"], 2),
            "max_floating_dd_usdt": round(cycle["max_floating_dd_usdt"], 2),
            "realized_pnl": round(cycle["realized_pnl"], 2),
            "fees_paid": round(cycle["fees_paid"], 2),
            "net_pnl": round(net_pnl, 2),
            "close_reason": close_reason,
            "is_win": is_win,
        }
        self.completed_cycles.append(finished_record)

        # Обновление per-coin статистики
        self._update_per_coin_record(finished_record)

    def _update_per_coin_record(self, record: Dict[str, Any]) -> None:
        sym = record["symbol"]
        if sym not in self.per_coin_stats:
            self.per_coin_stats[sym] = create_empty_coin_record(sym)

        s = self.per_coin_stats[sym]
        s["total_cycles"] += 1
        if record["is_win"]:
            s["winning_cycles"] += 1
        else:
            s["losing_cycles"] += 1

        s["realized_pnl"] = round(s["realized_pnl"] + record["realized_pnl"], 2)
        s["fees_paid"] = round(s["fees_paid"] + record["fees_paid"], 2)
        s["net_pnl"] = round(s["net_pnl"] + record["net_pnl"], 2)
        s["max_level_ever"] = max(s["max_level_ever"], record["max_level_reached"])
        s["level_depths_sum"] += record["max_level_reached"]
        s["total_duration_hours"] += record["duration_hours"]

        if record["max_adverse_from_entry0_pct"] > s["max_adverse_from_entry0_pct"]:
            s["max_adverse_from_entry0_pct"] = record["max_adverse_from_entry0_pct"]
        if record["max_adverse_from_avg_pct"] > s["max_adverse_from_avg_pct"]:
            s["max_adverse_from_avg_pct"] = record["max_adverse_from_avg_pct"]
        if record["max_floating_dd_usdt"] > s["max_floating_dd_usdt"]:
            s["max_floating_dd_usdt"] = record["max_floating_dd_usdt"]

        # Проверка критических порогов отступа
        if record["max_adverse_from_entry0_pct"] >= RiskThresholds.L8_THRESHOLD_PCT:
            s["exceeded_l8_count"] += 1
        if record["max_adverse_from_entry0_pct"] >= RiskThresholds.SL_120_THRESHOLD_PCT:
            s["would_hit_sl_120_count"] += 1
        if record["max_adverse_from_entry0_pct"] >= RiskThresholds.SL_130_THRESHOLD_PCT:
            s["would_hit_sl_130_count"] += 1

    def force_close_all_cycles(
        self,
        close_reason: str = "ROTATION",
        final_prices: Optional[Dict[str, float]] = None,
        timestamp: float = 0.0,
    ) -> None:
        """
        Принудительно закрывает все оставшиеся активные циклы
        (например, в конце декады при ротации или экстренном сбросе в 100% кэш).
        """
        for key in list(self.active_cycles.keys()):
            sym, side = key
            p = 0.0
            if final_prices and sym in final_prices:
                p = final_prices[sym]
            elif key in self.active_cycles:
                p = self.active_cycles[key]["avg_entry_price"]
            self._finalize_cycle(key, fill_price=p, timestamp=timestamp, close_reason=close_reason)

    def force_close_symbol_cycle(
        self,
        symbol: str,
        close_reason: str = "STOP_LOSS",
        final_price: float = 0.0,
        timestamp: float = 0.0,
    ) -> None:
        """
        Принудительно закрывает активный цикл конкретного символа
        (например, при срабатывании Per-Coin Stop-Loss).
        """
        sym_u = symbol.upper()
        for key in list(self.active_cycles.keys()):
            s, side = key
            if s.upper() == sym_u:
                p = final_price if final_price > 0.0 else self.active_cycles[key]["avg_entry_price"]
                self._finalize_cycle(key, fill_price=p, timestamp=timestamp, close_reason=close_reason)

    # =========================================================================
    # SUMMARY, MERGE & TABLE DELEGATION
    # =========================================================================

    def get_summary(self) -> Dict[str, Any]:
        """Возвращает агрегированный статистический отчет в структурированном виде."""
        return build_summary(self)

    def merge(self, other: Any) -> "CoinGridRiskAnalytics":
        """Сливает статистику из другого экземпляра CoinGridRiskAnalytics или словаря."""
        return merge_analytics(self, other)

    def format_grid_distribution_table(
        self,
        title: str = "РАСПРЕДЕЛЕНИЕ УРОВНЕЙ СЕТКИ И ВОРОНКА ВЫЖИВАНИЯ ЦИКЛОВ",
    ) -> str:
        """Форматирует визуальную ASCII-таблицу распределения уровней сетки и воронки циклов."""
        return format_grid_distribution_table(self, title=title)

    def format_per_coin_table(
        self,
        title: str = "РИСК-РЕЕСТР МОНЕТ (PER-COIN DRAWDOWN & RISK LEDGER)",
    ) -> str:
        """Форматирует визуальную ASCII-таблицу риска и показателей по каждой монете."""
        return format_per_coin_table(self, title=title)

    def format_worst_drawdowns_table(
        self,
        top_n: int = 10,
        title: str = "ТОП НАИХУДШИХ ПРОСАДОК (WORST ADVERSE EXCURSIONS)",
    ) -> str:
        """Форматирует визуальную ASCII-таблицу топ наихудших просадок за историю."""
        return format_worst_drawdowns_table(self, top_n=top_n, title=title)

