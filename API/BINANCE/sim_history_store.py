# ==============================================================================
# Path: API/BINANCE/sim_history_store.py
# Role: Кэшированное хранилище исторических свечей и предрасчитанных индикаторов (O(log N) поиск)
# ==============================================================================

from __future__ import annotations

import bisect
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from c_log import UnifiedLogger

logger = UnifiedLogger("SimHistoricalDataStore")


class SimHistoricalDataStore:
    """
    Singleton-кэш исторических данных (warmup 1h/4h/1d, sim_klines_1h, ATR, EMA-тренды).
    Индексирует закрытия 1h/4h/1d свечей через отсортированные массивы close_ts_ms,
    исключая линейное сканирование на каждом вызове get_klines().
    """

    _loaded: bool = False
    _warmup_1h: Dict[str, List[float]] = {}
    _warmup_4h: Dict[str, List[float]] = {}
    _warmup_1d: Dict[str, List[float]] = {}
    _sim_1h_close_times_ms: Dict[str, List[int]] = {}
    _sim_1h_closes: Dict[str, List[float]] = {}
    _sim_4h_close_times_ms: Dict[str, List[int]] = {}
    _sim_4h_closes: Dict[str, List[float]] = {}
    _sim_1d_close_times_ms: Dict[str, List[int]] = {}
    _sim_1d_closes: Dict[str, List[float]] = {}
    _precomputed_atr: Dict[str, float] = {}
    _precomputed_trends: Dict[str, str] = {}

    @classmethod
    def _backtest_data_dir(cls) -> Path:
        return Path(__file__).resolve().parent.parent.parent.parent / "cron3TradeBacktest" / "DATA"

    @classmethod
    def ensure_loaded(cls) -> None:
        if cls._loaded:
            return
        cls._loaded = True
        data_dir = cls._backtest_data_dir()

        cls._warmup_1h = cls._merge_json_dicts(data_dir, ["warmup_klines_1h.json"])
        cls._warmup_4h = cls._merge_json_dicts(data_dir, ["warmup_4h.json", "warmup_klines_4h.json"])
        cls._warmup_1d = cls._merge_json_dicts(data_dir, ["warmup_1d.json", "warmup_klines_1d.json"])
        cls._precomputed_atr = cls._merge_json_dicts(data_dir, ["precomputed_atr.json"])
        cls._precomputed_trends = cls._merge_json_dicts(data_dir, ["precomputed_ema_trends.json"])

        raw_sim_1h = cls._merge_json_dicts(data_dir, ["sim_klines_1h.json"])
        cls._build_sim_indices(raw_sim_1h)

    @classmethod
    def _merge_json_dicts(cls, data_dir: Path, filenames: List[str]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for name in filenames:
            p = data_dir / name
            if not p.exists():
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged.update(data)
            except Exception as e:
                logger.warning(f"[SimHistoricalDataStore] Error loading {name}: {e}")
        return merged

    @classmethod
    def _build_sim_indices(cls, raw_sim_1h: Dict[str, List[Dict[str, Any]]]) -> None:
        for sym, bars in raw_sim_1h.items():
            if not bars:
                continue
            sym_u = sym.upper().strip()
            t1: List[int] = []
            c1: List[float] = []
            t4: List[int] = []
            c4: List[float] = []
            td: List[int] = []
            cd: List[float] = []

            parsed: List[Tuple[int, float]] = [
                (int(b.get("open_time", 0)) + 3600000, float(b["close"]))
                for b in bars
            ]
            parsed.sort(key=lambda x: x[0])

            for close_ts_ms, close_px in parsed:
                t1.append(close_ts_ms)
                c1.append(close_px)
                if close_ts_ms % 14400000 == 0:
                    t4.append(close_ts_ms)
                    c4.append(close_px)
                if close_ts_ms % 86400000 == 0:
                    td.append(close_ts_ms)
                    cd.append(close_px)

            cls._sim_1h_close_times_ms[sym_u] = t1
            cls._sim_1h_closes[sym_u] = c1
            cls._sim_4h_close_times_ms[sym_u] = t4
            cls._sim_4h_closes[sym_u] = c4
            cls._sim_1d_close_times_ms[sym_u] = td
            cls._sim_1d_closes[sym_u] = cd

    @classmethod
    def get_precomputed_atr(
        cls,
        symbol: str,
        cur_sim_time_ms: int,
        timeframe: str = "1h",
        period: int = 14,
    ) -> Optional[float]:
        cls.ensure_loaded()
        if not cls._precomputed_atr or not cur_sim_time_ms:
            return None
        tf = str(timeframe).lower().strip()
        bucket_ts = (int(cur_sim_time_ms) // 3600000) * 3600000
        sym_u = symbol.upper().strip()

        key_p = f"{sym_u}|{tf}_{period}|{bucket_ts}"
        if key_p in cls._precomputed_atr:
            return float(cls._precomputed_atr[key_p])
        val = cls._precomputed_atr.get(f"{sym_u}|{tf}|{bucket_ts}")
        return float(val) if val is not None else None

    @classmethod
    def get_precomputed_trend(
        cls,
        symbol: str,
        cur_sim_time: float,
        timeframe: str = "4h",
        sma_fast: int = 10,
        sma_slow: int = 30,
    ) -> Optional[str]:
        cls.ensure_loaded()
        if not cls._precomputed_trends or cur_sim_time <= 0:
            return None
        ts_ms = int(cur_sim_time) if cur_sim_time > 1e11 else int(cur_sim_time * 1000)
        tf = timeframe.lower()
        bin_ms = 86400000 if "1d" in tf else (14400000 if "4h" in tf else 3600000)
        bin_ts = (ts_ms // bin_ms) * bin_ms
        suffix = "_7_25" if (sma_fast == 7 and sma_slow == 25) else ""
        key = f"{symbol.upper().strip()}|{timeframe}{suffix}|{bin_ts}"
        return cls._precomputed_trends.get(key)

    @classmethod
    def has_sim_hourly_bars(cls, symbol: str) -> bool:
        cls.ensure_loaded()
        return bool(cls._sim_1h_close_times_ms.get(symbol.upper().strip()))

    @classmethod
    def get_klines(
        cls,
        symbol: str,
        interval: str,
        cur_ts_ms: int,
        cur_price: float,
        limit: int = 35,
        dyn_bars: Optional[List[Tuple[int, float]]] = None,
    ) -> List[Dict[str, Any]]:
        """Возвращает последние `limit` баров (close) строго не новее cur_ts_ms за O(log N)."""
        cls.ensure_loaded()
        sym_u = symbol.upper().strip()
        interval_str = str(interval).lower().strip()
        if interval_str not in ("1h", "4h", "1d", "d"):
            return []

        use_warmup = cur_ts_ms >= 1775000000000
        has_file_bars = sym_u in cls._sim_1h_close_times_ms

        sim_closes: List[float] = []
        warmup: List[float] = []

        if has_file_bars:
            if interval_str == "1h":
                times_arr = cls._sim_1h_close_times_ms[sym_u]
                closes_arr = cls._sim_1h_closes[sym_u]
                warmup = cls._warmup_1h.get(sym_u, []) if use_warmup else []
            elif interval_str == "4h":
                times_arr = cls._sim_4h_close_times_ms[sym_u]
                closes_arr = cls._sim_4h_closes[sym_u]
                warmup = cls._warmup_4h.get(sym_u, []) if use_warmup else []
            else:
                times_arr = cls._sim_1d_close_times_ms[sym_u]
                closes_arr = cls._sim_1d_closes[sym_u]
                warmup = cls._warmup_1d.get(sym_u, []) if use_warmup else []

            end_idx = bisect.bisect_right(times_arr, cur_ts_ms)
            need_from_sim = min(end_idx, limit)
            sim_closes = closes_arr[end_idx - need_from_sim : end_idx] if need_from_sim > 0 else []
        else:
            raw_dyn = dyn_bars or []
            if interval_str == "1h":
                warmup = cls._warmup_1h.get(sym_u, []) if use_warmup else []
                sim_closes = [b[1] for b in raw_dyn]
            elif interval_str == "4h":
                warmup = cls._warmup_4h.get(sym_u, []) if use_warmup else []
                sim_closes = [b[1] for b in raw_dyn if (b[0] + 3600000) % 14400000 == 0]
            else:
                warmup = cls._warmup_1d.get(sym_u, []) if use_warmup else []
                sim_closes = [b[1] for b in raw_dyn if (b[0] + 3600000) % 86400000 == 0]

        reserve_cur = 1 if cur_price > 0.0 else 0
        max_hist = max(0, limit - reserve_cur)
        if len(sim_closes) >= max_hist:
            closes = list(sim_closes[-max_hist:]) if max_hist > 0 else []
        else:
            need_warm = max_hist - len(sim_closes)
            closes = (list(warmup[-need_warm:]) if warmup and need_warm > 0 else []) + list(sim_closes)

        if cur_price > 0.0:
            closes.append(cur_price)

        return [{"close": float(c)} for c in closes[-limit:]]
