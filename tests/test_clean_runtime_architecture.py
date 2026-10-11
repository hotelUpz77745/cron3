import unittest
import pickle
from pathlib import Path
from CORE.STRATEGY.strategy_applicator import StrategyApplicator
from CORE.STRATEGY.strategy_registry import StrategyConfig
from consts import DATA_DIR

class TestCleanRuntimeArchitecture(unittest.TestCase):
    def test_side_configs_do_not_contain_auto_closing_or_trend_bloat(self):
        """Проверка: side_data не содержит автозакрытия и тренд-депендера, они живут на уровне стратегии."""
        mock_cfg = {
            "name": "Test Clean Strat",
            "leverage": 20,
            "tp_purpose": "self",
            "auto_closing": {"positive": {"trailing_ratchet": {"levels": []}}},
            "trend_size_depender": {"enabled": True, "trend_mode": "test"},
            "LONG": {
                "invest_size": 600.0,
                "grid": {"0": {"indent": 0.0, "volume": 5.0}},
                "tp_map": {"0": {"indent": 2.0}}
            },
            "SHORT": {
                "invest_size": 600.0,
                "grid": {"0": {"indent": 0.0, "volume": 5.0}},
                "tp_map": {"0": {"indent": 2.0}}
            }
        }
        strat = StrategyConfig(strategy_id="test_clean", symbols=["BTCUSDT"], raw_meta=mock_cfg)
        self.assertIsNotNone(strat.auto_closing_cfg)
        
        applicator = StrategyApplicator()
        side_cfgs = applicator.build_side_configs(strat)
        
        for side in ["LONG", "SHORT"]:
            self.assertNotIn("auto_closing", side_cfgs[side], f"{side} не должен содержать дубликат auto_closing")
            self.assertNotIn("trend_size_depender", side_cfgs[side], f"{side} не должен содержать дубликат trend_size_depender")
            self.assertIn("invest_size", side_cfgs[side])
            self.assertIn("grid", side_cfgs[side])
            self.assertIn("tp_map", side_cfgs[side])

    def test_active_runtime_files_are_clean(self):
        """Проверка: существующие файлы рантайма в CFG/runtime очищены от мусора."""
        runtime_dir = DATA_DIR / "runtime"
        if not runtime_dir.exists():
            return
            
        from c_utils import Utils
        for rt_file in list(runtime_dir.glob("*/*.json")) + list(runtime_dir.glob("*/*.pkl")):
            if rt_file.suffix == ".json":
                data = Utils.read_json_file(rt_file)
            else:
                with open(rt_file, "rb") as f:
                    data = pickle.load(f)
            for side in ["LONG", "SHORT"]:
                if side in data:
                    self.assertNotIn("auto_closing", data[side], f"Файл {rt_file} {side} не должен содержать auto_closing")
                    self.assertNotIn("trend_size_depender", data[side], f"Файл {rt_file} {side} не должен содержать trend_size_depender")

if __name__ == "__main__":
    unittest.main()
