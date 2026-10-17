# tests/test_tp_managers_extended.py
# Role: Comprehensive unit tests for DynamicATRTakeProfitManager, FallbackTpManager, and TakeProfitRestorer

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.TP.dynamic_atr_tp_manager import DynamicATRTakeProfitManager
from CORE.TP.fallback_tp_manager import FallbackTpManager
from CORE.TP.take_profit_restorer import TakeProfitRestorer


class TestDynamicATRTakeProfitManager(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.spec_data = {
            "price_precision": 2,
            "min_notional": 5.0,
            "tick_size": 0.01,
            "step_size": 0.001,
        }
        self.cfg = {
            "enabled": True,
            "timeframe": "15m",
            "period": 14,
            "cache_ttl_sec": 60,
            "fallback_atr_pct": 2.0,
            "min_guaranteed_floor_pct": 0.50,
            "max_ceiling_pct": 5.0,
            "level_multipliers": {
                "0": 1.0,
                "1": 0.8,
                "2": 0.6,
            },
        }

    async def test_disabled_config_returns_none(self):
        cfg_disabled = {"enabled": False}
        price, pct = await DynamicATRTakeProfitManager.calculate_tp_price(
            client=None,
            symbol="BTCUSDT",
            side="LONG",
            avg_entry_price=50000.0,
            active_level="0",
            cfg=cfg_disabled,
            spec_data=self.spec_data,
        )
        self.assertIsNone(price)
        self.assertEqual(pct, 0.0)

    @patch("CORE.TP.dynamic_atr_tp_manager.ATRProvider.get_atr_pct", new_callable=AsyncMock)
    async def test_calculate_tp_price_long_and_short(self, mock_get_atr):
        # ATR is 2.0%. Multiplier for level 0 is 1.0 -> 2.0%
        mock_get_atr.return_value = 2.0

        # LONG: 100 * (1 + 0.02) = 102.0
        price_long, pct_long = await DynamicATRTakeProfitManager.calculate_tp_price(
            client=MagicMock(),
            symbol="ETHUSDT",
            side="LONG",
            avg_entry_price=100.0,
            active_level="0",
            cfg=self.cfg,
            spec_data=self.spec_data,
        )
        self.assertEqual(pct_long, 2.0)
        self.assertAlmostEqual(price_long, 102.0, places=2)

        # SHORT: 100 * (1 - 0.02) = 98.0
        price_short, pct_short = await DynamicATRTakeProfitManager.calculate_tp_price(
            client=MagicMock(),
            symbol="ETHUSDT",
            side="SHORT",
            avg_entry_price=100.0,
            active_level="0",
            cfg=self.cfg,
            spec_data=self.spec_data,
        )
        self.assertEqual(pct_short, 2.0)
        self.assertAlmostEqual(price_short, 98.0, places=2)

    @patch("CORE.TP.dynamic_atr_tp_manager.ATRProvider.get_atr_pct", new_callable=AsyncMock)
    async def test_clamping_floor_and_ceiling(self, mock_get_atr):
        # Test floor clamp: ATR very low (0.1% * 1.0 = 0.1% -> clamped to min_guaranteed_floor_pct=0.50%)
        mock_get_atr.return_value = 0.1
        _, pct_floor = await DynamicATRTakeProfitManager.calculate_tp_price(
            client=MagicMock(),
            symbol="ETHUSDT",
            side="LONG",
            avg_entry_price=100.0,
            active_level="0",
            cfg=self.cfg,
            spec_data=self.spec_data,
        )
        self.assertEqual(pct_floor, 0.50)

        # Test ceiling clamp: ATR very high (10.0% * 1.0 = 10.0% -> clamped to max_ceiling_pct=5.0%)
        mock_get_atr.return_value = 10.0
        _, pct_ceil = await DynamicATRTakeProfitManager.calculate_tp_price(
            client=MagicMock(),
            symbol="ETHUSDT",
            side="LONG",
            avg_entry_price=100.0,
            active_level="0",
            cfg=self.cfg,
            spec_data=self.spec_data,
        )
        self.assertEqual(pct_ceil, 5.0)

    @patch("CORE.TP.dynamic_atr_tp_manager.ATRProvider.get_atr_pct", new_callable=AsyncMock)
    async def test_level_multiplier_fallback_to_highest(self, mock_get_atr):
        # Request level "5", but max defined is "2" with mult=0.6
        mock_get_atr.return_value = 2.0
        _, pct = await DynamicATRTakeProfitManager.calculate_tp_price(
            client=MagicMock(),
            symbol="ETHUSDT",
            side="LONG",
            avg_entry_price=100.0,
            active_level="5",
            cfg=self.cfg,
            spec_data=self.spec_data,
        )
        # 2.0 * 0.6 = 1.2%
        self.assertAlmostEqual(pct, 1.2, places=2)


class TestFallbackTpManager(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = FallbackTpManager()
        self.spec_data = {
            "price_precision": 2,
            "min_notional": 5.0,
            "step_size": 0.001,
            "min_qty": 0.001,
        }

    async def test_not_in_position_skips(self):
        state = MagicMock()
        state.in_position = False
        client = AsyncMock()

        await self.manager.process(
            client=client,
            runtime_manager=MagicMock(),
            symbol="BTCUSDT",
            side="LONG",
            state=state,
            current_price=50000.0,
            spec_data=self.spec_data,
        )
        client.cancel_orders_for_side.assert_not_called()
        client.make_order.assert_not_called()

    async def test_fallback_market_trigger_long(self):
        state = MagicMock()
        state.in_position = True
        state.is_finished = False
        state.pending_rolling_tp = False
        state.avg_entry_price = 100.0
        state.total_volume = 2.5
        state.fallback_price = 105.0
        state.tp_map = {}
        state.trailing_tp_state = {}

        client = AsyncMock()
        client.cancel_orders_for_side = AsyncMock()
        client.make_order = AsyncMock(return_value=MagicMock(success=True))

        # Price is 106.0 >= fallback_price 105.0 -> Triggered!
        await self.manager.process(
            client=client,
            runtime_manager=MagicMock(),
            symbol="SOLUSDT",
            side="LONG",
            state=state,
            current_price=106.0,
            spec_data=self.spec_data,
        )

        client.cancel_orders_for_side.assert_awaited_once_with("SOLUSDT", "LONG")
        client.make_order.assert_awaited_once()
        call_kwargs = client.make_order.call_args.kwargs
        self.assertEqual(call_kwargs["symbol"], "SOLUSDT")
        self.assertEqual(call_kwargs["side"], "SELL")
        self.assertEqual(call_kwargs["market_type"], "MARKET")
        self.assertTrue(state.is_finished)
        self.assertFalse(state.pending_rolling_tp)

    async def test_fallback_market_handles_reduce_only_rejection(self):
        state = MagicMock()
        state.in_position = True
        state.is_finished = False
        state.pending_rolling_tp = False
        state.avg_entry_price = 100.0
        state.total_volume = 1.0
        state.fallback_price = 95.0
        state.tp_map = {}
        state.trailing_tp_state = {}

        client = AsyncMock()
        # -2022 error: ReduceOnly order rejected because position was already closed by limit TP
        mock_res = MagicMock(success=False, error_code=-2022, error_msg="Order would immediately trigger reduce-only.")
        client.make_order = AsyncMock(return_value=mock_res)

        # SHORT: price <= 95.0 -> 94.0 triggers
        await self.manager.process(
            client=client,
            runtime_manager=MagicMock(),
            symbol="SOLUSDT",
            side="SHORT",
            state=state,
            current_price=94.0,
            spec_data=self.spec_data,
        )
        self.assertTrue(state.is_finished)
        self.assertFalse(state.pending_rolling_tp)


class TestTakeProfitRestorer(unittest.IsolatedAsyncioTestCase):
    async def test_no_bot_core_returns_zero_stats(self):
        restorer = TakeProfitRestorer(bot_core=None)
        stats = await restorer.verify_and_restore_all()
        self.assertEqual(stats, {"checked": 0, "confirmed": 0, "restored": 0, "mismatch": 0})

    async def test_throttling_within_15s_without_force(self):
        mock_core = MagicMock()
        restorer = TakeProfitRestorer(bot_core=mock_core)
        restorer._last_run_ts = 1000.0

        with patch("time.time", return_value=1005.0):
            stats = await restorer.verify_and_restore_all(force=False)
            self.assertEqual(stats["checked"], 0)
            mock_core.client.fetch_open_orders.assert_not_called()

    async def test_transient_api_error_returns_clean_stats_without_exception(self):
        mock_core = MagicMock()
        mock_core.client.fetch_open_orders = AsyncMock(side_effect=ConnectionError("503 Service Unavailable"))
        restorer = TakeProfitRestorer(bot_core=mock_core)

        stats = await restorer.verify_and_restore_all(force=True)
        self.assertEqual(stats, {"checked": 0, "confirmed": 0, "restored": 0, "mismatch": 0})


if __name__ == "__main__":
    unittest.main()
