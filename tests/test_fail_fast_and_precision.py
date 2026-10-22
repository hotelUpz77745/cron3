# tests/test_fail_fast_and_precision.py
# -*- coding: utf-8 -*-
"""
Тесты для проверки Фазы 2 (Протокол Самсонова: Fail-Fast и устранение магических чисел).
"""

import unittest
from consts import (
    FLOAT_ZERO_EPSILON,
    QTY_ZERO_EPSILON,
    DEFAULT_GRID_VOLUME_TOLERANCE_PCT,
    DEFAULT_GRID_VOLUME_TOLERANCE_RATIO,
)
from c_utils import Utils
from CORE._utils import TradeMath
from CORE.GRID.avg_manager import AverageManager
from CORE.GRID.fsm_grid_resolver import FSMGridResolver, DEFAULT_VOLUME_TOLERANCE_PCT
from POS_FSM.models import PositionState


class TestFailFastAndPrecision(unittest.TestCase):
    def test_constants_precision_defined(self):
        self.assertEqual(FLOAT_ZERO_EPSILON, 1e-6)
        self.assertEqual(QTY_ZERO_EPSILON, 1e-8)
        self.assertEqual(DEFAULT_GRID_VOLUME_TOLERANCE_PCT, 10.0)
        self.assertEqual(DEFAULT_GRID_VOLUME_TOLERANCE_RATIO, 0.10)
        self.assertEqual(DEFAULT_VOLUME_TOLERANCE_PCT, DEFAULT_GRID_VOLUME_TOLERANCE_PCT)

    def test_strict_float_success(self):
        self.assertEqual(Utils.strict_float(12.5, "price"), 12.5)
        self.assertEqual(Utils.strict_float("12.5", "price"), 12.5)
        self.assertEqual(Utils.strict_float(0, "qty"), 0.0)
        self.assertEqual(Utils.strict_float("0.0", "qty"), 0.0)

    def test_strict_float_failures(self):
        with self.assertRaises(ValueError) as ctx:
            Utils.strict_float(None, "invest_size")
        self.assertIn("invest_size", str(ctx.exception))
        self.assertIn("None", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            Utils.strict_float("abc", "entry_price")
        self.assertIn("entry_price", str(ctx.exception))
        self.assertIn("abc", str(ctx.exception))

    def test_strict_int_success(self):
        self.assertEqual(Utils.strict_int(5, "level"), 5)
        self.assertEqual(Utils.strict_int("10", "level"), 10)

    def test_strict_int_failures(self):
        with self.assertRaises(ValueError) as ctx:
            Utils.strict_int(None, "level")
        self.assertIn("level", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            Utils.strict_int("bad", "level")
        self.assertIn("bad", str(ctx.exception))

    def test_trade_math_calculate_order_volume_fail_fast(self):
        symbol_info = {
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            ]
        }
        # price <= 0 must fail fast
        with self.assertRaises(ValueError):
            TradeMath.calculate_order_volume(100.0, 10.0, 0.0, symbol_info, "BTCUSDT")

        with self.assertRaises(ValueError):
            TradeMath.calculate_order_volume(100.0, 10.0, -10.0, symbol_info, "BTCUSDT")

        # invest_size <= 0 must fail fast
        with self.assertRaises(ValueError):
            TradeMath.calculate_order_volume(0.0, 10.0, 50000.0, symbol_info, "BTCUSDT")

        with self.assertRaises(ValueError):
            TradeMath.calculate_order_volume(-50.0, 10.0, 50000.0, symbol_info, "BTCUSDT")

        # valid inputs should calculate correctly
        vol = TradeMath.calculate_order_volume(100.0, 50.0, 100.0, symbol_info, "BTCUSDT")
        self.assertEqual(vol, 0.5)

    def test_trade_math_calculate_take_profit_price_fail_fast(self):
        symbol_info = {
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            ]
        }
        with self.assertRaises(ValueError):
            TradeMath.calculate_take_profit_price(0.0, 1.5, "LONG", symbol_info, "BTCUSDT")

        with self.assertRaises(ValueError):
            TradeMath.calculate_take_profit_price(-100.0, 1.5, "LONG", symbol_info, "BTCUSDT")

        # Valid inputs
        tp = TradeMath.calculate_take_profit_price(100.0, 1.5, "LONG", symbol_info, "BTCUSDT")
        self.assertEqual(tp, 101.5)


if __name__ == "__main__":
    unittest.main()
