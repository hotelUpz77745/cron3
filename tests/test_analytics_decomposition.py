# tests/test_analytics_decomposition.py
# -*- coding: utf-8 -*-
"""
Unit tests for decomposed Analytics subsystem:
- CircuitBreaker
- LedgerRecorder
- DrawdownTracker
- StrategyStatsEngine
- ReconstructionService
- AnalyticsManager (Facade delegation & state management)
"""

import asyncio
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ANALYTICS.analytics import AnalyticsManager
from ANALYTICS.circuit_breaker import (
    is_banned,
    update_ban_from_error,
    wait_ban_lifted,
)
from ANALYTICS.drawdown_tracker import DrawdownTracker
from ANALYTICS.ledger_recorder import LedgerRecorder
from ANALYTICS.reconstruction import ReconstructionService
from ANALYTICS.strategy_stats import StrategyStatsEngine


class TestAnalyticsDecomposition(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.stats_dir = self.temp_dir / "STATS"
        self.ledgers_dir = self.temp_dir / "LEDGERS"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)

        self.mgr = AnalyticsManager()
        self.mgr.stats_dir = self.stats_dir
        self.mgr.ledgers_dir = self.ledgers_dir
        self.mgr.log_file = self.stats_dir / "analytics.json"
        self.mgr.txt_file = self.ledgers_dir / "trades_ledger.txt"
        self.mgr.peels_ledger = self.ledgers_dir / "peels_ledger.txt"
        self.mgr.recycling_ledger = self.ledgers_dir / "recycling_ledger.txt"
        self.mgr._ensure_files()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_circuit_breaker(self):
        # Initial state should not be banned
        with patch("ANALYTICS.circuit_breaker._ban_until_ms", 0):
            self.assertFalse(is_banned())
            update_ban_from_error("APIError(code=-1003): Way too many requests; IP banned until 1792433999000.")
            self.assertTrue(is_banned())

    def test_ledger_recorder_dynamic_paths(self):
        # Verify dynamic property resolution from manager
        recorder = self.mgr.ledger_recorder
        self.assertEqual(recorder.ledgers_dir, self.ledgers_dir)
        self.assertEqual(recorder.txt_file, self.ledgers_dir / "trades_ledger.txt")
        self.assertEqual(recorder.peels_ledger, self.ledgers_dir / "peels_ledger.txt")
        self.assertEqual(recorder.recycling_ledger, self.ledgers_dir / "recycling_ledger.txt")

    def test_record_subgrid_peel(self):
        self.mgr.record_subgrid_peel(
            strategy_id="strat_test",
            symbol="BTCUSDT",
            side="LONG",
            level="1",
            fill_price=50000.0,
            peel_price=50500.0,
            rebound_pct=1.0,
            peel_qty=0.01,
            rem_pos_qty=0.05,
        )

        self.assertTrue(self.mgr.peels_ledger.exists())
        content = self.mgr.peels_ledger.read_text(encoding="utf-8")
        self.assertIn("BTCUSDT", content)
        self.assertIn("strat_test", content)

    def test_record_cross_recycling(self):
        self.mgr.record_cross_recycling(
            strategy_id="strat_test",
            symbol="ETHUSDT",
            winning_side="LONG",
            realized_profit=15.0,
            losing_side="SHORT",
            losing_level=2,
            losing_dd_pct=-5.0,
            burn_qty=0.1,
            current_price=2500.0,
            actual_burn_loss=4.0,
            burn_budget=8.0,
            rem_pos_qty=0.5,
        )

        self.assertTrue(self.mgr.recycling_ledger.exists())
        content = self.mgr.recycling_ledger.read_text(encoding="utf-8")
        self.assertIn("ETHUSDT", content)
        self.assertIn("strat_test", content)

    def test_strategy_iteration_reset(self):
        res = self.mgr.reset_strategy_iteration("strat_test")
        self.assertEqual(res["strategy_id"], "strat_test")
        self.assertIn("new_iteration", res)
        self.assertEqual(res["new_iteration"], 2)

    def test_global_iteration_reset(self):
        res = self.mgr.reset_global_iteration()
        self.assertIn("iteration_index", res)
        self.assertEqual(res["iteration_index"], 2)

    def test_rebase_start_balance(self):
        self.mgr.rebase_start_balance(500.0)
        data = self.mgr._read_data()
        self.assertEqual(data["start_balance_usdt"], 500.0)
        self.assertEqual(data["cur_balance_usdt"], 500.0)

    def test_facade_delegation(self):
        # Check that facade methods exist and point to sub-engines
        self.assertIsInstance(self.mgr.ledger_recorder, LedgerRecorder)
        self.assertIsInstance(self.mgr.drawdown_tracker, DrawdownTracker)
        self.assertIsInstance(self.mgr.strategy_stats, StrategyStatsEngine)
        self.assertIsInstance(self.mgr.reconstruction, ReconstructionService)

        # Check iteration cache methods
        self.mgr._save_iteration_cache("s1", {"iteration_index": 3})
        cache = self.mgr._get_iteration_cache("s1")
        self.assertEqual(cache.get("iteration_index"), 3)


class TestAsyncAnalyticsDecomposition(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.stats_dir = self.temp_dir / "STATS"
        self.ledgers_dir = self.temp_dir / "LEDGERS"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)

        self.mgr = AnalyticsManager()
        self.mgr.stats_dir = self.stats_dir
        self.mgr.ledgers_dir = self.ledgers_dir
        self.mgr.log_file = self.stats_dir / "analytics.json"
        self.mgr.txt_file = self.ledgers_dir / "trades_ledger.txt"
        self.mgr.peels_ledger = self.ledgers_dir / "peels_ledger.txt"
        self.mgr.recycling_ledger = self.ledgers_dir / "recycling_ledger.txt"
        self.mgr._ensure_files()

    async def asyncTearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_append_to_csv(self):
        await self.mgr._append_to_csv(
            symbol="BTCUSDT",
            side="LONG",
            open_time=1700000000000,
            close_time=1700000060000,
            pnl=12.5,
            balance=512.5,
        )
        self.assertTrue(self.mgr.txt_file.exists())
        content = self.mgr.txt_file.read_text(encoding="utf-8")
        self.assertIn("BTCUSDT", content)
        self.assertIn("12.5", content)

    async def test_update_drawdowns(self):
        mock_client = MagicMock()
        mock_client.fetch_account_info = AsyncMock(return_value=MagicMock(
            success=True,
            data={
                "totalMarginBalance": "500.0",
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "0.1",
                        "entryPrice": "50000",
                        "unrealizedProfit": "-5.0",
                        "positionSide": "BOTH",
                    }
                ]
            }
        ))

        data = {
            "start_balance_usdt": 500.0,
            "total_trades": 1,
            "per_coin": {"BTCUSDT": {"realized_pnl_usdt": 10.0}}
        }

        success = await self.mgr._update_drawdowns(mock_client, data)
        self.assertTrue(success)
        self.assertEqual(data.get("unrealized_pnl_usdt"), -5.0)

    async def test_reset_analytics_state(self):
        await self.mgr.reset_analytics_state()
        self.assertFalse(self.mgr.log_file.exists())
        self.assertFalse(self.mgr.txt_file.exists())
        self.assertFalse(self.mgr.peels_ledger.exists())
        self.assertFalse(self.mgr.recycling_ledger.exists())


if __name__ == "__main__":
    unittest.main()
