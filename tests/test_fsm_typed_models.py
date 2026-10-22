# tests/test_fsm_typed_models.py
# -*- coding: utf-8 -*-
"""
==============================================================================
FILE: tests/test_fsm_typed_models.py
ROLE: Тестирование строгой типизации FSM моделей (Phase 3 плана рефакторинга)
      GridLevel, TakeProfitLevel, PositionState Typed Accessors & State Guards
==============================================================================
"""

import unittest
from POS_FSM.models import PositionState, GridLevel, TakeProfitLevel


class TestFSMTypedModels(unittest.TestCase):
    """Тесты структур данных и предикатов переходов состояний FSM."""

    def test_grid_level_dataclass_and_serialization(self):
        """Проверка инициализации, to_dict и from_dict для GridLevel."""
        raw = {
            "enabled": True,
            "indent": -2.5,
            "volume": 15.0,
            "is_active": True,
            "is_armed": False,
            "peeled": True,
            "peel_count": 2,
            "order_id": 987654321
        }
        lvl = GridLevel.from_dict(raw)
        self.assertTrue(lvl.enabled)
        self.assertEqual(lvl.indent, -2.5)
        self.assertEqual(lvl.volume, 15.0)
        self.assertTrue(lvl.is_active)
        self.assertFalse(lvl.is_armed)
        self.assertTrue(lvl.peeled)
        self.assertEqual(lvl.peel_count, 2)
        self.assertEqual(lvl.order_id, 987654321)

        d = lvl.to_dict()
        self.assertEqual(d["indent"], -2.5)
        self.assertEqual(d["peeled_count"], 2)
        self.assertEqual(d["order_id"], 987654321)
        self.assertTrue(d["is_active"])

    def test_tp_level_dataclass_and_serialization(self):
        """Проверка инициализации, to_dict и from_dict для TakeProfitLevel."""
        raw = {
            "profit": 1.75,
            "close": 50.0,
            "is_active": False,
            "order_id": 112233
        }
        tp = TakeProfitLevel.from_dict(raw)
        self.assertEqual(tp.profit, 1.75)
        self.assertEqual(tp.close, 50.0)
        self.assertFalse(tp.is_active)
        self.assertEqual(tp.order_id, 112233)

        d = tp.to_dict()
        self.assertEqual(d["indent"], 1.75)
        self.assertEqual(d["volume"], 50.0)
        self.assertEqual(d["order_id"], 112233)
        self.assertFalse(d["is_active"])

    def test_position_state_typed_grid_accessors(self):
        """Проверка типизированных аксессоров сетки в PositionState."""
        state = PositionState(symbol="ETHUSDT", side="LONG")
        state.grid = {
            "0": {"enabled": True, "indent": 0.0, "volume": 10.0, "is_active": True, "peeled": False},
            "1": {"enabled": True, "indent": -3.0, "volume": 20.0, "is_active": False, "peeled": False},
            "2": {"enabled": False, "indent": -6.0, "volume": 30.0, "is_active": False, "peeled": False},
        }

        # get_typed_grid_level
        lvl0 = state.get_typed_grid_level("0")
        self.assertIsNotNone(lvl0)
        self.assertEqual(lvl0.volume, 10.0)
        self.assertTrue(lvl0.is_active)

        self.assertIsNone(state.get_typed_grid_level("99"))

        # iter_grid_levels
        typed_map = dict(state.iter_grid_levels())
        self.assertEqual(len(typed_map), 3)
        self.assertEqual(typed_map["1"].indent, -3.0)

        # active_grid_keys
        active_keys = state.active_grid_keys()
        self.assertEqual(active_keys, ["0"])

        # mark_level_active
        state.mark_level_active("1", True)
        self.assertTrue(state.is_level_active("1"))
        self.assertEqual(sorted(state.active_grid_keys()), ["0", "1"])

        # peel operations
        self.assertFalse(state.is_level_peeled("0"))
        state.increment_peel_count("0")
        self.assertTrue(state.is_level_peeled("0"))
        lvl0_after = state.get_typed_grid_level("0")
        self.assertEqual(lvl0_after.peel_count, 1)

    def test_position_state_typed_tp_accessors(self):
        """Проверка типизированных аксессоров TP в PositionState."""
        state = PositionState(symbol="BTCUSDT", side="SHORT")
        state.tp_map = {
            "0": {"profit": 1.2, "close": 50.0, "is_active": True},
            "1": {"profit": 2.4, "close": 50.0, "is_active": False},
        }

        tp0 = state.get_typed_tp_level("0")
        self.assertIsNotNone(tp0)
        self.assertEqual(tp0.profit, 1.2)
        self.assertTrue(state.is_tp_active("0"))
        self.assertFalse(state.is_tp_active("1"))

        state.mark_tp_active("1", True)
        self.assertTrue(state.is_tp_active("1"))

    def test_state_machine_guards(self):
        """Проверка институциональных защитных предикатов FSM."""
        state = PositionState(symbol="SOLUSDT", side="LONG")
        # 1. Позиция не открыта
        self.assertFalse(state.can_start_averaging())
        self.assertFalse(state.can_peel())
        self.assertFalse(state.can_place_tp())

        # 2. Позиция открыта, объем на бирже подтвержден
        state.in_position = True
        state.total_volume = 15.0
        state.avg_entry_price = 140.0
        self.assertTrue(state.can_start_averaging())
        self.assertTrue(state.can_peel())
        self.assertTrue(state.can_place_tp())

        # 3. Блокировка усреднения при уже взведенном pending_avg
        state.pending_avg = True
        self.assertFalse(state.can_start_averaging())
        state.pending_avg = False
        self.assertTrue(state.can_start_averaging())

        # 4. Блокировка пилинга при уже взведенном pending_peel
        state.pending_peel = True
        self.assertFalse(state.can_peel())
        state.pending_peel = False
        self.assertTrue(state.can_peel())

        # 5. При флаге завершения (is_finished=True) любые новые ордера заблокированы
        state.is_finished = True
        self.assertFalse(state.can_start_averaging())
        self.assertFalse(state.can_peel())
        self.assertFalse(state.can_place_tp())

    def test_in_position_papper_idempotency_sanctity(self):
        """
        Проверка неприкосновенности флага in_position_papper:
        Держит статус 'в позиции' до подтверждения биржей, защищая от дублей.
        При подтверждении биржей set_in_position(True) автоматически гасит papper.
        """
        state = PositionState(symbol="DOGEUSDT", side="LONG")
        self.assertFalse(state.in_pos)
        self.assertFalse(state.in_position)
        self.assertFalse(state.in_position_papper)

        # Выставили ордер на вход - взводим paper
        state.in_position_papper = True
        self.assertTrue(state.in_position_papper)
        self.assertFalse(state.in_position)
        # Усреднение НЕ разрешено, пока позиция только на бумаге (биржевой fill еще не пришел)
        self.assertFalse(state.can_start_averaging())

        # Пришел WebSocket ACCOUNT_UPDATE с реальным объемом
        state.total_volume = 500.0
        state.avg_entry_price = 0.15
        state.set_in_position(True)

        self.assertTrue(state.in_pos)
        self.assertTrue(state.in_position)
        self.assertFalse(state.in_position_papper)
        self.assertTrue(state.can_start_averaging())


if __name__ == "__main__":
    unittest.main()
