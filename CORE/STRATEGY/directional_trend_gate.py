# ==============================================================================
# Path: CORE/STRATEGY/directional_trend_gate.py
# Role: Однонаправленный макро-шлюз первого ордера (Directional Trend-Gate)
# ==============================================================================

from typing import Dict, Any, Optional, Tuple
from c_log import UnifiedLogger
from CORE.STRATEGY.momentum_engine import MomentumEngine

logger = UnifiedLogger("DirectionalTrendGate")


class DirectionalTrendGate:
    """
    Однонаправленный шлюз первого ордера (Directional Trend-Gate):
    Фильтрует открытие новых торговых циклов (L0 / первый ордер сетки) строго
    по тренду старшего таймфрейма (по умолчанию 1d Dual EMA 10/30).

    Правила шлюза:
    1. Trend == 'UP':
       - Разрешен ТОЛЬКО LONG.
       - Вход в SHORT жестко блокируется (защита от контртрендовой ловли ножей).
    2. Trend == 'DOWN':
       - Разрешен ТОЛЬКО SHORT.
       - Вход в LONG жестко блокируется.
    3. Trend in ('FLAT', 'UNSTABLE', 'NEUTRAL'):
       - Блокируются ОБЕ стороны (скип и выжидание направленного тренда).
    4. Если directional_trend_gate отключен (enabled: false) или отсутствует:
       - Разрешены обе стороны (100% обратная совместимость).
    """

    @classmethod
    async def is_side_allowed(
        cls,
        symbol: str,
        side: str,
        gate_cfg: Optional[Dict[str, Any]],
        client: Optional[Any] = None,
        strategy_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Проверяет, разрешено ли открытие нового цикла (L0) для указанной стороны (LONG/SHORT).
        Возвращает (is_allowed: bool, reason: str).
        """
        if not gate_cfg or not isinstance(gate_cfg, dict) or not gate_cfg.get("enabled", False):
            return True, "Trend-Gate is disabled (both sides allowed)"

        side_u = side.upper().strip()
        ema_cfg = gate_cfg.get("auto_ema")
        if not ema_cfg or not isinstance(ema_cfg, dict):
            # Канонический дефолт: 1d Dual EMA 10/30
            ema_cfg = {
                "timeframe": "1d",
                "sma_fast": 10,
                "sma_slow": 30,
                "confirmation_candles": 3,
                "require_rising": True,
                "trend_positive": True,
            }

        trend_st = await MomentumEngine.get_ema_trend_state(
            symbol=symbol,
            ema_cfg=ema_cfg,
            client=client,
        )

        strat_tag = f"[{strategy_id}] " if strategy_id else ""

        if trend_st == "UP":
            if side_u == "LONG":
                return True, f"Trend is UP -> LONG allowed"
            else:
                return False, f"{strat_tag}Directional Trend-Gate: Trend is UP -> SHORT blocked"

        elif trend_st == "DOWN":
            if side_u == "SHORT":
                return True, f"Trend is DOWN -> SHORT allowed"
            else:
                return False, f"{strat_tag}Directional Trend-Gate: Trend is DOWN -> LONG blocked"

        else:
            # FLAT, UNSTABLE, NEUTRAL и консолидация
            if gate_cfg.get("allow_flat", False):
                return True, f"Trend is {trend_st} (FLAT) -> both sides allowed"
            return False, f"{strat_tag}Directional Trend-Gate: Trend is {trend_st} (FLAT/NEUTRAL) -> both sides skipped, waiting for trend"
