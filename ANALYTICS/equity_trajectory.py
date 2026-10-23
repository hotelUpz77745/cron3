# ==============================================================================
# Path: ANALYTICS/equity_trajectory.py
# Role: Построитель траектории эквити и чистого Net Profit на хронологических опорных точках
# ==============================================================================

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple


class EquityTrajectoryBuilder:
    """
    Строит непрерывную хронологическую траекторию баланса и чистого Net Profit
    на основе закрытых сделок и подтвержденных экстремумов (ATH и дно просадки).
    Гарантирует сохранение и точную отрисовку точек дна просадки и выхода из нее.
    """

    def __init__(
        self,
        start_balance: float,
        net_profit_final: float,
        raw_trades: List[Tuple[datetime, float]],
        start_dt: Optional[datetime] = None,
        peak_np: Optional[float] = None,
        peak_dt: Optional[datetime] = None,
        trough_np: Optional[float] = None,
        trough_dt: Optional[datetime] = None,
        now_dt: Optional[datetime] = None,
    ):
        self.start_balance: float = float(start_balance)
        self.net_profit_final: float = float(net_profit_final)
        self.raw_trades: List[Tuple[datetime, float]] = raw_trades
        self.start_dt: Optional[datetime] = start_dt
        self.peak_np: Optional[float] = float(peak_np) if peak_np is not None else None
        self.peak_dt: Optional[datetime] = peak_dt
        self.trough_np: Optional[float] = float(trough_np) if trough_np is not None else None
        self.trough_dt: Optional[datetime] = trough_dt
        self.now_dt: datetime = now_dt if now_dt else datetime.now(timezone.utc).replace(tzinfo=None)

    def build_trajectory(self) -> Tuple[List[datetime], List[float], List[float]]:
        """
        Формирует списки (times, net_profits, balances) для построения графиков.
        Хронологические опорные точки гарантируют, что экстремумы не затираются.
        """
        now = self.now_dt
        if self.start_dt:
            t_start = self.start_dt
        elif self.raw_trades:
            t_start = self.raw_trades[0][0] - timedelta(minutes=30)
        else:
            t_start = now - timedelta(hours=3)

        if t_start >= now:
            t_start = now - timedelta(minutes=10)

        has_peak = (self.peak_np is not None and self.peak_np > 0.0001)
        has_trough = (self.trough_np is not None and self.trough_np < -0.0001)

        # 1. Определение временной метки пика
        t_peak = self.peak_dt
        if has_peak and not t_peak:
            if abs(self.net_profit_final - self.peak_np) < 0.01:
                t_peak = now
            elif self.raw_trades:
                cum = 0.0
                best_c = -1e9
                best_t = self.raw_trades[0][0]
                for dt_tr, pnl_tr in self.raw_trades:
                    cum += pnl_tr
                    if cum > best_c:
                        best_c = cum
                        best_t = dt_tr
                t_peak = best_t
            else:
                t_peak = t_start + (now - t_start) * 0.33

        # 2. Определение временной метки дна просадки
        t_trough = self.trough_dt
        if has_trough and not t_trough:
            if abs(self.net_profit_final - self.trough_np) < 0.01:
                t_trough = now
            elif self.raw_trades:
                cum = 0.0
                worst_c = 1e9
                worst_t = self.raw_trades[-1][0]
                for dt_tr, pnl_tr in self.raw_trades:
                    cum += pnl_tr
                    if cum < worst_c:
                        worst_c = cum
                        worst_t = dt_tr
                t_trough = worst_t
            else:
                t_trough = t_start + (now - t_start) * 0.67

        # 3. Ограничение меток временным интервалом [t_start, now]
        if has_peak and t_peak:
            t_peak = max(t_start, min(now, t_peak))
        if has_trough and t_trough:
            t_trough = max(t_start, min(now, t_trough))

        # 4. Формирование первичных опорных точек
        raw_anchors = [(t_start, 0.0)]
        if has_peak and t_peak:
            raw_anchors.append((t_peak, self.peak_np))
        if has_trough and t_trough:
            raw_anchors.append((t_trough, self.trough_np))

        raw_anchors.sort(key=lambda x: x[0])

        # 5. Разрешение коллизий одинаковых временных меток
        distinct_anchors = [raw_anchors[0]]
        for dt_item, val_item in raw_anchors[1:]:
            prev_dt, prev_val = distinct_anchors[-1]
            if dt_item > prev_dt:
                distinct_anchors.append((dt_item, val_item))
            else:
                adjusted_dt = prev_dt + timedelta(seconds=1)
                if adjusted_dt <= now:
                    distinct_anchors.append((adjusted_dt, val_item))
                else:
                    distinct_anchors[-1] = (prev_dt, val_item)

        # 6. Фиксация конечной точки (now, net_profit_final) без затирания экстремума
        last_dt, last_val = distinct_anchors[-1]
        if last_dt < now - timedelta(seconds=5):
            distinct_anchors.append((now, self.net_profit_final))
        else:
            if abs(last_val - self.net_profit_final) > 0.001:
                if last_dt >= now:
                    distinct_anchors[-1] = (now - timedelta(seconds=30), last_val)
                distinct_anchors.append((now, self.net_profit_final))
            else:
                distinct_anchors[-1] = (now, self.net_profit_final)

        times: List[datetime] = []
        net_profits: List[float] = []

        # 7. Интерполяция по сегментам с учетом закрытых сделок
        for a_idx in range(len(distinct_anchors) - 1):
            t_seg_start, v_seg_start = distinct_anchors[a_idx]
            t_seg_end, v_seg_end = distinct_anchors[a_idx + 1]

            if not times:
                times.append(t_seg_start)
                net_profits.append(v_seg_start)

            seg_trades = [tr for tr in self.raw_trades if t_seg_start < tr[0] < t_seg_end]

            if not seg_trades:
                times.append(t_seg_end)
                net_profits.append(v_seg_end)
            else:
                t_span = max(1.0, (t_seg_end - t_seg_start).total_seconds())
                cum_w = 0.0
                weights: List[float] = []
                is_downward = (v_seg_end < v_seg_start)

                for _, pnl in seg_trades:
                    if is_downward:
                        w = abs(pnl) if pnl < 0 else 0.0
                    else:
                        w = max(0.0, pnl)
                    cum_w += w
                    weights.append(cum_w)

                tot_w = cum_w if cum_w > 0 else 1.0

                for sub_i, (dt_trade, _) in enumerate(seg_trades):
                    if dt_trade <= times[-1]:
                        continue
                    t_ratio = (dt_trade - t_seg_start).total_seconds() / t_span
                    w_ratio = weights[sub_i] / tot_w
                    if is_downward:
                        blend = min(1.0, max(0.0, 0.45 * t_ratio + 0.55 * w_ratio))
                    else:
                        blend = min(1.0, max(0.0, 0.40 * t_ratio + 0.60 * w_ratio))
                    val = v_seg_start + blend * (v_seg_end - v_seg_start)
                    times.append(dt_trade)
                    net_profits.append(round(val, 4))

                if times[-1] < t_seg_end:
                    times.append(t_seg_end)
                    net_profits.append(v_seg_end)
                else:
                    net_profits[-1] = v_seg_end

        balances = [round(self.start_balance + np_val, 4) for np_val in net_profits]
        return times, net_profits, balances
