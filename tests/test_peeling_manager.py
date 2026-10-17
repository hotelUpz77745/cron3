# tests/test_peeling_manager.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: tests.test_peeling_manager
ROLE: Comprehensive unit tests for PeelingManager (Subgrid LIFO Peeling)
==============================================================================
"""

import unittest
from unittest.mock import AsyncMock, MagicMock
from CORE.STRATEGY.peeling_manager import PeelingManager
from POS_FSM.models import PositionState


class TestPeelingManager(unittest.IsolatedAsyncioTestCase):
    """
    Тестирование LIFO-пилинга объемов сетки (Subgrid LIFO Peeling):
    1. has_peel_levels: детекция уровней с peel_tp_pct.
    2. process_peel: условия срабатывания, расчет отскока цены, выставление ордеров,
       учет лимитов peel_num / peel_once, сброс или блокировка уровня.
    """

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.cancel_orders_for_side = AsyncMock(return_value=True)
        self.mock_client.make_order = AsyncMock(return_value=MagicMock(success=True, order_id="PEEL123", error_msg=""))
        self.mock_client.fetch_positions = AsyncMock(return_value=[
            {"positionSide": "LONG", "positionAmt": "0.50", "entryPrice": "100.0"}
        ])

        self.mock_runtime = MagicMock()
        self.mock_runtime.mark_dirty = MagicMock()
        self.mock_runtime.caches = {}

        self.mock_tp = MagicMock()
        self.mock_tp.place_take_profit = AsyncMock(return_value=True)

        self.spec_data = {
            "symbols": [
                {
                    "symbol": "TESTUSDT",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    ],
                }
            ]
        }

    def test_has_peel_levels_detection(self):
        """Проверяет детекцию уровней с peel_tp_pct."""
        state = PositionState(symbol="TESTUSDT", side="LONG")
        state.grid = {
            "0": {"indent": 1.0, "volume": 10.0},
            "1": {"indent": 2.0, "volume": 20.0},
        }
        self.assertFalse(PeelingManager.has_peel_levels(state))

        state.grid["2"] = {"indent": 3.0, "volume": 30.0, "peel_tp_pct": 1.5}
        self.assertTrue(PeelingManager.has_peel_levels(state))

    async def test_process_peel_long_rebound_success(self):
        """Проверяет успешное срабатывание пилинга в LONG при отскоке цены вверх."""
        state = PositionState(symbol="TESTUSDT", side="LONG")
        state.in_position = True
        state.total_volume = 1.0
        state.grid = {
            "0": {"is_active": True, "fill_price": 100.0, "fill_qty": 0.2, "enabled": True},
            "1": {"is_active": True, "fill_price": 95.0, "fill_qty": 0.3, "enabled": True},
            "2": {
                "is_active": True,
                "fill_price": 90.0,
                "fill_qty": 0.5,
                "peel_tp_pct": 2.0,
                "peel_ratio": 1.0,
                "peel_num": 1,
                "peeled": False,
                "enabled": True,
            },
        }

        # Отскок от 90.0: при цене 92.0 -> отскок (92 - 90)/90 = +2.22% >= 2.0%
        result = await PeelingManager.process_peel(
            client=self.mock_client,
            runtime_manager=self.mock_runtime,
            symbol="TESTUSDT",
            side="LONG",
            state=state,
            current_price=92.0,
            spec_data=self.spec_data,
            tp_manager=self.mock_tp,
        )

        self.assertTrue(result)
        self.mock_client.cancel_orders_for_side.assert_called_once_with("TESTUSDT", "LONG")
        self.mock_client.make_order.assert_called_once()
        call_kwargs = self.mock_client.make_order.call_args[1]
        self.assertEqual(call_kwargs["symbol"], "TESTUSDT")
        self.assertEqual(call_kwargs["side"], "SELL")
        self.assertEqual(call_kwargs["position_side"], "LONG")
        self.assertEqual(call_kwargs["market_type"], "MARKET")

        # Проверяем, что уровень 2 получил статус peeled
        lvl2 = state.grid["2"]
        self.assertTrue(lvl2["peeled"])
        self.assertEqual(lvl2["peeled_count"], 1)

    async def test_process_peel_rebound_insufficient_no_trigger(self):
        """Проверяет, что при недостаточном отскоке пилинг не срабатывает."""
        state = PositionState(symbol="TESTUSDT", side="LONG")
        state.in_position = True
        state.total_volume = 1.0
        state.grid = {
            "2": {
                "is_active": True,
                "fill_price": 90.0,
                "fill_qty": 0.5,
                "peel_tp_pct": 5.0,
                "peel_ratio": 1.0,
                "peel_num": 1,
                "peeled": False,
                "enabled": True,
            },
        }

        # Отскок от 90.0: при цене 91.0 -> отскок (91 - 90)/90 = +1.11% < 5.0%
        result = await PeelingManager.process_peel(
            client=self.mock_client,
            runtime_manager=self.mock_runtime,
            symbol="TESTUSDT",
            side="LONG",
            state=state,
            current_price=91.0,
            spec_data=self.spec_data,
            tp_manager=self.mock_tp,
        )

        self.assertFalse(result)
        self.mock_client.make_order.assert_not_called()

    async def test_process_peel_short_rebound_success(self):
        """Проверяет успешное срабатывание пилинга в SHORT при падении цены вниз."""
        state = PositionState(symbol="TESTUSDT", side="SHORT")
        state.in_position = True
        state.total_volume = -1.0
        state.grid = {
            "1": {
                "is_active": True,
                "fill_price": 100.0,
                "fill_qty": 0.5,
                "peel_tp_pct": 2.0,
                "peel_ratio": 0.5,
                "peel_num": 2,
                "peeled": False,
                "enabled": True,
            },
        }

        # В SHORT отскок вниз: при цене 97.0 -> отскок (100 - 97)/100 = +3.0% >= 2.0%
        result = await PeelingManager.process_peel(
            client=self.mock_client,
            runtime_manager=self.mock_runtime,
            symbol="TESTUSDT",
            side="SHORT",
            state=state,
            current_price=97.0,
            spec_data=self.spec_data,
            tp_manager=self.mock_tp,
        )

        self.assertTrue(result)
        call_kwargs = self.mock_client.make_order.call_args[1]
        self.assertEqual(call_kwargs["side"], "BUY")
        self.assertEqual(call_kwargs["position_side"], "SHORT")


if __name__ == "__main__":
    unittest.main()
