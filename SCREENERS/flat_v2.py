# ==============================================================================
# Path: SCREENERS/flat_v2.py
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


def analyze_sideways(klines: list, min_alt_touches: int = 2, min_sma_crosses: int = 3) -> dict:
    if not klines or len(klines) < 2:
        return {"is_sideways": False}
        
    try:
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        
        max_high = max(highs)
        min_low = min(lows)
        total_range = max_high - min_low
        
        if total_range == 0:
            return {"is_sideways": False}
            
        # 1. Ping-Pong Method
        top_zone = max_high - (total_range * 0.25)
        bottom_zone = min_low + (total_range * 0.25)
        
        top_touches = 0
        bottom_touches = 0
        last_touch = None
        alternating_touches = 0
        
        for i in range(len(klines)):
            h = highs[i]
            l = lows[i]
            if h >= top_zone:
                if last_touch != 'top':
                    top_touches += 1
                    if last_touch == 'bottom': alternating_touches += 1
                    last_touch = 'top'
            if l <= bottom_zone:
                if last_touch != 'bottom':
                    bottom_touches += 1
                    if last_touch == 'top': alternating_touches += 1
                    last_touch = 'bottom'
                    
        # 2. SMA Crosses Method
        sma = sum(closes) / len(closes)
        crosses = 0
        above = closes[0] > sma
        for c in closes[1:]:
            curr_above = c > sma
            if curr_above != above:
                crosses += 1
                above = curr_above
                
        # Criteria for "Volatile Sideways"
        is_sideways = (alternating_touches >= min_alt_touches) and (crosses >= min_sma_crosses)
        
        return {
            "is_sideways": is_sideways,
            "alt_touches": alternating_touches,
            "sma_crosses": crosses
        }
    except Exception as e:
        logger.error(f"Error calculating sideways: {e}")
        return {"is_sideways": False}

async def main():
    logger.info("Starting V2 Flat Screener (True Volatile Sideways)...")
    
    app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
    scanner_cfg = app_cfg.get("volatility_scanner", {})
    timeframe = scanner_cfg.get("timeframe", "1w")
    window = scanner_cfg.get("window", 12)
    cache_lifetime_hours = scanner_cfg.get("cache_lifetime_hours", 1)
    cache_lifetime_sec = int(cache_lifetime_hours * 3600)
    
    flat_v2_cfg = app_cfg.get("flat_scanner_v2", {})
    min_alt_touches = flat_v2_cfg.get("min_alt_touches", 2)
    min_sma_crosses = flat_v2_cfg.get("min_sma_crosses", 3)
    
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
            
        stats = analyze_sideways(klines, min_alt_touches, min_sma_crosses)
        if stats["is_sideways"]:
            flat_item = dict(original_item)
            flat_item["alt_touches"] = stats["alt_touches"]
            flat_item["sma_crosses"] = stats["sma_crosses"]
            flat_symbols.append(flat_item)
            
    flat_symbols.sort(key=lambda x: x["volatility"], reverse=True)
    
    output_file = CACHE_DIR / "flat_symbols_v2.txt"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("# ==========================================\n")
        f.write("# FLAT SCREENER V2 (Истинный Боковик)\n")
        f.write(f"# Найдено символов: {len(flat_symbols)}\n")
        f.write("# \n")
        f.write("# Условия фильтрации (Таймфрейм: {timeframe}, Окно: {window}):\n")
        f.write("# - Пинг-Понг: Минимум 2 чередующихся касания верхних 25% и нижних 25% диапазона.\n")
        f.write("# - Пила: Минимум 3 пересечения средней цены (SMA).\n")
        f.write("# ==========================================\n\n")
        f.write(f"{'Symbol':<15} | {'Volatility':<12} | {'PingPong':<10} | {'SMA Crosses'}\n")
        f.write("-" * 65 + "\n")
        for item in flat_symbols:
            f.write(f"{item['symbol']:<15} | {item['volatility']:>5.2f}%       | {item['alt_touches']:<10} | {item['sma_crosses']}\n")
            
    logger.info(f"Cache stats: {cache_hits} / {len(symbols_to_check)} symbols loaded from cache.")
    logger.info(f"Flat Screener finished! Found {len(flat_symbols)} flat symbols out of {len(symbols_to_check)}.")
    logger.info(f"Results saved to {output_file}")

if __name__ == "__main__":
    asyncio.run(main())
