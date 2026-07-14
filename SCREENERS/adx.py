# ==============================================================================
# Path: SCREENERS/adx.py
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


def calculate_adx(klines: list, window: int) -> dict:
    if not klines or len(klines) < window * 2:
        return {"is_sideways": False}
        
    try:
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        
        plus_dms = [0.0]
        minus_dms = [0.0]
        trs = [highs[0] - lows[0]]
        
        for i in range(1, len(klines)):
            h = highs[i]
            l = lows[i]
            prev_h = highs[i-1]
            prev_l = lows[i-1]
            prev_c = closes[i-1]
            
            # TR
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)
            
            # Directional Movement
            up_move = h - prev_h
            down_move = prev_l - l
            
            if up_move > down_move and up_move > 0:
                plus_dms.append(up_move)
            else:
                plus_dms.append(0.0)
                
            if down_move > up_move and down_move > 0:
                minus_dms.append(down_move)
            else:
                minus_dms.append(0.0)
                
        # Wilder's Smoothing Function
        def wilder_smooth(values, n):
            smoothed = [0.0] * len(values)
            smoothed[n] = sum(values[1:n+1])
            for i in range(n+1, len(values)):
                smoothed[i] = smoothed[i-1] - (smoothed[i-1] / n) + values[i]
            return smoothed
            
        smooth_tr = wilder_smooth(trs, window)
        smooth_plus = wilder_smooth(plus_dms, window)
        smooth_minus = wilder_smooth(minus_dms, window)
        
        dxs = [0.0] * len(klines)
        for i in range(window, len(klines)):
            tr = smooth_tr[i]
            if tr == 0:
                continue
            plus_di = 100 * (smooth_plus[i] / tr)
            minus_di = 100 * (smooth_minus[i] / tr)
            
            di_sum = plus_di + minus_di
            if di_sum > 0:
                dxs[i] = 100 * abs(plus_di - minus_di) / di_sum
                
        # Calculate ADX
        adx = [0.0] * len(klines)
        # First ADX is the simple average of the first 'window' DX values
        start_idx = window * 2 - 1
        if start_idx < len(dxs):
            adx[start_idx] = sum(dxs[window:window*2]) / window
            for i in range(start_idx + 1, len(dxs)):
                adx[i] = ((adx[i-1] * (window - 1)) + dxs[i]) / window
                
        last_adx = adx[-1]
        return {
            "is_sideways": True if last_adx > 0 else False,
            "adx": last_adx
        }
    except Exception as e:
        logger.error(f"Error calculating ADX: {e}")
        return {"is_sideways": False}

async def main():
    logger.info("Starting ADX Screener (Average Directional Index)...")
    
    app_cfg = Utils.read_json_file(DATA_DIR / "app.json")
    scanner_cfg = app_cfg.get("volatility_scanner", {})
    timeframe = scanner_cfg.get("timeframe", "1w")
    
    adx_cfg = app_cfg.get("adx_scanner", {})
    window = adx_cfg.get("window", 14)
    max_adx_value = adx_cfg.get("max_adx_value", 25.0)
    
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
    # Fetch window*3 candles for ADX to stabilize
    fetch_window = window * 3
    for sym in symbols_to_check:
        tasks.append(fetch_klines(sym, timeframe, fetch_window, cache_dir, cache_lifetime_sec))
        
    results = await asyncio.gather(*tasks)
    
    cache_hits = 0
    for idx, (klines, is_cached) in enumerate(results):
        if is_cached:
            cache_hits += 1
            
        sym = symbols_to_check[idx]
        original_item = vol_data_map[sym]
        
        if not klines:
            continue
            
        stats = calculate_adx(klines, window)
        if stats["is_sideways"] and 0 < stats["adx"] <= max_adx_value:
            flat_item = dict(original_item)
            flat_item["adx"] = round(stats["adx"], 2)
            flat_symbols.append(flat_item)
            
    flat_symbols.sort(key=lambda x: x["adx"])
    
    output_file = CACHE_DIR / "adx_symbols.txt"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("# ==========================================\n")
        f.write("# ADX SCREENER (Average Directional Index)\n")
        f.write(f"# Найдено символов: {len(flat_symbols)}\n")
        f.write("# \n")
        f.write(f"# Условия фильтрации (Таймфрейм: {timeframe}, Окно: {window}):\n")
        f.write(f"# - ADX <= {max_adx_value} (Отсутствие направленного тренда)\n")
        f.write("# ==========================================\n\n")
        f.write(f"{'Symbol':<15} | {'Volatility':<12} | {'ADX'}\n")
        f.write("-" * 50 + "\n")
        for item in flat_symbols:
            f.write(f"{item['symbol']:<15} | {item['volatility']:>5.2f}%       | {item['adx']:.2f}\n")
            
    logger.info(f"Cache stats: {cache_hits} / {len(symbols_to_check)} symbols loaded from cache.")
    logger.info(f"ADX Screener finished! Found {len(flat_symbols)} symbols out of {len(symbols_to_check)}.")
    logger.info(f"Results saved to {output_file}")

if __name__ == "__main__":
    asyncio.run(main())
