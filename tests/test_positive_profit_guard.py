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
        import ANALYTICS.circuit_breaker as cb
        cb._ban_until_ms = 0

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
        """Positive section must NEVER close when iter_net <= 0.0 and must DE-ARM (armed_floor = None)."""
        state = {
            "armed_floor": 8.0,
            "active_level_idx": 0,
            "armed_level_desc": "L0",
            "highest_net": 15.0,
        }

        # Negative net (drawdown) -> must de-arm!
        should_close, trig_floor, info, is_stag = BaseTrailingSystem.evaluate_ratchet(
            iter_net=-0.23,
            iter_age_hours=10.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
        )
        self.assertFalse(should_close, "Ratchet must NOT trigger when iter_net < 0")
        self.assertIsNone(state.get("armed_floor"), "Ratchet must be de-armed when net <= 0")
        self.assertIn("De-armed", info)

        # Re-arm manually to test zero net
        state["armed_floor"] = 8.0
        should_close_zero, _, info_zero, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=0.0,
            iter_age_hours=10.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
        )
        self.assertFalse(should_close_zero, "Ratchet must NOT trigger when iter_net == 0.0")
        self.assertIsNone(state.get("armed_floor"), "Ratchet must be de-armed when net == 0.0")
        self.assertIn("De-armed", info_zero)

    def test_evaluate_ratchet_min_profit_guarantee_default_half_step(self):
        """When min_guaranteed_profit_usdt is not specified, default to half of first step floor (> 0)."""
        state = {
            "armed_floor": 8.0,
            "active_level_idx": 0,
            "armed_level_desc": "L0",
            "highest_net": 15.0,
        }
        # Default min profit guarantee is 0.5 * 8.0 = 4.0 USDT
        # If iter_net drops to 3.0 USDT (below 4.0 guarantee), it must be blocked!
        should_close, trig_floor, info, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=3.0,
            iter_age_hours=5.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
        )
        self.assertFalse(should_close, "Ratchet must NOT trigger below minimum guarantee (4.0 USDT)")
        self.assertIn("MinGuard 4.00", info)

        # But if iter_net is 8.0 USDT (<= armed_floor 8.0, and >= 5.0), it should normally trigger
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


    def test_strategy_stats_hands_off_analytics_when_unsynced(self):
        """When _floating_synced is False, update_strategies_stats must NOT recalculate or write to disk."""
        mock_manager = MagicMock()
        mock_manager._floating_synced = False
        mock_manager.strategy_registry = MagicMock()
        mock_manager.stats_dir = Path("/nonexistent/stats")

        engine = StrategyStatsEngine(mock_manager)
        global_data = {"_floating_synced": False, "per_coin": {}}

        # Should return immediately and not call strategy_registry or disk
        engine.update_strategies_stats(global_data)
        mock_manager.strategy_registry.get_active_strategies.assert_not_called()

    def test_strategy_applicator_does_not_use_base_json(self):
        """StrategyApplicator must build configs directly from strategy metadata without _base.json."""
        from CORE.strategy_applicator import StrategyApplicator

        applicator = StrategyApplicator()
        self.assertFalse(hasattr(applicator, "_load_base_cfg"), "Must not have _load_base_cfg")

        class DummyStrategy:
            strategy_id = "test_strat"
            name = "Test Strategy"
            leverage = 20
            super_grid_enabled = False
            raw_meta = {
                "leverage": 20,
                "LONG": {
                    "enabled": True,
                    "invest_size": 400.0,
                    "grid": {"0": {"indent": 0.0, "volume": 7.0}},
                    "tp_map": {"0": {"indent": 1.0}},
                },
                "SHORT": {
                    "enabled": True,
                    "invest_size": 400.0,
                    "grid": {"0": {"indent": 0.0, "volume": 7.0}},
                    "tp_map": {"0": {"indent": 1.0}},
                },
            }
            overrides = {}

        strat_mock = DummyStrategy()
        side_cfgs = applicator.build_side_configs(strat_mock, "BTCUSDT")
        long_cfg = side_cfgs["LONG"]
        short_cfg = side_cfgs["SHORT"]
        self.assertEqual(long_cfg["invest_size"], 400.0)
        self.assertEqual(short_cfg["invest_size"], 400.0)
        self.assertIn("0", long_cfg["grid"])

    def test_analytics_restorer_initialization(self):
        """AnalyticsManager must restore frozen state on startup and lock recalculations until verified API."""
        from ANALYTICS.analytics import AnalyticsManager
        from consts import ANALYTICS_START_BALANCE_USDT
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with patch("ANALYTICS.analytics.ANALYTICS_STATS_DIR", tmp_path / "STATS"), \
                 patch("ANALYTICS.analytics.ANALYTICS_LEDGERS_DIR", tmp_path / "LEDGERS"), \
                 patch("ANALYTICS.analytics.CACHE_DIR", tmp_path / "CACHE"):
                
                # Pre-populate a frozen analytics file
                stats_dir = tmp_path / "STATS"
                stats_dir.mkdir(parents=True, exist_ok=True)
                frozen_file = stats_dir / "analytics.json"
                init_bal = float(ANALYTICS_START_BALANCE_USDT)
                frozen_file.write_text(json.dumps({
                    "start_balance_usdt": init_bal,
                    "cur_balance_usdt": init_bal + 15.0,
                    "net_profit_usdt": 15.0,
                    "peak_balance_usdt": init_bal + 15.0,
                }), encoding="utf-8")

                mgr = AnalyticsManager(strategy_registry=None)
                self.assertFalse(mgr._floating_synced, "Must be initialized as unsynced")
                self.assertEqual(mgr._persisted_global_cache.get("net_profit_usdt"), 15.0)

                data = mgr._read_data()
                self.assertFalse(data.get("_floating_synced"), "_read_data must reflect unverified state")
                self.assertEqual(data.get("cur_balance_usdt"), init_bal + 15.0)

    def test_analytics_fsm_four_states_lifecycle(self):
        """Verify the 4-state lifecycle of AnalyticsFSM and AutoCloser gating."""
        from ANALYTICS.analytics_fsm import AnalyticsFSM, AnalyticsState
        with tempfile.TemporaryDirectory() as tmp_dir:
            p = Path(tmp_dir)
            fsm = AnalyticsFSM(stats_dir=p / "STATS", cache_dir=p / "CACHE", log_file=p / "STATS" / "analytics.json")
            fsm.restore_on_boot(initial_start_balance=600.0)

            # State 1: RESTORED_FROZEN
            self.assertEqual(fsm.state, AnalyticsState.RESTORED_FROZEN)
            self.assertFalse(fsm.is_autocloser_ready())

            # State 2: AWAITING_API
            fsm.transition_to_awaiting_api()
            self.assertEqual(fsm.state, AnalyticsState.AWAITING_API)
            self.assertFalse(fsm.is_autocloser_ready())

            # State 3: LIVE_SYNCED
            fsm.on_api_sync_success({"totalMarginBalance": "620.0"})
            self.assertEqual(fsm.state, AnalyticsState.LIVE_SYNCED)
            self.assertFalse(fsm.is_autocloser_ready())

            # State 4: AUTOCLOSER_READY
            fsm.transition_to_autocloser_ready()
            self.assertEqual(fsm.state, AnalyticsState.AUTOCLOSER_READY)
            self.assertTrue(fsm.is_autocloser_ready())

            # API Error disarms back to AWAITING_API
            fsm.on_api_error("Exchange 502 Bad Gateway")
            self.assertEqual(fsm.state, AnalyticsState.AWAITING_API)
            self.assertFalse(fsm.is_autocloser_ready())

    def test_analytics_fsm_cold_start_initialization(self):
        """When cache is missing, cold start creates base from starting balance and zeroes all PnLs."""
        from ANALYTICS.analytics_fsm import AnalyticsFSM, AnalyticsState
        with tempfile.TemporaryDirectory() as tmp_dir:
            p = Path(tmp_dir)
            log_f = p / "STATS" / "analytics.json"
            fsm = AnalyticsFSM(stats_dir=p / "STATS", cache_dir=p / "CACHE", log_file=log_f)
            self.assertFalse(log_f.exists())

            fsm.restore_on_boot(initial_start_balance=555.0)
            self.assertTrue(log_f.exists())
            self.assertEqual(fsm.state, AnalyticsState.RESTORED_FROZEN)
            self.assertFalse(fsm.is_autocloser_ready())

            disk_data = json.loads(log_f.read_text(encoding="utf-8"))
            self.assertEqual(disk_data["start_balance_usdt"], 555.0)
            self.assertEqual(disk_data["cur_balance_usdt"], 555.0)
            self.assertEqual(disk_data["peak_balance_usdt"], 555.0)
            self.assertEqual(disk_data["net_profit_usdt"], 0.0)
            self.assertEqual(disk_data["realized_pnl_usdt"], 0.0)
            self.assertEqual(disk_data["iteration_index"], 1)
            self.assertFalse(disk_data["_floating_synced"])

    def test_iteration_index_preservation_across_restarts(self):
        """Iteration index from CACHE/iteration_state.json must be preserved and never revert to 1."""
        from ANALYTICS.analytics_fsm import AnalyticsFSM
        with tempfile.TemporaryDirectory() as tmp_dir:
            p = Path(tmp_dir)
            cache_d = p / "CACHE"
            cache_d.mkdir(parents=True, exist_ok=True)
            iter_f = cache_d / "iteration_state.json"
            iter_f.write_text(json.dumps({
                "u17_stephensonE": {"iteration_index": 5, "iteration_start_net_profit_usdt": 42.0},
                "__global__": {"iteration_index": 3, "iteration_start_net_profit_usdt": 100.0}
            }), encoding="utf-8")

            fsm = AnalyticsFSM(stats_dir=p / "STATS", cache_dir=cache_d, log_file=p / "STATS" / "analytics.json")
            fsm.restore_on_boot(initial_start_balance=600.0)

            self.assertEqual(fsm.get_iteration_index("u17_stephensonE"), 5)
            self.assertEqual(fsm.get_iteration_index("__global__"), 3)
            self.assertEqual(fsm.get_iteration_index("unknown_strat"), 1)

    async def test_auto_closer_gated_by_fsm_fourth_state(self):
        """AutoCloser.check() must be strictly blocked unless AnalyticsFSM is in AUTOCLOSER_READY."""
        from ANALYTICS.analytics_fsm import AnalyticsFSM, AnalyticsState
        with tempfile.TemporaryDirectory() as tmp_dir:
            p = Path(tmp_dir)
            fsm = AnalyticsFSM(stats_dir=p / "STATS", cache_dir=p / "CACHE", log_file=p / "STATS" / "analytics.json")
            fsm.restore_on_boot(initial_start_balance=600.0)

            mock_bot = MagicMock()
            mock_bot.analytics = MagicMock()
            mock_bot.analytics.fsm = fsm
            mock_bot.close_strategy_positions = AsyncMock()

            closer = AutoCloser(mock_bot)

            # In State 1 (RESTORED_FROZEN), AutoCloser must be blocked
            self.assertEqual(fsm.state, AnalyticsState.RESTORED_FROZEN)
            await closer.check()
            mock_bot.close_strategy_positions.assert_not_called()

            # In State 2 (AWAITING_API), AutoCloser must be blocked
            fsm.transition_to_awaiting_api()
            await closer.check()
            mock_bot.close_strategy_positions.assert_not_called()

            # In State 3 (LIVE_SYNCED), AutoCloser must still be blocked
            fsm.on_api_sync_success()
            await closer.check()
            mock_bot.close_strategy_positions.assert_not_called()

    def test_auto_closer_idempotency_via_history_cache(self):
        """Closed iterations recorded in auto_close_history.json must prevent duplicate closes."""
        from ANALYTICS.analytics_fsm import AnalyticsFSM
        with tempfile.TemporaryDirectory() as tmp_dir:
            p = Path(tmp_dir)
            fsm = AnalyticsFSM(stats_dir=p / "STATS", cache_dir=p / "CACHE", log_file=p / "STATS" / "analytics.json")
            fsm.restore_on_boot(initial_start_balance=600.0)

            strat = "u17_stephensonE"
            self.assertFalse(fsm.is_iteration_closed(strat, 1))

            fsm.mark_iteration_closed(strat, iter_idx=1, details={"close_type": "TRAILING RATCHET", "iter_net": 15.5})

            self.assertTrue(fsm.is_iteration_closed(strat, 1), "Iteration #1 must be marked closed")
            self.assertFalse(fsm.is_iteration_closed(strat, 2), "Iteration #2 must NOT be marked closed")
            self.assertEqual(fsm.get_iteration_index(strat), 2, "Next iteration index must be advanced to 2")

            # Check persistent file on disk
            hist_file = p / "CACHE" / "auto_close_history.json"
            self.assertTrue(hist_file.exists())
            hist_data = json.loads(hist_file.read_text(encoding="utf-8"))
            self.assertEqual(len(hist_data[strat]), 1)
            self.assertEqual(hist_data[strat][0]["iteration_index"], 1)
            self.assertEqual(hist_data[strat][0]["iter_net"], 15.5)


if __name__ == "__main__":
    unittest.main()

