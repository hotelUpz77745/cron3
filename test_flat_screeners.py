# ==============================================================================
# Path: test_flat_screeners.py
# Role: Надстройка над сканером волатильности для поиска "плоских" боковиков
# ==============================================================================
import asyncio
import time
from pathlib import Path
from c_log import UnifiedLogger
from c_utils import Utils
from consts import DATA_DIR, CACHE_DIR
from API.BINANCE.public import BinancePublic

logger = UnifiedLogger("FlatScreener")

async def fetch_klines(symbol: str, timeframe: str, window: int, cache_dir: Path, cache_lifetime: int) -> tuple[list, bool]:
    # Check cache
    cache_file = cache_dir / f"{symbol}.json"
    if cache_file.exists():
        if time.time() - cache_file.stat().st_mtime < cache_lifetime:
            try:
                data = Utils.read_json_file(cache_file)
                if data and len(data) >= window:
                    return data, True
            except Exception:
                pass
                
    data = await BinancePublic.get_klines(symbol, timeframe, window)
    if data:
        Utils.write_json_file(cache_file, data)
        return data, False
    return [], False

def calculate_flatness(klines: list) -> float:
    if not klines or len(klines) < 2:
        return 1.0 # default to not flat
        
    try:
        # Binance kline format: [open_time, open, high, low, close, volume, ...]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        
        first_open = float(klines[0][1])
        last_close = float(klines[-1][4])
        
        max_high = max(highs)
        min_low = min(lows)
        
        total_range = max_high - min_low
        if total_range == 0:
            return 0.0 # Perfectly flat
            
        net_move = abs(last_close - first_open)
        return net_move / total_range
    except Exception as e:
        logger.error(f"Error calculating flatness: {e}")
        return 1.0

async def main():
    logger.info("Starting Flat Screener based on Volatility Screener results...")
    
    app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
    scanner_cfg = app_cfg.get("volatility_scanner", {})
    timeframe = scanner_cfg.get("timeframe", "1w")
    window = scanner_cfg.get("window", 12)
    cache_lifetime_hours = scanner_cfg.get("cache_lifetime_hours", 1)
    cache_lifetime_sec = int(cache_lifetime_hours * 3600)
    
    cache_dir = CACHE_DIR / f"flat_klines_{timeframe}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    flat_cfg = app_cfg.get("flat_scanner", {})
    max_flatness_pct = flat_cfg.get("max_flatness_pct", 35.0)
    max_flatness_ratio = max_flatness_pct / 100.0
    
    vol_file = CACHE_DIR / "volatile_symbols.txt"
    vol_json_file = CACHE_DIR / "volatile_symbols.json"
    
    symbols_to_check = []
    vol_data_map = {}
    
    # Try txt first, fallback to json
    if vol_file.exists():
        with open(vol_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip() or "Symbol" in line:
                    continue
                parts = line.split("|")
                if len(parts) >= 3:
                    sym = parts[0].strip()
                    vol = float(parts[1].replace("%", "").strip())
                    symbols_to_check.append(sym)
                    vol_data_map[sym] = {"symbol": sym, "volatility": vol}
    elif vol_json_file.exists():
        vol_data = Utils.read_json_file(vol_json_file)
        if vol_data:
            for item in vol_data:
                sym = item["symbol"]
                symbols_to_check.append(sym)
                vol_data_map[sym] = item
    else:
        import sys
        sys.stderr.write("🛑 Отсутствуют исходные данные! Сперва обязательно запустите скринер волатильности (ATR SCREENER).\n")
        sys.exit(1)

    logger.info(f"Loaded {len(symbols_to_check)} symbols from ATR screener.")
    
    flat_symbols = []
    
    tasks = []
    for sym in symbols_to_check:
        tasks.append(fetch_klines(sym, timeframe, window, cache_dir, cache_lifetime_sec))
        
    results = await asyncio.gather(*tasks)
    
    cache_hits = 0
    for idx, (klines, is_cached) in enumerate(results):
        if is_cached:
            cache_hits += 1
            
        sym = symbols_to_check[idx]
        original_item = vol_data_map[sym]
        
        if not klines:
            continue
            
        flatness = calculate_flatness(klines)
        if flatness < max_flatness_ratio:
            flat_item = dict(original_item)
            flat_item["flatness"] = round(flatness, 4)
            flat_symbols.append(flat_item)
            
    flat_symbols.sort(key=lambda x: x["volatility"], reverse=True)
    
    output_file = CACHE_DIR / "flat_symbols.txt"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("# ==========================================\n")
        f.write("# FLAT SCREENER RESULTS\n")
        f.write(f"# Найдено символов: {len(flat_symbols)}\n")
        f.write("# \n")
        f.write("# FLATNESS (Степень боковика):\n")
        f.write("# Значение от 0.0 до 1.0. Чем ближе к 0, тем идеальнее пара стоит в волатильном коридоре (боковике)\n")
        f.write("# без направленного тренда. Значение показывает отношение чистого смещения цены к ее полному размаху.\n")
        f.write("# ==========================================\n\n")
        f.write(f"{'Symbol':<15} | {'Volatility':<12} | {'Flatness'}\n")
        f.write("-" * 50 + "\n")
        for item in flat_symbols:
            f.write(f"{item['symbol']:<15} | {item['volatility']:>5.2f}%       | {item['flatness']:.4f}\n")
            
    logger.info(f"Cache stats: {cache_hits} / {len(symbols_to_check)} symbols loaded from cache.")
    logger.info(f"Flat Screener finished! Found {len(flat_symbols)} flat symbols out of {len(symbols_to_check)}.")
    logger.info(f"Results saved to {output_file}")

if __name__ == "__main__":
    asyncio.run(main())
