# ==============================================================================
# Path: SCREENERS/chop.py
# Role: Надстройка над сканером волатильности для поиска "плоских" боковиков
# ==============================================================================
import asyncio
import time
from pathlib import Path
from c_log import UnifiedLogger
from c_utils import Utils
from consts import DATA_DIR, CACHE_DIR
from API.BINANCE.public import BinancePublic
from _screener_utils import fetch_klines

logger = UnifiedLogger("FlatScreener")


import math

def calculate_chop(klines: list, window: int) -> dict:
    if not klines or len(klines) < window:
        return {"is_sideways": False}
        
    try:
        # Take only the last 'window' candles if we have more
        klines = klines[-window:]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        
        trs = []
        for i in range(len(klines)):
            h = highs[i]
            l = lows[i]
            if i == 0:
                trs.append(h - l)
            else:
                prev_c = closes[i-1]
                trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
                
        sum_tr = sum(trs)
        max_h = max(highs)
        min_l = min(lows)
        total_range = max_h - min_l
        
        if total_range == 0 or sum_tr == 0:
            return {"is_sideways": False}
            
        chop = 100 * math.log10(sum_tr / total_range) / math.log10(window)
        
        return {
            "is_sideways": True,
            "chop": chop
        }
    except Exception as e:
        logger.error(f"Error calculating CHOP: {e}")
        return {"is_sideways": False}

async def main():
    logger.info("Starting CHOP Screener (Choppiness Index)...")
    
    app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
    scanner_cfg = app_cfg.get("volatility_scanner", {})
    timeframe = scanner_cfg.get("timeframe", "1w")
    
    chop_cfg = app_cfg.get("chop_scanner", {})
    window = chop_cfg.get("window", 14)
    min_chop_value = chop_cfg.get("min_chop_value", 61.8)
    
    cache_lifetime_hours = scanner_cfg.get("cache_lifetime_hours", 1)
    cache_lifetime_sec = int(cache_lifetime_hours * 3600)
    
    cache_dir = CACHE_DIR / f"flat_klines_{timeframe}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    vol_file = CACHE_DIR / "volatile_symbols.txt"
    vol_json_file = CACHE_DIR / "volatile_symbols.json"
    
    symbols_to_check = []
    vol_data_map = {}
    
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
    # We fetch enough candles for the window
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
            
        stats = calculate_chop(klines, window)
        if stats["is_sideways"] and stats["chop"] >= min_chop_value:
            flat_item = dict(original_item)
            flat_item["chop"] = round(stats["chop"], 2)
            flat_symbols.append(flat_item)
            
    flat_symbols.sort(key=lambda x: x["chop"], reverse=True)
    
    output_file = CACHE_DIR / "chop_symbols.txt"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("# ==========================================\n")
        f.write("# CHOP SCREENER (Choppiness Index)\n")
        f.write(f"# Найдено символов: {len(flat_symbols)}\n")
        f.write("# \n")
        f.write(f"# Условия фильтрации (Таймфрейм: {timeframe}, Окно: {window}):\n")
        f.write(f"# - CHOP >= {min_chop_value} (Сильный боковик > 61.8)\n")
        f.write("# ==========================================\n\n")
        f.write(f"{'Symbol':<15} | {'Volatility':<12} | {'CHOP Index'}\n")
        f.write("-" * 50 + "\n")
        for item in flat_symbols:
            f.write(f"{item['symbol']:<15} | {item['volatility']:>5.2f}%       | {item['chop']:.2f}\n")
            
    logger.info(f"Cache stats: {cache_hits} / {len(symbols_to_check)} symbols loaded from cache.")
    logger.info(f"CHOP Screener finished! Found {len(flat_symbols)} symbols out of {len(symbols_to_check)}.")
    logger.info(f"Results saved to {output_file}")
if __name__ == "__main__":
    asyncio.run(main())
