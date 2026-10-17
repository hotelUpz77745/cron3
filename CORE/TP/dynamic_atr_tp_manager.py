# ==============================================================================
# Path: CORE/TP/dynamic_atr_tp_manager.py
# Role: Менеджер динамического Take-Profit на основе волатильности (ATR%) с деэскалацией
# ==============================================================================

from typing import Tuple, Optional
from c_log import UnifiedLogger
from CORE._utils import TradeMath
from CORE.SERVICES.VOLATILITY.atr_provider import ATRProvider

logger = UnifiedLogger("DynamicATRTP")


class DynamicATRTakeProfitManager:
    """
    Менеджер динамического Take-Profit на основе волатильности (ATR%).
    Параметры timeframe, period и cache_ttl_sec считываются напрямую из конфигурации стратегии.
    Поддерживает инвертированный волатильный демпфер (деэскалацию множителей) и гарантированный пол.
    """

    @classmethod
    async def calculate_tp_price(
        cls,
        client: Optional[object],
        symbol: str,
        side: str,
        avg_entry_price: float,
        active_level: str,
        cfg: dict,
        spec_data: dict,
    ) -> Tuple[Optional[float], float]:
        """
        Рассчитывает целевую цену Take-Profit по ATR с учетом множителя уровня (деэскалации)
        и защитного гарантированного пола (min_guaranteed_floor_pct).
        Возвращает (tp_price, target_tp_pct).
        """
        if not cfg or not cfg.get("enabled", False) or avg_entry_price <= 0:
            return None, 0.0

        # Читаем строго напрямую через [''], без скрытых дефолтов!
        timeframe = str(cfg["timeframe"])
        period = int(cfg["period"])
        cache_ttl = int(cfg["cache_ttl_sec"])
        fallback_atr_pct = float(cfg["fallback_atr_pct"])
        min_floor = float(cfg["min_guaranteed_floor_pct"])
        max_ceil = float(cfg["max_ceiling_pct"])
        multipliers = cfg["level_multipliers"]

        level_str = str(active_level)
        if level_str in multipliers:
            mult = float(multipliers[level_str])
        elif multipliers:
            sorted_keys = sorted(multipliers.keys(), key=lambda x: int(x) if str(x).isdigit() else x)
            mult = float(multipliers[sorted_keys[-1]])
        else:
            raise KeyError(f"level_multipliers is empty in dynamic_atr_tp config for symbol {symbol}")

        atr_pct = await ATRProvider.get_atr_pct(
            client=client,
            symbol=symbol,
            timeframe=timeframe,
            period=period,
            cache_ttl_sec=cache_ttl,
            fallback_atr_pct=fallback_atr_pct,
        )

        raw_tp_pct = atr_pct * mult
        target_tp_pct = max(min_floor, min(max_ceil, raw_tp_pct))
        target_tp_pct = round(target_tp_pct, 4)

        tp_price = TradeMath.calculate_take_profit_price(
            avg_entry_price, target_tp_pct, side, spec_data, symbol
        )
        return tp_price, target_tp_pct
