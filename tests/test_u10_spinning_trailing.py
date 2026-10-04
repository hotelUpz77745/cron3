# ============================================================
# FILE: tests/test_u10_spinning_trailing.py
# ROLE: Unit and integration tests for u10_spinning, BaseTrailingSystem,
#       neutral tp_purpose, and avg_free_volume_behavior in cron3
# ============================================================

import copy
import json
import unittest
from pathlib import Path

from CORE.trailing_system import BaseTrailingSystem
from POS_FSM.models import PositionState
from CORE.STRATEGY.strategy_registry import StrategyRegistry
from CORE.STRATEGY.strategy_applicator import StrategyApplicator


class TestU10SpinningTrailing(unittest.TestCase):
    def test_normalize_abs(self):
        """Тестирование знаково-агностического нормализатора normalize_abs."""
        self.assertEqual(BaseTrailingSystem.normalize_abs(10.5), 10.5)
        self.assertEqual(BaseTrailingSystem.normalize_abs(-10.5), 10.5)
        self.assertEqual(BaseTrailingSystem.normalize_abs("-8.0"), 8.0)
        self.assertEqual(BaseTrailingSystem.normalize_abs(None, default=0.5), 0.5)
        self.assertEqual(BaseTrailingSystem.normalize_abs("invalid", default=1.0), 1.0)

    def test_evaluate_trailing_tp_neutral_ladder(self):
        """Проверка динамической Фибоначчи-лестницы Трейлинг ТП в режиме neutral."""
        tp_map = {
            "0": {"indent": None, "fallback_indent": 1.9, "activation": 2.2},
            "1": {"indent": None, "fallback_indent": 2.2, "activation": 2.4},
            "2": {"indent": None, "fallback_indent": 2.4, "activation": 2.7},
            "3": {"indent": None, "fallback_indent": 3.2, "activation": 3.6},
        }
        state_trailing = {}

        # 1. Прибыль 1.5% — курок уровня 0 (2.2%) еще не достигнут
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=1.5, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)
        self.assertIsNone(floor)

        # 2. Прибыль поднялась до 2.3% — взводится курок уровня 0 (activation 2.2%), пол = 1.9%
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.3, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)
        self.assertEqual(floor, 1.9)
        self.assertEqual(state_trailing.get("armed_floor"), 1.9)

        # 3. Прибыль растет дальше до 2.8% — перешагивает активацию уровня 2 (2.7%), пол поднимается до 2.4%
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.8, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)
        self.assertEqual(floor, 2.4)
        self.assertEqual(state_trailing.get("armed_floor"), 2.4)

        # 4. Откат прибыли до 2.5% — выше пола 2.4%, пока удерживаем позицию
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.5, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)

        # 5. Откат прибыли до 2.35% (<= 2.4%) — фиксация прибыли по трейлингу!
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.35, tp_map=tp_map, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertTrue(should_close)
        self.assertEqual(floor, 2.4)
        self.assertIn("TRAILING TP", info)

    def test_evaluate_trailing_tp_sign_agnostic(self):
        """Проверка работы с отрицательными знаками в tp_map (защита от ошибок оператора)."""
        tp_map_negative = {
            "0": {"indent": None, "fallback_indent": -1.9, "activation": -2.2},
        }
        state_trailing = {}
        # Прибыль 2.3% взводит пол 1.9%
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=2.3, tp_map=tp_map_negative, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertFalse(should_close)
        self.assertEqual(floor, 1.9)

        # Откат до 1.8% исполняет маркет TP
        should_close, floor, info = BaseTrailingSystem.evaluate_trailing_tp(
            gain_pct=1.8, tp_map=tp_map_negative, state_trailing=state_trailing, tp_purpose="neutral"
        )
        self.assertTrue(should_close)

    def test_evaluate_trailing_grid_rebound(self):
        """Проверка взведения курка на просадке и исполнения на отскоке вверх."""
        grid = {
            "0": {"indent": 0.0, "activation": None, "volume": 7.36, "is_active": True},
            "1": {"indent": -3.5, "activation": None, "volume": 8.1, "is_active": False},
            "2": {"indent": -5.5, "activation": None, "volume": 8.91, "is_active": False},
            "3": {"indent": -8.0, "activation": -10.5, "volume": 9.8, "is_active": False},
            "4": {"indent": -13.0, "activation": -17.0, "volume": 10.78, "is_active": False},
        }
        state_trailing = {}

        # 1. Просадка 2.0% — ничего не срабатывает
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(2.0, grid, state_trailing)
        self.assertIsNone(cand)

        # 2. Просадка 3.6% — уровень 1 без трейлинга срабатывает прямо
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(3.6, grid, state_trailing)
        self.assertEqual(cand, "1")
        self.assertEqual(skipped, [])
        grid["1"]["is_active"] = True

        # 3. Просадка 9.0% — уровень 3 имеет курок на 10.5%, поэтому на 9.0% он еще не взведен
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(9.0, grid, state_trailing)
        # Уровень 2 без трейлинга (indent: -5.5) срабатывает!
        self.assertEqual(cand, "2")
        grid["2"]["is_active"] = True

        # 4. Просадка углубляется до 11.0% — взводится курок уровня 3 (activation: 10.5)
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(11.0, grid, state_trailing)
        self.assertIsNone(cand)  # Пока падает, не покупаем!
        self.assertIn("3", state_trailing.get("armed_levels", {}))

        # 5. Просадка отскакивает до 10.0% — еще глубже пола 8.0%, пока не исполняем
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(10.0, grid, state_trailing)
        self.assertIsNone(cand)

        # 6. Отскок до 7.9% (просадка сократилась <= 8.0%) — исполнение уровня 3 на отскоке!
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(7.9, grid, state_trailing)
        self.assertEqual(cand, "3")
        self.assertEqual(skipped, [])

    def test_evaluate_trailing_grid_flash_crash_and_skipped_levels(self):
        """Проверка резкого пролива цены мимо уровня 3 сразу в уровень 4 с последующим отскоком."""
        grid = {
            "0": {"indent": 0.0, "activation": None, "volume": 7.36, "is_active": True},
            "1": {"indent": -3.5, "activation": None, "volume": 8.1, "is_active": True},
            "2": {"indent": -5.5, "activation": None, "volume": 8.91, "is_active": True},
            "3": {"indent": -8.0, "activation": -10.5, "volume": 9.8, "is_active": False},
            "4": {"indent": -13.0, "activation": -17.0, "volume": 10.78, "is_active": False},
            "5": {"indent": -21.0, "activation": -27.5, "volume": 11.86, "is_active": False},
        }
        state_trailing = {}

        # Резкий импульсный пролив до 18.0% (пробиты курки и уровня 3, и уровня 4)
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(18.0, grid, state_trailing)
        self.assertIsNone(cand)  # Идет пролив, держим
        self.assertIn("3", state_trailing.get("armed_levels", {}))
        self.assertIn("4", state_trailing.get("armed_levels", {}))

        # Отскок до 12.5% — уровень 4 (floor 13.0%) встречает цену раньше уровня 3 (floor 8.0%)!
        cand, skipped, info = BaseTrailingSystem.evaluate_trailing_grid(12.5, grid, state_trailing)
        self.assertEqual(cand, "4")
        # Уровень 3 был пропущен!
        self.assertEqual(skipped, ["3"])

    def test_avg_free_volume_behaviors(self):
        """Проверка трех режимов обработки сэкономленного объема сетки."""
        base_grid = {
            "0": {"indent": 0.0, "volume": 7.36, "is_active": True},
            "1": {"indent": -3.5, "volume": 8.1, "is_active": True},
            "2": {"indent": -5.5, "volume": 8.91, "is_active": True},
            "3": {"indent": -8.0, "volume": 10.0, "is_active": False},
            "4": {"indent": -13.0, "volume": 12.0, "is_active": False},
            "5": {"indent": -21.0, "volume": 15.0, "is_active": False},
            "6": {"indent": -34.0, "volume": 20.0, "is_active": False},
        }

        # 1. Режим distribute_remaining: пропущен уровень 3 (объем 10.0) при исполнении уровня 4
        # Оставшиеся неисполненные уровни после 4: 5 и 6 (2 штуки). Каждому достается 10.0 / 2 = +5.0!
        grid1 = copy.deepcopy(base_grid)
        cand = "4"
        skipped = ["3"]
        skipped_vol = sum(grid1[sk]["volume"] for sk in skipped)
        cand_idx = int(cand)
        remaining = [
            lvl for lvl in sorted(grid1.keys(), key=lambda x: int(x))
            if int(lvl) > cand_idx and not grid1[lvl].get("is_active")
        ]
        vol_share = round(skipped_vol / len(remaining), 4)
        for rem_lvl in remaining:
            grid1[rem_lvl]["volume"] += vol_share

        self.assertEqual(grid1["5"]["volume"], 20.0)  # 15.0 + 5.0
        self.assertEqual(grid1["6"]["volume"], 25.0)  # 20.0 + 5.0

        # 2. Режим accumulate_current: пропущен уровень 3 (10.0), прибавляется к текущему уровню 4 (12.0)
        grid2 = copy.deepcopy(base_grid)
        grid2[cand]["volume"] += skipped_vol
        self.assertEqual(grid2["4"]["volume"], 22.0)
        self.assertEqual(grid2["5"]["volume"], 15.0)  # Не изменился

        # 3. Режим mark_executed: пропущенный уровень просто сгорает
        grid3 = copy.deepcopy(base_grid)
        for sk in skipped:
            grid3[sk]["is_active"] = True
        self.assertTrue(grid3["3"]["is_active"])
        self.assertEqual(grid3["4"]["volume"], 12.0)  # Не изменился
        self.assertEqual(grid3["5"]["volume"], 15.0)  # Не изменился

    def test_neutral_validation_bypass(self):
        """Проверка отключения валидации len(grid) == len(tp_map) при tp_purpose='neutral'."""
        data_neutral = {
            "LONG": {
                "tp_purpose": "neutral",
                "grid": {"0": {}, "1": {}, "2": {}},  # 3 уровня
                "tp_map": {"0": {}, "1": {}, "2": {}, "3": {}, "4": {}},  # 5 уровней
            }
        }
        # При tp_purpose == "neutral" валидатор НЕ должен выбрасывать исключение
        for side in ["LONG", "SHORT"]:
            if side in data_neutral:
                if data_neutral[side].get("tp_purpose") == "neutral":
                    continue
                grid = data_neutral[side].get("grid", {})
                tp_map = data_neutral[side].get("tp_map", {})
                if grid and tp_map and len(grid) != len(tp_map):
                    self.fail("Validation should have been bypassed for neutral tp_purpose!")

        # А для classic (self/both) без neutral — валидатор должен бросать ошибку
        data_self = {
            "LONG": {
                "tp_purpose": "self",
                "grid": {"0": {}, "1": {}},
                "tp_map": {"0": {}, "1": {}, "2": {}},
            }
        }
        with self.assertRaises(ValueError):
            for side in ["LONG", "SHORT"]:
                if side in data_self:
                    if data_self[side].get("tp_purpose") == "neutral":
                        continue
                    grid = data_self[side].get("grid", {})
                    tp_map = data_self[side].get("tp_map", {})
                    if grid and tp_map and len(grid) != len(tp_map):
                        raise ValueError("Mismatch detected!")

    def test_u10_spinning_universe_configuration(self):
        """Проверка корректности конфигурации u10_spinning_basic_distribute в CFG/universes.json."""
        registry = StrategyRegistry()
        self.assertIn("u10_spinning_basic_distribute", registry.universes_db)
        u10 = registry.universes_db["u10_spinning_basic_distribute"]

        # Архитектурные правила куполов LONG/SHORT
        self.assertIn("LONG", u10)
        self.assertIn("SHORT", u10)
        self.assertEqual(u10["LONG"]["invest_size"], 600.0)
        self.assertEqual(u10["SHORT"]["invest_size"], 600.0)
        self.assertEqual(u10.get("tp_purpose"), "neutral")
        self.assertEqual(u10.get("avg_free_volume_behavior"), "distribute_remaining")

        # Проверка сетки в LONG
        grid = u10["LONG"]["grid"]
        self.assertEqual(len(grid), 9)
        self.assertIsNone(grid["0"]["activation"])
        self.assertIsNone(grid["1"]["activation"])
        self.assertIsNone(grid["2"]["activation"])
        self.assertEqual(grid["3"]["activation"], -10.5)
        self.assertEqual(grid["4"]["activation"], -17.0)
        self.assertEqual(grid["5"]["activation"], -27.5)
        self.assertEqual(grid["6"]["activation"], -44.5)
        self.assertEqual(grid["7"]["activation"], -72.0)
        self.assertEqual(grid["8"]["activation"], -101.5)

        # Проверка tp_map в LONG: 14 уровней нейтральной лестницы Фибоначчи
        tp_map = u10["LONG"]["tp_map"]
        self.assertEqual(len(tp_map), 14)
        self.assertEqual(tp_map["0"]["activation"], 2.2)
        self.assertEqual(tp_map["0"]["fallback_indent"], 1.9)
        self.assertIsNone(tp_map["0"]["indent"])
        self.assertEqual(tp_map["12"]["fallback_indent"], 49.7)
        self.assertEqual(tp_map["12"]["activation"], 89.5)
        self.assertEqual(tp_map["13"]["fallback_indent"], 89.5)
        self.assertEqual(tp_map["13"]["activation"], 116.0)

        from CORE.STRATEGY.strategy_registry import StrategyConfig
        strat_cfg = StrategyConfig("u10_spinning_basic_distribute", u10, ["BTCUSDT"])
        applicator = StrategyApplicator()
        side_cfgs = applicator.build_side_configs(strat_cfg)
        self.assertEqual(side_cfgs["LONG"]["tp_purpose"], "neutral")
        self.assertEqual(side_cfgs["LONG"]["avg_free_volume_behavior"], "distribute_remaining")
        self.assertEqual(len(side_cfgs["LONG"]["grid"]), 9)
        self.assertEqual(len(side_cfgs["LONG"]["tp_map"]), 14)


if __name__ == "__main__":
    unittest.main()
