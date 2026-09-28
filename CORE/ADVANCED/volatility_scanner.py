# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\ADVANCED\volatility_scanner.py
# Role: volatility_scanner.py module

# ==============================================================================
# Path: CORE/ADVANCED/volatility_scanner.py
# python -m CORE.ADVANCED.volatility_scanner
# Role: Сканер волатильности для фьючерсов Binance
# ==============================================================================

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from c_log import UnifiedLogger
from consts import DATA_DIR
from c_utils import Utils
from API.BINANCE.public import BinancePublic
from API.BINANCE.client_prod import BinanceClient

logger = UnifiedLogger("VolatilityScanner")

class VolatilityScanner:
    def __init__(self, client=None):
        self.config_path = DATA_DIR / "app.json"
        self.output_path = DATA_DIR / "volatile_symbols.json"
        self.output_txt_path = DATA_DIR / "volatile_symbols.txt"
        
        self.client = client
        self.semaphore = asyncio.Semaphore(10)

    def _get_config(self):
        default_cfg = {
            "timeframe": "1w",
            "window": 8,
            "min_volatility_pct": 15.0,
            "max_volatility_pct": None,
            "strict_window": True,
            "min_dayly_vol_usdt": None
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
                klines = await BinancePublic.get_klines(symbol, timeframe, window)
                if not klines and self.client:
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
        min_dayly_vol_usdt = cfg.get("min_dayly_vol_usdt")
        if min_dayly_vol_usdt is not None: min_dayly_vol_usdt = float(min_dayly_vol_usdt)
        
        logger.info(f"[Scanner] Starting scan. TF={timeframe}, window={window}, min_vol={min_vol}%, max_vol={max_vol}%, strict={strict_window}, min_dayly_vol_usdt={min_dayly_vol_usdt}")
        
        symbols = await BinancePublic.get_perp_symbols()
        if not symbols:
            logger.error("[Scanner] Failed to fetch symbols.")
            return None
            
        logger.info(f"[Scanner] Found {len(symbols)} USDT-M Perpetual symbols.")
        
        if min_dayly_vol_usdt is not None:
            logger.info(f"[Scanner] Filtering symbols by min_dayly_vol_usdt >= {min_dayly_vol_usdt}")
            tickers = await BinancePublic.get_24h_ticker()
            if tickers:
                vol_map = {t["symbol"]: float(t.get("quoteVolume", 0.0)) for t in tickers}
                symbols = [s for s in symbols if vol_map.get(s, 0.0) >= min_dayly_vol_usdt]
                logger.info(f"[Scanner] {len(symbols)} symbols passed volume filter.")
            else:
                logger.warning("[Scanner] Failed to fetch 24h tickers, skipping volume filter.")
        
        tasks = []
        for symbol in symbols:
            tasks.append(self._process_symbol(symbol, timeframe, window, min_vol, max_vol, strict_window))
            
        results = await asyncio.gather(*tasks)
        
        matching_symbols = [r for r in results if r is not None]
        
        # Sort by volatility descending
        matching_symbols.sort(key=lambda x: x["volatility"], reverse=True)
        
        try:
            with open(self.output_path, "w", encoding="utf-8") as f:
                json.dump(matching_symbols, f, indent=4)
            logger.info(f"[Scanner] Saved {len(matching_symbols)} matching symbols to {self.output_path}")
        except Exception as e:
            logger.error(f"[Scanner] Error saving to {self.output_path}: {e}")

        # Сохранение TXT отчета с двойными кавычками
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            symbols_list = [item["symbol"] for item in matching_symbols]
            json_symbols_inline = json.dumps(symbols_list, ensure_ascii=False)
            json_symbols_multiline = json.dumps(symbols_list, indent=4, ensure_ascii=False)
            lines = [
                f"Дата: {now_str}",
                f"Количество монет: {len(matching_symbols)}",
                "",
                "JSON Symbols List (для app.json):",
                json_symbols_inline,
                "",
                "JSON Symbols List (развернутый):",
                json_symbols_multiline,
                "",
                "Детализация:",
                "_____"
            ]
            for item in matching_symbols:
                lines.append(f"Символ: {item['symbol']}")
                lines.append(f"Волатильность: {item['volatility']}%")
                lines.append(f"Свечей: {item['candles']}")
                lines.append("_____")
            with open(self.output_txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logger.info(f"[Scanner] Saved TXT report to {self.output_txt_path}")
        except Exception as e:
            logger.error(f"[Scanner] Error saving TXT: {e}")
            
        return self.output_path

async def run_scanner():
    scanner = VolatilityScanner()
    try:
        path = await scanner.scan()
        return path
    finally:
        if scanner.client:
            await scanner.client.shutdown()

if __name__ == "__main__":
    asyncio.run(run_scanner())
