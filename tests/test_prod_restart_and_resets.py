# tests/test_prod_restart_and_resets.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: tests.test_prod_restart_and_resets
ROLE: Production-Grade Torture Test for Restorers, FSM Lifecycle,
      Iteration Persistence, AutoCloser Idempotency, and Safe Resets.
==============================================================================
"""

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from ANALYTICS.analytics_fsm import AnalyticsFSM, AnalyticsState
from ANALYTICS.analytics import AnalyticsManager
from ANALYTICS import circuit_breaker
from CORE.SERVICES.auto_closer import AutoCloser
from POS_FSM.models import PositionState
from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
from c_utils import Utils


class TestProdRestartAndResets(unittest.IsolatedAsyncioTestCase):
    """
    Torture test suite validating zero false defaults, zero phantom profits,
    safe restart flows, and robust FSM transitions.
    """

    def setUp(self):
        # Reset any global circuit breaker bans
        circuit_breaker._ban_until_ms = 0
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_prod_restart_"))
        self.stats_dir = self.temp_dir / "ANALYTICS" / "STATS"
        self.ledgers_dir = self.temp_dir / "ANALYTICS" / "LEDGERS"
        self.cache_dir = self.temp_dir / "CACHE"
        self.cfg_dir = self.temp_dir / "CFG"
        self.runtime_dir = self.cfg_dir / "runtime"

        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        self.log_file = self.stats_dir / "analytics.json"
        self.root_legacy = self.temp_dir / "ANALYTICS" / "analytics.json"

    def tearDown(self):
        circuit_breaker._ban_until_ms = 0
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # 1. ANALYTICS RESTORER: COLD & PERSISTED RESTARTS
    # =========================================================================

    def test_cold_start_analytics_restorer_initializes_434_balance_and_zero_pnl(self):
        """
        Cold restart when cache files are missing:
        Must create initial analytics starting at exactly 434.0 USDT,
        zeroing all PnL, trades, and drawdowns. No phantom profit permitted.
        """
        fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.log_file
        )
        # Execute boot restoration from clean slate
        fsm.restore_on_boot(initial_start_balance=434.0)

        self.assertEqual(fsm.state, AnalyticsState.RESTORED_FROZEN)
        self.assertTrue(self.log_file.exists())
        self.assertFalse(self.root_legacy.exists(), "Root legacy analytics.json clone must never be created")

        data = json.loads(self.log_file.read_text(encoding="utf-8"))
        self.assertEqual(data["start_balance_usdt"], 434.0)
        self.assertEqual(data["cur_balance_usdt"], 434.0)
        self.assertEqual(data["peak_balance_usdt"], 434.0)
        self.assertEqual(data["min_balance_usdt"], 434.0)
        self.assertEqual(data["net_profit_usdt"], 0.0)
        self.assertEqual(data["realized_pnl_usdt"], 0.0)
        self.assertEqual(data["unrealized_pnl_usdt"], 0.0)
        self.assertEqual(data["total_trades"], 0)
        self.assertFalse(data["_floating_synced"])

    def test_persisted_restart_locks_analytics_until_verified_api_sync(self):
        """
        Restart with existing persisted cache:
        Metrics must be restored in frozen state (_floating_synced = False).
        ATH peaks must NOT be updated until live REST API verification.
        """
        persisted_data = {
            "start_balance_usdt": 434.0,
            "_rebased_start_balance_usdt": 434.0,
            "cur_balance_usdt": 450.0,
            "peak_balance_usdt": 450.0,
            "min_balance_usdt": 430.0,
            "net_profit_usdt": 16.0,
            "realized_pnl_usdt": 16.0,
            "unrealized_pnl_usdt": 0.0,
            "total_trades": 5,
            "iteration_index": 2,
            "_floating_synced": True,  # was synced before restart
        }
        self.log_file.write_text(json.dumps(persisted_data, indent=4), encoding="utf-8")

        fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.log_file
        )
        fsm.restore_on_boot(initial_start_balance=434.0)

        # On restart, FSM enters RESTORED_FROZEN and forces _floating_synced = False
        self.assertEqual(fsm.state, AnalyticsState.RESTORED_FROZEN)
        cached = fsm._persisted_global_cache
        self.assertFalse(cached["_floating_synced"])
        self.assertEqual(cached["cur_balance_usdt"], 450.0)
        self.assertEqual(cached["net_profit_usdt"], 16.0)
        self.assertEqual(cached["iteration_index"], 2)

    # =========================================================================
    # 2. AUTOCLOSER ITERATOR RESTORER & IDEMPOTENCY
    # =========================================================================

    def test_iteration_index_never_resets_to_1_on_reload(self):
        """
        AutoCloser iterator restorer must read CACHE/iteration_state.json and
        preserve iteration_index across restarts, never resetting to 1.
        """
        strat_id = "u17_stephensonE__trailing_ratchet"
        iteration_cache = {
            strat_id: {
                "iteration_index": 7,
                "iteration_start_net_profit_usdt": 120.0,
                "iteration_peak_net_profit_usdt": 15.0,
            }
        }
        iter_file = self.cache_dir / "iteration_state.json"
        iter_file.write_text(json.dumps(iteration_cache, indent=4), encoding="utf-8")

        # Disk analytics has stale iteration_index 3
        stale_strat_data = {
            "universe_id": strat_id,
            "iteration_index": 3,
            "start_balance_usdt": 434.0,
            "net_profit_usdt": 125.0
        }
        (self.stats_dir / f"analytics_{strat_id}.json").write_text(
            json.dumps(stale_strat_data, indent=4), encoding="utf-8"
        )

        fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.log_file
        )
        fsm.restore_on_boot(initial_start_balance=434.0)

        # Iteration must be restored as 7 (from iteration_state.json), NOT 1 or 3
        restored_idx = fsm.get_iteration_index(strat_id)
        self.assertEqual(restored_idx, 7)
        self.assertEqual(fsm._persisted_strat_cache[strat_id]["iteration_index"], 7)

    def test_autocloser_idempotency_prevents_duplicate_close_after_crash(self):
        """
        If an iteration close occurred and was recorded in auto_close_history.json,
        a reboot while profits are still unadjusted MUST NOT re-trigger closure.
        """
        strat_id = "u17_stephensonE__trailing_ratchet"
        fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.log_file
        )
        fsm.restore_on_boot(initial_start_balance=434.0)

        # Mark iteration 2 as closed
        fsm.mark_iteration_closed(
            strat_id=strat_id,
            iter_idx=2,
            details={"profit_usdt": 50.0, "reason": "TRAILING_RATCHET"}
        )

        self.assertTrue(fsm.is_iteration_closed(strat_id, 2))
        self.assertFalse(fsm.is_iteration_closed(strat_id, 3))
        self.assertEqual(fsm.get_iteration_index(strat_id), 3)

        # Simulate bot crash and restart
        fsm_restarted = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.log_file
        )
        fsm_restarted.restore_on_boot(initial_start_balance=434.0)

        # Post-crash verification: iteration 2 remains marked closed
        self.assertTrue(fsm_restarted.is_iteration_closed(strat_id, 2))
        self.assertEqual(fsm_restarted.get_iteration_index(strat_id), 3)

    # =========================================================================
    # 3. POSITION STATS RESTORER & INTEGRITY
    # =========================================================================

    def test_position_fsm_restorer_populates_state_from_runtime_cache(self):
        """
        Verifies that PositionState correctly reconstructs grid, volumes,
        and trailing ratchets from runtime cache upon restart.
        """
        mgr = RuntimeFsmManager()
        mgr.runtime_dir = self.runtime_dir
        sym = "ICNTUSDT"
        cache_data = {
            sym: {
                "LONG": {
                    "in_position": True,
                    "total_volume": 150.0,
                    "avg_entry_price": 0.4250,
                    "initial_entry_price": 0.4300,
                    "grid": {
                        "0": {"indent": 0.0, "volume": 50.0, "is_active": True},
                        "1": {"indent": -3.5, "volume": 100.0, "is_active": True},
                    },
                    "tp_map": {
                        "0": {"indent": 1.5, "fallback_indent": 1.0, "activation": 1.5}
                    },
                    "trailing_tp_state": {"armed_floor": 1.0, "active_level": "0"},
                },
                "SHORT": {
                    "in_position": False,
                    "total_volume": 0.0,
                    "avg_entry_price": 0.0,
                    "grid": {},
                    "tp_map": {},
                }
            }
        }
        mgr.caches = cache_data

        fsm_states = {
            sym: {
                "LONG": PositionState(symbol=sym, side="LONG"),
                "SHORT": PositionState(symbol=sym, side="SHORT")
            }
        }
        mgr.populate_fsm_from_cache(fsm_states)

        long_st = fsm_states[sym]["LONG"]
        self.assertTrue(long_st.in_position)
        self.assertEqual(long_st.total_volume, 150.0)
        self.assertEqual(long_st.avg_entry_price, 0.4250)
        self.assertEqual(long_st.trailing_tp_state.get("armed_floor"), 1.0)
        self.assertEqual(len(long_st.grid), 2)

        short_st = fsm_states[sym]["SHORT"]
        self.assertFalse(short_st.in_position)
        self.assertEqual(short_st.total_volume, 0.0)

    # =========================================================================
    # 4. FSM LIFECYCLE (STATES 1 -> 2 -> 3 -> 4) & DISARM ON API ERROR
    # =========================================================================

    def test_fsm_lifecycle_and_disarm_on_api_error(self):
        """
        Tests the 4-state sequence:
        1. RESTORED_FROZEN (boot)
        2. AWAITING_API (waiting for Binance REST response)
        3. LIVE_SYNCED (verified response received, PnL calculated)
        4. AUTOCLOSER_READY (metrics written, AutoCloser armed)
        And verifies immediate disarm to State 2 upon API error / 429 ban.
        """
        fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.log_file
        )
        # Step 1: Boot
        fsm.restore_on_boot(initial_start_balance=434.0)
        self.assertEqual(fsm.state, AnalyticsState.RESTORED_FROZEN)
        self.assertFalse(fsm.is_autocloser_ready())

        # Step 2: Transition to Awaiting API
        fsm.transition_to_awaiting_api()
        self.assertEqual(fsm.state, AnalyticsState.AWAITING_API)
        self.assertFalse(fsm.is_autocloser_ready())

        # Step 3: Verified REST API response arrives
        fsm.on_api_sync_success({"positions": []})
        self.assertEqual(fsm.state, AnalyticsState.LIVE_SYNCED)
        self.assertFalse(fsm.is_autocloser_ready())

        # Step 4: Metrics committed -> AutoCloser Armed
        fsm.transition_to_autocloser_ready()
        self.assertEqual(fsm.state, AnalyticsState.AUTOCLOSER_READY)
        self.assertTrue(fsm.is_autocloser_ready())

        # Step 5: Network dropout or API 429 -> Immediate Disarm
        fsm.on_api_error("HTTP 429: Too Many Requests")
        self.assertEqual(fsm.state, AnalyticsState.AWAITING_API)
        self.assertFalse(fsm.is_autocloser_ready())

    # =========================================================================
    # 5. RESETS TORTURE: STRATEGY, GLOBAL & FULL ANALYTICS
    # =========================================================================

    def test_reset_strategy_iteration_preserves_historical_peaks(self):
        """
        reset_strategy_iteration must advance iteration_index, anchor start
        net profit to current, reset iteration PnL to 0.0, and preserve
        all-time historical peaks in iteration_state.json.
        """
        strat_id = "u17_stephensonE__trailing_ratchet"
        strat_file = self.stats_dir / f"analytics_{strat_id}.json"
        initial_data = {
            "universe_id": strat_id,
            "iteration_index": 1,
            "net_profit_usdt": 45.5,
            "realized_pnl_net_usdt": 42.0,
            "iteration_start_net_profit_usdt": 0.0,
            "iteration_net_profit_usdt": 45.5,
            "peak_balance_usdt": 479.5,
        }
        strat_file.write_text(json.dumps(initial_data, indent=4), encoding="utf-8")

        mock_manager = MagicMock()
        mock_manager.stats_dir = self.stats_dir
        mock_manager.cache_dir = self.cache_dir
        mock_manager.log_file = self.log_file
        mock_manager._read_data.return_value = initial_data

        from ANALYTICS.strategy_stats import StrategyStatsEngine
        engine = StrategyStatsEngine(mock_manager)

        res = engine.reset_strategy_iteration(strat_id)
        self.assertEqual(res["prev_iteration"], 1)
        self.assertEqual(res["new_iteration"], 2)
        self.assertEqual(res["base_net"], 45.5)

        updated_strat = json.loads(strat_file.read_text(encoding="utf-8"))
        self.assertEqual(updated_strat["iteration_index"], 2)
        self.assertEqual(updated_strat["iteration_start_net_profit_usdt"], 45.5)
        self.assertEqual(updated_strat["iteration_net_profit_usdt"], 0.0)

        c_file = self.cache_dir / "iteration_state.json"
        c_dict = json.loads(c_file.read_text(encoding="utf-8"))
        self.assertEqual(c_dict[strat_id]["iteration_index"], 2)
        self.assertEqual(c_dict[strat_id]["historical_peak_net_profit_usdt"], 45.5)

    def test_reset_global_iteration_advances_index_and_anchors_net(self):
        """
        reset_global_iteration must increment global iteration_index and
        anchor global start net profit.
        """
        g_data = {
            "iteration_index": 3,
            "net_profit_usdt": 88.0,
            "start_balance_usdt": 434.0,
            "cur_balance_usdt": 522.0
        }
        self.log_file.write_text(json.dumps(g_data, indent=4), encoding="utf-8")

        mock_manager = MagicMock()
        mock_manager.stats_dir = self.stats_dir
        mock_manager.cache_dir = self.cache_dir
        mock_manager.log_file = self.log_file
        mock_manager._read_data.return_value = g_data

        from ANALYTICS.strategy_stats import StrategyStatsEngine
        engine = StrategyStatsEngine(mock_manager)

        res = engine.reset_global_iteration()
        self.assertEqual(res["iteration_index"], 4)
        self.assertEqual(res["base_net"], 88.0)

        updated_g = json.loads(self.log_file.read_text(encoding="utf-8"))
        self.assertEqual(updated_g["iteration_index"], 4)
        self.assertEqual(updated_g["iteration_start_net_profit_usdt"], 88.0)
        self.assertEqual(updated_g["iteration_net_profit_usdt"], 0.0)

    def test_rebase_start_balance_with_zero_trades_wipes_phantom_profits(self):
        """
        If total_trades == 0 (no trades occurred), rebase_start_balance must
        forcefully wipe any phantom profits and set cur_balance == start_balance.
        """
        dirty_data = {
            "start_balance_usdt": 459.0,
            "cur_balance_usdt": 474.0,
            "net_profit_usdt": 15.0,  # Phantom profit
            "total_trades": 0,
            "winning_trades": 0,
            "realized_pnl_usdt": 0.0,
            "_floating_synced": False
        }
        self.log_file.write_text(json.dumps(dirty_data, indent=4), encoding="utf-8")

        # Mock manager with dirty log file
        manager = AnalyticsManager.__new__(AnalyticsManager)
        manager.log_file = self.log_file
        manager.stats_dir = self.stats_dir
        manager._persisted_global_cache = {}
        manager._lock = asyncio.Lock()
        manager.velocity_engine = None

        rebased = manager.rebase_start_balance(434.0)

        # Phantom profit must be wiped to 0.0, and balance anchored to 434.0
        self.assertEqual(rebased["start_balance_usdt"], 434.0)
        self.assertEqual(rebased["cur_balance_usdt"], 434.0)
        self.assertEqual(rebased["peak_balance_usdt"], 434.0)
        self.assertEqual(rebased["min_balance_usdt"], 434.0)
        self.assertEqual(rebased["net_profit_usdt"], 0.0)

    # =========================================================================
    # 6. AUTOCLOSER GATEKEEPER & ITERATION NET PURGE
    # =========================================================================

    async def test_autocloser_strictly_blocked_in_states_1_2_3(self):
        """
        AutoCloser check() method must return immediately without evaluation
        if FSM state is RESTORED_FROZEN, AWAITING_API, or LIVE_SYNCED.
        """
        fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.log_file
        )
        fsm.restore_on_boot(initial_start_balance=434.0)

        bot_mock = MagicMock()
        bot_mock.analytics.fsm = fsm
        bot_mock.is_paused = False
        bot_mock.close_all_positions = AsyncMock()

        closer = AutoCloser(bot_mock)

        # State 1: RESTORED_FROZEN -> Blocked
        fsm.state = AnalyticsState.RESTORED_FROZEN
        await closer.check()
        bot_mock.close_all_positions.assert_not_called()

        # State 2: AWAITING_API -> Blocked
        fsm.state = AnalyticsState.AWAITING_API
        await closer.check()
        bot_mock.close_all_positions.assert_not_called()

        # State 3: LIVE_SYNCED -> Blocked
        fsm.state = AnalyticsState.LIVE_SYNCED
        await closer.check()
        bot_mock.close_all_positions.assert_not_called()

        # State 4: AUTOCLOSER_READY -> Allowed to evaluate
        fsm.state = AnalyticsState.AUTOCLOSER_READY
        self.assertTrue(fsm.is_autocloser_ready())

    async def test_reset_analytics_state_full_wipe_and_clean_cold_reboot(self):
        """
        reset_analytics_state must cleanly remove analytics and ledger files,
        and subsequent boot must recreate pristine files with 434.0 balance.
        """
        # Create some files
        self.log_file.write_text('{"dirty": true}', encoding="utf-8")
        test_strat_file = self.stats_dir / "analytics_test_strat.json"
        test_strat_file.write_text('{"dirty": true}', encoding="utf-8")
        test_ledger = self.ledgers_dir / "test_ledger.txt"
        test_ledger.write_text("header;line", encoding="utf-8")

        manager = AnalyticsManager.__new__(AnalyticsManager)
        manager.log_file = self.log_file
        manager.txt_file = self.temp_dir / "trades_ledger.txt"
        manager.peels_ledger = self.ledgers_dir / "peels_ledger.txt"
        manager.recycling_ledger = self.ledgers_dir / "recycling_ledger.txt"
        manager.stats_dir = self.stats_dir
        manager.ledgers_dir = self.ledgers_dir
        manager._lock = asyncio.Lock()
        manager._csv_lock = asyncio.Lock()

        await manager.reset_analytics_state()

        self.assertFalse(self.log_file.exists())
        self.assertFalse(test_strat_file.exists())

        # Now reboot from clean slate
        fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.log_file
        )
        fsm.restore_on_boot(initial_start_balance=434.0)

        self.assertTrue(self.log_file.exists())
        cold = json.loads(self.log_file.read_text(encoding="utf-8"))
        self.assertEqual(cold["start_balance_usdt"], 434.0)
        self.assertEqual(cold["cur_balance_usdt"], 434.0)
        self.assertEqual(cold["net_profit_usdt"], 0.0)

    def test_rebase_with_existing_trades_shifts_peaks_accurately(self):
        """
        If total_trades > 0, rebase_start_balance shifts peaks and balance
        by delta without corrupting net_profit or creating false drawdowns.
        """
        trade_data = {
            "start_balance_usdt": 400.0,
            "_rebased_start_balance_usdt": 400.0,
            "cur_balance_usdt": 430.0,
            "peak_balance_usdt": 435.0,
            "min_balance_usdt": 395.0,
            "_current_trough_usdt": 420.0,
            "net_profit_usdt": 30.0,
            "total_trades": 12,
            "winning_trades": 10,
            "realized_pnl_usdt": 30.0,
            "_floating_synced": True
        }
        self.log_file.write_text(json.dumps(trade_data, indent=4), encoding="utf-8")

        manager = AnalyticsManager.__new__(AnalyticsManager)
        manager.log_file = self.log_file
        manager.stats_dir = self.stats_dir
        manager._persisted_global_cache = {}
        manager._lock = asyncio.Lock()
        manager.velocity_engine = None

        # Rebase to 434.0 (delta = +34.0)
        rebased = manager.rebase_start_balance(434.0)

        self.assertEqual(rebased["start_balance_usdt"], 434.0)
        self.assertEqual(rebased["cur_balance_usdt"], 464.0)
        self.assertEqual(rebased["peak_balance_usdt"], 469.0)
        self.assertEqual(rebased["min_balance_usdt"], 429.0)
        self.assertEqual(rebased["_current_trough_usdt"], 454.0)
        self.assertEqual(rebased["net_profit_usdt"], 30.0)  # preserved exact PnL!

    def test_autocloser_iteration_net_calculation_with_prior_closed_history(self):
        """
        Тестирование пользовательского сценария:
        Закрыто автоклозером 3 раза (суммарный профит 30$).
        Текущий профит портфеля 33$.
        Итерация 4 должна видеть строго 3$ (33$ - 30$), а не 33$,
        и НЕ поднимать ложных триггеров автозакрытия.
        """
        history = {
            "test_strat": [
                {"iteration_index": 1, "iter_net": 10.0, "strat_net": 10.0, "closed_at": "2026-10-22 00:00:00 UTC"},
                {"iteration_index": 2, "iter_net": 10.0, "strat_net": 20.0, "closed_at": "2026-10-22 01:00:00 UTC"},
                {"iteration_index": 3, "iter_net": 10.0, "strat_net": 30.0, "closed_at": "2026-10-22 02:00:00 UTC"},
            ]
        }
        history_file = self.cache_dir / "auto_close_history.json"
        history_file.write_text(json.dumps(history, indent=4), encoding="utf-8")

        fsm = AnalyticsFSM(stats_dir=self.stats_dir, cache_dir=self.cache_dir, log_file=self.log_file)
        fsm.restore_on_boot(initial_start_balance=434.0)

        # Для 4-й итерации базовый профит должен быть строго 30.0
        baseline = fsm.get_iteration_baseline_net("test_strat", current_iter_idx=4)
        self.assertEqual(baseline, 30.0)

        # Текущий кумулятивный профит 33.0$
        strat_net = 33.0
        iter_net = round(strat_net - baseline, 4)
        self.assertEqual(iter_net, 3.0)  # строго 3.0$, а не 33.0$!


if __name__ == "__main__":
    unittest.main()
