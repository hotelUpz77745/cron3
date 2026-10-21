# TESTS/test_positive_profit_guard.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: TESTS/test_positive_profit_guard.py
Role: Unit tests for Positive Section Profit Guard, Min Profit Guarantee,
      and Unverified API Sync / Restart Peak Protection.
==============================================================================
"""

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from CORE.STRATEGY.trailing_system import BaseTrailingSystem
from CORE.SERVICES.auto_closer import AutoCloser
from ANALYTICS.drawdown_tracker import DrawdownTracker
from ANALYTICS.strategy_stats import StrategyStatsEngine


class TestPositiveProfitGuard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ratchet_cfg = {
            "enabled": True,
            "scale_money": 1.0,
            "levels": [
                {"activation": 13.0, "floor": 8.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L0"},
                {"activation": 21.0, "floor": 13.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L1"},
                {"activation": 34.0, "floor": 21.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L2"},
            ],
        }

    def test_evaluate_ratchet_blocks_negative_and_zero_net(self):
        """Positive section must NEVER close when iter_net <= 0.0 regardless of armed floor."""
        state = {
            "armed_floor": 8.0,
            "active_level_idx": 0,
            "armed_level_desc": "L0",
            "highest_net": 15.0,
        }

        # Negative net (drawdown)
        should_close, trig_floor, info, is_stag = BaseTrailingSystem.evaluate_ratchet(
            iter_net=-0.23,
            iter_age_hours=10.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
        )
        self.assertFalse(should_close, "Ratchet must NOT trigger when iter_net < 0")
        self.assertIn("TRAILING RATCHET BLOCKED", info)
        self.assertIn("strictly forbidden", info)

        # Zero net
        should_close_zero, _, info_zero, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=0.0,
            iter_age_hours=10.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
        )
        self.assertFalse(should_close_zero, "Ratchet must NOT trigger when iter_net == 0.0")
        self.assertIn("TRAILING RATCHET BLOCKED", info_zero)

    def test_evaluate_ratchet_min_profit_guarantee_default_half_step(self):
        """When min_guaranteed_profit_usdt is not specified, default to half of step floor (> 0)."""
        state = {
            "armed_floor": 8.0,
            "active_level_idx": 0,
            "armed_level_desc": "L0",
            "highest_net": 15.0,
        }
        # L0 floor is 8.0 -> default min profit guarantee is 0.5 * 8.0 = 4.0 USDT
        # If iter_net drops to 3.0 USDT (below 4.0 guarantee), it must be blocked!
        should_close, trig_floor, info, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=3.0,
            iter_age_hours=5.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
        )
        self.assertFalse(should_close, "Ratchet must NOT trigger below half-step minimum guarantee (4.0 USDT)")
        self.assertIn("MinGuard 4.00", info)

        # But if iter_net is 8.0 USDT (<= armed_floor 8.0, and >= 4.0), it should normally trigger
        should_close_ok, trig_ok, info_ok, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=8.0,
            iter_age_hours=5.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
        )
        self.assertTrue(should_close_ok, "Ratchet SHOULD trigger when at armed floor and above min guarantee")
        self.assertEqual(trig_ok, 8.0)

    def test_evaluate_ratchet_explicit_min_guaranteed_profit(self):
        """When min_guaranteed_profit_usdt is explicitly configured, it must be strictly enforced."""
        cfg = copy.deepcopy(self.ratchet_cfg)
        cfg["min_guaranteed_profit_usdt"] = 6.0
        state = {
            "armed_floor": 8.0,
            "active_level_idx": 0,
            "armed_level_desc": "L0",
            "highest_net": 15.0,
        }

        # iter_net = 5.5 < 6.0 min guarantee -> blocked
        should_close, _, info, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=5.5,
            iter_age_hours=5.0,
            ratchet_cfg=cfg,
            state=state,
        )
        self.assertFalse(should_close)
        self.assertIn("MinGuard 6.00", info)

        # iter_net = 7.5 <= floor 8.0 and >= 6.0 min guarantee -> triggers
        should_close_ok, trig_ok, _, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=7.5,
            iter_age_hours=5.0,
            ratchet_cfg=cfg,
            state=state,
        )
        self.assertTrue(should_close_ok)
        self.assertEqual(trig_ok, 8.0)

    async def test_auto_closer_invariant_blocks_positive_loss_closure(self):
        """AutoCloser top-level invariant must block any positive close if iter_net <= 0.0."""
        mock_bot = MagicMock()
        mock_bot.close_strategy_positions = AsyncMock()
        mock_bot.close_all_positions = AsyncMock()
        mock_bot.notifier = MagicMock()
        mock_bot.notifier.send_alert = AsyncMock()
        mock_bot.strategy_registry = MagicMock()

        strat_cfg = MagicMock()
        strat_cfg.name = "TestStrat"
        strat_cfg.symbols = ["BTCUSDT"]
        strat_cfg.auto_closing_cfg = {
            "mode": "trailing_ratchet",
            "positive": {
                "trailing_ratchet": self.ratchet_cfg,
            },
            "negative": {"threshold": None},
        }
        mock_bot.strategy_registry.get_active_strategies.return_value = {"u_test": strat_cfg}

        closer = AutoCloser(bot_core=mock_bot)

        # Mock strat analytics with negative iteration profit (-2.50)
        app_data = {"active_strategies": {"u_test": {"enabled": True}}}
        strat_analytics = {
            "_floating_synced": True,
            "net_profit_usdt": -2.50,
            "iteration_index": 1,
            "iteration_start_net_profit_usdt": 0.0,
            "iteration_net_profit_usdt": -2.50,
        }

        def mock_read_json_neg(path):
            return app_data if "app.json" in str(path) else strat_analytics

        with patch("CORE.SERVICES.auto_closer.Utils.read_json_file", side_effect=mock_read_json_neg):
            await closer.check()

        mock_bot.close_strategy_positions.assert_not_called()
        mock_bot.close_all_positions.assert_not_called()

    async def test_auto_closer_skips_when_floating_unverified(self):
        """AutoCloser must skip evaluation when _floating_synced is False or net_profit_usdt is None."""
        mock_bot = MagicMock()
        mock_bot.close_strategy_positions = AsyncMock()
        mock_bot.strategy_registry = MagicMock()

        strat_cfg = MagicMock()
        strat_cfg.name = "TestStrat"
        strat_cfg.symbols = ["BTCUSDT"]
        strat_cfg.auto_closing_cfg = {"mode": "trailing_ratchet", "positive": self.ratchet_cfg}
        mock_bot.strategy_registry.get_active_strategies.return_value = {"u_test": strat_cfg}

        closer = AutoCloser(bot_core=mock_bot)
        app_data = {"active_strategies": {"u_test": {"enabled": True}}}

        # Case 1: _floating_synced is False
        strat_analytics_unsynced = {
            "_floating_synced": False,
            "net_profit_usdt": 100.0,
            "iteration_net_profit_usdt": 100.0,
        }
        with patch("CORE.SERVICES.auto_closer.Utils.read_json_file", side_effect=lambda p: app_data if "app.json" in str(p) else strat_analytics_unsynced):
            await closer.check()
        mock_bot.close_strategy_positions.assert_not_called()

        # Case 2: net_profit_usdt is None
        strat_analytics_none = {
            "_floating_synced": True,
            "net_profit_usdt": None,
            "iteration_net_profit_usdt": None,
        }
        with patch("CORE.SERVICES.auto_closer.Utils.read_json_file", side_effect=lambda p: app_data if "app.json" in str(p) else strat_analytics_none):
            await closer.check()
        mock_bot.close_strategy_positions.assert_not_called()

    async def test_drawdown_tracker_invalid_api_does_not_modify_metrics(self):
        """If fetch_account_info returns invalid/failed response, metrics are NOT recalculated."""
        mock_manager = MagicMock()
        mock_manager._sync_locks = set()
        mock_manager._floating_synced = False
        tracker = DrawdownTracker(mock_manager)

        mock_client = MagicMock()
        res_fail = MagicMock()
        res_fail.success = False
        res_fail.error_msg = "API Rate Limit 429"
        mock_client.fetch_account_info = AsyncMock(return_value=res_fail)

        data = {"start_balance_usdt": 450.0, "cur_balance_usdt": 450.0, "peak_balance_usdt": 450.0}
        success = await tracker.update_drawdowns(mock_client, data)
        self.assertFalse(success)
        self.assertFalse(tracker._floating_synced)
        self.assertEqual(data["peak_balance_usdt"], 450.0)

    async def test_drawdown_tracker_preserves_unrealized_during_sync_locks(self):
        """DrawdownTracker must never artificially zero out unrealized PnL during _sync_locks."""
        mock_manager = MagicMock()
        mock_manager._sync_locks = {("BTCUSDT", "LONG")}
        mock_manager._symbol_fsm_positions = {}
        tracker = DrawdownTracker(mock_manager)

        mock_client = MagicMock()
        res_ok = MagicMock()
        res_ok.success = True
        res_ok.data = {
            "totalMarginBalance": "400.0",
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "LONG",
                    "positionAmt": "0.1",
                    "entryPrice": "50000.0",
                    "unrealizedProfit": "-25.0",
                    "notional": "5000.0",
                }
            ],
        }
        mock_client.fetch_account_info = AsyncMock(return_value=res_ok)

        data = {
            "start_balance_usdt": 450.0,
            "peak_balance_usdt": 450.0,
            "per_coin": {
                "BTCUSDT": {"current_drawdown": 0.0, "realized_pnl_usdt": 0.0}
            },
        }

        # First sync pass
        success = await tracker.update_drawdowns(mock_client, data)
        self.assertTrue(success)
        self.assertTrue(tracker._floating_synced)
        # Verify drawdown was NOT zeroed out despite ("BTCUSDT", "LONG") in _sync_locks
        btc_dd = data["per_coin"]["BTCUSDT"]["current_drawdown"]
        self.assertEqual(btc_dd, -25.0, "Drawdown must reflect real exchange position PnL (-25.0)")
        self.assertEqual(data["unrealized_pnl_usdt"], -25.0)

    async def test_drawdown_tracker_blocks_peak_update_on_first_transition_pass(self):
        """Peak balance update must be blocked on the first sync transition tick upon restart."""
        mock_manager = MagicMock()
        mock_manager._sync_locks = set()
        mock_manager._symbol_fsm_positions = {}
        tracker = DrawdownTracker(mock_manager)
        tracker._floating_synced = False

        mock_client = MagicMock()
        res_ok = MagicMock()
        res_ok.success = True
        res_ok.data = {
            "totalMarginBalance": "500.0",
            "positions": [
                {
                    "symbol": "ETHUSDT",
                    "positionSide": "LONG",
                    "positionAmt": "1.0",
                    "entryPrice": "2000.0",
                    "unrealizedProfit": "50.0",
                    "notional": "2000.0",
                }
            ],
        }
        mock_client.fetch_account_info = AsyncMock(return_value=res_ok)

        data = {
            "start_balance_usdt": 400.0,
            "peak_balance_usdt": 400.0,
            "iteration_peak_net_profit_usdt": 0.0,
            "per_coin": {"ETHUSDT": {"realized_pnl_usdt": 0.0}},
        }

        # On the first sync pass from unverified state, can_update_peak must be False
        await tracker.update_drawdowns(mock_client, data)
        self.assertEqual(data["peak_balance_usdt"], 400.0, "Peak balance must not spike on initial sync transition tick")
        self.assertEqual(data["iteration_peak_net_profit_usdt"], 0.0)

        # On the second sync pass (when tracker._floating_synced is True and data is verified), peak can update
        await tracker.update_drawdowns(mock_client, data)
        self.assertEqual(data["peak_balance_usdt"], 450.0, "Peak balance updates on subsequent verified ticks")


if __name__ == "__main__":
    unittest.main()
