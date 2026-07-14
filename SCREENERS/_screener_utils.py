# ==============================================================================
# Path: SCREENERS/_screener_utils.py
# Role: Общие утилиты для скринеров
# ==============================================================================
import time
from pathlib import Path
from c_utils import Utils
from API.BINANCE.public import BinancePublic

async def fetch_klines(symbol: str, timeframe: str, window: int, cache_dir: Path, cache_lifetime: int) -> tuple[list, bool]:
    """
    Умная обертка для BinancePublic.get_klines с файловым кэшированием.
    Возвращает (klines, is_cached).
    """
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
