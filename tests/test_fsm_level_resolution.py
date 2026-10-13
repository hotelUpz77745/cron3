# ============================================================
# FILE: tests/test_fsm_level_resolution.py
# ROLE: Unit test suite for universal config-driven FSM Level Resolution
#       (eliminating symbol/level hardcodes, verifying tolerance and FSM state)
# ============================================================

import unittest
from pathlib import Path
from unittest.mock import MagicMock

from CORE.STRATEGY.strategy_applicator import StrategyApplicator
from CORE.STRATEGY.strategy_registry import StrategyConfig


class TestFsmLevelResolution(unittest.TestCase):
    """
    Юнит-тесты для верификации универсального алгоритма FSM Level Resolution:
    1. Автоматическая разметка исполненных уровней сетки по накопленному объему позиции.
    2. Учет коэффициента допуска (volume_tolerance_pct).
    3. Сохранение уже исполненных уровней из предыдущего FSM-стейта.
    4. Безопасность при отсутствии позиции (total_volume == 0).
    5. Поддержка отключения через конфиг.
    6. Полный отказ от хардкода имен символов и номеров уровней (любой символ и любая глубина сетки).
    """

    def setUp(self):
        # 7-уровневая асимметричная сетка Чита ($400 капитал)
        # L0: 12% ($48), L1: 8% ($32, cum $80), L2: 10% ($40, cum $120),
        # L3: 13% ($52, cum $172), L4: 17% ($68, cum $240),
        # L5: 25% ($100, cum $340), L6: 15% ($60, cum $400)
        self.mock_grid = {
            "0": {"enabled": True, "indent": 0.0, "volume": 12.0, "is_active": False, "is_armed": False},
            "1": {"enabled": True, "indent": -3.0, "volume": 8.0, "is_active": False, "is_armed": False},
            "2": {"enabled": True, "indent": -8.0, "volume": 10.0, "is_active": False, "is_armed": False},
            "3": {"enabled": True, "indent": -21.0, "volume": 13.0, "is_active": False, "is_armed": False},
            "4": {"enabled": True, "indent": -34.0, "volume": 17.0, "is_active": False, "is_armed": False},
            "5": {"enabled": True, "indent": -55.0, "volume": 25.0, "is_active": False, "is_armed": False},
            "6": {"enabled": True, "indent": -340.0, "volume": 15.0, "is_active": False, "is_armed": False},
        }
        self.mock_strategy = MagicMock(spec=StrategyConfig)
        self.mock_strategy.raw_meta = {
            "fsm_level_resolution": {
                "enabled": True,
                "mode": "cumulative_volume_threshold",
                "volume_tolerance_pct": 10.0,
                "auto_mark_executed": True
            }
        }

    def test_01_fsm_resolution_marks_covered_levels_active(self):
        """
        Проверка: объем позиции $120 USDT (30% депозита) размечает L0, L1, L2 как исполненные,
        а уровни L3..L6 оставляет ожидающими (is_active=False).
        """
        new_side = {
            "invest_size": 400.0,
            "grid": {k: dict(v) for k, v in self.mock_grid.items()},
            "trailing_grid_state": {"armed_levels": {"1": {}, "2": {}}}
        }
        # 1413 монет по цене $0.085 = ~$120.1 USDT (ровно L0 + L1 + L2)
        exist_side = {
            "in_position": True,
            "total_volume": 1413.0,
            "avg_entry_price": 0.085,
            "grid": {}
        }

        StrategyApplicator.resolve_executed_grid_levels(
            self.mock_strategy, "CUSTOM_COIN_USDT", "LONG", new_side, exist_side
        )

        grid = new_side["grid"]
        # L0, L1, L2 должны быть активны (исполнены) и разоружены
        self.assertTrue(grid["0"]["is_active"])
        self.assertFalse(grid["0"]["is_armed"])
        self.assertTrue(grid["1"]["is_active"])
        self.assertFalse(grid["1"]["is_armed"])
        self.assertTrue(grid["2"]["is_active"])
        self.assertFalse(grid["2"]["is_armed"])

        # L3..L6 должны остаться неисполненными
        self.assertFalse(grid["3"]["is_active"])
        self.assertFalse(grid["4"]["is_active"])
        self.assertFalse(grid["5"]["is_active"])
        self.assertFalse(grid["6"]["is_active"])

        # armed_levels очищен для исполненных ступеней
        armed = new_side["trailing_grid_state"]["armed_levels"]
        self.assertNotIn("1", armed)
        self.assertNotIn("2", armed)

    def test_02_tolerance_threshold_safety(self):
        """
        Проверка допуска volume_tolerance_pct:
        При допуске 10%, позиция объемом $110 USDT (немного меньше $120 USDT из-за комиссии/проскальзывания)
        все равно безопасно покрывает L2 (120 * 0.9 = 108 threshold).
        """
        new_side = {
            "invest_size": 400.0,
            "grid": {k: dict(v) for k, v in self.mock_grid.items()},
            "trailing_grid_state": {}
        }
        exist_side = {
            "in_position": True,
            "total_volume": 1000.0,
            "avg_entry_price": 0.110,  # $110 USDT
            "grid": {}
        }

        StrategyApplicator.resolve_executed_grid_levels(
            self.mock_strategy, "ANY_SYMBOL", "SHORT", new_side, exist_side
        )

        grid = new_side["grid"]
        self.assertTrue(grid["0"]["is_active"])
        self.assertTrue(grid["1"]["is_active"])
        self.assertTrue(grid["2"]["is_active"])
        self.assertFalse(grid["3"]["is_active"])

    def test_03_preserves_already_active_fsm_levels(self):
        """
        Проверка: если уровень L3 уже был помечен is_active=True в предыдущем FSM,
        он сохраняет активность, даже если текущий объем позиции слегка сократился (например, после частичного пилинга).
        """
        new_side = {
            "invest_size": 400.0,
            "grid": {k: dict(v) for k, v in self.mock_grid.items()},
            "trailing_grid_state": {}
        }
        # Объем меньше обычного L3, но L3 в предыдущем FSM был active
        exist_side = {
            "in_position": True,
            "total_volume": 1000.0,
            "avg_entry_price": 0.100,  # $100 USDT
            "grid": {
                "0": {"is_active": True},
                "1": {"is_active": True},
                "2": {"is_active": True},
                "3": {"is_active": True}
            }
        }

        StrategyApplicator.resolve_executed_grid_levels(
            self.mock_strategy, "VOLATILE_USDT", "LONG", new_side, exist_side
        )

        grid = new_side["grid"]
        self.assertTrue(grid["0"]["is_active"])
        self.assertTrue(grid["1"]["is_active"])
        self.assertTrue(grid["2"]["is_active"])
        self.assertTrue(grid["3"]["is_active"])
        self.assertFalse(grid["4"]["is_active"])

    def test_04_zero_position_keeps_all_inactive(self):
        """
        Проверка: при отсутствии позиции (total_volume == 0) все уровни сетки остаются неактивными.
        """
        new_side = {
            "invest_size": 400.0,
            "grid": {k: dict(v) for k, v in self.mock_grid.items()},
            "trailing_grid_state": {}
        }
        exist_side = {
            "in_position": False,
            "total_volume": 0.0,
            "avg_entry_price": 0.0,
            "grid": {}
        }

        StrategyApplicator.resolve_executed_grid_levels(
            self.mock_strategy, "IDLE_COIN", "LONG", new_side, exist_side
        )

        grid = new_side["grid"]
        for lvl_cfg in grid.values():
            self.assertFalse(lvl_cfg["is_active"])

    def test_05_disabled_config_flag_bypasses_resolution(self):
        """
        Проверка: если в конфиге enabled: false, резолвер не меняет флаги сетки.
        """
        disabled_strat = MagicMock(spec=StrategyConfig)
        disabled_strat.raw_meta = {
            "fsm_level_resolution": {
                "enabled": False
            }
        }
        new_side = {
            "invest_size": 400.0,
            "grid": {k: dict(v) for k, v in self.mock_grid.items()},
            "trailing_grid_state": {}
        }
        exist_side = {
            "in_position": True,
            "total_volume": 5000.0,
            "avg_entry_price": 0.1,  # $500 USDT (покрыло бы всё)
            "grid": {}
        }

        StrategyApplicator.resolve_executed_grid_levels(
            disabled_strat, "TEST_COIN", "LONG", new_side, exist_side
        )

        # Все уровни должны остаться нетронутыми (False)
        for lvl_cfg in new_side["grid"].values():
            self.assertFalse(lvl_cfg["is_active"])

    def test_06_arbitrary_grid_depth_and_symbol_neutrality(self):
        """
        Проверка нейтральности к монете и глубине сетки:
        Произвольная 4-уровневая сетка для монеты 'ETHUSDT' ($1000 капитал).
        """
        custom_grid = {
            "0": {"enabled": True, "volume": 25.0, "is_active": False},  # $250
            "1": {"enabled": True, "volume": 25.0, "is_active": False},  # $250 (cum $500)
            "2": {"enabled": True, "volume": 25.0, "is_active": False},  # $250 (cum $750)
            "3": {"enabled": True, "volume": 25.0, "is_active": False},  # $250 (cum $1000)
        }
        new_side = {
            "invest_size": 1000.0,
            "grid": custom_grid,
            "trailing_grid_state": {}
        }
        # $520 USDT в позиции -> должно покрыть L0 и L1, оставить L2 и L3
        exist_side = {
            "in_position": True,
            "total_volume": 0.2,
            "avg_entry_price": 2600.0,  # $520 USDT
            "grid": {}
        }

        StrategyApplicator.resolve_executed_grid_levels(
            self.mock_strategy, "ETHUSDT", "LONG", new_side, exist_side
        )

        self.assertTrue(new_side["grid"]["0"]["is_active"])
        self.assertTrue(new_side["grid"]["1"]["is_active"])
        self.assertFalse(new_side["grid"]["2"]["is_active"])
        self.assertFalse(new_side["grid"]["3"]["is_active"])


if __name__ == "__main__":
    unittest.main()
