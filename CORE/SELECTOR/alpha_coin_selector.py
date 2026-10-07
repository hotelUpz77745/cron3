# ==============================================================================
# Path: CORE/SELECTOR/alpha_coin_selector.py
# Role: Автономный Институциональный Альфа-Селектор монет для боевого бота cron3
# ==============================================================================

from __future__ import annotations

import asyncio
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from c_log import UnifiedLogger
from API.BINANCE.public import BinancePublic, is_valid_symbol

logger = UnifiedLogger("AlphaCoinSelector")
VALID_SYMBOL_REGEX = re.compile(r"^[A-Za-z0-9]+$")


class AlphaCoinSelector:
    """
    Институциональный Альфа-Селектор монет (Mean-Reversion, Wiggle Density, Volatility Gate):
    1. Volatility Gate: NATR >= 0.25% (топливо для сеточного робота и LIFO Peeling).
    2. Wiggle Ratio: плотность микроколебаний относительно чистого смещения.
       Оценивает, сколько монета 'пилит' внутри диапазона, не улетая в тренд.
    3. Anti-Parabolic Gate: отсекает 'протухших лидеров' (памп > +60% за горизонт).
    4. Anti-Death Spiral Gate: отсекает монеты в отвесном падении (дамп < -45%).
    5. Alpha Score: взвешенный композит пригодности для сеточных систем.
    """

    @staticmethod
    def evaluate_coin_metrics(
        open_arr: np.ndarray,
        high_arr: np.ndarray,
        low_arr: np.ndarray,
        close_arr: np.ndarray,
        min_natr_threshold: float = 0.25,
        max_parabolic_pump_pct: float = 60.0,
        max_death_spiral_dump_pct: float = -45.0,
    ) -> Dict[str, Any]:
        """Анализирует свечной ряд и возвращает метрики пригодности."""
        if len(close_arr) < 20:
            return {"eligible": False, "score": 0.0, "reason": "insufficient_data"}

        # 1. Normalized ATR (% размаха свечи)
        ranges_pct = (high_arr - low_arr) / np.maximum(close_arr, 1e-8) * 100.0
        natr = float(np.mean(ranges_pct))

        # 2. Чистое смещение за окно (Net Drift)
        start_p = float(open_arr[0])
        end_p = float(close_arr[-1])
        net_move_pct = (end_p - start_p) / max(start_p, 1e-8) * 100.0
        abs_drift = abs(net_move_pct)

        # 3. Вихревой коэффициент осцилляции (Wiggle Ratio)
        tot_path = float(np.sum(ranges_pct))
        wiggle_ratio = tot_path / (abs_drift + 1.0)

        # 4. Фильтр истощения (Anti-Exhaustion Gate)
        if net_move_pct > max_parabolic_pump_pct:
            return {
                "eligible": False,
                "score": 0.0,
                "natr": round(natr, 3),
                "move_pct": round(net_move_pct, 1),
                "wiggle": round(wiggle_ratio, 1),
                "reason": f"parabolic_exhaustion_pump (+{net_move_pct:.1f}%)",
            }

        if net_move_pct < max_death_spiral_dump_pct:
            return {
                "eligible": False,
                "score": 0.0,
                "natr": round(natr, 3),
                "move_pct": round(net_move_pct, 1),
                "wiggle": round(wiggle_ratio, 1),
                "reason": f"death_spiral_dump ({net_move_pct:.1f}%)",
            }

        # 5. Фильтр сонной волатильности (Zombie Filter)
        if natr < min_natr_threshold:
            return {
                "eligible": False,
                "score": 0.0,
                "natr": round(natr, 3),
                "move_pct": round(net_move_pct, 1),
                "wiggle": round(wiggle_ratio, 1),
                "reason": f"dead_volatility_zombie (NATR {natr:.2f}% < {min_natr_threshold}%)",
            }

        # 6. Composite Alpha Score
        score = (natr * math.log10(wiggle_ratio + 10.0)) / (1.0 + abs_drift / 50.0)

        return {
            "eligible": True,
            "score": round(score, 4),
            "natr": round(natr, 3),
            "move_pct": round(net_move_pct, 1),
            "wiggle": round(wiggle_ratio, 1),
            "reason": "optimal_high_beta_swing",
        }

    @classmethod
    def rank_symbols(
        cls,
        symbols: List[str],
        candles_dict: Dict[str, Dict[str, np.ndarray]],
        top_k: int = 4,
    ) -> List[Dict[str, Any]]:
        """Ранжирует словарь свечей и возвращает Top-K лидеров."""
        results = []
        for sym in symbols:
            c_data = candles_dict.get(sym)
            if not c_data:
                continue
            metrics = cls.evaluate_coin_metrics(
                open_arr=c_data["open"],
                high_arr=c_data["high"],
                low_arr=c_data["low"],
                close_arr=c_data["close"],
            )
            metrics["symbol"] = sym
            results.append(metrics)

        results.sort(key=lambda x: (x["eligible"], x["score"]), reverse=True)
        return results[:top_k]

    @classmethod
    def calculate_coin_metrics(cls, symbol: str, klines: List[Any]) -> Dict[str, Any]:
        """Удобный хелпер для оценки списка сырых свечей [[ts, open, high, low, close, ...], ...] или словарей."""
        if not klines or len(klines) < 2:
            return {"symbol": symbol, "eligible": False, "score": 0.0, "reject_reason": "insufficient_data", "reason": "insufficient_data"}

        if isinstance(klines[0], dict):
            opens = [float(k["open"]) for k in klines]
            highs = [float(k["high"]) for k in klines]
            lows = [float(k["low"]) for k in klines]
            closes = [float(k["close"]) for k in klines]
        else:
            opens = [float(k[1]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            closes = [float(k[4]) for k in klines]

        res = cls.evaluate_coin_metrics(
            open_arr=np.array(opens, dtype=np.float64),
            high_arr=np.array(highs, dtype=np.float64),
            low_arr=np.array(lows, dtype=np.float64),
            close_arr=np.array(closes, dtype=np.float64),
        )
        res["symbol"] = symbol
        res["reject_reason"] = res.get("reason", "")
        return res

    @classmethod
    async def select_top_coins(
        cls,
        target_count: int = 4,
        universe_filter: Optional[List[str]] = None,
        interval: str = "15m",
        limit: int = 96,  # 96 свечей по 15m = ровно 24 часа истории
        universe: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Асинхронный метод боевого отбора монет через прямое сканирование рынка.
        Если передан universe_filter или universe, оценивает именно эти символы.
        Если нет, берет перпетуал-тикеры с объемом > 30M USDT.
        """
        candidates_to_fetch = universe_filter or universe
        if not candidates_to_fetch:
            try:
                tickers = await BinancePublic.get_24h_ticker()
                if tickers:
                    filtered = []
                    for t in tickers:
                        sym = t.get("symbol", "")
                        vol = float(t.get("quoteVolume", 0.0))
                        if sym.endswith("USDT") and vol >= 30_000_000.0:
                            filtered.append((sym, vol))
                    filtered.sort(key=lambda x: x[1], reverse=True)
                    candidates_to_fetch = [x[0] for x in filtered[:30]]
            except Exception as e:
                logger.error(f"[AlphaCoinSelector] Failed to fetch 24h tickers: {e}")

        if not candidates_to_fetch:
            candidates_to_fetch = ["NEARUSDT", "SOONUSDT", "PUMPUSDT", "GRASSUSDT", "SOLUSDT", "DOGEUSDT"]

        # Только латиница/цифры: тикеры с иероглифами и прочим отсекаются
        _rejected = [s for s in candidates_to_fetch if not is_valid_symbol(s)]
        if _rejected:
            logger.warning(f"[AlphaCoinSelector] Отсечены нелатинские тикеры: {_rejected}")
            candidates_to_fetch = [s for s in candidates_to_fetch if is_valid_symbol(s)]

        sem = asyncio.Semaphore(10)

        async def fetch_klines(sym: str) -> Optional[tuple[str, dict]]:
            async with sem:
                try:
                    raw = await BinancePublic.get_klines(sym, interval=interval, limit=limit)
                    if not raw or len(raw) < 20:
                        return None
                    return sym, {
                        "open": np.array([x["open"] for x in raw], dtype=np.float64),
                        "high": np.array([x["high"] for x in raw], dtype=np.float64),
                        "low": np.array([x["low"] for x in raw], dtype=np.float64),
                        "close": np.array([x["close"] for x in raw], dtype=np.float64),
                    }
                except Exception:
                    return None

        tasks = [fetch_klines(s) for s in candidates_to_fetch]
        fetched = await asyncio.gather(*tasks)

        candles_dict = {}
        valid_symbols = []
        for res in fetched:
            if res:
                sym, c_data = res
                candles_dict[sym] = c_data
                valid_symbols.append(sym)

        if not valid_symbols:
            logger.warning("[AlphaCoinSelector] Failed to load klines for candidates.")
            return []

        ranked = cls.rank_symbols(valid_symbols, candles_dict, top_k=target_count)
        logger.info(
            f"[AlphaCoinSelector] Top-{target_count} selected: "
            + ", ".join([f"{x['symbol']} (score: {x['score']})" for x in ranked])
        )
        return ranked

    @classmethod
    def select_top_coins_sync(
        cls,
        target_count: int = 4,
        universe_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Синхронный фасад для запуска без async boilerplate."""
        try:
            return asyncio.run(cls.select_top_coins(target_count=target_count, universe_filter=universe_filter))
        except RuntimeError:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(cls.select_top_coins(target_count=target_count, universe_filter=universe_filter))
