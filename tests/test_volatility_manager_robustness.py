import asyncio
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.SERVICES.VOLATILITY.volatility_manager import VolatilityManager


class TestVolatilityManagerRobustness(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cron3_vm_test_")
        self.runtime_dir = Path(self.temp_dir) / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        self.mock_bot = MagicMock()
        self.mock_bot.symbols = ["STRKUSDT"]
        self.mock_bot.client = MagicMock()
        self.mock_bot.client.get_klines = AsyncMock(return_value=[
            {"high": 1.10, "low": 1.00},
            {"high": 1.12, "low": 1.02},
            {"high": 1.15, "low": 1.05},
        ])

        self.mock_runtime_mgr = MagicMock()
        self.runtime_file = self.runtime_dir / "strkusdt.pkl"
        self.mock_runtime_mgr.get_runtime_path.return_value = self.runtime_file
        self.mock_runtime_mgr.caches = {}
        self.mock_bot.runtime_manager = self.mock_runtime_mgr
        self.mock_bot.fsm_states = {}

        # Mock strategy dome with super_grid enabled
        mock_strat = MagicMock()
        mock_strat.strategy_id = "u16_chita__classic"
        mock_strat.super_grid_enabled = True
        mock_strat.super_grid_cfg = {
            "timeframe": "1w",
            "window": 12,
            "multiplier": 1.5,
            "min_volatility_pct": 1.0,
            "max_volatility_pct": 50.0
        }
        mock_registry = MagicMock()
        mock_registry.get_strategy_for_symbol.return_value = mock_strat
        mock_registry.active_strategies = {"u16_chita__classic": mock_strat}
        self.mock_bot.strategy_registry = mock_registry

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_grid_with_none_indent_levels_does_not_crash(self):
        """Проверяем, что уровни сетки с indent: None (отключенные L6/L7) не приводят к TypeError в VolatilityManager."""
        rt_data = {
            "LONG": {
                "grid": {
                    "0": {"indent": 0.5, "enabled": True},
                    "1": {"indent": 1.0, "enabled": True},
                    "2": {"indent": 2.0, "enabled": True},
                    "3": {"indent": 4.0, "enabled": True},
                    "4": {"indent": 8.0, "enabled": True},
                    "5": {"indent": 16.0, "enabled": True},
                    "6": {"indent": None, "enabled": False, "volume": None},
                    "7": {"indent": None, "enabled": False, "volume": None},
                }
            },
            "SHORT": {
                "grid": {
                    "0": {"indent": 0.5, "enabled": True},
                    "1": {"indent": 1.0, "enabled": True},
                }
            }
        }
        with open(self.runtime_file, "wb") as f:
            pickle.dump(rt_data, f)

        vm = VolatilityManager(self.mock_bot)
        vm.is_running = True

        # Запускаем process_all - не должно быть никаких исключений
        stats = asyncio.run(vm.process_all())

        self.assertIn("STRKUSDT", stats)
        self.assertTrue(stats["STRKUSDT"]["is_advanced"])

        # Проверяем сохраненные данные в pkl
        with open(self.runtime_file, "rb") as f:
            updated_rt = pickle.load(f)

        long_grid = updated_rt["LONG"]["grid"]
        # Активные уровни 0-5 должны иметь числовой super_indent
        for lvl in ("0", "1", "2", "3", "4", "5"):
            self.assertIsNotNone(long_grid[lvl]["super_indent"])
            self.assertIsInstance(long_grid[lvl]["super_indent"], float)

        # Отключенные уровни 6 и 7 должны оставаться None
        self.assertIsNone(long_grid["6"].get("super_indent"))
        self.assertIsNone(long_grid["7"].get("super_indent"))
        self.assertIsNone(long_grid["6"]["indent"])
        self.assertIsNone(long_grid["7"]["indent"])


if __name__ == "__main__":
    unittest.main()
