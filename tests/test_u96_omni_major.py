# ==============================================================================
# Path: tests/test_u96_omni_major.py
# Role: Юнит-тесты паспорта стратегии u96_omni_major (бейзлайн тейков 1.3x меньше, super_grid 1.6x, dynamic_atr_grid)
# ==============================================================================

import json
import os
import unittest
from pathlib import Path

from CORE.strategy_applicator import StrategyApplicator
from CORE.strategy_registry import StrategyConfig, StrategyRegistry

_ROOT = Path(__file__).resolve().parent.parent
_BT_ROOT = _ROOT.parent / "cron3TradeBacktest"


class TestU96OmniMajorPassport(unittest.TestCase):
    def setUp(self):
        self.major_path = _ROOT / "CFG" / "universes" / "u96_omni_major.json"
        self.major_bt_path = _BT_ROOT / "CFG" / "universes" / "u96_omni_major.json"
        self.zenith13_path = _ROOT / "CFG" / "universes" / "u96_omni_zenith13.json"

        self.assertTrue(self.major_path.exists(), "u96_omni_major.json отсутствует в cron3!")
        self.assertTrue(self.major_bt_path.exists(), "u96_omni_major.json отсутствует в cron3TradeBacktest!")
        self.assertTrue(self.zenith13_path.exists(), "u96_omni_zenith13.json отсутствует в cron3!")

        self.major_data = json.loads(self.major_path.read_text(encoding="utf-8"))
        self.major_bt_data = json.loads(self.major_bt_path.read_text(encoding="utf-8"))
        self.z13_data = json.loads(self.zenith13_path.read_text(encoding="utf-8"))

    def test_parity_between_cron3_and_backtest(self):
        """Паспорта стратегии в cron3 и cron3TradeBacktest должны быть побитово идентичны."""
        self.assertEqual(self.major_data, self.major_bt_data)

    def test_super_grid_and_dynamic_atr_enabled(self):
        """Проверка: super_grid включен (1.6x), dynamic_atr_grid включен."""
        sg = self.major_data["super_grid"]
        self.assertTrue(sg["enabled"], "super_grid должен быть включен")
        self.assertEqual(sg["multiplier"], 1.6, "multiplier super_grid должен быть ровно 1.6")

        dag = self.major_data["dynamic_atr_grid"]
        self.assertTrue(dag["enabled"], "dynamic_atr_grid должен быть включен")

    def test_take_profits_1_3_times_smaller_than_zenith13(self):
        """Проверка: отступы тейк-профитов в 1.3 раза меньше, чем у u96_omni_zenith13."""
        self.assertAlmostEqual(self.major_data["min_tp_indent"], 0.62)

        for side in ["LONG", "SHORT"]:
            major_tp = self.major_data[side]["tp_map"]
            z13_tp = self.z13_data[side]["tp_map"]

            expected_indents = {
                "0": (1.8, 2.6),
                "1": (1.35, 2.0),
                "2": (1.0, 1.5),
                "3": (0.75, 1.2),
                "4": (0.62, 1.05),
                "5": (0.62, 0.95),
                "6": (0.62, 0.85),
                "7": (0.62, 0.8),
                "8": (0.62, 0.8),
            }

            for lvl_k, (exp_ind, exp_fb) in expected_indents.items():
                m_ind = major_tp[lvl_k]["indent"]
                m_fb = major_tp[lvl_k]["fallback_indent"]
                z_ind = z13_tp[lvl_k]["indent"]
                z_fb = z13_tp[lvl_k]["fallback_indent"]

                # Проверка точных ожидаемых значений
                self.assertAlmostEqual(m_ind, exp_ind, places=2)
                self.assertAlmostEqual(m_fb, exp_fb, places=2)

                # Проверка соотношения: значение в zenith13 / 1.3
                ratio_ind = z_ind / m_ind
                ratio_fb = z_fb / m_fb
                self.assertAlmostEqual(ratio_ind, 1.3, delta=0.03)
                self.assertAlmostEqual(ratio_fb, 1.3, delta=0.03)

    def test_strategy_registry_and_applicator_for_majors(self):
        """Проверка загрузки u96_omni_major в StrategyRegistry и генерации рантайма для 6 мажоров."""
        majors_symbols = ["ETHUSDT", "SOLUSDT", "LTCUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"]
        reg = StrategyRegistry(
            active_strategies_cfg={
                "u96_omni_major__live": {
                    "template": "u96_omni_major",
                    "enabled": True,
                    "symbols": majors_symbols,
                    "default_invest_size": 1200.0,
                }
            },
            universes_db={"u96_omni_major": self.major_data},
        )
        self.assertIn("u96_omni_major__live", reg.active_strategies)
        strat = reg.active_strategies["u96_omni_major__live"]

        self.assertEqual(strat.auto_closing_mode, "regime_adaptive")
        self.assertTrue(strat.super_grid_enabled)
        self.assertEqual(strat.super_grid_cfg["multiplier"], 1.6)
        self.assertTrue(strat.dynamic_atr_grid_cfg.get("enabled"))
        self.assertEqual(strat.min_tp_indent, 0.62)

        # Проверка сборки конфигураций сторон через StrategyApplicator
        applicator = StrategyApplicator()
        for sym in majors_symbols:
            side_cfgs = applicator.build_side_configs(strat, symbol=sym)
            self.assertIn("LONG", side_cfgs)
            self.assertIn("SHORT", side_cfgs)
            self.assertEqual(side_cfgs["LONG"]["tp_map"]["0"]["indent"], 1.8)
            self.assertEqual(side_cfgs["SHORT"]["tp_map"]["0"]["indent"], 1.8)
            self.assertEqual(side_cfgs["LONG"]["tp_map"]["4"]["indent"], 0.62)
            self.assertEqual(side_cfgs["SHORT"]["tp_map"]["4"]["indent"], 0.62)


if __name__ == "__main__":
    unittest.main()
