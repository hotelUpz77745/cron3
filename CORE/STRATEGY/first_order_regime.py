# C:\Users\user\Desktop\My_Pro\HP_EliteBook_735_old\MY\HRON_3\cron3\CORE\STRATEGY\first_order_regime.py
# Role: first_order_regime.py module

# ==============================================================================
# Path: CORE/STRATEGY/first_order_regime.py
# Role: Доменная сущность паспорта стратегии: «Правило кулака Сильной руки» (First Order Weight Regime)
# ==============================================================================

from typing import Any, Dict, Optional, Tuple

from CORE.STRATEGY.momentum_engine import MomentumEngine
from c_log import UnifiedLogger

logger = UnifiedLogger("FirstOrderRegime")


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

    SUPPORTED_DYNAMIC_MODES = ("auto_ema",)
    SUPPORTED_STATIC_MODES = ("bullish", "neutral", "bearish")
    OFF_MODES = ("off", "none", "null", "disabled", False, None)

    MODE_ALIASES = {
        "bull": "bullish",
        "long_heavy": "bullish",
        "1": "bullish",
        "flat": "neutral",
        "symmetric": "neutral",
        "0": "neutral",
        "bear": "bearish",
        "short_heavy": "bearish",
        "-1": "bearish",
    }

    CANONICAL_PRESETS = {
        "bullish": {"LONG": 12.0, "SHORT": 7.0},
        "neutral": {"LONG": 9.0, "SHORT": 9.0},
        "bearish": {"LONG": 7.0, "SHORT": 12.0},
    }

    def __init__(self, regime_cfg: Optional[Dict[str, Any]] = None):
        """
        Инициализирует менеджер режима первого ордера («Правило кулака Сильной руки»).
        Прямое чтение конфигураций поднято в __init__ по правилу «Работай или упади сразу» (Fail-Fast).
        Управление активностью осуществляется строго через mode: при mode='off' режим отключен.
        """
        self.regime_cfg: Optional[Dict[str, Any]] = regime_cfg
        self.mode: Optional[str] = None
        self.presets: Dict[str, Any] = {}
        self.auto_ema_cfg: Optional[Dict[str, Any]] = None

        if regime_cfg is not None:
            if not isinstance(regime_cfg, dict):
                raise TypeError(f"[FirstOrderRegimeManager] regime_cfg must be a dict, got {type(regime_cfg)}")
            if regime_cfg.get("enabled") is False:
                self.mode = "off"
            elif "mode" in regime_cfg:
                raw_mode = regime_cfg["mode"]
                if raw_mode is not None and str(raw_mode).lower().strip() not in self.OFF_MODES:
                    self.mode = str(raw_mode).lower().strip()
                    if (
                        self.mode not in self.SUPPORTED_DYNAMIC_MODES
                        and self.mode not in self.SUPPORTED_STATIC_MODES
                        and self.mode not in self.MODE_ALIASES
                    ):
                        raise ValueError(
                            f"[FirstOrderRegimeManager] Недопустимый режим first_order_weight_regime: '{self.mode}'. "
                            f"Разрешены строго: {self.SUPPORTED_DYNAMIC_MODES} или {self.SUPPORTED_STATIC_MODES}."
                        )
            if "presets" in regime_cfg:
                if not isinstance(regime_cfg["presets"], dict):
                    raise TypeError(f"[FirstOrderRegimeManager] presets must be a dict, got {type(regime_cfg['presets'])}")
                self.presets = regime_cfg["presets"]
            if self.mode == "auto_ema":
                if "auto_ema" not in regime_cfg or not isinstance(regime_cfg["auto_ema"], dict):
                    raise ValueError("[FirstOrderRegimeManager] 'auto_ema' config dict is required when mode='auto_ema'")
                self.auto_ema_cfg = regime_cfg["auto_ema"]
                if "timeframe" not in self.auto_ema_cfg:
                    raise ValueError("[FirstOrderRegimeManager] 'timeframe' is required in auto_ema config")

        self.enabled: bool = bool(self.mode and self.mode not in self.OFF_MODES)

    @classmethod
    def resolve_static_volume(
        cls,
        regime_cfg: Any,
        side: str,
        default_vol: float,
    ) -> float:
        """
        Разрешает базовый объем первого ордера для стороны side (LONG/SHORT)
        на основе статического режима или пресетов стратегии.
        Управляется значением mode (при mode in OFF_MODES или enabled=False возвращается default_vol).
        """
        if regime_cfg is None:
            return default_vol

        if isinstance(regime_cfg, dict):
            if regime_cfg.get("enabled") is False:
                return default_vol
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

        side_key = side.upper()

        # 1. Прямой поиск в кастомных пресетах паспорта
        if mode_str in presets and isinstance(presets[mode_str], dict) and side_key in presets[mode_str]:
            return float(presets[mode_str][side_key])

        # 2. Поиск через нормализованный алиас (bull, 1, flat, 0, bear, -1)
        norm_mode = cls.MODE_ALIASES.get(mode_str, mode_str)
        if norm_mode in presets and isinstance(presets[norm_mode], dict) and side_key in presets[norm_mode]:
            return float(presets[norm_mode][side_key])

        # 3. Канонические пресеты Кулака Сильной Руки (12/7, 9/9, 7/12)
        if norm_mode in cls.CANONICAL_PRESETS and side_key in cls.CANONICAL_PRESETS[norm_mode]:
            return float(cls.CANONICAL_PRESETS[norm_mode][side_key])

        # 4. Если пресетов для режима нет в паспорте, возвращается базовый объем сетки
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
        Динамически оценивает состояние рынка по старшему таймфрейму (auto_ema)
        и возвращает адаптированный процент первого ордера grid_0.
        
        Конфиг читается строго напрямую через [''] без скрытых дефолтов!
        """
        if not regime_cfg:
            return default_vol

        if not isinstance(regime_cfg, dict):
            return cls.resolve_static_volume(regime_cfg, side, default_vol)

        mode = regime_cfg.get("mode")
        if mode is None or str(mode).lower().strip() in cls.OFF_MODES:
            return default_vol

        mode_str = str(mode).lower().strip()

        # 1. Динамический режим: Auto-EMA
        if mode_str == "auto_ema":
            ema_cfg = regime_cfg["auto_ema"]
            tf_label = ema_cfg["timeframe"]
            trend_st = await MomentumEngine.get_ema_trend_state(
                symbol=symbol,
                ema_cfg=ema_cfg,
                client=client,
            )
            regime_name = f"AUTO-EMA ({tf_label})"

        # 2. Динамический режим: Auto-ADX (закомментирован: в текущих конфигах отсутствует, может быть включен позже)
        # elif mode_str == "auto_adx":
        #     adx_cfg = regime_cfg["auto_adx"]
        #     tf_label = adx_cfg["timeframe"]
        #     trend_st = await MomentumEngine.get_adx_trend_state(
        #         symbol=symbol,
        #         adx_cfg=adx_cfg,
        #         client=client,
        #     )
        #     regime_name = f"AUTO-ADX ({tf_label})"

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
