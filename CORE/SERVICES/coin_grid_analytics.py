# ==============================================================================
# Path: CORE/SERVICES/coin_grid_analytics.py
# Role: Институциональный сервис трекинга, аналитики сеточных усреднений
#       и оценки рисков просадки per-coin (Grid Averaging & Adverse Excursion Tracker).
# ==============================================================================

from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple, Set
import copy
import math


class CoinGridRiskAnalytics:
    """
    Институциональный сервис сбора, аналитики и визуализации поведения сеточных ступеней,
    глубины усреднений и риска неблагоприятного движения цены (Adverse Price Excursion) per-coin.

    Фиксирует:
    1. Распределение срабатываний сеточных уровней (L0, L1, L2 ... L8):
       - Количество исполненных ордеров per-level (Fills Count)
       - Долю в общем пуле ордеров (Fills Share %)
       - Суммарный и средний задействованный объем капитала ($ Notional)
    2. Распределение максимальной глубины торговых циклов (Cycle Depth Funnel):
       - Сколько циклов закрылись на L0 (без единого усреднения)
       - Сколько циклов потребовали добора L1, L2, L3 ... L8
       - Воронку выживания корзины (Survival Funnel % от L0 к L8)
       - Среднюю глубину усреднения (Average Cycle Level)
    3. Риск-профиль каждой монеты (Per-Coin Risk Ledger):
       - Максимальное неблагоприятное движение цены против первого входа L0 (Max Adverse from L0 %)
       - Максимальное неблагоприятное движение против средней цены (Max Adverse from Avg %)
       - Максимальную плавающую просадку в USDT (Max Floating Drawdown $)
       - Число циклов, Win Rate %, суммарный Net PnL и комиссии
       - Случаи выхода цены за пределы L8 (-89%) и потенциальное касание стоп-лосса 100%
    4. Топ наихудших просадок за всю историю тестов (Worst Adverse Excursions Hall of Fame).
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

        # Описания ступеней сетки (L0, L1, ...)
        self.level_metadata: Dict[str, Dict[str, Any]] = {
            "L0": {"role": "Стартовый вход", "indent_pct": 0.0, "volume_pct": 7.7},
            "L1": {"role": "Зона 1: Первое колено", "indent_pct": -3.5, "volume_pct": 7.7},
            "L2": {"role": "Зона 1: Второе колено", "indent_pct": -5.5, "volume_pct": 8.47},
            "L3": {"role": "Зона 2: Третье колено", "indent_pct": -9.0, "volume_pct": 9.32},
            "L4": {"role": "Зона 2: Четвертое колено", "indent_pct": -15.0, "volume_pct": 10.25},
            "L5": {"role": "Зона 2: Пятое колено", "indent_pct": -24.0, "volume_pct": 11.27},
            "L6": {"role": "Зона 3: Глубинная броня", "indent_pct": -38.0, "volume_pct": 12.4},
            "L7": {"role": "Зона 3: Глубинная броня", "indent_pct": -58.0, "volume_pct": 13.64},
            "L8": {"role": "Зона 3: Предельная броня", "indent_pct": -89.0, "volume_pct": 19.95},
        }

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
        """
        Регистрирует исполнение ордера (вход, усреднение, частичный срез или полное закрытие).
        """
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
                if pos_qty_after <= 1e-8 or cycle["current_qty"] <= 1e-8:
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

                if cur_qty <= 1e-8 or avg_p <= 0 or e0 <= 0:
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
        duration_h = max(0.0, (end_t - start_t) / 3600.0) if start_t > 0 else 0.0

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
            self.per_coin_stats[sym] = {
                "symbol": sym,
                "total_cycles": 0,
                "winning_cycles": 0,
                "losing_cycles": 0,
                "realized_pnl": 0.0,
                "fees_paid": 0.0,
                "net_pnl": 0.0,
                "max_level_ever": 0,
                "level_depths_sum": 0,
                "max_adverse_from_entry0_pct": 0.0,
                "max_adverse_from_avg_pct": 0.0,
                "max_floating_dd_usdt": 0.0,
                "total_duration_hours": 0.0,
                "exceeded_l8_count": 0,
                "would_hit_sl_120_count": 0,
            }

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
        if record["max_adverse_from_entry0_pct"] >= 89.0:
            s["exceeded_l8_count"] += 1
        if record["max_adverse_from_entry0_pct"] >= 120.0:
            s["would_hit_sl_120_count"] += 1

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
    # SUMMARY & AGGREGATION
    # =========================================================================

    def get_summary(self) -> Dict[str, Any]:
        """Возвращает агрегированный статистический отчет в структурированном виде."""
        total_cycles = len(self.completed_cycles)
        total_fills = sum(self.level_fills_count.values())

        # 1. Распределение уровней сетки (Fills & Depth)
        all_lvl_keys = [f"L{i}" for i in range(9)]
        level_dist: Dict[str, Dict[str, Any]] = {}

        # Воронка выживания: сколько циклов дошли хотя бы до уровня i
        cycles_reached_at_least: Dict[str, int] = defaultdict(int)
        for c in self.completed_cycles:
            max_lvl = c["max_level_reached"]
            for i in range(max_lvl + 1):
                cycles_reached_at_least[f"L{i}"] += 1

        for lvl_key in all_lvl_keys:
            fills = self.level_fills_count.get(lvl_key, 0)
            fills_pct = (fills / total_fills * 100.0) if total_fills > 0 else 0.0
            notional = self.level_fills_notional.get(lvl_key, 0.0)
            avg_notional = (notional / fills) if fills > 0 else 0.0

            depth_cnt = self.cycle_depth_counts.get(lvl_key, 0)
            depth_pct = (depth_cnt / total_cycles * 100.0) if total_cycles > 0 else 0.0
            survival_cnt = cycles_reached_at_least.get(lvl_key, 0)
            survival_pct = (survival_cnt / total_cycles * 100.0) if total_cycles > 0 else 0.0

            meta = self.level_metadata.get(lvl_key, {})
            level_dist[lvl_key] = {
                "level": lvl_key,
                "role": meta.get("role", lvl_key),
                "indent_pct": meta.get("indent_pct", 0.0),
                "volume_pct": meta.get("volume_pct", 0.0),
                "fills_count": fills,
                "fills_share_pct": round(fills_pct, 2),
                "total_notional_usdt": round(notional, 2),
                "avg_notional_usdt": round(avg_notional, 2),
                "cycles_max_depth_count": depth_cnt,
                "cycles_max_depth_share_pct": round(depth_pct, 2),
                "survival_funnel_count": survival_cnt,
                "survival_funnel_pct": round(survival_pct, 2),
            }

        # 2. Средняя глубина цикла
        avg_cycle_depth = 0.0
        if total_cycles > 0:
            avg_cycle_depth = sum(c["max_level_reached"] for c in self.completed_cycles) / total_cycles

        # 3. Per-Coin Ledger
        coins_summary: Dict[str, Dict[str, Any]] = {}
        for sym, s in self.per_coin_stats.items():
            tot = s["total_cycles"]
            win_r = (s["winning_cycles"] / tot * 100.0) if tot > 0 else 0.0
            avg_lvl = (s["level_depths_sum"] / tot) if tot > 0 else 0.0
            avg_dur = (s["total_duration_hours"] / tot) if tot > 0 else 0.0

            coins_summary[sym] = {
                "symbol": sym,
                "total_cycles": tot,
                "winning_cycles": s["winning_cycles"],
                "losing_cycles": s["losing_cycles"],
                "win_rate_pct": round(win_r, 1),
                "realized_pnl": round(s["realized_pnl"], 2),
                "fees_paid": round(s["fees_paid"], 2),
                "net_pnl": round(s["net_pnl"], 2),
                "max_level_ever": s["max_level_ever"],
                "avg_cycle_level": round(avg_lvl, 2),
                "max_adverse_from_entry0_pct": round(s["max_adverse_from_entry0_pct"], 2),
                "max_adverse_from_avg_pct": round(s["max_adverse_from_avg_pct"], 2),
                "max_floating_dd_usdt": round(s["max_floating_dd_usdt"], 2),
                "avg_duration_hours": round(avg_dur, 2),
                "exceeded_l8_count": s["exceeded_l8_count"],
                "would_hit_sl_120_count": s.get("would_hit_sl_120_count", s.get("would_hit_sl_100_count", 0)),
            }

        # 4. Топ худших просадок (Worst 10 Adverse Excursions)
        sorted_worst = sorted(
            self.completed_cycles,
            key=lambda x: (x.get("max_adverse_from_entry0_pct", 0.0), x.get("max_floating_dd_usdt", 0.0)),
            reverse=True
        )[:15]

        # 5. Глобальные счетчики L8 и SL-120%
        tot_l8_exceeded = sum(s.get("exceeded_l8_count", 0) for s in coins_summary.values())
        tot_sl_120 = sum(s.get("would_hit_sl_120_count", 0) for s in coins_summary.values())

        return {
            "strategy_id": self.strategy_id,
            "total_cycles": total_cycles,
            "total_fills": total_fills,
            "average_cycle_level": round(avg_cycle_depth, 2),
            "total_exceeded_l8": tot_l8_exceeded,
            "total_would_hit_sl_120": tot_sl_120,
            "grid_level_distribution": level_dist,
            "per_coin_summary": coins_summary,
            "worst_drawdown_events": sorted_worst,
        }

    # =========================================================================
    # MERGE (CROSS-DECADE ACCUMULATION)
    # =========================================================================

    def merge(self, other: Any) -> "CoinGridRiskAnalytics":
        """
        Сливает статистику из другого экземпляра CoinGridRiskAnalytics или сериализованного словаря.
        Позволяет сквозное накопление данных по всем 18 декадам марафона.
        """
        if isinstance(other, dict):
            # Слияние из словаря
            summary = other
            for lvl_key, ldata in summary.get("grid_level_distribution", {}).items():
                self.level_fills_count[lvl_key] += ldata.get("fills_count", 0)
                self.level_fills_notional[lvl_key] += ldata.get("total_notional_usdt", 0.0)
                self.cycle_depth_counts[lvl_key] += ldata.get("cycles_max_depth_count", 0)

            for sym, cdata in summary.get("per_coin_summary", {}).items():
                if sym not in self.per_coin_stats:
                    self.per_coin_stats[sym] = {
                        "symbol": sym,
                        "total_cycles": 0,
                        "winning_cycles": 0,
                        "losing_cycles": 0,
                        "realized_pnl": 0.0,
                        "fees_paid": 0.0,
                        "net_pnl": 0.0,
                        "max_level_ever": 0,
                        "level_depths_sum": 0,
                        "max_adverse_from_entry0_pct": 0.0,
                        "max_adverse_from_avg_pct": 0.0,
                        "max_floating_dd_usdt": 0.0,
                        "total_duration_hours": 0.0,
                        "exceeded_l8_count": 0,
                        "would_hit_sl_120_count": 0,
                    }
                s = self.per_coin_stats[sym]
                s["total_cycles"] += cdata.get("total_cycles", 0)
                s["winning_cycles"] += cdata.get("winning_cycles", 0)
                s["losing_cycles"] += cdata.get("losing_cycles", 0)
                s["realized_pnl"] += cdata.get("realized_pnl", 0.0)
                s["fees_paid"] += cdata.get("fees_paid", 0.0)
                s["net_pnl"] += cdata.get("net_pnl", 0.0)
                s["max_level_ever"] = max(s["max_level_ever"], cdata.get("max_level_ever", 0))
                s["level_depths_sum"] += int(cdata.get("avg_cycle_level", 0.0) * cdata.get("total_cycles", 0))
                s["total_duration_hours"] += float(cdata.get("avg_duration_hours", 0.0) * cdata.get("total_cycles", 0))
                s["max_adverse_from_entry0_pct"] = max(s["max_adverse_from_entry0_pct"], cdata.get("max_adverse_from_entry0_pct", 0.0))
                s["max_adverse_from_avg_pct"] = max(s["max_adverse_from_avg_pct"], cdata.get("max_adverse_from_avg_pct", 0.0))
                s["max_floating_dd_usdt"] = max(s["max_floating_dd_usdt"], cdata.get("max_floating_dd_usdt", 0.0))
                s["exceeded_l8_count"] += cdata.get("exceeded_l8_count", 0)
                s["would_hit_sl_120_count"] += cdata.get("would_hit_sl_120_count", cdata.get("would_hit_sl_100_count", 0))

            for ev in summary.get("worst_drawdown_events", []):
                self.completed_cycles.append(ev)

        elif isinstance(other, CoinGridRiskAnalytics):
            for lvl_key, cnt in other.level_fills_count.items():
                self.level_fills_count[lvl_key] += cnt
                self.level_fills_notional[lvl_key] += other.level_fills_notional[lvl_key]

            for lvl_key, cnt in other.cycle_depth_counts.items():
                self.cycle_depth_counts[lvl_key] += cnt

            for c in other.completed_cycles:
                self.completed_cycles.append(c)

            for sym, s_other in other.per_coin_stats.items():
                if sym not in self.per_coin_stats:
                    self.per_coin_stats[sym] = copy.deepcopy(s_other)
                else:
                    s = self.per_coin_stats[sym]
                    s["total_cycles"] += s_other["total_cycles"]
                    s["winning_cycles"] += s_other["winning_cycles"]
                    s["losing_cycles"] += s_other["losing_cycles"]
                    s["realized_pnl"] += s_other["realized_pnl"]
                    s["fees_paid"] += s_other["fees_paid"]
                    s["net_pnl"] += s_other["net_pnl"]
                    s["max_level_ever"] = max(s["max_level_ever"], s_other["max_level_ever"])
                    s["level_depths_sum"] += s_other["level_depths_sum"]
                    s["total_duration_hours"] += s_other["total_duration_hours"]
                    s["max_adverse_from_entry0_pct"] = max(s["max_adverse_from_entry0_pct"], s_other["max_adverse_from_entry0_pct"])
                    s["max_adverse_from_avg_pct"] = max(s["max_adverse_from_avg_pct"], s_other["max_adverse_from_avg_pct"])
                    s["max_floating_dd_usdt"] = max(s["max_floating_dd_usdt"], s_other["max_floating_dd_usdt"])
                    s["exceeded_l8_count"] += s_other["exceeded_l8_count"]
                    s["would_hit_sl_120_count"] += s_other.get("would_hit_sl_120_count", s_other.get("would_hit_sl_100_count", 0))

        return self

    # =========================================================================
    # INSTITUTIONAL ASCII TABLES FORMATTING
    # =========================================================================

    def format_grid_distribution_table(self, title: str = "РАСПРЕДЕЛЕНИЕ УРОВНЕЙ СЕТКИ И ВОРОНКА ВЫЖИВАНИЯ ЦИКЛОВ") -> str:
        """Форматирует визуальную ASCII-таблицу распределения уровней сетки и воронки циклов."""
        summary = self.get_summary()
        dist = summary["grid_level_distribution"]
        total_cyc = summary["total_cycles"]
        total_fls = summary["total_fills"]

        if total_fls == 0:
            return f"\n[{title}]: Срабатывания сетки не зафиксированы (0 ордеров)\n"

        headers = [
            "Level", "Описание / Роль ступени", "Отступ %", "Объем %",
            "Ордеров", "Доля ордеров %", "Сумм. объем $",
            "Циклов (Max Lvl)", "Доля циклов %", "Воронка (Дошли >= L)"
        ]
        rows = []

        for lvl_key in [f"L{i}" for i in range(9)]:
            if lvl_key not in dist:
                continue
            data = dist[lvl_key]
            fls = data["fills_count"]
            fls_p = data["fills_share_pct"]
            notion = data["total_notional_usdt"]
            cyc_cnt = data["cycles_max_depth_count"]
            cyc_p = data["cycles_max_depth_share_pct"]
            surv_cnt = data["survival_funnel_count"]
            surv_p = data["survival_funnel_pct"]
            indent = data["indent_pct"]
            vol_p = data["volume_pct"]
            role = data["role"]

            rows.append([
                lvl_key,
                role,
                f"{indent:+.1f}%" if indent != 0 else "0.0% (Entry)",
                f"{vol_p:.2f}%",
                f"{fls:,}",
                f"{fls_p:5.1f}%",
                f"{notion:,.1f}$",
                f"{cyc_cnt:,}",
                f"{cyc_p:5.1f}%",
                f"{surv_cnt:,} ({surv_p:5.1f}%)"
            ])

        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(val))

        def make_row(cols):
            return " | ".join(val.rjust(col_widths[i]) if i in (0, 2, 3, 4, 5, 6, 7, 8, 9) else val.ljust(col_widths[i]) for i, val in enumerate(cols))

        sep = "-+-".join("-" * w for w in col_widths)
        banner = "=" * len(make_row(headers))
        title_line = f"📊 {title.upper()} (ЦИКЛОВ: {total_cyc:,} | ОРДЕРОВ: {total_fls:,} | СРЕДНЯЯ ГЛУБИНА: L{summary['average_cycle_level']:.2f})"

        footer = (
            f"ИТОГО: {total_cyc:,} закрытых циклов | {total_fls:,} исполнений | "
            f"Средняя глубина: L{summary['average_cycle_level']:.2f} | "
            f"Превысили L8 (-89%): {summary['total_exceeded_l8']} | Коснулись SL-120%: {summary['total_would_hit_sl_120']}"
        )

        table_lines = [
            banner,
            title_line,
            banner,
            make_row(headers),
            sep,
        ] + [make_row(r) for r in rows] + [
            sep,
            footer,
            banner
        ]
        return "\n" + "\n".join(table_lines) + "\n"

    def format_per_coin_table(self, title: str = "РИСК-РЕЕСТР МОНЕТ (PER-COIN DRAWDOWN & RISK LEDGER)") -> str:
        """Форматирует визуальную ASCII-таблицу риска и показателей по каждой монете."""
        summary = self.get_summary()
        coins = list(summary["per_coin_summary"].values())

        if not coins:
            return f"\n[{title}]: Данные по монетам отсутствуют\n"

        # Сортировка по максимальному неблагоприятному движению цены против L0
        sorted_coins = sorted(coins, key=lambda c: c["max_adverse_from_entry0_pct"], reverse=True)

        headers = [
            "Rank", "Символ", "Циклов", "WinRate %", "Макс Lvl", "Ср. Lvl",
            "Откат от L0 %", "Макс DD $", "Net PnL $", "Ср. время", ">L8", ">SL-120%"
        ]
        rows = []

        for rank, c in enumerate(sorted_coins, 1):
            sym = c["symbol"]
            cyc = c["total_cycles"]
            wr = c["win_rate_pct"]
            max_l = f"L{c['max_level_ever']}"
            avg_l = f"L{c['avg_cycle_level']:.2f}"
            adv_l0 = f"{c['max_adverse_from_entry0_pct']:5.1f}%"
            m_dd = f"{c['max_floating_dd_usdt']:6.2f}$"
            net_p = f"{c['net_pnl']:+7.2f}$"
            avg_dur = f"{c['avg_duration_hours']:4.1f}ч"
            ex_l8 = str(c["exceeded_l8_count"]) if c["exceeded_l8_count"] > 0 else "-"
            sl120 = str(c.get("would_hit_sl_120_count", c.get("would_hit_sl_100_count", 0))) if c.get("would_hit_sl_120_count", c.get("would_hit_sl_100_count", 0)) > 0 else "-"

            rows.append([
                str(rank),
                sym,
                f"{cyc:,}",
                f"{wr:5.1f}%",
                max_l,
                avg_l,
                adv_l0,
                m_dd,
                net_p,
                avg_dur,
                ex_l8,
                sl120
            ])

        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(val))

        def make_row(cols):
            return " | ".join(val.rjust(col_widths[i]) if i in (0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11) else val.ljust(col_widths[i]) for i, val in enumerate(cols))

        sep = "-+-".join("-" * w for w in col_widths)
        banner = "=" * len(make_row(headers))
        title_line = f"🛡️ {title.upper()} (МОНЕТ В КОРЗИНЕ: {len(coins)})"

        table_lines = [
            banner,
            title_line,
            banner,
            make_row(headers),
            sep,
        ] + [make_row(r) for r in rows] + [
            banner
        ]
        return "\n" + "\n".join(table_lines) + "\n"

    def format_worst_drawdowns_table(self, top_n: int = 10, title: str = "ТОП НАИХУДШИХ ПРОСАДОК (WORST ADVERSE EXCURSIONS)") -> str:
        """Форматирует визуальную ASCII-таблицу топ наихудших просадок за историю."""
        summary = self.get_summary()
        worst = summary.get("worst_drawdown_events", [])[:top_n]

        if not worst:
            return f"\n[{title}]: События глубоких просадок отсутствуют\n"

        headers = [
            "№", "Символ", "Side", "Откат от L0 %",
            "Макс DD $", "Макс Lvl", "Net PnL $", "Длительность", "Причина закрытия"
        ]
        rows = []

        for idx, ev in enumerate(worst, 1):
            sym = ev.get("symbol", "N/A")
            side = ev.get("side", "N/A")
            adv_l0 = f"{ev.get('max_adverse_from_entry0_pct', 0.0):5.1f}%"
            dd = f"{ev.get('max_floating_dd_usdt', 0.0):6.2f}$"
            lvl = f"L{ev.get('max_level_reached', 0)}"
            net_p = f"{ev.get('net_pnl', 0.0):+7.2f}$"
            dur = f"{ev.get('duration_hours', 0.0):4.1f}ч"
            reason = ev.get("close_reason", "TP")

            rows.append([
                str(idx),
                sym,
                side,
                adv_l0,
                dd,
                lvl,
                net_p,
                dur,
                reason
            ])

        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], len(val))

        def make_row(cols):
            return " | ".join(val.rjust(col_widths[i]) if i in (0, 3, 4, 5, 6, 7) else val.ljust(col_widths[i]) for i, val in enumerate(cols))

        sep = "-+-".join("-" * w for w in col_widths)
        banner = "=" * len(make_row(headers))
        title_line = f"⚠️ {title.upper()} (ТОП {len(worst)} ГЛУБОКИХ ДВИЖЕНИЙ)"

        table_lines = [
            banner,
            title_line,
            banner,
            make_row(headers),
            sep,
        ] + [make_row(r) for r in rows] + [
            banner
        ]
        return "\n" + "\n".join(table_lines) + "\n"

