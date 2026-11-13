# ==============================================================================
# Path: tests/test_u96_omni_major13.py
# Role: Юнит-тесты паспорта стратегии u96_omni_major13 (тейки Zenith-13, super_grid 1.6x, dynamic_atr_grid, ma_crossover)
# ==============================================================================

import json
import os
import unittest
from pathlib import Path

from CORE.strategy_applicator import StrategyApplicator
from CORE.strategy_registry import StrategyConfig, StrategyRegistry

_ROOT = Path(__file__).resolve().parent.parent
_BT_ROOT = _ROOT.parent / "cron3TradeBacktest"


class TestU96OmniMajor13Passport(unittest.TestCase):
    def setUp(self):
        self.strat_path = _ROOT / "CFG" / "universes" / "u96_omni_major13.json"
        self.strat_bt_path = _BT_ROOT / "CFG" / "universes" / "u96_omni_major13.json"
        self.zenith13_path = _ROOT / "CFG" / "universes" / "u96_omni_zenith13.json"

        self.assertTrue(self.strat_path.exists(), "u96_omni_major13.json отсутствует в cron3!")
        self.assertTrue(self.strat_bt_path.exists(), "u96_omni_major13.json отсутствует в cron3TradeBacktest!")
        self.assertTrue(self.zenith13_path.exists(), "u96_omni_zenith13.json отсутствует в cron3!")

        self.strat_data = json.loads(self.strat_path.read_text(encoding="utf-8"))
        self.strat_bt_data = json.loads(self.strat_bt_path.read_text(encoding="utf-8"))
        self.z13_data = json.loads(self.zenith13_path.read_text(encoding="utf-8"))

    def test_parity_between_cron3_and_backtest(self):
        """Паспорта стратегии в cron3 и cron3TradeBacktest должны быть побитово идентичны."""
        self.assertEqual(self.strat_data, self.strat_bt_data)

    def test_super_grid_and_dynamic_atr_enabled(self):
        """Проверка: super_grid включен (1.6x), dynamic_atr_grid включен."""
        sg = self.strat_data["super_grid"]
        self.assertTrue(sg["enabled"], "super_grid должен быть включен")
        self.assertEqual(sg["multiplier"], 1.6, "multiplier super_grid должен быть ровно 1.6")

        dag = self.strat_data["dynamic_atr_grid"]
        self.assertTrue(dag["enabled"], "dynamic_atr_grid должен быть включен")

    def test_auto_closing_mode(self):
        """Проверка: режим автозакрытия строго ma_crossover (ma_cross_50)."""
        ac = self.strat_data["auto_closing"]
        self.assertEqual(ac["mode"], "ma_crossover")

    def test_take_profits_match_zenith13_exactly(self):
        """Проверка: тейки возвращены на x1.3 к классическому Zenith-13."""
        self.assertEqual(self.strat_data["min_tp_indent"], self.z13_data["min_tp_indent"])
        self.assertEqual(self.strat_data["min_tp_indent"], 1.0)

        for side in ["LONG", "SHORT"]:
            strat_tp = self.strat_data[side]["tp_map"]
            z13_tp = self.z13_data[side]["tp_map"]
            self.assertEqual(strat_tp, z13_tp)

    def test_strategy_registry_and_applicator(self):
        """Проверка загрузки u96_omni_major13 в StrategyRegistry и сборки сайд-конфигов."""
        test_symbols = ["ARBUSDT", "OPUSDT", "ETHUSDT"]
        reg = StrategyRegistry(
            active_strategies_cfg={
                "u96_omni_major13__live": {
                    "template": "u96_omni_major13",
                    "enabled": True,
                    "symbols": test_symbols,
                    "default_invest_size": 1200.0,
                }
            },
            universes_db={"u96_omni_major13": self.strat_data},
        )
        self.assertIn("u96_omni_major13__live", reg.active_strategies)
        strat = reg.active_strategies["u96_omni_major13__live"]

        self.assertEqual(strat.auto_closing_mode, "ma_crossover")
        self.assertTrue(strat.super_grid_enabled)
        self.assertEqual(strat.super_grid_cfg["multiplier"], 1.6)
        self.assertTrue(strat.dynamic_atr_grid_cfg.get("enabled"))
        self.assertEqual(strat.min_tp_indent, 1.0)

        applicator = StrategyApplicator()
        for sym in test_symbols:
            side_cfgs = applicator.build_side_configs(strat, symbol=sym)
            self.assertIn("LONG", side_cfgs)
            self.assertIn("SHORT", side_cfgs)
            self.assertEqual(side_cfgs["LONG"]["tp_map"]["0"]["indent"], 2.34)
            self.assertEqual(side_cfgs["SHORT"]["tp_map"]["0"]["indent"], 2.34)
            self.assertEqual(side_cfgs["LONG"]["tp_map"]["4"]["indent"], 1.0)
            self.assertEqual(side_cfgs["SHORT"]["tp_map"]["4"]["indent"], 1.0)


if __name__ == "__main__":
    unittest.main()

