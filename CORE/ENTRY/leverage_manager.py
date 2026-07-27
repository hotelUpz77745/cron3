# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\ENTRY\leverage_manager.py
# Role: leverage_manager.py module

# ==============================================================================
# Path: CORE/leverage_manager.py
# Role: Кеширование и установка плеча и типа маржи
# ==============================================================================

from typing import Dict
from c_log import UnifiedLogger
from c_utils import Utils
from consts import CACHE_DIR

logger = UnifiedLogger("LeverageManager")
CACHE_FILE = CACHE_DIR / "leverage_cache.json"

class LeverageManager:
    def __init__(self):
        # Кэш персистентный, сохраняется в CACHE
        self._cache: Dict[str, bool] = self._load_cache()

    def _load_cache(self) -> Dict[str, bool]:
        if CACHE_FILE.exists():
            return Utils.read_json_file(CACHE_FILE) or {}
        return {}

    def _save_cache(self):
        Utils.write_json_file(CACHE_FILE, self._cache)

    async def set_leverage_and_margin(self, client, symbol: str, side_cfg: dict):
        """
        Извлекает настройки из side_cfg и устанавливает плечо и тип маржи для символа. 
        Скипает API вызов, если значения уже кешированы.
        """
        leverage = int(side_cfg["leverage"])
        margin_type = str(side_cfg["margin_type"]).upper()
        
        cache_key = f"{symbol}_{margin_type}_{leverage}"
        
        if self._cache.get(cache_key):
            # Уже установлено в этом рантайме
            return
        
        # Установка типа маржи
        margin_res = await client.set_margin_type(symbol, margin_type)
        if not margin_res.success:
            # Binance возвращает ошибку, если тип маржи уже установлен (No need to change margin type.)
            # Это можно игнорировать как ошибку, но запишем лог
            if margin_res.error_msg and "No need to change" in margin_res.error_msg:
                logger.debug(f"[{symbol}] Margin type {margin_type} is already set on exchange.")
            else:
                logger.warning(f"[{symbol}] Failed to set margin type {margin_type}: {margin_res.error_msg}")
                
        # Установка плеча
        leverage_res = await client.set_leverage(symbol, leverage)
        if not leverage_res.success:
            logger.error(f"[{symbol}] Failed to set leverage {leverage}: {leverage_res.error_msg}")
        else:
            logger.info(f"[{symbol}] Successfully set leverage to {leverage} and margin type to {margin_type}")
            # Кешируем успешную установку и сохраняем на диск
            self._cache[cache_key] = True
            self._save_cache()
