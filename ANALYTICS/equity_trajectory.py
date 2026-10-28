# ==============================================================================
# Path: ANALYTICS/equity_trajectory.py
# Role: Хранилище периодических слепков текущего баланса (каждые 5 минут, хвост 10 дней)
# ==============================================================================

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from consts import (
        EQUITY_SNAPSHOT_INTERVAL_SEC,
        EQUITY_SNAPSHOT_RETENTION_DAYS,
    )
except ImportError:
    EQUITY_SNAPSHOT_INTERVAL_SEC = 300
    EQUITY_SNAPSHOT_RETENTION_DAYS = 10.0


class EquitySnapshotStore:
    """
    Хранит периодические слепки текущего эквити `[ts_ms, round(balance, 4)]`
    в списке `data["equity_snapshots"]`:
      - Запись нового слепка строго раз в `interval_sec` (300с = 5 минут).
      - Автоматическая подрезка старого хвоста старше `retention_days` (10 суток).
      - Прямая выдача точек `(times, net_profits, balances)` для отрисовки графика
        без синтетического сглаживания и без потери прошлых пиков и просадок.
    """

    @staticmethod
    def record_snapshot(
        data: Dict[str, Any],
        current_equity: float,
        now_ms: Optional[int] = None,
        interval_sec: int = EQUITY_SNAPSHOT_INTERVAL_SEC,
        retention_days: float = EQUITY_SNAPSHOT_RETENTION_DAYS,
        force: bool = False,
    ) -> bool:
        """Пишет слепок баланса раз в 5 минут и подрезает хвост старше 10 дней."""
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        eq_val = round(float(current_equity), 4)
        step_ms = max(1_000, int(interval_sec) * 1000)
        retention_ms = max(step_ms * 2, int(float(retention_days) * 86_400_000.0))
        max_len = max(4, int((float(retention_days) * 86_400.0) / max(1, int(interval_sec))) + 2)

        raw_list = data.get("equity_snapshots")
        if not isinstance(raw_list, list):
            raw_list = []

        if raw_list and not force:
            last_item = raw_list[-1]
            if isinstance(last_item, (list, tuple)) and len(last_item) >= 2:
                if (ts - int(last_item[0])) < step_ms:
                    return False

        if raw_list and isinstance(raw_list[-1], (list, tuple)) and len(raw_list[-1]) >= 2 and int(raw_list[-1][0]) == ts:
            raw_list[-1] = [ts, eq_val]
        else:
            raw_list.append([ts, eq_val])

        cutoff_ms = ts - retention_ms
        trimmed: List[List[Any]] = [
            [int(item[0]), round(float(item[1]), 4)]
            for item in raw_list
            if isinstance(item, (list, tuple)) and len(item) >= 2 and int(item[0]) >= cutoff_ms
        ]
        if len(trimmed) > max_len:
            trimmed = trimmed[-max_len:]

        data["equity_snapshots"] = trimmed
        return True

    @staticmethod
    def extract_series(
        data: Optional[Dict[str, Any]],
        start_balance: float,
        current_equity: float,
        start_dt: Optional[datetime] = None,
        now_dt: Optional[datetime] = None,
        retention_days: float = EQUITY_SNAPSHOT_RETENTION_DAYS,
    ) -> Tuple[List[datetime], List[float], List[float]]:
        """Возвращает `(times_naive_utc, net_profits, balances)` напрямую по списку слепков."""
        now_u = now_dt if now_dt is not None else datetime.now(timezone.utc).replace(tzinfo=None)
        ts_now = int(now_u.replace(tzinfo=timezone.utc).timestamp() * 1000)
        retention_ms = max(60_000, int(float(retention_days) * 86_400_000.0))
        cutoff_ms = ts_now - retention_ms

        pts: List[Tuple[int, float]] = []
        raw_list = data.get("equity_snapshots") if isinstance(data, dict) else None
        if isinstance(raw_list, list):
            for item in raw_list:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    try:
                        p_ts = int(item[0])
                        p_eq = float(item[1])
                        if p_ts >= cutoff_ms:
                            pts.append((p_ts, p_eq))
                    except (ValueError, TypeError):
                        continue

        pts.sort(key=lambda x: x[0])
        cur_eq_clean = round(float(current_equity), 4)
        start_bal_clean = round(float(start_balance), 4)

        if not pts:
            if start_dt is not None and start_dt < now_u:
                s_ts = max(int(start_dt.replace(tzinfo=timezone.utc).timestamp() * 1000), cutoff_ms)
            else:
                s_ts = ts_now - 60_000
            if s_ts >= ts_now:
                s_ts = ts_now - 60_000
            pts = [(s_ts, start_bal_clean), (ts_now, cur_eq_clean)]
        else:
            if ts_now > pts[-1][0]:
                pts.append((ts_now, cur_eq_clean))
            else:
                pts[-1] = (pts[-1][0], cur_eq_clean)

            if len(pts) == 1:
                only_ts, _ = pts[0]
                s_ts = (
                    int(start_dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
                    if (start_dt is not None and int(start_dt.replace(tzinfo=timezone.utc).timestamp() * 1000) < only_ts)
                    else (only_ts - 60_000)
                )
                pts.insert(0, (max(s_ts, cutoff_ms), start_bal_clean))

        times = [datetime.fromtimestamp(t / 1000.0, tz=timezone.utc).replace(tzinfo=None) for t, _ in pts]
        balances = [round(v, 4) for _, v in pts]
        net_profits = [round(v - start_bal_clean, 4) for v in balances]
        return times, net_profits, balances
