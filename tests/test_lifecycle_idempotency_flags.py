# tests/test_lifecycle_idempotency_flags.py
# -*- coding: utf-8 -*-
"""
Унитарные тесты для институциональных защитных флагов идемпотентности жизненного цикла:
1. ВХОД: in_position_papper (изоляция дублирующих ордеров до подтверждения сделки от WS).
2. УСРЕДНЕНИЕ: pending_avg (изоляция повторного усреднения сетки в полете).
3. ТЕЙК-ПРОФИТ: pending_rolling_tp (изоляция параллельного выставления/подтяжки TP).
4. ЗАКРЫТИЕ: is_closing / is_finished (изоляция всех операций при экстренном или плановом закрытии).
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from POS_FSM.models import PositionState
from CORE.TP.tp_manager import TakeProfitManager
from CORE.TP.fallback_tp_manager import FallbackTpManager
from CORE.TP.take_profit_restorer import TakeProfitRestorer
from CORE.CYCLE.open_position_lifecycle import OpenPositionLifecycleHandler


class TestLifecycleIdempotencyFlags(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.state = PositionState(symbol="BTCUSDT", side="LONG")
        self.state.total_volume = 1.0
        self.state.avg_entry_price = 50000.0
        self.state.grid = {"0": {"is_active": True}}
        self.state.tp_map = {"0": {"indent": 1.5, "is_active": True}}
        self.spec_data = {"tick_size": 0.1, "step_size": 0.001, "min_qty": 0.001}

    # -------------------------------------------------------------
    # 1. ВХОД: in_position_papper
    # -------------------------------------------------------------
    def test_entry_in_position_papper_barrier(self):
        """Проверка: in_position_papper блокирует повторный вход и сбрасывается при WS sync."""
        state = self.state
        state.in_position = False
        state.in_position_papper = False

        # До входа позиция свободна
        self.assertFalse(state.in_position)
        self.assertFalse(state.in_position_papper)

        # Сигнал получен: институциональный барьер взводится ДО отправки на биржу
        state.in_position_papper = True
        self.assertTrue(state.in_position_papper)

        # Повторный сигнал на том же тике должен быть отвергнут
        can_enter = (not state.in_position and not state.in_position_papper)
        self.assertFalse(can_enter)

        # Биржа прислала подтверждение по WebSocket: set_in_position(True)
        state.set_in_position(True)
        self.assertTrue(state.in_position)
        self.assertFalse(state.in_position_papper)  # Автоматический сброс

    # -------------------------------------------------------------
    # 2. УСРЕДНЕНИЕ: pending_avg
    # -------------------------------------------------------------
    def test_averaging_pending_avg_invariants(self):
        """Проверка: pending_avg блокирует повторные усреднения и конфликты с TP/закрытием."""
        state = self.state
        state.in_position = True
        state.pending_avg = False
        state.pending_rolling_tp = False
        state.is_closing = False
        state.is_finished = False

        # В штатном состоянии усреднение разрешено
        self.assertTrue(state.can_start_averaging())

        # 1. В процессе усреднения: pending_avg = True
        state.pending_avg = True
        self.assertFalse(state.can_start_averaging())
        state.pending_avg = False

        # 2. В процессе выставления TP: pending_rolling_tp = True блокирует усреднение
        state.pending_rolling_tp = True
        self.assertFalse(state.can_start_averaging())
        state.pending_rolling_tp = False

        # 3. В процессе закрытия: is_closing = True блокирует усреднение
        state.is_closing = True
        self.assertFalse(state.can_start_averaging())
        state.is_closing = False

        # 4. Позиция завершена: is_finished = True блокирует усреднение
        state.is_finished = True
        self.assertFalse(state.can_start_averaging())

    # -------------------------------------------------------------
    # 3. ТЕЙК-ПРОФИТ: pending_rolling_tp
    # -------------------------------------------------------------
    async def test_take_profit_pending_rolling_tp_isolation(self):
        """Проверка: place_take_profit изолирован флагом pending_rolling_tp."""
        mock_rm = MagicMock()
        tp_mgr = TakeProfitManager(mock_rm)
        mock_client = AsyncMock()
        mock_client.make_order = AsyncMock(return_value=MagicMock(success=True, data={"orderId": 12345}))
        mock_client.cancel_orders_for_side = AsyncMock()

        state = self.state
        state.in_position = True
        state.pending_rolling_tp = False
        state.is_closing = False
        state.is_finished = False

        # 1. Если pending_rolling_tp уже True (другой ордер летит), вызов отвергается
        state.pending_rolling_tp = True
        res = await tp_mgr.place_take_profit(
            mock_client, "BTCUSDT", "LONG", 50000.0, self.spec_data, state
        )
        self.assertFalse(res)
        mock_client.make_order.assert_not_called()
        state.pending_rolling_tp = False

        # 2. При нормальном вызове флаг pending_rolling_tp взводится и в finally сбрасывается
        res = await tp_mgr.place_take_profit(
            mock_client, "BTCUSDT", "LONG", 50000.0, self.spec_data, state
        )
        self.assertTrue(res)
        mock_client.make_order.assert_called_once()
        self.assertFalse(state.pending_rolling_tp)  # Гарантированно сброшен в False

        # 3. При ошибке API или исключении флаг pending_rolling_tp также гарантированно снимается
        mock_client.make_order.reset_mock()
        mock_client.make_order.side_effect = RuntimeError("Binance 502 Bad Gateway")
        with self.assertRaises(RuntimeError):
            await tp_mgr.place_take_profit(
                mock_client, "BTCUSDT", "LONG", 50000.0, self.spec_data, state
            )
        self.assertFalse(state.pending_rolling_tp)

    # -------------------------------------------------------------
    # 4. ЗАКРЫТИЕ: is_closing и is_finished
    # -------------------------------------------------------------
    async def test_closing_flags_block_all_operations(self):
        """Проверка: флаг is_closing строго изолирует все жизненные циклы."""
        state = self.state
        state.in_position = True
        state.is_closing = True

        # Нельзя усреднять
        self.assertFalse(state.can_start_averaging())
        # Нельзя пилить
        self.assertFalse(state.can_peel())
        # Нельзя ставить TP
        self.assertFalse(state.can_place_tp())

        # Fallback TP игнорирует позиции с is_closing=True
        fb_mgr = FallbackTpManager()
        mock_client = AsyncMock()
        mock_rm = MagicMock()
        await fb_mgr.process(mock_client, mock_rm, "BTCUSDT", "LONG", state, 50000.0, self.spec_data)
        mock_client.make_order.assert_not_called()

        # OpenPositionLifecycleHandler игнорирует позиции с is_closing=True или is_finished=True
        mock_bot = MagicMock()
        mock_bot._closing_symbols = set()
        await OpenPositionLifecycleHandler.process_open_position(
            mock_bot, "BTCUSDT", "LONG", state, None, 50000.0
        )
        mock_bot.avg_manager.process.assert_not_called()
        mock_bot.fallback_tp_manager.process.assert_not_called()

        # При сбросе в дефолт (reset) все флаги очищаются
        state.reset()
        self.assertFalse(state.in_position)
        self.assertFalse(state.in_position_papper)
        self.assertFalse(state.pending_avg)
        self.assertFalse(state.pending_rolling_tp)
        self.assertFalse(state.is_closing)
        self.assertFalse(state.is_finished)


if __name__ == "__main__":
    unittest.main()
