# tests/test_quant_calculator.py
# Role: Unit tests for QuantCalculator liquidation simulation and reporting

import unittest
from quant_calculator import QuantCalculator


class TestQuantCalculator(unittest.TestCase):
    def setUp(self):
        self.calc = QuantCalculator(
            deposit=500.0,
            leverage=10,
            coins_count=4,
            grid_steps=[0, -3.0, -7.0, -12.0, -18.0, -25.0],
            volumes=[10.0, 15.0, 20.0, 25.0, 30.0, 40.0],
            invest_size=120.0,
        )

    def test_initialization(self):
        self.assertEqual(self.calc.total_deposit, 500.0)
        self.assertEqual(self.calc.leverage, 10)
        self.assertEqual(self.calc.coins_count, 4)
        self.assertEqual(self.calc.invest_size, 120.0)
        self.assertEqual(len(self.calc.grid_steps_pct), 6)
        self.assertEqual(len(self.calc.volume_pct), 6)

    def test_simulate_drop_zero_drawdown(self):
        mb, mm = self.calc.simulate_drop(drop_pct=0.0, active_coins=1, invest_size=120.0)
        # At 0 drop, only step 0 (0.0%) executed.
        # Deposit is 500.0, unrealized pnl is 0.
        self.assertAlmostEqual(mb, 500.0, places=2)
        self.assertGreater(mm, 0.0)

    def test_simulate_drop_severe_drawdown(self):
        mb_mild, mm_mild = self.calc.simulate_drop(drop_pct=-10.0, active_coins=2, invest_size=120.0)
        mb_severe, mm_severe = self.calc.simulate_drop(drop_pct=-50.0, active_coins=2, invest_size=120.0)
        # Margin balance should be much lower in severe drop
        self.assertLess(mb_severe, mb_mild)

    def test_find_liquidation_point(self):
        liq_1 = self.calc.find_liquidation_point(active_coins=1, invest_size=120.0)
        liq_all = self.calc.find_liquidation_point(active_coins=4, invest_size=120.0)
        # 4 coins dropping simultaneously liquidates at a much shallower (or equal) drop than 1 coin
        self.assertLessEqual(liq_1, 0.0)
        self.assertLessEqual(liq_all, 0.0)
        self.assertGreaterEqual(liq_all, liq_1)

    def test_generate_report(self):
        report = self.calc.generate_report()
        self.assertIn("QUANT LIQUIDATION CALCULATOR", report)
        self.assertIn("Депозит: 500.0 USDT", report)
        self.assertIn("Монет: 4", report)
        self.assertIn("Плечо: 10x", report)
        self.assertIn("Черный лебедь", report)
        self.assertIn("Апокалипсис", report)

    def test_edge_case_zero_coins_count_safeguard(self):
        calc_zero = QuantCalculator(
            deposit=200.0,
            leverage=5,
            coins_count=0,
            grid_steps=[0, -5],
            volumes=[50, 50],
            invest_size=50.0,
        )
        self.assertEqual(calc_zero.coins_count, 1)

    def test_super_grid_report_header(self):
        self.calc.super_grid_enabled = True
        self.calc.super_grid_multiplier = 1.5
        report = self.calc.generate_report()
        self.assertIn("АКТИВЕН SUPER GRID (Множитель: 1.5x)", report)


if __name__ == "__main__":
    unittest.main()
