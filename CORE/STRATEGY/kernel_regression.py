# ==============================================================================
# FILE: CORE/STRATEGY/kernel_regression.py
# ROLE: Nadaraya-Watson Non-Parametric Kernel Regression & Forecast Engine
# ==============================================================================

import time
import math
from typing import Dict, List, Optional, Tuple, Any, Union
import numpy as np

from c_log import UnifiedLogger

logger = UnifiedLogger("KernelRegression")


class NadarayaWatsonEstimator:
    """
    Высокопроизводительный непараметрический оценщик Надарая-Ватсона (NWKR)
    с поддержкой гауссовского и рационального квадратичного ядер.
    
    Обеспечивает строгое каузальное (non-repainting) сглаживание временных рядов,
    мгновенную оценку градиента тренда (slope), локальную экстраполяцию (forecast)
    и адаптивный динамический ценовой конверт (Kernel Volatility Envelope).
    """

    def __init__(
        self,
        bandwidth: float = 8.0,
        window_size: int = 30,
        kernel_type: str = "rational_quadratic",
        alpha: float = 1.0,
        envelope_multiplier: float = 1.5,
        slope_threshold_pct: float = 0.05
    ):
        """
        :param bandwidth: Параметр гладкости h (чем больше h, тем шире колокол весов).
        :param window_size: Длина окна ретроспективы W (количество прошлых баров).
        :param kernel_type: Тип ядра ('rational_quadratic', 'gaussian', 'epanechnikov').
        :param alpha: Параметр формы для Rational Quadratic ядра.
        :param envelope_multiplier: Множитель MAE для построения границ конверта.
        :param slope_threshold_pct: Порог наклона в % от цены для классификации UP/DOWN vs FLAT.
        """
        self.bandwidth: float = max(1.0, float(bandwidth))
        self.window_size: int = max(5, int(window_size))
        self.kernel_type: str = str(kernel_type).lower().strip()
        self.alpha: float = max(0.01, float(alpha))
        self.envelope_multiplier: float = float(envelope_multiplier)
        self.slope_threshold_pct: float = float(slope_threshold_pct)

        # Предварительно рассчитываем нормализованный вектор весов для окна W
        self._weights: np.ndarray = self._precompute_kernel_weights()

    def _precompute_kernel_weights(self) -> np.ndarray:
        """
        Предварительно рассчитывает вектор весов ядра для лагов от 0 (текущий бар)
        до W-1 (самый старый бар в окне). Сумма весов строго равна 1.0.
        """
        lags = np.arange(self.window_size, dtype=np.float64)
        h = self.bandwidth

        if self.kernel_type == "gaussian":
            # K(u) = exp(-u^2 / 2), где u = lag / h
            w = np.exp(-0.5 * (lags / h) ** 2)
        elif self.kernel_type == "epanechnikov":
            # K(u) = 0.75 * (1 - u^2) для |u| <= 1, иначе 0
            u = lags / (h * math.sqrt(5))
            w = np.maximum(0.0, 0.75 * (1.0 - u ** 2))
            if np.sum(w) <= 0.0:
                w[0] = 1.0
        else:
            # Rational Quadratic: K(r) = (1 + r^2 / (2 * alpha * h^2)) ^ (-alpha)
            w = np.power(1.0 + (lags ** 2) / (2.0 * self.alpha * (h ** 2)), -self.alpha)

        w_sum = np.sum(w)
        if w_sum > 0.0:
            return w / w_sum
        return np.ones(self.window_size) / self.window_size

    def estimate_at_bar(self, window_prices: Union[List[float], np.ndarray]) -> float:
        """
        Возвращает сглаженное значение Надарая-Ватсона для текущего бара (крайняя точка).
        window_prices: массив цен длиной window_size (последний элемент — текущая цена).
        """
        arr = np.asarray(window_prices, dtype=np.float64)
        if len(arr) < self.window_size:
            if len(arr) == 0:
                return 0.0
            return float(np.mean(arr))

        # Текущий бар имеет lag=0 (индекс -1), предыдущий lag=1 (индекс -2) и т.д.
        # Поэтому разворачиваем крайние W цен и берем скалярное произведение с весами
        slice_rev = arr[-1 : -self.window_size - 1 : -1]
        return float(np.dot(self._weights, slice_rev))

    def evaluate_regime(
        self,
        prices: Union[List[float], np.ndarray]
    ) -> Dict[str, Any]:
        """
        Комплексная оценка рыночного режима по ценовому ряду:
        - smoothed: Текущее ядерное сглаживание Y_hat(t)
        - slope: Скорость изменения линии Надарая-Ватсона (производная)
        - slope_pct: Наклон в процентах от цены
        - forecast: Экстраполированное значение на 1 бар вперед
        - upper_band: Верхняя граница конверта (Overbought)
        - lower_band: Нижняя граница конверта (Oversold)
        - trend_state: 'UP' | 'DOWN' | 'FLAT'
        - is_overbought: bool (цена выше upper_band)
        - is_oversold: bool (цена ниже lower_band)
        """
        arr = np.asarray(prices, dtype=np.float64)
        min_required = self.window_size + 2
        if len(arr) < min_required:
            last_p = float(arr[-1]) if len(arr) > 0 else 0.0
            return {
                "trend_state": "FLAT",
                "smoothed": last_p,
                "slope": 0.0,
                "slope_pct": 0.0,
                "forecast": last_p,
                "upper_band": last_p * 1.01,
                "lower_band": last_p * 0.99,
                "mae": 0.0,
                "is_overbought": False,
                "is_oversold": False,
                "confidence": 0.0
            }

        cur_price = float(arr[-1])

        # 1. Сглаженное значение на текущем баре t
        y_cur = self.estimate_at_bar(arr)

        # 2. Сглаженное значение на предыдущем баре t-1
        arr_prev = arr[:-1]
        y_prev = self.estimate_at_bar(arr_prev)

        # 3. Мгновенный градиент / наклон
        slope = y_cur - y_prev
        slope_pct = (slope / y_cur * 100.0) if y_cur > 0 else 0.0

        # 4. Прогноз на 1 бар вперед (локальная линейная экстраполяция)
        forecast = y_cur + slope

        # 5. Расчет средней абсолютной ошибки (MAE) ядерного конверта за окно W
        # Вычисляем отклонения цен от ядерной кривой
        history_slice = arr[-self.window_size :]
        mae = float(np.mean(np.abs(history_slice - y_cur)))
        band_width = max(mae * self.envelope_multiplier, y_cur * 0.002)

        upper_band = y_cur + band_width
        lower_band = y_cur - band_width

        is_overbought = cur_price > upper_band
        is_oversold = cur_price < lower_band

        # 6. Классификация тренда:
        # Для подтвержденного UP: наклон положительный (> порога) и цена выше линии регрессии
        # Для подтвержденного DOWN: наклон отрицательный (< -порога) и цена ниже линии регрессии
        # Иначе: FLAT / MEAN_REVERTING
        if slope_pct > self.slope_threshold_pct and cur_price >= (y_cur - band_width * 0.2):
            trend_state = "UP"
        elif slope_pct < -self.slope_threshold_pct and cur_price <= (y_cur + band_width * 0.2):
            trend_state = "DOWN"
        else:
            trend_state = "FLAT"

        # Метрика уверенности (отношение наклона к ширине конверта)
        confidence = min(1.0, abs(slope) / (band_width + 1e-8))

        return {
            "trend_state": trend_state,
            "smoothed": round(y_cur, 6),
            "slope": round(slope, 6),
            "slope_pct": round(slope_pct, 4),
            "forecast": round(forecast, 6),
            "upper_band": round(upper_band, 6),
            "lower_band": round(lower_band, 6),
            "mae": round(mae, 6),
            "is_overbought": is_overbought,
            "is_oversold": is_oversold,
            "confidence": round(confidence, 4)
        }


