# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\ADVANCED\volatility_scanner2.py
# Role: Продвинутый сканер плотности волатильности и перцентилей (Volatility Scanner 2)

# ==============================================================================
# Path: CORE/ADVANCED/volatility_scanner2.py
# python -m CORE.ADVANCED.volatility_scanner2
# Role: Сканер плотности волатильности и перцентилей для фьючерсов Binance
# ==============================================================================

import asyncio
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from c_log import UnifiedLogger
from consts import DATA_DIR
from c_utils import Utils
from API.BINANCE.public import BinancePublic
from API.BINANCE.client_prod import BinanceClient

logger = UnifiedLogger("VolatilityScanner2")

class VolatilityScanner2:
    """
    Продвинутый сканер волатильности фьючерсов Binance.
    Ключевые особенности:
    1. Расчет слоя плотности (Density Layer %): проверяет, чтобы не менее minimum_density процентов
       свечей из окна попадали строго в рабочий коридор [min_volatility_pct, max_volatility_pct].
    2. Фильтр последней свечи (last_candle): гарантирует, что текущая/последняя закрытая свеча
       также находится внутри рабочего диапазона.
    3. Ранжирование по перцентилю и плотности: отсекает редкие единичные аномальные пампы.
    """
    def __init__(self, client=None):
        self.config_path = DATA_DIR / "app.json"
        self.output_json_path = DATA_DIR / "volatile_symbols2.json"
        self.output_txt_path = DATA_DIR / "volatile_symbols2.txt"
        self.client = client
        self.semaphore = asyncio.Semaphore(10)

    def _get_config(self) -> dict:
        default_cfg = {
            "timeframe": "1d",
            "window": 30,
            "min_volatility_pct": 5.0,
            "max_volatility_pct": 16.0,
            "max_candle_vol_pct": 25.0,
            "strict_window": True,
            "min_dayly_vol_usdt": 5000000.0,
            "minimum_density": 70.0,
            "last_candle": True,
            "max_last_delta_pct": -15.0,
            "min_range_position_pct": 25.0,
            "max_dump_pct": -15.0,
            "max_recent_dump_pct": -18.0
        }

        if self.config_path.exists():
            try:
                data = Utils.read_json_file(self.config_path)
                if "volatility_scanner2" in data:
                    default_cfg.update(data["volatility_scanner2"])
                elif "volatility_scanner" in data:
                    default_cfg.update({k: v for k, v in data["volatility_scanner"].items() if k in default_cfg})
            except Exception as e:
                logger.error(f"[Scanner2] Error reading config: {e}")

        return default_cfg

    async def _process_symbol(
        self,
        symbol: str,
        timeframe: str,
        window: int,
        min_vol: float,
        max_vol: float,
        strict_window: bool,
        minimum_density: float,
        check_last_candle: bool,
        max_candle_vol_pct: float | None = None,
        max_last_delta_pct: float | None = None,
        min_range_position_pct: float | None = None,
        max_dump_pct: float | None = None,
        max_recent_dump_pct: float | None = None
    ):
        async with self.semaphore:
            try:
                klines = await BinancePublic.get_klines(symbol, timeframe, window)
                if not klines and self.client:
                    klines = await self.client.get_klines(symbol, timeframe, window)
                if not klines:
                    return None

                if strict_window and len(klines) < window:
                    return None

                vols = []
                closes = []
                highs = []
                lows = []
                for k in klines:
                    high = float(k.get("high", 0.0))
                    low = float(k.get("low", 0.0))
                    close = float(k.get("close", 0.0))
                    if low > 0 and high >= low:
                        vol = ((high / low) - 1.0) * 100.0
                        vols.append(vol)
                        closes.append(close)
                        highs.append(high)
                        lows.append(low)

                if not vols or not closes:
                    return None

                # 1. Запрет на аномально большую свечу (Hard Ceiling / Anti-Blow-off / Anti-Spike)
                max_observed_vol = max(vols)
                if max_candle_vol_pct is not None and max_observed_vol > max_candle_vol_pct:
                    return None

                total_count = len(vols)
                matching_count = sum(1 for v in vols if min_vol <= v <= max_vol)
                density_pct = (matching_count / total_count) * 100.0

                # 2. Проверка плотности слоя волатильности
                if density_pct < minimum_density:
                    return None

                # 3. Проверка последней свечи на вхождение в рабочий коридор
                last_candle_vol = vols[-1]
                if check_last_candle:
                    # Разрешаем небольшой запас сверху для закрывающейся свечи
                    max_allowed_last = max_vol * 1.35 if max_candle_vol_pct is None else min(max_vol * 1.35, max_candle_vol_pct)
                    if not (min_vol <= last_candle_vol <= max_allowed_last):
                        return None

                # 4. Глобальный тренд и защита от слива (Anti-Dump Filters)
                # 4.1. Дельта между последними двумя свечами (Anti-Dump: текущий слив)
                if len(closes) >= 2:
                    last_delta_pct = ((closes[-1] - closes[-2]) / closes[-2]) * 100.0
                    if max_last_delta_pct is not None and last_delta_pct < max_last_delta_pct:
                        return None
                else:
                    last_delta_pct = 0.0

                # 4.2. Положение в диапазоне окна (Anti-Waterfall: монета не скребет дно)
                min_p = min(lows)
                max_p = max(highs)
                range_position_pct = ((closes[-1] - min_p) / (max_p - min_p) * 100.0) if max_p > min_p else 50.0
                if min_range_position_pct is not None and range_position_pct < min_range_position_pct:
                    return None

                # 4.3. Макроснос за все окно (Anti-Death-Spiral)
                drift_pct = ((closes[-1] - closes[0]) / closes[0]) * 100.0
                if max_dump_pct is not None and drift_pct < max_dump_pct:
                    return None

                # 4.4. Краткосрочный обвал за последние 3-4 свечи (Anti-Flash-Dump)
                if len(closes) >= 4:
                    recent_drift_pct = ((closes[-1] - closes[-4]) / closes[-4]) * 100.0
                elif len(closes) >= 3:
                    recent_drift_pct = ((closes[-1] - closes[-3]) / closes[-3]) * 100.0
                else:
                    recent_drift_pct = drift_pct

                if max_recent_dump_pct is not None and recent_drift_pct < max_recent_dump_pct:
                    return None

                # 4.5. Отклонение от средней цены окна (SMA)
                sma_price = sum(closes) / len(closes)
                sma_diff_pct = ((closes[-1] - sma_price) / sma_price) * 100.0

                # 5. Расчет статистических перцентилей
                med_vol = statistics.median(vols)
                avg_vol = sum(vols) / total_count

                if total_count >= 4:
                    q = statistics.quantiles(vols, n=4)
                    p25 = q[0]
                    p75 = q[2]
                else:
                    p25 = min(vols)
                    p75 = max(vols)

                return {
                    "symbol": symbol,
                    "density_pct": round(density_pct, 2),
                    "matching_candles": matching_count,
                    "total_candles": total_count,
                    "last_candle_vol": round(last_candle_vol, 2),
                    "max_candle_vol": round(max_observed_vol, 2),
                    "last_delta_pct": round(last_delta_pct, 2),
                    "range_pos_pct": round(range_position_pct, 2),
                    "drift_pct": round(drift_pct, 2),
                    "recent_drift_pct": round(recent_drift_pct, 2),
                    "sma_diff_pct": round(sma_diff_pct, 2),
                    "median_vol": round(med_vol, 2),
                    "avg_vol": round(avg_vol, 2),
                    "p25_vol": round(p25, 2),
                    "p75_vol": round(p75, 2),
                    "candle_vols": [round(v, 2) for v in vols]
                }
            except Exception as e:
                logger.error(f"[Scanner2] Error processing {symbol}: {e}")
                return None

    async def scan(self) -> Path:
        cfg = self._get_config()
        timeframe = cfg.get("timeframe", "1d")
        window = int(cfg.get("window", 30))

        min_vol = cfg.get("min_volatility_pct")
        max_vol = cfg.get("max_volatility_pct")
        if min_vol is None or max_vol is None:
            raise ValueError("[Scanner2] min_volatility_pct and max_volatility_pct MUST be numeric, null is forbidden!")

        min_vol = float(min_vol)
        max_vol = float(max_vol)
        if min_vol > max_vol:
            raise ValueError(f"[Scanner2] min_volatility_pct ({min_vol}) cannot exceed max_volatility_pct ({max_vol})!")

        strict_window = bool(cfg.get("strict_window", True))
        min_dayly_vol_usdt = cfg.get("min_dayly_vol_usdt")
        if min_dayly_vol_usdt is not None:
            min_dayly_vol_usdt = float(min_dayly_vol_usdt)

        minimum_density = float(cfg.get("minimum_density", 70.0))
        check_last_candle = bool(cfg.get("last_candle", True))

        max_candle_vol_pct = cfg.get("max_candle_vol_pct")
        if max_candle_vol_pct is not None:
            max_candle_vol_pct = float(max_candle_vol_pct)

        max_last_delta_pct = cfg.get("max_last_delta_pct")
        if max_last_delta_pct is not None:
            max_last_delta_pct = float(max_last_delta_pct)

        min_range_position_pct = cfg.get("min_range_position_pct")
        if min_range_position_pct is not None:
            min_range_position_pct = float(min_range_position_pct)

        max_dump_pct = cfg.get("max_dump_pct")
        if max_dump_pct is not None:
            max_dump_pct = float(max_dump_pct)

        max_recent_dump_pct = cfg.get("max_recent_dump_pct")
        if max_recent_dump_pct is not None:
            max_recent_dump_pct = float(max_recent_dump_pct)

        logger.info(
            f"[Scanner2] Starting scan. TF={timeframe}, window={window}, range=[{min_vol}%..{max_vol}%], "
            f"max_candle_vol<={max_candle_vol_pct}%, min_density={minimum_density}%, last_candle={check_last_candle}, "
            f"anti_dump: last_delta>={max_last_delta_pct}%, pos>={min_range_position_pct}%, drift>={max_dump_pct}%, "
            f"min_dayly_vol_usdt={min_dayly_vol_usdt}"
        )

        symbols = await BinancePublic.get_perp_symbols()
        if not symbols:
            logger.error("[Scanner2] Failed to fetch symbols from Binance.")
            return None

        daily_vol_map = {}
        if min_dayly_vol_usdt is not None:
            tickers = await BinancePublic.get_24h_ticker()
            if tickers:
                daily_vol_map = {t["symbol"]: float(t.get("quoteVolume", 0.0)) for t in tickers}
                symbols = [s for s in symbols if daily_vol_map.get(s, 0.0) >= min_dayly_vol_usdt]
                logger.info(f"[Scanner2] {len(symbols)} symbols passed 24h volume filter (>= ${min_dayly_vol_usdt:,.0f}).")
            else:
                logger.warning("[Scanner2] Failed to fetch 24h tickers, skipping volume filter.")

        tasks = [
            self._process_symbol(
                symbol=s,
                timeframe=timeframe,
                window=window,
                min_vol=min_vol,
                max_vol=max_vol,
                strict_window=strict_window,
                minimum_density=minimum_density,
                check_last_candle=check_last_candle,
                max_candle_vol_pct=max_candle_vol_pct,
                max_last_delta_pct=max_last_delta_pct,
                min_range_position_pct=min_range_position_pct,
                max_dump_pct=max_dump_pct,
                max_recent_dump_pct=max_recent_dump_pct
            )
            for s in symbols
        ]

        results = await asyncio.gather(*tasks)
        matching_symbols = [r for r in results if r is not None]

        # Attach 24h volume if available
        for item in matching_symbols:
            item["volume_24h_usdt"] = round(daily_vol_map.get(item["symbol"], 0.0), 2)

        # Ранжирование: сначала по плотности слоя (desc), затем по медиане волатильности (desc)
        matching_symbols.sort(key=lambda x: (x["density_pct"], x["median_vol"]), reverse=True)

        # Сохранение JSON
        try:
            with open(self.output_json_path, "w", encoding="utf-8") as f:
                json.dump(matching_symbols, f, indent=4)
            logger.info(f"[Scanner2] Saved {len(matching_symbols)} matching symbols to {self.output_json_path}")
        except Exception as e:
            logger.error(f"[Scanner2] Error saving JSON to {self.output_json_path}: {e}")

        # Генерация форматированного TXT отчета
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            symbols_list = [item["symbol"] for item in matching_symbols]
            json_symbols_inline = json.dumps(symbols_list, ensure_ascii=False)
            json_symbols_multiline = json.dumps(symbols_list, indent=4, ensure_ascii=False)
            lines = [
                "=" * 80,
                "VOLATILITY SCANNER 2 (DENSITY, PERCENTILE & GLOBAL ANTI-DUMP) REPORT",
                f"Generated at: {now_str}",
                f"Settings: TF={timeframe} | Window={window} candles | Target Vol Range=[{min_vol:.1f}% .. {max_vol:.1f}%]",
                f"Criteria: Min Density={minimum_density:.1f}% | Max Single Candle={max_candle_vol_pct or 'None'}% | Min 24h Vol=${min_dayly_vol_usdt or 0:,.0f}",
                f"Anti-Dump: Last Delta={max_last_delta_pct or 0:.1f}% | Min Range Pos={min_range_position_pct or 0:.1f}% | Macro Drift={max_dump_pct or 0:.1f}%",
                "=" * 80,
                f"Found matching symbols: {len(matching_symbols)}",
                "",
                "JSON Symbols List (для app.json):",
                json_symbols_inline,
                "",
                "JSON Symbols List (развернутый):",
                json_symbols_multiline,
                "",
                "Detailed Breakdown:",
                "-" * 80
            ]

            for rank, item in enumerate(matching_symbols, 1):
                vol_str = f"${item.get('volume_24h_usdt', 0):,.0f}"
                history_str = ", ".join(f"{v:.1f}%" for v in item["candle_vols"])
                lines.append(
                    f"#{rank:<2} {item['symbol']:<12} | Density: {item['density_pct']}% ({item['matching_candles']}/{item['total_candles']} candles) | "
                    f"Med(P50): {item['median_vol']}% | MaxVol: {item['max_candle_vol']}% | LastVol: {item['last_candle_vol']}% | "
                    f"DeltaLast: {item['last_delta_pct']:+5.1f}% | Pos: {item['range_pos_pct']}% | Drift: {item['drift_pct']:+6.1f}% | 24h Vol: {vol_str}"
                )
                lines.append(f"    History ({item['total_candles']} candles): [{history_str}]")
                lines.append("-" * 80)

            with open(self.output_txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logger.info(f"[Scanner2] Saved detailed TXT report to {self.output_txt_path}")
        except Exception as e:
            logger.error(f"[Scanner2] Error formatting TXT report: {e}")

        return self.output_txt_path

async def run_scanner2():
    scanner = VolatilityScanner2()
    try:
        path = await scanner.scan()
        return path
    finally:
        if scanner.client:
            await scanner.client.shutdown()

if __name__ == "__main__":
    asyncio.run(run_scanner2())
