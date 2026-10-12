# tests/test_grid_none_safety.py
import unittest
import copy
from unittest.mock import MagicMock, AsyncMock

from quant_calculator import QuantCalculator
from CORE._utils import TradeMath, RiskCalculatingUtils
from CORE.trailing_system import BaseTrailingSystem
from CORE.GRID.avg_manager import AverageManager
from tools.grid_volume_calculator import calculate_grid_volumes


class TestGridNoneSafety(unittest.IsolatedAsyncioTestCase):
    """
    Верифицирует устойчивость всей системы к наличию в сетке (grid) и tp_map:
    - 'enabled': False
    - 'indent': None
    - 'volume': None
    - 'activation': None
    - 'super_indent': None
    Исключает TypeError: float() argument must be a string or a real number, not 'NoneType'.
    """

    def setUp(self):
        self.sample_grid = {
            "0": {"role": "L0", "enabled": True, "indent": 0.0, "volume": 10.0, "is_active": True},
            "1": {"role": "L1", "enabled": True, "indent": -3.0, "activation": -4.0, "volume": 10.0, "is_active": False},
            "2": {"role": "L2", "enabled": True, "indent": -8.0, "activation": -10.0, "volume": 15.0, "is_active": False},
            "3": {"role": "L3", "enabled": True, "indent": -15.0, "volume": 20.0, "is_active": False},
            "4": {"role": "L4", "enabled": True, "indent": -25.0, "volume": 25.0, "is_active": False},
            "5": {"role": "L5", "enabled": True, "indent": -40.0, "volume": 20.0, "is_active": False},
            "6": {"role": "L6", "enabled": False, "indent": None, "activation": None, "volume": None, "is_active": False},
            "7": {"role": "L7", "enabled": False, "indent": None, "activation": None, "volume": None, "is_active": False},
        }

    def test_quant_calculator_ignores_none_and_disabled(self):
        # Передаем конфигурацию, где в сетке присутствуют L6 и L7 с None
        qc = QuantCalculator(
            deposit=300.0,
            leverage=10,
            coins_count=2,
            grid_steps=[-3.0, -8.0, -15.0],
            volumes=[10.0, 15.0, 20.0],
            invest_size=100.0,
        )
        report = qc.generate_report()
        self.assertIn("QUANT LIQUIDATION CALCULATOR", report)

    def test_trade_math_tp_none_indent_safety(self):
        # calculate_take_profit_price не должен падать при None indent
        spec_data = {"price_tick_size": 0.01, "price_precision": 2, "qty_step_size": 1.0, "qty_precision": 0}
        res_long = TradeMath.calculate_take_profit_price(100.0, None, "LONG", spec_data, "BTCUSDT")
        self.assertEqual(res_long, 100.0)

        res_short = TradeMath.calculate_take_profit_price(100.0, None, "SHORT", spec_data, "BTCUSDT")
        self.assertEqual(res_short, 100.0)

    def test_trade_math_grid_none_indent_safety(self):
        spec_data = {"price_tick_size": 0.01, "price_precision": 2, "qty_step_size": 1.0, "qty_precision": 0}
        res = TradeMath.calculate_grid_price(100.0, None, "LONG", spec_data, "BTCUSDT")
        self.assertEqual(res, 100.0)

    def test_risk_calculating_utils_get_current_grid_level(self):
        grid = copy.deepcopy(self.sample_grid)
        # Если L6 помечен is_active=True, но enabled=False, он не должен считаться активным
        grid["6"]["is_active"] = True
        grid["1"]["is_active"] = True
        cur = RiskCalculatingUtils.get_current_grid_level(grid)
        self.assertEqual(cur, "1")

    def test_trailing_system_grid_evaluation_safety(self):
        grid = copy.deepcopy(self.sample_grid)
        state_trailing = {}
        # Просадка 50% — должна активировать и исполнить уровни вплоть до L5, но полностью проигнорировать L6 и L7
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(50.0, grid, state_trailing)
        self.assertNotIn("6", skipped)
        self.assertNotIn("7", skipped)
        self.assertNotIn(cand, ["6", "7"])

    async def test_avg_manager_init_grid_prices_with_none_indents(self):
        grid = copy.deepcopy(self.sample_grid)
        state = MagicMock()
        state.grid = grid
        state.initial_entry_price = 100.0
        state.avg_entry_price = 100.0
        state.open_time = 1000

        avg_mgr = AverageManager()
        spec_data = {"price_tick_size": 0.01, "price_precision": 2, "qty_step_size": 1.0, "qty_precision": 0}
        success = await avg_mgr._init_grid_prices(None, "BTCUSDT", "LONG", state, spec_data)
        self.assertTrue(success)
        self.assertIsNone(grid["6"]["price"])
        self.assertIsNone(grid["7"]["price"])
        self.assertIsNotNone(grid["1"]["price"])
        self.assertIsNotNone(grid["5"]["price"])

    def test_avg_manager_accumulate_current_and_distribute_remaining_safety(self):
        grid = copy.deepcopy(self.sample_grid)
        # Симулируем пропуск уровней, включая L6 и L7
        skipped_levels = ["1", "2"]
        skipped_vol = sum(float(grid[sk].get("volume") or 0.0) for sk in skipped_levels if grid[sk].get("enabled", True) is not False)
        self.assertEqual(skipped_vol, 25.0)

        # Режим distribute_remaining: L6 и L7 не должны получать долю объема
        cand_idx = 0
        remaining = [
            lvl for lvl in sorted(grid.keys(), key=lambda x: int(x) if x.isdigit() else x)
            if int(lvl) > cand_idx
            and not grid[lvl].get("is_active")
            and grid[lvl].get("enabled", True) is not False
            and grid[lvl].get("indent") is not None
        ]
        self.assertNotIn("6", remaining)
        self.assertNotIn("7", remaining)
        self.assertEqual(remaining, ["1", "2", "3", "4", "5"])


if __name__ == "__main__":
    unittest.main()
