# ==============================================================================
# Path: tests/test_u96_omni_major13_topwide.py
# Role: Юнит-тесты паспорта стратегии u96_omni_major13_topwide (разреженная сетка L1–L3, super_grid 1.6x, dynamic_atr_grid, ma_crossover)
# ==============================================================================

import json
import os
import unittest
from pathlib import Path

from CORE.strategy_applicator import StrategyApplicator
from CORE.strategy_registry import StrategyRegistry

_ROOT = Path(__file__).resolve().parent.parent
_BT_ROOT = _ROOT.parent / "cron3TradeBacktest"


class TestU96OmniMajor13TopwidePassport(unittest.TestCase):
    def setUp(self):
        self.strat_path = _ROOT / "CFG" / "universes" / "u96_omni_major13_topwide.json"
        self.strat_bt_path = _BT_ROOT / "CFG" / "universes" / "u96_omni_major13_topwide.json"

        self.assertTrue(self.strat_path.exists(), "u96_omni_major13_topwide.json отсутствует в cron3!")
        self.assertTrue(self.strat_bt_path.exists(), "u96_omni_major13_topwide.json отсутствует в cron3TradeBacktest!")

        self.strat_data = json.loads(self.strat_path.read_text(encoding="utf-8"))
        self.strat_bt_data = json.loads(self.strat_bt_path.read_text(encoding="utf-8"))

    def test_parity_between_cron3_and_backtest(self):
        """Паспорта стратегии в cron3 и cron3TradeBacktest должны быть побитово идентичны."""
        self.assertEqual(self.strat_data, self.strat_bt_data)

    def test_grid_top_wide_indents(self):
        """Проверка: геометрия сетки соответствует формуле Top-Wide (L1 -4%, L2 -7%, L3 -11.5%)."""
        expected_indents = {
            "0": 0.0,
            "1": -4.0,
            "2": -7.0,
            "3": -11.5,
            "4": -18.0,
            "5": -28.0,
            "6": -44.0,
            "7": -66.0,
            "8": -92.0,
        }
        for side in ["LONG", "SHORT"]:
            grid = self.strat_data[side]["grid"]
            for lvl, exp_ind in expected_indents.items():
                self.assertIn(lvl, grid)
                self.assertEqual(grid[lvl]["indent"], exp_ind, f"Неверный отступ на {side} L{lvl}")

    def test_super_grid_and_recycler_v2_enabled(self):
        """Проверка: super_grid 1.6x и усиленный рециклер L4-L6 (квота 35%)."""
        sg = self.strat_data["super_grid"]
        self.assertTrue(sg["enabled"])
        self.assertEqual(sg["multiplier"], 1.6)

        rec = self.strat_data["cross_side_recycling"]
        self.assertTrue(rec["enabled"])
        self.assertEqual(rec["max_slice_pct"], 35.0)

    def test_auto_closing_mode(self):
        """Проверка: режим автозакрытия строго ma_crossover."""
        ac = self.strat_data["auto_closing"]
        self.assertEqual(ac["mode"], "ma_crossover")

    def test_strategy_registry_and_applicator(self):
        """Проверка загрузки u96_omni_major13_topwide в StrategyRegistry и сборки сайд-конфигов."""
        test_symbols = ["ARBUSDT", "OPUSDT", "ETHUSDT"]
        reg = StrategyRegistry(
            active_strategies_cfg={
                "u96_omni_major13_topwide__live": {
                    "template": "u96_omni_major13_topwide",
                    "enabled": True,
                    "symbols": test_symbols,
                    "default_invest_size": 1200.0,
                }
            },
            universes_db={"u96_omni_major13_topwide": self.strat_data},
        )
        self.assertIn("u96_omni_major13_topwide__live", reg.active_strategies)
        strat = reg.active_strategies["u96_omni_major13_topwide__live"]

        self.assertEqual(strat.auto_closing_mode, "ma_crossover")
        self.assertTrue(strat.super_grid_enabled)
        self.assertEqual(strat.super_grid_cfg["multiplier"], 1.6)

        applicator = StrategyApplicator()
        for sym in test_symbols:
            side_cfgs = applicator.build_side_configs(strat, symbol=sym)
            self.assertIn("LONG", side_cfgs)
            self.assertIn("SHORT", side_cfgs)
            self.assertEqual(side_cfgs["LONG"]["grid"]["1"]["indent"], -4.0)
            self.assertEqual(side_cfgs["LONG"]["grid"]["2"]["indent"], -7.0)
            self.assertEqual(side_cfgs["LONG"]["grid"]["3"]["indent"], -11.5)


if __name__ == "__main__":
    unittest.main()
