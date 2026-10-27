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

from consts import DATA_DIR
from c_log import UnifiedLogger
from c_utils import Utils
from API.BINANCE.public import BinancePublic, is_valid_symbol
from CORE.SERVICES.VOLATILITY.klines_cache import KlinesCache
from CORE.SERVICES.VOLATILITY.volatility_scanner import VolatilityScanner

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
    def compute_hurst_exponent(prices: np.ndarray) -> float:
        """
        Вычисляет показатель Херста (Hurst Exponent) через масштабирование дисперсии лагов цен:
        Var(P_{t+tau} - P_t) ~ tau^(2H)
        H < 0.45 -> Выраженный возврат к среднему (Mean-Reverting, идеален для сеточника).
        0.45 <= H <= 0.55 -> Случайное блуждание (Random Walk).
        H > 0.55 -> Устойчивый тренд / памп-дамп (Trending / Persistent, опасен для сетки).
        """
        if len(prices) < 30:
            return 0.5
        log_p = np.log(np.maximum(prices, 1e-8))
        tau_list = [2, 4, 8, 16, 24, 32]
        tau_list = [t for t in tau_list if t < len(log_p) // 2]
        if len(tau_list) < 2:
            return 0.5

        variances = []
        for tau in tau_list:
            diffs = log_p[tau:] - log_p[:-tau]
            variances.append(float(np.var(diffs)))

        poly = np.polyfit(np.log(tau_list), np.log(variances), 1)
        hurst = float(poly[0]) / 2.0
        return max(0.0, min(1.0, hurst))

    @staticmethod
    def evaluate_coin_metrics(
        open_arr: np.ndarray,
        high_arr: np.ndarray,
        low_arr: np.ndarray,
        close_arr: np.ndarray,
        volume_arr: Optional[np.ndarray] = None,
        min_natr_threshold: float = 0.25,
        max_parabolic_pump_pct: float = 60.0,
        max_death_spiral_dump_pct: float = -45.0,
        enable_hurst_filter: bool = False,
        max_hurst_threshold: float = 0.50,
        enable_volume_climax_filter: bool = False,
        volume_climax_mult: float = 4.5,
        enable_multi_hurst_filter: bool = False,
        short_hurst_candles: int = 48,
        max_short_hurst_threshold: float = 0.53,
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

        # 6. Фильтр кульминации объема (Volume Climax / Meme Pump Detector)
        vol_climax_val = None
        if enable_volume_climax_filter and volume_arr is not None and len(volume_arr) >= 30:
            base_window = volume_arr[:-12]
            recent_window = volume_arr[-12:]
            med_base = float(np.median(base_window))
            if med_base > 0:
                vol_climax_val = float(np.mean(recent_window) / med_base)
                if vol_climax_val > volume_climax_mult and net_move_pct > 20.0:
                    return {
                        "eligible": False,
                        "score": 0.0,
                        "natr": round(natr, 3),
                        "move_pct": round(net_move_pct, 1),
                        "wiggle": round(wiggle_ratio, 1),
                        "vol_climax": round(vol_climax_val, 2),
                        "reason": f"volume_climax_pump (vol {vol_climax_val:.1f}x > {volume_climax_mult}x & +{net_move_pct:.1f}%)",
                    }

        # 7. Фильтр показателя Херста (Mean-Reversion Suitability Filter)
        hurst = AlphaCoinSelector.compute_hurst_exponent(close_arr)
        if enable_hurst_filter and hurst > max_hurst_threshold:
            return {
                "eligible": False,
                "score": 0.0,
                "natr": round(natr, 3),
                "move_pct": round(net_move_pct, 1),
                "wiggle": round(wiggle_ratio, 1),
                "hurst": round(hurst, 3),
                "reason": f"persistent_trend_hurst ({hurst:.3f} > {max_hurst_threshold})",
            }

        # 8. Двухфакторный фильтр моментум-пробоя (Multi-Horizon Hurst Guard)
        short_hurst = None
        if enable_multi_hurst_filter and len(close_arr) >= (short_hurst_candles + 15):
            short_hurst = AlphaCoinSelector.compute_hurst_exponent(close_arr[-short_hurst_candles:])
            if short_hurst > max_short_hurst_threshold:
                return {
                    "eligible": False,
                    "score": 0.0,
                    "natr": round(natr, 3),
                    "move_pct": round(net_move_pct, 1),
                    "wiggle": round(wiggle_ratio, 1),
                    "hurst": round(hurst, 3),
                    "short_hurst": round(short_hurst, 3),
                    "reason": f"short_term_momentum_breakout (H_short {short_hurst:.3f} > {max_short_hurst_threshold})",
                }

        # 9. Composite Alpha Score (с учетом mean-reverting бонуса при низком H)
        score = (natr * math.log10(wiggle_ratio + 10.0)) / (1.0 + abs_drift / 50.0)
        if enable_hurst_filter and hurst < 0.45:
            # Бонус 10% за выраженную антиперсистентность
            score *= 1.10

        res_dict = {
            "eligible": True,
            "score": round(score, 4),
            "natr": round(natr, 3),
            "move_pct": round(net_move_pct, 1),
            "wiggle": round(wiggle_ratio, 1),
            "hurst": round(hurst, 3),
            "reason": "optimal_high_beta_swing",
        }
        if vol_climax_val is not None:
            res_dict["vol_climax"] = round(vol_climax_val, 2)
        if short_hurst is not None:
            res_dict["short_hurst"] = round(short_hurst, 3)
        return res_dict

    @classmethod
    def _get_config(cls) -> Dict[str, Any]:
        """Считывает актуальную конфигурацию альфа-селектора динамически из app.json строго напрямую по Протоколу 2."""
        app_path = DATA_DIR / "app.json"
        if not app_path.exists():
            raise FileNotFoundError(f"[AlphaCoinSelector] Файл конфигурации не найден: {app_path}")

        data = Utils.read_json_file(app_path)
        if not data or "alpha_coin_selector" not in data:
            raise KeyError(f"[AlphaCoinSelector] Секция 'alpha_coin_selector' отсутствует в {app_path}!")

        raw_cfg = data["alpha_coin_selector"]
        cfg = {
            "enabled": bool(raw_cfg.get("enabled", True)),
            "target_count": int(raw_cfg["target_count"]),
            "timeframe": str(raw_cfg.get("timeframe", "15m")),
            "candles_limit": int(raw_cfg.get("candles_limit", 96)),
            "use_volatility_scanner_pool": bool(raw_cfg.get("use_volatility_scanner_pool", True)),
            "min_natr_threshold": float(raw_cfg.get("min_natr_threshold", 0.25)),
            "max_parabolic_pump_pct": float(raw_cfg.get("max_parabolic_pump_pct", 60.0)),
            "max_death_spiral_dump_pct": float(raw_cfg.get("max_death_spiral_dump_pct", -45.0)),
            "enable_hurst_filter": bool(raw_cfg.get("enable_hurst_filter", False)),
            "max_hurst_threshold": float(raw_cfg.get("max_hurst_threshold", 0.50)),
            "enable_volume_climax_filter": bool(raw_cfg.get("enable_volume_climax_filter", False)),
            "volume_climax_multiplier": float(raw_cfg.get("volume_climax_multiplier", 4.5)),
            "enable_multi_hurst_filter": bool(raw_cfg.get("enable_multi_hurst_filter", False)),
            "short_hurst_candles": int(raw_cfg.get("short_hurst_candles", 48)),
            "max_short_hurst_threshold": float(raw_cfg.get("max_short_hurst_threshold", 0.53)),
            "klines_cache_ttl_sec": 300.0,
        }
        if "volatility_scanner" in data and "klines_cache_ttl_sec" in data["volatility_scanner"]:
            cfg["klines_cache_ttl_sec"] = float(data["volatility_scanner"]["klines_cache_ttl_sec"])
        return cfg

    @classmethod
    def rank_symbols(
        cls,
        symbols: List[str],
        candles_dict: Dict[str, Dict[str, np.ndarray]],
        top_k: Optional[int] = None,
        min_natr_threshold: float = 0.25,
        max_parabolic_pump_pct: float = 60.0,
        max_death_spiral_dump_pct: float = -45.0,
        enable_hurst_filter: bool = False,
        max_hurst_threshold: float = 0.50,
        enable_volume_climax_filter: bool = False,
        volume_climax_mult: float = 4.5,
        enable_multi_hurst_filter: bool = False,
        short_hurst_candles: int = 48,
        max_short_hurst_threshold: float = 0.53,
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
                volume_arr=c_data.get("volume"),
                min_natr_threshold=min_natr_threshold,
                max_parabolic_pump_pct=max_parabolic_pump_pct,
                max_death_spiral_dump_pct=max_death_spiral_dump_pct,
                enable_hurst_filter=enable_hurst_filter,
                max_hurst_threshold=max_hurst_threshold,
                enable_volume_climax_filter=enable_volume_climax_filter,
                volume_climax_mult=volume_climax_mult,
                enable_multi_hurst_filter=enable_multi_hurst_filter,
                short_hurst_candles=short_hurst_candles,
                max_short_hurst_threshold=max_short_hurst_threshold,
            )
            metrics["symbol"] = sym
            results.append(metrics)

        results.sort(key=lambda x: (x["eligible"], x["score"]), reverse=True)
        if top_k is not None:
            return results[:top_k]
        return results

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
        target_count: Optional[int] = None,
        universe_filter: Optional[List[str]] = None,
        interval: Optional[str] = None,
        limit: Optional[int] = None,
        universe: Optional[List[str]] = None,
        use_volatility_pool: Optional[bool] = None,
        volatile_file_path: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        """
        Асинхронный метод боевого отбора монет.
        Если use_volatility_pool=True (по дефолту из app.json), берет стек монет
        строго из отфильтрованного результата VolatilityScanner.
        """
        cfg = cls._get_config()
        effective_count = int(target_count) if target_count is not None else int(cfg["target_count"])
        effective_interval = interval or str(cfg.get("timeframe", "15m"))
        effective_limit = limit if limit is not None else int(cfg.get("candles_limit", 96))
        use_vol_pool = use_volatility_pool if use_volatility_pool is not None else bool(cfg.get("use_volatility_scanner_pool", True))
        ttl_sec = float(cfg.get("klines_cache_ttl_sec", 300.0))

        candidates_to_fetch = universe_filter or universe

        if not candidates_to_fetch and use_vol_pool:
            volatile_file = volatile_file_path if volatile_file_path is not None else (DATA_DIR / "volatile_symbols.json")
            if not volatile_file.exists():
                try:
                    scanner = VolatilityScanner()
                    await scanner.scan()
                except Exception as e:
                    logger.error(f"[AlphaCoinSelector] Failed to run VolatilityScanner: {e}")

            if volatile_file.exists():
                try:
                    vdata = Utils.read_json_file(volatile_file)
                    if isinstance(vdata, list) and vdata:
                        candidates_to_fetch = [
                            str(x["symbol"]).upper().strip()
                            for x in vdata if isinstance(x, dict) and "symbol" in x
                        ]
                        logger.info(f"[AlphaCoinSelector] Loaded {len(candidates_to_fetch)} symbols strictly from VolatilityScanner pool.")
                except Exception as e:
                    logger.error(f"[AlphaCoinSelector] Error loading volatile_symbols.json: {e}")

            if not candidates_to_fetch:
                logger.warning("[AlphaCoinSelector] VolatilityScanner pool is empty. 0 coins qualified.")
                return []

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
                    async def fetcher():
                        return await BinancePublic.get_klines(sym, interval=effective_interval, limit=effective_limit)

                    raw = await KlinesCache.get_or_fetch(sym, effective_interval, effective_limit, fetcher, ttl_sec=ttl_sec)
                    if not raw or len(raw) < 20:
                        return None
                    return sym, {
                        "open": np.array([x["open"] for x in raw], dtype=np.float64),
                        "high": np.array([x["high"] for x in raw], dtype=np.float64),
                        "low": np.array([x["low"] for x in raw], dtype=np.float64),
                        "close": np.array([x["close"] for x in raw], dtype=np.float64),
                        "volume": np.array([float(x.get("volume", x.get("vol", 0.0))) for x in raw], dtype=np.float64),
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

        ranked = cls.rank_symbols(
            valid_symbols,
            candles_dict,
            top_k=effective_count,
            min_natr_threshold=float(cfg.get("min_natr_threshold", 0.25)),
            max_parabolic_pump_pct=float(cfg.get("max_parabolic_pump_pct", 60.0)),
            max_death_spiral_dump_pct=float(cfg.get("max_death_spiral_dump_pct", -45.0)),
            enable_hurst_filter=bool(cfg.get("enable_hurst_filter", False)),
            max_hurst_threshold=float(cfg.get("max_hurst_threshold", 0.50)),
            enable_volume_climax_filter=bool(cfg.get("enable_volume_climax_filter", False)),
            volume_climax_mult=float(cfg.get("volume_climax_multiplier", 4.5)),
            enable_multi_hurst_filter=bool(cfg.get("enable_multi_hurst_filter", False)),
            short_hurst_candles=int(cfg.get("short_hurst_candles", 48)),
            max_short_hurst_threshold=float(cfg.get("max_short_hurst_threshold", 0.53)),
        )
        logger.info(
            f"[AlphaCoinSelector] Top-{effective_count} selected: "
            + ", ".join([f"{x['symbol']} (score: {x['score']})" for x in ranked])
        )
        return ranked

    @classmethod
    def select_top_coins_sync(
        cls,
        target_count: Optional[int] = None,
        universe_filter: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Синхронный фасад для запуска без async boilerplate."""
        try:
            return asyncio.run(cls.select_top_coins(target_count=target_count, universe_filter=universe_filter))
        except RuntimeError:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(cls.select_top_coins(target_count=target_count, universe_filter=universe_filter))
