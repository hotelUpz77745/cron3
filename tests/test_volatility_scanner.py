import unittest
from CORE.ADVANCED.volatility_scanner import VolatilityScanner

class TestVolatilityScanner(unittest.TestCase):
    def test_compute_atr_volatility(self):
        klines = [
            {"open": 102.0, "high": 110.0, "low": 100.0, "close": 108.0},  # H-L: 10%, O-C: (6/102)*100 = 5.88%, Green
            {"open": 118.0, "high": 120.0, "low": 100.0, "close": 102.0},  # H-L: 20%, O-C: (16/102)*100 = 15.69%, Red
            {"open": 105.0, "high": 130.0, "low": 100.0, "close": 125.0},  # H-L: 30%, O-C: (20/105)*100 = 19.05%, Green
        ]
        # Strict window check: len < 4 fails
        passed, avg_vol, count, max_candle = VolatilityScanner.compute_atr_volatility(klines, window=4, strict_window=True)
        self.assertFalse(passed)

        # Strict window check: len >= 3 passes, average = 20.0% (H-L base)
        passed, avg_vol, count, max_candle = VolatilityScanner.compute_atr_volatility(klines, window=3, strict_window=True, calc_base="H-L")
        self.assertTrue(passed)
        self.assertEqual(avg_vol, 20.0)
        self.assertEqual(count, 3)
        self.assertIsNotNone(max_candle)
        self.assertEqual(max_candle["atr_pct"], 30.0)
        self.assertEqual(max_candle["shadow_span_usdt"], 30.0)
        self.assertEqual(max_candle["color"], "green")
        self.assertEqual(max_candle["color_label"], "🟢 Зеленая")

        # O-C calculation base check
        passed_oc, avg_oc, count_oc, max_candle_oc = VolatilityScanner.compute_atr_volatility(klines, window=3, strict_window=True, calc_base="O-C")
        self.assertTrue(passed_oc)
        self.assertEqual(count_oc, 3)
        self.assertIsNotNone(max_candle_oc)
        self.assertEqual(max_candle_oc["calc_base"], "O-C")
        self.assertEqual(max_candle_oc["color"], "green")
        self.assertEqual(max_candle_oc["body_span_usdt"], 20.0)

        # Non-strict window with len < 5: uses available 3
        passed, avg_vol, count, max_candle = VolatilityScanner.compute_atr_volatility(klines, window=5, strict_window=False)
        self.assertTrue(passed)
        self.assertEqual(avg_vol, 20.0)

    def test_compute_rsi(self):
        # 1. Constant prices -> RSI = 50.0
        flat_prices = [100.0] * 20
        rsi_flat = VolatilityScanner.compute_rsi(flat_prices, period=14)
        self.assertEqual(rsi_flat, 50.0)

        # 2. Strictly rising prices -> RSI = 100.0
        rising_prices = [100.0 + i * 2.0 for i in range(25)]
        rsi_up = VolatilityScanner.compute_rsi(rising_prices, period=14)
        self.assertEqual(rsi_up, 100.0)

        # 3. Strictly falling prices -> RSI = 0.0
        falling_prices = [200.0 - i * 2.0 for i in range(25)]
        rsi_down = VolatilityScanner.compute_rsi(falling_prices, period=14)
        self.assertEqual(rsi_down, 0.0)

        # 4. Oscillating prices in normal range 40..60
        oscillating = [100.0, 102.0, 99.0, 101.0, 100.0, 103.0, 98.0, 101.0, 99.0, 102.0,
                       100.0, 101.0, 99.0, 100.0, 102.0, 99.0, 101.0, 100.0, 101.0, 100.0]
        rsi_osc = VolatilityScanner.compute_rsi(oscillating, period=14)
        self.assertIsNotNone(rsi_osc)
        self.assertTrue(35.0 <= rsi_osc <= 65.0)

    def test_compute_ma_sma_and_ema(self):
        prices = [10.0, 11.0, 12.0, 13.0, 14.0]
        # SMA of last 5: (10+11+12+13+14)/5 = 12.0
        sma = VolatilityScanner.compute_ma(prices, period=5, ma_type="SMA")
        self.assertEqual(sma, 12.0)

        # EMA of 5:
        ema = VolatilityScanner.compute_ma(prices, period=5, ma_type="EMA")
        self.assertIsNotNone(ema)
        self.assertEqual(ema, 12.0)  # On linear series initialized with mean

        # Dynamic EMA check: price spike at the end
        prices_spike = [10.0] * 5 + [20.0]
        ema_spike = VolatilityScanner.compute_ma(prices_spike, period=5, ma_type="EMA")
        sma_spike = VolatilityScanner.compute_ma(prices_spike, period=5, ma_type="SMA")
        # alpha = 2/(5+1) = 1/3. ema = 20*(1/3) + 10*(2/3) = 6.67 + 6.67 = 13.33
        self.assertAlmostEqual(ema_spike, 13.33, places=2)
        # sma = (10+10+10+10+20)/5 = 12.0
        self.assertEqual(sma_spike, 12.0)

    def test_compute_bollinger_bands_formula(self):
        # 20 closes with mean 100.0 and known std dev
        # Formula: BB Width = ((Upper / Lower) - 1) * 100%
        closes = [95.0, 105.0] * 10  # Mean = 100.0, Std Dev = 5.0
        bb = VolatilityScanner.compute_bollinger_bands(closes, period=20, multiplier=2.0, ma_type="SMA")
        self.assertIsNotNone(bb)
        self.assertEqual(bb["middle"], 100.0)
        self.assertEqual(bb["upper"], 110.0)  # 100 + 2*5
        self.assertEqual(bb["lower"], 90.0)   # 100 - 2*5

        # BB Width = ((110 / 90) - 1) * 100 = (1.2222 - 1)*100 = 22.22%
        expected_width = round(((110.0 / 90.0) - 1.0) * 100.0, 2)
        self.assertEqual(bb["width_pct"], expected_width)

    def test_compute_center_proximity(self):
        middle = 100.0
        upper = 110.0
        lower = 90.0
        # Total span = 20.0

        # Case 1: Exactly at middle -> deviation = 0%
        dev_center = VolatilityScanner.compute_center_proximity(100.0, middle, upper, lower)
        self.assertEqual(dev_center, 0.0)

        # Case 2: At Upper Band (110) -> dev = 10 / 20 = 50%
        dev_border = VolatilityScanner.compute_center_proximity(110.0, middle, upper, lower)
        self.assertEqual(dev_border, 50.0)

        # Case 3: Beyond border at 115 -> dev = 15 / 20 = 75%
        dev_75 = VolatilityScanner.compute_center_proximity(115.0, middle, upper, lower)
        self.assertEqual(dev_75, 75.0)

        # Case 4: Far beyond border at 120 -> dev = 20 / 20 = 100%
        dev_100 = VolatilityScanner.compute_center_proximity(120.0, middle, upper, lower)
        self.assertEqual(dev_100, 100.0)

    def test_filter_evaluation_pipeline(self):
        # Mock scanner with full new configuration
        cfg = {
            "enabled": True,
            "strict_window": True,
            "min_dayly_vol_usdt": 3000000,
            "atr_volatility_filter": {
                "enabled": True,
                "timeframe": "1w",
                "window": 12,
                "min_volatility_pct": 25.0,
                "max_volatility_pct": 40.0
            },
            "rsi_filter": {
                "enabled": True,
                "timeframe": "4h",
                "period": 14,
                "min_rsi": 35.0,
                "max_rsi": 65.0
            },
            "bollinger_width_filter": {
                "enabled": True,
                "timeframe": "4h",
                "MA_type": "SMA",
                "period": 20,
                "multiplier": 2.0,
                "min_width_pct": 8.0,
                "max_width_pct": 13.0,
                "center_proximity_filter": {
                    "enabled": True,
                    "max_deviation_from_center_pct": 75.0
                }
            }
        }
        scanner = VolatilityScanner()

        # Synthetic 1w klines (ATR vol ~ 30%)
        klines_1w = [{"high": 130.0, "low": 100.0}] * 12

        # Synthetic 4h klines (RSI ~ 50, BB width ~ 10%, price near center)
        # 50 candles with small oscillation: closes around 100 with std dev ~ 2.4 -> BB Width ~ 9.8%
        klines_4h = []
        for i in range(50):
            p = 100.0 + (2.5 if i % 2 == 0 else -2.5)
            klines_4h.append({"close": p, "high": p + 1, "low": p - 1})

        passed, res = scanner._evaluate_symbol_filters("TESTUSDT", klines_1w, klines_4h, cfg)
        self.assertTrue(passed)
        self.assertIsNotNone(res)
        self.assertEqual(res["symbol"], "TESTUSDT")
        self.assertEqual(res["volatility"], 30.0)
        self.assertTrue(35.0 <= res["rsi"] <= 65.0)
        self.assertTrue(8.0 <= res["bb_width_pct"] <= 13.0)
        self.assertTrue(res["bb_center_dev_pct"] <= 75.0)

    def test_filter_rejections(self):
        scanner = VolatilityScanner()
        base_cfg = {
            "enabled": True,
            "strict_window": True,
            "atr_volatility_filter": {"enabled": True, "min_volatility_pct": 25.0, "max_volatility_pct": 40.0, "window": 12},
            "rsi_filter": {"enabled": True, "min_rsi": 35.0, "max_rsi": 65.0, "period": 14},
            "bollinger_width_filter": {
                "enabled": True, "period": 20, "multiplier": 2.0, "min_width_pct": 8.0, "max_width_pct": 13.0,
                "center_proximity_filter": {"enabled": True, "max_deviation_from_center_pct": 75.0}
            }
        }
        klines_1w_good = [{"high": 130.0, "low": 100.0}] * 12  # 30% vol

        # 1. Overbought RSI rejection (pumping prices)
        klines_4h_pump = [{"close": 100.0 + i * 2.0} for i in range(50)]
        passed, _ = scanner._evaluate_symbol_filters("PUMP", klines_1w_good, klines_4h_pump, base_cfg)
        self.assertFalse(passed)

        # 2. Oversold RSI rejection (dumping prices)
        klines_4h_dump = [{"close": 200.0 - i * 2.0} for i in range(50)]
        passed, _ = scanner._evaluate_symbol_filters("DUMP", klines_1w_good, klines_4h_dump, base_cfg)
        self.assertFalse(passed)

        # 3. BB Width too narrow rejection (< 8.0%)
        # Tiny flat moves: close ~ 100.0, std dev ~ 0.5 -> BB width ~ 2.0%
        klines_4h_flat = []
        for i in range(50):
            p = 100.0 + (0.5 if i % 2 == 0 else -0.5)
            klines_4h_flat.append({"close": p})
        passed, _ = scanner._evaluate_symbol_filters("FLAT", klines_1w_good, klines_4h_flat, base_cfg)
        self.assertFalse(passed)

        # 4. Center Proximity rejection (> 75%)
        klines_4h_skewed = []
        for i in range(49):
            p = 100.0 + (2.5 if i % 2 == 0 else -2.5)
            klines_4h_skewed.append({"close": p})
        klines_4h_skewed.append({"close": 125.0})  # Extreme price spike > 75% of channel span
        passed, _ = scanner._evaluate_symbol_filters("SKEW", klines_1w_good, klines_4h_skewed, base_cfg)
        self.assertFalse(passed)

    def test_subfilter_toggles(self):
        scanner = VolatilityScanner()
        # When RSI is disabled, a pumping coin should pass RSI check
        cfg_no_rsi = {
            "enabled": True,
            "strict_window": True,
            "atr_volatility_filter": {"enabled": True, "min_volatility_pct": 25.0, "max_volatility_pct": 40.0, "window": 12},
            "rsi_filter": {"enabled": False},  # DISABLED
            "bollinger_width_filter": {
                "enabled": True, "period": 20, "multiplier": 2.0, "min_width_pct": 8.0, "max_width_pct": 13.0,
                "center_proximity_filter": {"enabled": False}  # DISABLED
            }
        }
        klines_1w = [{"high": 130.0, "low": 100.0}] * 12
        # Normal oscillating 4h klines but with a price spike
        klines_4h = []
        for i in range(49):
            p = 100.0 + (2.5 if i % 2 == 0 else -2.5)
            klines_4h.append({"close": p})
        klines_4h.append({"close": 107.0})  # High deviation

        passed, res = scanner._evaluate_symbol_filters("TOGGLE", klines_1w, klines_4h, cfg_no_rsi)
        self.assertTrue(passed)
        self.assertIsNone(res["rsi"])  # RSI was disabled

    def test_klines_cache_ttl_and_storage(self):
        from CORE.ADVANCED.klines_cache import KlinesCache
        KlinesCache.clear()
        self.assertEqual(KlinesCache.size(), 0)

        mock_klines = [{"open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0}]
        KlinesCache.set("BTCUSDT", "1w", 12, mock_klines)
        self.assertEqual(KlinesCache.size(), 1)

        # Immediate get
        cached = KlinesCache.get("BTCUSDT", "1w", 12, ttl_sec=300.0)
        self.assertEqual(cached, mock_klines)

        # Case-insensitive symbol
        cached_lower = KlinesCache.get("btcusdt", "1w", 12, ttl_sec=300.0)
        self.assertEqual(cached_lower, mock_klines)

        # Expired TTL
        cached_expired = KlinesCache.get("BTCUSDT", "1w", 12, ttl_sec=-1.0)
        self.assertIsNone(cached_expired)

if __name__ == "__main__":
    unittest.main()

