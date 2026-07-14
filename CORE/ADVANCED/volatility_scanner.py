# ==============================================================================
# Path: CORE/ADVANCED/volatility_scanner.py
# python -m CORE.ADVANCED.volatility_scanner
# Role: Сканер волатильности для фьючерсов Binance
# ==============================================================================

import asyncio
import json
from pathlib import Path
from c_log import UnifiedLogger
from consts import DATA_DIR, CACHE_DIR
from c_utils import Utils
from API.BINANCE.public import BinancePublic
from API.BINANCE.client import BinanceClient

logger = UnifiedLogger("VolatilityScanner")

class VolatilityScanner:
    def __init__(self):
        self.config_path = DATA_DIR / "app.json"
        self.output_path = CACHE_DIR / "volatile_symbols.txt"
        
        # We need a BinanceClient to get klines. In an isolated script, we can initialize it without keys.
        self.client = BinanceClient("", "")
        self.semaphore = asyncio.Semaphore(5)

    def _get_config(self):
        default_cfg = {
            "timeframe": "1w",
            "window": 8,
            "min_volatility_pct": 15.0,
            "max_volatility_pct": None,
            "strict_window": True
        }
        
        if self.config_path.exists():
            try:
                data = Utils.read_json_file(self.config_path)
                if "volatility_scanner" in data:
                    default_cfg.update(data["volatility_scanner"])
            except Exception as e:
                logger.error(f"[Scanner] Error reading config: {e}")
                
        return default_cfg

    async def _process_symbol(self, symbol, timeframe, window, min_vol, max_vol, strict_window):
        async with self.semaphore:
            try:
                klines = await self.client.get_klines(symbol, timeframe, window)
                if not klines:
                    return None
                    
                if strict_window and len(klines) < window:
                    return None
                    
                total_vol = 0.0
                count = 0
                for k in klines:
                    high = k.get("high", 0.0)
                    low = k.get("low", 0.0)
                    if low > 0:
                        vol = ((high / low) - 1) * 100
                        total_vol += vol
                        count += 1
                        
                if count == 0:
                    return None
                    
                avg_vol = total_vol / count
                if min_vol is not None and avg_vol < min_vol:
                    return None
                    
                if max_vol is not None and avg_vol > max_vol:
                    return None
                
                return {
                    "symbol": symbol,
                    "volatility": round(avg_vol, 2),
                    "candles": count
                }
                    
            except Exception as e:
                logger.error(f"[Scanner] Error processing {symbol}: {e}")
                
        return None

    async def scan(self):
        cfg = self._get_config()
        timeframe = cfg.get("timeframe", "1w")
        window = int(cfg.get("window", 8))
        min_vol = cfg.get("min_volatility_pct")
        if min_vol is not None: min_vol = float(min_vol)
        
        max_vol = cfg.get("max_volatility_pct")
        if max_vol is not None: max_vol = float(max_vol)
        
        strict_window = cfg.get("strict_window", True)
        
        logger.info(f"[Scanner] Starting scan. TF={timeframe}, window={window}, min_vol={min_vol}%, max_vol={max_vol}%, strict={strict_window}")
        
        symbols = await BinancePublic.get_perp_symbols()
        if not symbols:
            logger.error("[Scanner] Failed to fetch symbols.")
            return None
            
        logger.info(f"[Scanner] Found {len(symbols)} USDT-M Perpetual symbols.")
        
        tasks = []
        for symbol in symbols:
            tasks.append(self._process_symbol(symbol, timeframe, window, min_vol, max_vol, strict_window))
            
        results = await asyncio.gather(*tasks)
        
        matching_symbols = [r for r in results if r is not None]
        
        # Sort by volatility descending
        matching_symbols.sort(key=lambda x: x["volatility"], reverse=True)
        
        try:
            with open(self.output_path, "w", encoding="utf-8") as f:
                f.write(f"# ATR VOLATILITY SCREENER\n")
                f.write(f"# Найдено символов: {len(matching_symbols)}\n\n")
                f.write(f"{'Symbol':<15} | {'Volatility':<12} | {'Candles'}\n")
                f.write("-" * 45 + "\n")
                for item in matching_symbols:
                    f.write(f"{item['symbol']:<15} | {item['volatility']:>5.2f}%       | {item['candles']}\n")
            logger.info(f"[Scanner] Saved {len(matching_symbols)} matching symbols to {self.output_path}")
        except Exception as e:
            logger.error(f"[Scanner] Error saving to {self.output_path}: {e}")
            
        return self.output_path

async def run_scanner():
    scanner = VolatilityScanner()
    try:
        path = await scanner.scan()
        return path
    finally:
        await scanner.client.shutdown()

if __name__ == "__main__":
    asyncio.run(run_scanner())
