# ==============================================================================
# Path: ANALYTICS/equity_trajectory.py
# Role: Персистентный кэш 5-минутных слепков эквити (CACHE/equity_snapshots_cache.json, хвост 10 дней)
# ==============================================================================

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from c_utils import Utils

try:
    from consts import (
        CACHE_DIR,
        EQUITY_SNAPSHOT_INTERVAL_SEC,
        EQUITY_SNAPSHOT_RETENTION_DAYS,
        EQUITY_SNAPSHOTS_CACHE_FILE,
    )
except ImportError:
    CACHE_DIR = Path(__file__).resolve().parent.parent / "CACHE"
    EQUITY_SNAPSHOT_INTERVAL_SEC = 300
    EQUITY_SNAPSHOT_RETENTION_DAYS = 10.0
    EQUITY_SNAPSHOTS_CACHE_FILE = CACHE_DIR / "equity_snapshots_cache.json"


class EquitySnapshotStore:
    """
    Персистентный RAM + дисковый кэш периодических слепков баланса (`CACHE/equity_snapshots_cache.json`).
      - При любом перезапуске сразу подтягивает с диска последний накопленный прогресс слепков
        по каждому куполу (`strat_id`) и глобальному портфелю (`ALL`).
      - На частых тиках (< 5 минут) проверяет таймер за O(1) в RAM без обращения к диску.
      - Каждые `interval_sec` (300с = 5 минут) дописывает `[ts_ms, current_equity]`,
        обрезает хвост старше `retention_days` (10 суток) и атомарно пишет кэш на диск.
    """

    def __init__(
        self,
        cache_file: Path = EQUITY_SNAPSHOTS_CACHE_FILE,
        interval_sec: int = EQUITY_SNAPSHOT_INTERVAL_SEC,
        retention_days: float = EQUITY_SNAPSHOT_RETENTION_DAYS,
    ):
        self.cache_file: Path = Path(cache_file)
        self.interval_sec: int = max(1, int(interval_sec))
        self.retention_days: float = max(0.01, float(retention_days))
        self._cache: Dict[str, List[List[float]]] = {}
        self._disk_mtime: float = -1.0
        self._load_from_disk(force=True)

    def _load_from_disk(self, force: bool = False) -> None:
        if not self.cache_file.exists():
            if force:
                self._cache = {}
                self._disk_mtime = -1.0
            return
        try:
            stat_mtime = self.cache_file.stat().st_mtime
            if not force and stat_mtime == self._disk_mtime:
                return
            with open(self.cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                parsed: Dict[str, List[List[float]]] = {}
                for k, v in raw.items():
                    if not isinstance(v, list) or not v:
                        continue
                    ref_ts = max(
                        (int(it[0]) for it in v if isinstance(it, (list, tuple)) and len(it) >= 2),
                        default=int(time.time() * 1000),
                    )
                    parsed[str(k)] = self._normalize_and_trim(v, ref_now_ms=ref_ts)
                self._cache = parsed
                self._disk_mtime = stat_mtime
        except Exception:
            pass

    def _save_to_disk(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            Utils.atomic_write_json(self.cache_file, self._cache)
            if self.cache_file.exists():
                self._disk_mtime = self.cache_file.stat().st_mtime
        except Exception:
            pass

    def _normalize_and_trim(self, raw_items: List[Any], ref_now_ms: int) -> List[List[Any]]:
        step_ms = self.interval_sec * 1000
        retention_ms = max(step_ms * 2, int(self.retention_days * 86_400_000.0))
        max_len = max(4, int((self.retention_days * 86_400.0) / self.interval_sec) + 2)
        cutoff_ms = int(ref_now_ms) - retention_ms

        valid: List[List[Any]] = []
        for item in raw_items:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    p_ts = int(item[0])
                    p_eq = round(float(item[1]), 4)
                    if p_ts >= cutoff_ms:
                        valid.append([p_ts, p_eq])
                except (ValueError, TypeError):
                    continue
        valid.sort(key=lambda x: x[0])
        if len(valid) > max_len:
            valid = valid[-max_len:]
        return valid

    def record_snapshot(
        self,
        scope_id: str,
        current_equity: float,
        now_ms: Optional[int] = None,
        force: bool = False,
    ) -> bool:
        key = (scope_id or "ALL").strip()
        ts = int(now_ms if now_ms is not None else time.time() * 1000)
        eq_val = round(float(current_equity), 4)
        step_ms = self.interval_sec * 1000

        bucket = list(self._cache.get(key, []))
        if bucket and not force:
            if (ts - int(bucket[-1][0])) < step_ms:
                return False

        if bucket and int(bucket[-1][0]) == ts:
            bucket[-1] = [ts, eq_val]
        else:
            bucket.append([ts, eq_val])

        self._cache[key] = self._normalize_and_trim(bucket, ref_now_ms=ts)
        self._save_to_disk()
        return True

    def get_snapshots(self, scope_id: str) -> List[List[Any]]:
        self._load_from_disk(force=False)
        key = (scope_id or "ALL").strip()
        return [list(pt) for pt in self._cache.get(key, [])]

    def extract_series(
        self,
        scope_id: str,
        start_balance: float,
        current_equity: float,
        start_dt: Optional[datetime] = None,
        now_dt: Optional[datetime] = None,
    ) -> Tuple[List[datetime], List[float], List[float]]:
        now_u = now_dt if now_dt is not None else datetime.now(timezone.utc).replace(tzinfo=None)
        ts_now = int(now_u.replace(tzinfo=timezone.utc).timestamp() * 1000)
        retention_ms = max(60_000, int(self.retention_days * 86_400_000.0))
        cutoff_ms = ts_now - retention_ms

        raw_pts = self._normalize_and_trim(self.get_snapshots(scope_id), ref_now_ms=ts_now)
        pts: List[Tuple[int, float]] = [(int(p[0]), float(p[1])) for p in raw_pts]

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


DEFAULT_SNAPSHOT_STORE = EquitySnapshotStore()
