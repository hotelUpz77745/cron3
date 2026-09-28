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
    3. Запрет дурной свечи (High Vol Density Ceiling): отсекает монеты, у которых плотность свечей
       выше порога high_vol_threshold_pct превышает max_high_vol_density_pct (отсекает памп-дампы).
    4. Ранжирование по перцентилю и плотности слоя.
    """
    def __init__(self, client=None):
        self.config_path = DATA_DIR / "app.json"
        self.output_json_path = DATA_DIR / "volatile_symbols2.json"
        self.output_txt_path = DATA_DIR / "volatile_symbols2.txt"
        self.client = client
        self.semaphore = asyncio.Semaphore(10)

    def _normalize_layers(self, cfg: dict) -> list[dict]:
        """
        Нормализует слои / диапазоны волатильности из конфига.
        Поддерживает:
        - "layers": [...] или {...}
        - "ranges": [...] или {...}
        - Fallback на старые плоские ключи: min_volatility_pct, max_volatility_pct, minimum_density, high_vol_threshold_pct, max_high_vol_density_pct
        """
        raw_layers = cfg.get("layers") or cfg.get("ranges")
        normalized = []

        if isinstance(raw_layers, list):
            for idx, item in enumerate(raw_layers):
                if isinstance(item, dict):
                    entry = dict(item)
                    if "name" not in entry:
                        entry["name"] = f"layer_{idx + 1}"
                    normalized.append(entry)
        elif isinstance(raw_layers, dict):
            for name, item in raw_layers.items():
                if isinstance(item, dict):
                    entry = dict(item)
                    entry["name"] = name
                    normalized.append(entry)

        if not normalized:
            # Fallback на плоский формат параметров
            min_v = cfg.get("min_volatility_pct", 20.0)
            max_v = cfg.get("max_volatility_pct", 40.0)
            min_d = cfg.get("minimum_density", 50.0)
            normalized.append({
                "name": "target_corridor",
                "range": [min_v, max_v],
                "min_density": min_d
            })
            high_th = cfg.get("high_vol_threshold_pct")
            max_hd = cfg.get("max_high_vol_density_pct")
            if high_th is not None and max_hd is not None:
                normalized.append({
                    "name": "anti_spike",
                    "range": [high_th, None],
                    "max_density": max_hd
                })

        return normalized

    def _get_config(self) -> dict:
        default_cfg = {
            "timeframe": "1w",
            "window": 12,
            "strict_window": True,
            "min_dayly_vol_usdt": 3000000.0,
            "last_candle": True,
            "layers": [
                {
                    "name": "target_corridor",
                    "range": [20.0, 40.0],
                    "min_density": 50.0
                },
                {
                    "name": "anti_spike",
                    "range": [70.0, None],
                    "max_density": 5.0
                }
            ]
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

    @staticmethod
    def _in_range(val: float, r_min: float | None, r_max: float | None) -> bool:
        if r_min is not None and val < r_min:
            return False
        if r_max is not None and val > r_max:
            return False
        return True

    async def _process_symbol(
        self,
        symbol: str,
        timeframe: str,
        window: int,
        strict_window: bool,
        layers: list[dict],
        global_last_candle: bool = True
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
                for k in klines:
                    high = float(k.get("high", 0.0))
                    low = float(k.get("low", 0.0))
                    if low > 0 and high >= low:
                        vol = ((high / low) - 1.0) * 100.0
                        vols.append(vol)

                if not vols:
                    return None

                total_count = len(vols)
                last_candle_vol = vols[-1]

                layer_results = []
                for idx, layer in enumerate(layers):
                    name = layer.get("name", f"layer_{idx + 1}")
                    r = layer.get("range", [None, None])
                    r_min = float(r[0]) if len(r) > 0 and r[0] is not None else None
                    r_max = float(r[1]) if len(r) > 1 and r[1] is not None else None

                    # Подсчет свечей, попавших в диапазон слоя
                    match_count = sum(1 for v in vols if self._in_range(v, r_min, r_max))
                    density_pct = (match_count / total_count) * 100.0

                    min_d = layer.get("min_density")
                    if min_d is not None and density_pct < float(min_d):
                        return None

                    max_d = layer.get("max_density")
                    if max_d is not None and density_pct > float(max_d):
                        return None

                    # Проверка последней свечи
                    check_lc = layer.get("last_candle")
                    if check_lc is None and idx == 0 and global_last_candle:
                        check_lc = True

                    if check_lc:
                        if not self._in_range(last_candle_vol, r_min, r_max):
                            return None

                    layer_results.append({
                        "name": name,
                        "range": [r_min, r_max],
                        "matching_candles": match_count,
                        "density_pct": round(density_pct, 2),
                        "min_density": min_d,
                        "max_density": max_d
                    })

                # Статистические показатели
                med_vol = statistics.median(vols)
                avg_vol = sum(vols) / total_count
                max_vol_observed = max(vols)
                min_vol_observed = min(vols)

                if total_count >= 4:
                    q = statistics.quantiles(vols, n=4)
                    p25 = q[0]
                    p75 = q[2]
                else:
                    p25 = min(vols)
                    p75 = max(vols)

                primary_density = layer_results[0]["density_pct"] if layer_results else 0.0

                return {
                    "symbol": symbol,
                    "density_pct": primary_density,
                    "layers": layer_results,
                    "total_candles": total_count,
                    "last_candle_vol": round(last_candle_vol, 2),
                    "max_vol": round(max_vol_observed, 2),
                    "min_vol": round(min_vol_observed, 2),
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
        timeframe = cfg.get("timeframe", "1w")
        window = int(cfg.get("window", 12))
        strict_window = bool(cfg.get("strict_window", True))
        global_last_candle = bool(cfg.get("last_candle", True))

        min_dayly_vol_usdt = cfg.get("min_dayly_vol_usdt")
        if min_dayly_vol_usdt is not None:
            min_dayly_vol_usdt = float(min_dayly_vol_usdt)

        layers = self._normalize_layers(cfg)
        if not layers:
            raise ValueError("[Scanner2] No layers/ranges defined in configuration!")

        # Формирование описания слоев для логов и отчета
        layer_descs = []
        for idx, l in enumerate(layers, 1):
            r = l.get("range", [None, None])
            r_low = f"{r[0]:.1f}%" if len(r) > 0 and r[0] is not None else "0%"
            r_high = f"{r[1]:.1f}%" if len(r) > 1 and r[1] is not None else "inf"
            conds = []
            if "min_density" in l and l["min_density"] is not None:
                conds.append(f"MinDens>={l['min_density']}%")
            if "max_density" in l and l["max_density"] is not None:
                conds.append(f"MaxDens<={l['max_density']}%")
            if l.get("last_candle") or (idx == 1 and global_last_candle and l.get("last_candle") is not False):
                conds.append("LastCandle=True")
            layer_descs.append(f"#{idx} '{l.get('name', f'layer_{idx}')}': [{r_low}..{r_high}] ({', '.join(conds)})")

        logger.info(
            f"[Scanner2] Starting multi-layer scan. TF={timeframe}, window={window}, strict={strict_window}, "
            f"min_dayly_vol_usdt={min_dayly_vol_usdt} | Layers: {'; '.join(layer_descs)}"
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
                strict_window=strict_window,
                layers=layers,
                global_last_candle=global_last_candle
            )
            for s in symbols
        ]

        results = await asyncio.gather(*tasks)
        matching_symbols = [r for r in results if r is not None]

        # Attach 24h volume if available
        for item in matching_symbols:
            item["volume_24h_usdt"] = round(daily_vol_map.get(item["symbol"], 0.0), 2)

        # Ранжирование: сначала по плотности основного слоя (desc), затем по медиане волатильности (desc)
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
                "VOLATILITY SCANNER 2 (MULTI-LAYER DENSITY & ANTI-SPIKE) REPORT",
                f"Generated at: {now_str}",
                f"Settings: TF={timeframe} | Window={window} candles | Liquidity: Min 24h Vol=${min_dayly_vol_usdt or 0:,.0f}",
                "Active Layers / Tasks:"
            ]
            for desc in layer_descs:
                lines.append(f"  * {desc}")
            lines.extend([
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
            ])

            for rank, item in enumerate(matching_symbols, 1):
                vol_str = f"${item.get('volume_24h_usdt', 0):,.0f}"
                history_str = ", ".join(f"{v:.1f}%" for v in item["candle_vols"])
                layer_strs = []
                for lr in item.get("layers", []):
                    layer_strs.append(f"{lr['name']}: {lr['density_pct']}% ({lr['matching_candles']}/{item['total_candles']})")
                layers_summary = " | ".join(layer_strs)

                lines.append(
                    f"#{rank:<2} {item['symbol']:<12} | {layers_summary} | "
                    f"Last: {item['last_candle_vol']}% | Med(P50): {item['median_vol']}% | MaxVol: {item['max_vol']}% | 24h Vol: {vol_str}"
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