class KernelForecastEngine:
    """
    Институциональный фасад для управления ядерными прогнозами (Nadaraya-Watson)
    в боевой логике бота и интеграции с ордерным куполом.
    """

    @staticmethod
    def evaluate_klines_regime(
        klines: List[Any],
        bandwidth: float = 8.0,
        window_size: int = 30,
        kernel_type: str = "rational_quadratic",
        envelope_multiplier: float = 1.5,
        slope_threshold_pct: float = 0.05
    ) -> Dict[str, Any]:
        """
        Принимает сырые свечи Binance (dict или list) и вычисляет режим Надарая-Ватсона.
        """
        if not klines:
            return {"trend_state": "FLAT", "smoothed": 0.0, "slope": 0.0, "forecast": 0.0}

        closes: List[float] = []
        for k in klines:
            if isinstance(k, (int, float, np.floating, np.integer)):
                closes.append(float(k))
            elif isinstance(k, dict):
                closes.append(float(k.get("close", 0.0)))
            elif isinstance(k, (list, tuple)) and len(k) > 4:
                closes.append(float(k[4]))

        estimator = NadarayaWatsonEstimator(
            bandwidth=bandwidth,
            window_size=window_size,
            kernel_type=kernel_type,
            envelope_multiplier=envelope_multiplier,
            slope_threshold_pct=slope_threshold_pct
        )
        return estimator.evaluate_regime(closes)
