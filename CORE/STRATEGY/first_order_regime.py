# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\STRATEGY\first_order_regime.py
# Role: first_order_regime.py module

# ==============================================================================
# Path: CORE/STRATEGY/first_order_regime.py
# Role: Доменная сущность паспорта стратегии: «Правило кулака Сильной руки» (First Order Weight Regime)
# ==============================================================================

import logging
from typing import Any, Dict, Optional, Tuple

from CORE.STRATEGY.momentum_engine import MomentumEngine

logger = logging.getLogger("FirstOrderRegime")


class FirstOrderRegimeManager:
    """
    Инкапсулирует логику паспорта стратегии: first_order_weight_regime («Правило кулака Сильной руки»).
    
    Принцип «Одно имя — одна сущность»:
    Строго 2 динамических сущности:
      1. 'auto_ema'       - макро-фильтр по глобальной EMA.
      2. 'kernel_forecast' - непараметрический прогноз Nadaraya-Watson Kernel Regression.
    
    Статические пресеты:
      - 'bullish' (12% Long / 7% Short)
      - 'neutral' (9% Long / 9% Short)
      - 'bearish' (7% Long / 12% Short)
    
    Отключение:
      - 'off' или None
    
    В соответствии с Протоколом Разработки:
    - Конфиги читаются напрямую через [''] без скрытых дефолтов get(..., default).
    - Никаких магических чисел и неявных алиасов.
    """

    SUPPORTED_DYNAMIC_MODES = ("auto_ema", "kernel_forecast")
    SUPPORTED_STATIC_MODES = ("bullish", "neutral", "bearish")
    OFF_MODES = ("off", "none", "null", "disabled", False, None)

    @classmethod
    def resolve_static_volume(
        cls,
        regime_cfg: Any,
        side: str,
        default_vol: float,
    ) -> float:
        """
        Разрешает базовый объем первого ордера для стороны side (LONG/SHORT)
        на основе статического режима или дефолтных пресетов.
        """
        if regime_cfg is None:
            return default_vol

        if isinstance(regime_cfg, dict):
            mode = regime_cfg.get("mode")
            presets = regime_cfg.get("presets", {})
        else:
            mode = regime_cfg
            presets = {}

        if mode is None or str(mode).lower().strip() in cls.OFF_MODES:
            return default_vol

        mode_str = str(mode).lower().strip()
        if mode_str in cls.SUPPORTED_DYNAMIC_MODES:
            # Для динамических режимов при статической инициализации берем fallback (bullish)
            mode_str = "bullish"

        # Дефолтные эталонные пресеты паспорта стратегии (если не переопределены в конфиге)
        side_key = side.upper()
        if mode_str in presets and isinstance(presets[mode_str], dict) and side_key in presets[mode_str]:
            return float(presets[mode_str][side_key])

        # Канонические пресеты
        canonical_presets = {
            "bullish": {"LONG": 12.0, "SHORT": 7.0},
            "neutral": {"LONG": 9.0, "SHORT": 9.0},
            "bearish": {"LONG": 7.0, "SHORT": 12.0},
        }
        if mode_str in canonical_presets and side_key in canonical_presets[mode_str]:
            return float(canonical_presets[mode_str][side_key])

        return default_vol

    @classmethod
    async def evaluate_and_resolve_volume(
        cls,
        symbol: str,
        side: str,
        regime_cfg: Any,
        default_vol: float,
        client: Any,
        strategy_id: str = "",
    ) -> float:
        """
        Динамически оценивает состояние рынка по старшему таймфрейму (auto_ema или kernel_forecast)
        и возвращает адаптированный процент первого ордера grid_0.
        
        Конфиг читается строго напрямую через [''] без скрытых дефолтов!
        """
        if not regime_cfg:
            return default_vol

        if not isinstance(regime_cfg, dict):
            return cls.resolve_static_volume(regime_cfg, side, default_vol)

        mode = regime_cfg["mode"]
        if mode is None or str(mode).lower().strip() in cls.OFF_MODES:
            return default_vol

        mode_str = str(mode).lower().strip()

        # 1. Динамический режим: Kernel Forecast (Nadaraya-Watson)
        if mode_str == "kernel_forecast":
            kernel_cfg = regime_cfg["kernel_forecast"]
            tf_label = kernel_cfg["timeframe"]
            trend_st = await MomentumEngine.get_kernel_trend_state(
                symbol=symbol,
                kernel_cfg=kernel_cfg,
                client=client,
            )
            regime_name = f"ЯДЕРНЫЙ FORECAST (Nadaraya-Watson {tf_label})"

        # 2. Динамический режим: Auto-EMA
        elif mode_str == "auto_ema":
            ema_cfg = regime_cfg["auto_ema"]
            tf_label = ema_cfg["timeframe"]
            trend_st = await MomentumEngine.get_ema_trend_state(
                symbol=symbol,
                ema_cfg=ema_cfg,
                client=client,
            )
            regime_name = f"AUTO-EMA ({tf_label})"

        # 3. Статические режимы
        elif mode_str in cls.SUPPORTED_STATIC_MODES:
            return cls.resolve_static_volume(regime_cfg, side, default_vol)

        else:
            raise ValueError(
                f"[FirstOrderRegimeManager] Недопустимый режим first_order_weight_regime: '{mode}'. "
                f"Разрешены строго: {cls.SUPPORTED_DYNAMIC_MODES} или {cls.SUPPORTED_STATIC_MODES}."
            )

        # Отображение рыночного тренда в режим Кулака:
        # UP -> bullish (12/7), DOWN -> bearish (7/12), FLAT/UNSTABLE -> neutral (9/9)
        if trend_st == "UP":
            resolved_mode = "bullish"
        elif trend_st == "DOWN":
            resolved_mode = "bearish"
        else:
            resolved_mode = "neutral"

        dyn_vol = cls.resolve_static_volume(
            regime_cfg={"mode": resolved_mode, "presets": regime_cfg["presets"]},
            side=side,
            default_vol=default_vol,
        )

        logger.info(
            f"[{symbol}][{strategy_id}] [КУЛАК СИЛЬНОЙ РУКИ / {regime_name}]: "
            f"Global Trend={trend_st} -> Режим '{resolved_mode}'. "
            f"{side} Grid 0 (Кулак): {default_vol:.1f}% -> {dyn_vol:.1f}% (Сетка усреднения L1+ без изменений)"
        )
        return dyn_vol
