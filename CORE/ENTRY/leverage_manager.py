# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\ENTRY\leverage_manager.py
# Role: leverage_manager.py module

# ==============================================================================
# Path: CORE/leverage_manager.py
# Role: Кеширование и установка плеча и типа маржи
# ==============================================================================

import asyncio
from typing import Dict, Optional
from c_log import UnifiedLogger
from c_utils import Utils
from consts import CACHE_DIR

logger = UnifiedLogger("LeverageManager")
CACHE_FILE = CACHE_DIR / "leverage_cache.json"

class LeverageManager:
    def __init__(self):
        # Кэш персистентный, сохраняется в CACHE
        self._cache: Dict[str, bool] = self._load_cache()
        # Кэш максимально доступного плеча по спецификации символов
        self._max_leverage_cache: Dict[str, int] = {}
        # Асинхронный лок для предотвращения параллельных race condition вызовов
        self._lock = asyncio.Lock()

    def _load_cache(self) -> Dict[str, bool]:
        if CACHE_FILE.exists():
            return Utils.read_json_file(CACHE_FILE) or {}
        return {}

    def _save_cache(self):
        Utils.write_json_file(CACHE_FILE, self._cache)

    def is_configured(self, symbol: str, *args, **kwargs) -> bool:
        """Быстрая проверка кеша по чистому имени символа без сетевых вызовов."""
        sym = symbol.upper()
        return bool(self._cache.get(sym))

    async def get_max_available_leverage(self, client, symbol: str) -> Optional[int]:
        """
        Получает максимально доступное плечо для символа по спецификации биржи (/fapi/v1/leverageBracket).
        Кэширует значение в памяти, чтобы не делать повторные сетевые вызовы.
        """
        sym = symbol.upper()
        if sym in self._max_leverage_cache:
            return self._max_leverage_cache[sym]

        try:
            max_lev = await client.get_max_leverage(sym)
            if max_lev and max_lev > 0:
                self._max_leverage_cache[sym] = max_lev
                return max_lev
        except Exception as e:
            logger.error(f"[{symbol}] Ошибка при получении макс. плеча: {e}")
        return None

    async def set_leverage_and_margin(self, client, symbol: str, side_cfg: dict):
        """
        Извлекает настройки из side_cfg и устанавливает плечо и тип маржи для символа.
        Берет минимальное плечо между заданным в конфиге и максимально доступным по спецификации.
        Ключ в кеше строго чистое имя тикера (например, 'GRASSUSDT').
        Мгновенно скипает API вызов, если символ уже закеширован.
        Для изменения плеча/маржи пользователю достаточно удалить имя символа из кеша.
        """
        sym = symbol.upper()
        configured_leverage = int(side_cfg.get("leverage", 20))
        margin_type = str(side_cfg.get("margin_type", "CROSSED")).upper()

        # 1. БЫСТРЫЙ ВЫХОД: если символ уже в кэше — мгновенный возврат (0 сетевых запросов)
        if self._cache.get(sym):
            side_cfg["leverage"] = configured_leverage
            return

        async with self._lock:
            # 2. Повторная проверка под блокировкой (Double-Checked Locking)
            if self._cache.get(sym):
                side_cfg["leverage"] = configured_leverage
                return

            # Безопасно получаем максимально доступное плечо по спецификации биржи
            max_leverage = await self.get_max_available_leverage(client, sym)
            if max_leverage and max_leverage > 0:
                effective_leverage = min(configured_leverage, max_leverage)
                if effective_leverage < configured_leverage:
                    logger.warning(
                        f"[{sym}] Configured leverage {configured_leverage}x exceeds max available {max_leverage}x. "
                        f"Clamped to minimum: {effective_leverage}x"
                    )
            else:
                effective_leverage = configured_leverage

            side_cfg["leverage"] = effective_leverage

            # 3. Установка типа маржи
            margin_res = await client.set_margin_type(sym, margin_type)
            if not margin_res.success:
                # Binance возвращает ошибку, если тип маржи уже установлен (No need to change margin type.)
                if margin_res.error_msg and "No need to change" in margin_res.error_msg:
                    logger.debug(f"[{sym}] Margin type {margin_type} is already set on exchange.")
                else:
                    logger.warning(f"[{sym}] Failed to set margin type {margin_type}: {margin_res.error_msg}")

            # 4. Установка плеча
            leverage_res = await client.set_leverage(sym, effective_leverage)
            if not leverage_res.success:
                logger.error(f"[{sym}] Failed to set leverage {effective_leverage}: {leverage_res.error_msg}")
            else:
                logger.info(f"[{sym}] Successfully set leverage to {effective_leverage}x and margin type to {margin_type}")
                # Кешируем успешную установку по чистому имени символа и сохраняем на диск
                self._cache[sym] = True
                self._save_cache()
