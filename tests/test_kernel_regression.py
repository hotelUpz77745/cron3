# ==============================================================================
# FILE: tests/test_kernel_regression.py
# ROLE: Unit and integration tests for Nadaraya-Watson Non-Parametric Kernel Engine
# ==============================================================================

import unittest
import numpy as np

from CORE.STRATEGY.kernel_regression import (
    NadarayaWatsonEstimator,
    KernelForecastEngine
)
from CORE.STRATEGY.strategy_applicator import StrategyApplicator


class TestNadarayaWatsonKernel(unittest.TestCase):
    """Тестирование математического аппарата ядерной регрессии Надарая-Ватсона."""

    def test_precomputed_weights_sum_to_one(self):
        """Веса ядра для окна W обязаны строго суммироваться в 1.0."""
        for k_type in ("rational_quadratic", "gaussian", "epanechnikov"):
            est = NadarayaWatsonEstimator(bandwidth=8.0, window_size=30, kernel_type=k_type)
            self.assertAlmostEqual(float(np.sum(est._weights)), 1.0, places=6)
            self.assertEqual(len(est._weights), 30)
            # Текущий бар (lag=0) должен иметь максимальный вес
            self.assertTrue(est._weights[0] > est._weights[-1])

    def test_uptrend_detection(self):
        """Проверка детекции восходящего тренда (UP) на монотонно растущей кривой."""
        # 40 цен от 100 до 140
        prices = [100.0 + i * 1.0 for i in range(40)]
        est = NadarayaWatsonEstimator(bandwidth=8.0, window_size=20, slope_threshold_pct=0.05)
        res = est.evaluate_regime(prices)

        self.assertEqual(res["trend_state"], "UP")
        self.assertGreater(res["slope"], 0.0)
        self.assertGreater(res["slope_pct"], 0.05)
        self.assertGreater(res["forecast"], res["smoothed"])

    def test_downtrend_detection(self):
        """Проверка детекции нисходящего тренда (DOWN) на падающей кривой."""
        # 40 цен от 140 до 100
        prices = [140.0 - i * 1.0 for i in range(40)]
        est = NadarayaWatsonEstimator(bandwidth=8.0, window_size=20, slope_threshold_pct=0.05)
        res = est.evaluate_regime(prices)

        self.assertEqual(res["trend_state"], "DOWN")
        self.assertLess(res["slope"], 0.0)
        self.assertLess(res["slope_pct"], -0.05)
        self.assertLess(res["forecast"], res["smoothed"])

    def test_flat_mean_reverting(self):
        """Проверка детекции флэта (FLAT) на колебаниях около среднего."""
        # Синусоида с малым размахом
        prices = [100.0 + math_sin * 0.02 for math_sin in (np.sin(np.linspace(0, 10, 40)))]
        est = NadarayaWatsonEstimator(bandwidth=10.0, window_size=20, slope_threshold_pct=0.05)
        res = est.evaluate_regime(prices)

        self.assertEqual(res["trend_state"], "FLAT")

    def test_envelope_overbought_oversold(self):
        """Проверка выхода за MAE-конверт (Overbought / Oversold)."""
        base = [100.0] * 35
        # Всплеск вверх
        ob_prices = base + [115.0]
        est = NadarayaWatsonEstimator(bandwidth=5.0, window_size=20, envelope_multiplier=1.2)
        res_ob = est.evaluate_regime(ob_prices)
        self.assertTrue(res_ob["is_overbought"])

        # Всплеск вниз
        os_prices = base + [85.0]
        res_os = est.evaluate_regime(os_prices)
        self.assertTrue(res_os["is_oversold"])

    def test_evaluate_klines_regime_various_inputs(self):
        """KernelForecastEngine должен корректно парсить и списки float, и словари, и кортежи klines."""
        raw_floats = [100.0 + i for i in range(35)]
        dict_klines = [{"close": p} for p in raw_floats]
        tuple_klines = [[0, 0, 0, 0, p, 0] for p in raw_floats]

        res_f = KernelForecastEngine.evaluate_klines_regime(raw_floats, window_size=20)
        res_d = KernelForecastEngine.evaluate_klines_regime(dict_klines, window_size=20)
        res_t = KernelForecastEngine.evaluate_klines_regime(tuple_klines, window_size=20)

        self.assertEqual(res_f["trend_state"], res_d["trend_state"])
        self.assertEqual(res_f["trend_state"], res_t["trend_state"])
        self.assertAlmostEqual(res_f["smoothed"], res_d["smoothed"], places=4)

    def test_strategy_applicator_kernel_resolution(self):
        """Интеграция с StrategyApplicator: разрешение первого ордера grid_0."""
        regime_cfg = {
            "mode": "bullish",
            "presets": {
                "bullish": {"LONG": 12.0, "SHORT": 7.0},
                "neutral": {"LONG": 9.0, "SHORT": 9.0},
                "bearish": {"LONG": 7.0, "SHORT": 12.0}
            }
        }
        vol_long = StrategyApplicator.resolve_first_order_volume(regime_cfg, "LONG", 10.0)
        vol_short = StrategyApplicator.resolve_first_order_volume(regime_cfg, "SHORT", 10.0)
        self.assertEqual(vol_long, 12.0)
        self.assertEqual(vol_short, 7.0)


if __name__ == "__main__":
    unittest.main()
