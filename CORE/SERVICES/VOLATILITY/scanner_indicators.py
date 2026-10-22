# CORE/SERVICES/VOLATILITY/scanner_indicators.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: CORE.SERVICES.VOLATILITY.scanner_indicators
ROLE: Математические индикаторы и фильтры сканера волатильности (ATR, RSI, BB, Center Proximity)
==============================================================================
"""

import math
from typing import List, Dict, Any, Optional, Tuple


class ScannerIndicators:
    """
    Чистые математические методы расчета технических индикаторов для сканера волатильности:
    - compute_atr: свечная волатильность за окно N свечей по базам H-L или O-C.
    - compute_rsi: индекс относительной силы с экспоненциальным сглаживанием Уайлдера.
    - compute_ma: скользящая средняя (SMA или EMA).
    - compute_bollinger_bands: полосы Боллинджера и процентная ширина канала.
    - compute_center_proximity: отклонение цены от middleBB в процентах от полуширины.
    """

    @staticmethod
    def compute_atr(
        klines: List[Dict[str, Any]],
        window: int = 12,
        strict_window: bool = True,
        calc_base: str = "H-L"
    ) -> Tuple[bool, Optional[float], int, Optional[Dict[str, Any]]]:
        """
        Расчет средней свечной волатильности (ATR) за окно `window` свечей.
        Базы расчета:
          - calc_base 'H-L': между хай и лоу ((High / Low) - 1) * 100%.
          - calc_base 'O-C': между опен и клоз (|Open - Close| / min(Open, Close)) * 100%.
        Возвращает (passed_strict, avg_volatility, candles_count, max_candle_dict).
        """
        if not klines:
            return False, None, 0, None

        if strict_window and len(klines) < window:
            return False, None, len(klines), None

        target_klines = klines[-window:] if len(klines) >= window else klines
        base_upper = (calc_base or "H-L").strip().upper()
        total_vol = 0.0
        count = 0
        max_vol = -1.0
        extreme_candle_data: Optional[Dict[str, Any]] = None

        for k in target_klines:
            high = float(k.get("high", 0.0))
            low = float(k.get("low", 0.0))
            open_p = float(k.get("open", low))
            close_p = float(k.get("close", high))

            if base_upper == "O-C":
                body_abs = abs(open_p - close_p)
                base_denom = min(open_p, close_p) if min(open_p, close_p) > 0 else max(open_p, close_p, 1e-8)
                vol = (body_abs / base_denom) * 100.0 if base_denom > 0 else 0.0
            else:
                vol = ((high / low) - 1.0) * 100.0 if low > 0 else 0.0

            total_vol += vol
            count += 1

            if vol > max_vol:
                max_vol = vol
                shadow_abs = max(high - low, 0.0)
                shadow_pct = ((high - low) / low) * 100.0 if low > 0 else 0.0
                body_abs = abs(open_p - close_p)
                body_denom = min(open_p, close_p) if min(open_p, close_p) > 0 else max(open_p, close_p, 1e-8)
                body_pct = (body_abs / body_denom) * 100.0 if body_denom > 0 else 0.0
                is_green = close_p >= open_p
                color = "green" if is_green else "red"
                color_label = "🟢 Зеленая" if is_green else "🔴 Красная"

                extreme_candle_data = {
                    "atr_pct": round(vol, 2),
                    "calc_base": base_upper,
                    "shadow_span_usdt": round(shadow_abs, 6),
                    "shadow_span_pct": round(shadow_pct, 2),
                    "body_span_usdt": round(body_abs, 6),
                    "body_span_pct": round(body_pct, 2),
                    "color": color,
                    "color_label": color_label,
                    "open": round(open_p, 6),
                    "high": round(high, 6),
                    "low": round(low, 6),
                    "close": round(close_p, 6),
                    "open_time": k.get("open_time")
                }

        if count == 0:
            return False, None, 0, None

        avg_vol = total_vol / count
        return True, round(avg_vol, 2), count, extreme_candle_data

    compute_atr_volatility = compute_atr

    @staticmethod
    def compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
        """
        Стандартный расчет индекса относительной силы (RSI) с экспоненциальным сглаживанием Уайлдера.
        """
        if len(closes) < period + 1:
            return None

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0.0) for d in deltas]
        losses = [max(-d, 0.0) for d in deltas]

        # Первое среднее: простое среднее за первые `period` изменений
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # Последующие сглаживания по формуле Уайлдера
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0.0 else 50.0
        if avg_gain == 0.0:
            return 0.0

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return round(rsi, 2)

    @staticmethod
    def compute_ma(closes: List[float], period: int = 20, ma_type: str = "SMA") -> Optional[float]:
        """
        Расчет скользящей средней (SMA или EMA).
        """
        if len(closes) < period:
            return None

        ma_type_upper = (ma_type or "SMA").upper().strip()
        if ma_type_upper == "EMA":
            alpha = 2.0 / (period + 1.0)
            ema = sum(closes[:period]) / period
            for price in closes[period:]:
                ema = price * alpha + ema * (1.0 - alpha)
            return ema
        else:
            return sum(closes[-period:]) / period

    @staticmethod
    def compute_bollinger_bands(
        closes: List[float],
        period: int = 20,
        multiplier: float = 2.0,
        ma_type: str = "SMA"
    ) -> Optional[Dict[str, float]]:
        """
        Расчет полос Боллинджера и ширины канала относительно нижней границы:
          BB Width = ((Upper Band / Lower Band) - 1) * 100%
        """
        if len(closes) < period:
            return None

        middle_bb = ScannerIndicators.compute_ma(closes, period, ma_type)
        if middle_bb is None or middle_bb <= 0:
            return None

        window = closes[-period:]
        variance = sum((x - middle_bb) ** 2 for x in window) / period
        std_dev = math.sqrt(variance)

        upper_bb = middle_bb + multiplier * std_dev
        lower_bb = middle_bb - multiplier * std_dev

        if lower_bb <= 0:
            return None

        bb_width_pct = ((upper_bb / lower_bb) - 1.0) * 100.0

        return {
            "middle": middle_bb,
            "upper": upper_bb,
            "lower": lower_bb,
            "std_dev": std_dev,
            "width_pct": round(bb_width_pct, 2)
        }

    @staticmethod
    def compute_center_proximity(
        current_price: float,
        middle_bb: float,
        upper_bb: float,
        lower_bb: float
    ) -> float:
        """
        Расчет отклонения цены от middleBB в процентах от общей ширины канала:
          Dev Pct = (|current_price - middleBB| / (upper_bb - lower_bb)) * 100%
        50% = цена на границе канала, 75% = цена не дальше 1.5x полуширины от центра.
        """
        band_span = upper_bb - lower_bb
        if band_span <= 0:
            return 0.0

        dev_abs = abs(current_price - middle_bb)
        dev_pct = (dev_abs / band_span) * 100.0
        return round(dev_pct, 2)
