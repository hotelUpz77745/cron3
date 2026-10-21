# CORE/GRID/dynamic_atr_grid.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.GRID.dynamic_atr_grid
ROLE: Динамическое масштабирование шагов сетки и курков активации через ATR инструмента
==============================================================================
"""

from typing import Optional
from c_log import UnifiedLogger
from CORE.SERVICES.VOLATILITY.atr_provider import ATRProvider

logger = UnifiedLogger("DynamicAtrGrid")


class DynamicAtrGridManager:
    """
    Рассчитывает множитель масштабирования шагов сетки (indent) и курков (activation)
    на основе текущей волатильности монеты (ATR%) относительно базовой волатильности рынка (ref_atr_pct).
    Позволяет на волатильных монетах (например, GTC) автоматически расширять шаг сетки,
    предотвращая преждевременную маржинальную перегрузку на глубоких просадках.
    """

    @classmethod
    async def get_grid_atr_multiplier(
        cls,
        client: Optional[object],
        symbol: str,
        cfg: Optional[dict] = None,
    ) -> float:
        """
        Возвращает коэффициент масштабирования отступов сетки (clamped).
        Если механика выключена или конфигурация отсутствует, возвращает 1.0 (без изменений).
        """
        if not cfg or not cfg.get("enabled", False):
            return 1.0

        timeframe = str(cfg.get("timeframe", "1h"))
        period = int(cfg.get("period", 14))
        cache_ttl_sec = int(cfg.get("cache_ttl_sec", 300))
        fallback_atr_pct = float(cfg.get("fallback_atr_pct", 2.0))
        ref_atr_pct = float(cfg.get("ref_atr_pct", 2.0))
        min_mult = float(cfg.get("min_multiplier", 0.75))
        max_mult = float(cfg.get("max_multiplier", 2.25))

        if ref_atr_pct <= 0:
            return 1.0

        try:
            atr_pct = await ATRProvider.get_atr_pct(
                client=client,
                symbol=symbol,
                timeframe=timeframe,
                period=period,
                cache_ttl_sec=cache_ttl_sec,
                fallback_atr_pct=fallback_atr_pct,
            )
            raw_mult = atr_pct / ref_atr_pct
            clamped_mult = max(min_mult, min(max_mult, raw_mult))
            clamped_mult = round(clamped_mult, 4)
            logger.debug(
                f"[DynamicAtrGrid] [{symbol}] ATR%: {atr_pct:.2f}% | Ref: {ref_atr_pct:.2f}% -> Mult: {clamped_mult:.3f}x"
            )
            return clamped_mult
        except Exception as e:
            logger.warning(f"[DynamicAtrGrid] [{symbol}] Error computing ATR multiplier ({e}), using 1.0x")
            return 1.0

    @classmethod
    def calculate_effective_indent(
        cls,
        base_indent: float,
        level_index: int,
        atr_mult: float,
        cfg: Optional[dict] = None
    ) -> float:
        """
        Масштабирует отступ уровня с защитой от размытия сетки («резинового метода»):
        - Масштабируются строго заданные по номеру ступени (по умолчанию только L1..L2 через max_scaled_level или scale_levels).
        - Разворотные ступени (L3+) и глубинная броня остаются строго на фиксированных Фибо-позициях.
        - Минимальный пол отступа min_indent_floor_pct (по умолчанию 1.5%).
        """
        if not cfg or not cfg.get("enabled", False) or atr_mult == 1.0:
            return float(base_indent)

        # 1. Проверка по конкретным номерам уровней:
        scale_levels = cfg.get("scale_levels")
        if scale_levels is not None:
            if level_index not in scale_levels:
                return float(base_indent)
        # По умолчанию масштабируются рабочие ступени Зоны 1 и 2 (L1..L3, номер <= 3)
        max_scaled_lvl = int(cfg.get("max_scaled_level", 3))
        if level_index > max_scaled_lvl:
            return float(base_indent)

        min_floor = float(cfg.get("min_indent_floor_pct", 1.5)) if cfg.get("min_indent_floor_pct") is not None else None
        max_cap = float(cfg.get("max_indent_cap_pct", 21.0)) if cfg.get("max_indent_cap_pct") is not None else None

        scaled = float(base_indent) * atr_mult
        if base_indent < 0:
            eff = scaled
            if min_floor is not None:
                eff = min(-min_floor, eff)
            if max_cap is not None:
                eff = max(-max_cap, eff)
        else:
            eff = scaled
            if min_floor is not None:
                eff = max(min_floor, eff)
            if max_cap is not None:
                eff = min(max_cap, eff)
        return round(eff, 4)


