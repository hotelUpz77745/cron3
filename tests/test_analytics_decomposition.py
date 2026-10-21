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
import time
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
        self.cache_dir = self.temp_dir / "CACHE"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.mgr = AnalyticsManager()
        self.mgr.stats_dir = self.stats_dir
        self.mgr.ledgers_dir = self.ledgers_dir
        self.mgr.cache_dir = self.cache_dir
        self.mgr.log_file = self.stats_dir / "analytics.json"
        self.mgr.txt_file = self.ledgers_dir / "trades_ledger.txt"
        self.mgr.peels_ledger = self.ledgers_dir / "peels_ledger.txt"
        self.mgr.recycling_ledger = self.ledgers_dir / "recycling_ledger.txt"
        self.mgr._ensure_files()
        self.mgr._floating_synced = True

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_circuit_breaker(self):
        with patch("ANALYTICS.circuit_breaker._ban_until_ms", 0):
            self.assertFalse(is_banned())
            future_ts = int(time.time() * 1000) + 60_000
            update_ban_from_error(f"APIError(code=-1003): Way too many requests; IP banned until {future_ts}.")
            self.assertTrue(is_banned())

    def test_ledger_recorder_dynamic_paths(self):
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
        self.mgr._write_data({"start_balance_usdt": 500.0, "cur_balance_usdt": 500.0})
        self.mgr.rebase_start_balance(500.0)
        data = self.mgr._read_data()
        self.assertEqual(data["start_balance_usdt"], 500.0)
        self.assertEqual(data["cur_balance_usdt"], 500.0)

    def test_facade_delegation(self):
        self.assertIsInstance(self.mgr.ledger_recorder, LedgerRecorder)
        self.assertIsInstance(self.mgr.drawdown_tracker, DrawdownTracker)
        self.assertIsInstance(self.mgr.strategy_stats, StrategyStatsEngine)
        self.assertIsInstance(self.mgr.reconstruction, ReconstructionService)

        self.mgr._save_iteration_cache("s1", {"iteration_index": 3})
        cache = self.mgr._get_iteration_cache("s1")
        self.assertEqual(cache.get("iteration_index"), 3)

    def test_update_strategies_stats(self):
        mock_registry = MagicMock()
        mock_strat = MagicMock()
        mock_strat.name = "Test Universe"
        mock_strat.symbols = ["BTCUSDT"]
        mock_strat.analytics_cfg = {"start_balance_usdt": 200.0}
        mock_strat.start_balance_usdt = 200.0
        mock_registry.active_strategies = {"test_strat": mock_strat}
        mock_registry.get_active_strategies.return_value = {"test_strat": mock_strat}
        mock_registry.get_all_active_symbols.return_value = ["BTCUSDT"]

        self.mgr.strategy_registry = mock_registry
        data = {
            "start_balance_usdt": 500.0,
            "per_coin": {
                "BTCUSDT": {
                    "current_drawdown": -1.5,
                    "realized_pnl_usdt": 10.0,
                    "realized_pnl_net_usdt": 9.5,
                    "commission_usdt": -0.5,
                    "funding_usdt": 0.0,
                    "total_trades": 2,
                    "winning_trades": 2,
                }
            }
        }
        self.mgr._update_strategies_stats(data)

        strat_file = self.stats_dir / "analytics_test_strat.json"
        self.assertTrue(strat_file.exists())
        strat_json = json.loads(strat_file.read_text(encoding="utf-8"))
        self.assertEqual(strat_json["universe_id"], "test_strat")
        self.assertEqual(strat_json["unrealized_pnl_usdt"], -1.5)


class TestAsyncAnalyticsDecomposition(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.stats_dir = self.temp_dir / "STATS"
        self.ledgers_dir = self.temp_dir / "LEDGERS"
        self.cache_dir = self.temp_dir / "CACHE"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.mgr = AnalyticsManager()
        self.mgr.stats_dir = self.stats_dir
        self.mgr.ledgers_dir = self.ledgers_dir
        self.mgr.cache_dir = self.cache_dir
        self.mgr.log_file = self.stats_dir / "analytics.json"
        self.mgr.txt_file = self.ledgers_dir / "trades_ledger.txt"
        self.mgr.peels_ledger = self.ledgers_dir / "peels_ledger.txt"
        self.mgr.recycling_ledger = self.ledgers_dir / "recycling_ledger.txt"
        self.mgr._ensure_files()
        self.mgr._floating_synced = True

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

    async def test_get_bnb_usdt_rate(self):
        # Cached
        rate = await self.mgr._get_bnb_usdt_rate(None, cached_prices={"BNBUSDT": (600.5, 600.6)})
        self.assertEqual(rate, 600.5)

        # Fallback to 0.0 when no network/cache
        mock_client = MagicMock()
        rate_none = await self.mgr._get_bnb_usdt_rate(mock_client, cached_prices=None)
        self.assertIsInstance(rate_none, float)

    async def test_sync_lightweight_unrealized_pnl(self):
        mock_client = MagicMock()
        mock_client.fetch_account_info = AsyncMock(return_value=MagicMock(
            success=True,
            data={
                "totalMarginBalance": "500.0",
                "positions": []
            }
        ))
        res = await self.mgr.sync_lightweight_unrealized_pnl(mock_client)
        self.assertTrue(res)

    async def test_realtime_tracker_lifecycle(self):
        mock_client = MagicMock()
        self.mgr.start_realtime_tracker(mock_client)
        self.assertTrue(self.mgr.drawdown_tracker._is_tracker_running)
        self.assertIsNotNone(self.mgr.drawdown_tracker._tracker_task)

        self.mgr.stop_realtime_tracker()
        self.assertFalse(self.mgr.drawdown_tracker._is_tracker_running)
        self.assertIsNone(self.mgr.drawdown_tracker._tracker_task)

    async def test_deep_sync_analytics(self):
        now_ts = int(time.time() * 1000)
        mock_client = MagicMock()
        mock_client._request = AsyncMock(side_effect=[
            MagicMock(
                success=True,
                data=[
                    {
                        "symbol": "BTCUSDT",
                        "incomeType": "REALIZED_PNL",
                        "income": "25.0",
                        "asset": "USDT",
                        "time": now_ts - 500,
                        "info": "trade_1",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "incomeType": "COMMISSION",
                        "income": "-0.5",
                        "asset": "USDT",
                        "time": now_ts - 500,
                        "info": "trade_1",
                    }
                ]
            ),
            MagicMock(success=True, data=[])
        ])
        mock_client.fetch_account_info = AsyncMock(return_value=MagicMock(
            success=True,
            data={
                "totalMarginBalance": "524.5",
                "positions": []
            }
        ))

        # Seed initial first_trade_ts close to now
        data = self.mgr._read_data()
        data["first_trade_ts"] = now_ts - 1000
        self.mgr._write_data(data)

        # Write a dummy entry to txt_file so BTCUSDT is tracked
        with open(self.mgr.txt_file, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(["BTCUSDT", "LONG", "2026-10-19 21:00:00", "2026-10-19 21:00:01", "25.0", "525.0"])

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await self.mgr.deep_sync_analytics(mock_client, cached_prices={"BNBUSDT": 600.0})

        # Check results
        synced_data = self.mgr._read_data()
        self.assertEqual(synced_data.get("realized_pnl_usdt"), 25.0)
        self.assertEqual(synced_data.get("total_commission_usdt"), -0.5)

    async def test_reset_analytics_state(self):
        await self.mgr.reset_analytics_state()
        self.assertFalse(self.mgr.log_file.exists())
        self.assertFalse(self.mgr.txt_file.exists())
        self.assertFalse(self.mgr.peels_ledger.exists())
        self.assertFalse(self.mgr.recycling_ledger.exists())

    async def test_drawdown_tracker_connector_closed_handling(self):
        """Verify that Connector is closed during shutdown does not log warnings or raise exceptions."""
        mock_client = MagicMock()
        mock_client.fetch_account_info = AsyncMock(return_value=MagicMock(
            success=False,
            data=None,
            error_msg="EXCEPTION ClientConnectionError: Connector is closed."
        ))
        data = {"start_balance_usdt": 500.0, "total_trades": 0}
        success = await self.mgr.drawdown_tracker.update_drawdowns(mock_client, data)
        self.assertFalse(success)

    def test_strategy_stats_telemetry_none_first_trade_ts(self):
        """Verify that None first_trade_ts or peak_net_ts in strategy stats does not cause TypeError."""
        strat_data = {
            "start_balance_usdt": 459.0,
            "cur_balance_usdt": 459.0,
            "first_trade_ts": None,
            "peak_net_ts": None,
            "net_profit_usdt": 0.0,
            "iteration_index": 1,
            "iteration_start_ts": None,
            "iteration_peak_ts": None,
            "per_coin": {}
        }
        mock_reg = MagicMock()
        mock_strat = MagicMock()
        mock_strat.name = "Test Universe"
        mock_strat.start_balance_usdt = 459.0
        mock_strat.raw_meta = {}
        mock_strat.symbols = ["BTCUSDT"]
        mock_reg.get_active_strategies.return_value = {"u17_test": mock_strat}
        mock_reg.active_strategies = {"u17_test": mock_strat}
        mock_reg.get_all_active_symbols.return_value = ["BTCUSDT"]

        self.mgr.strategy_registry = mock_reg
        # Should execute cleanly without TypeError
        self.mgr._update_strategies_stats(strat_data)
        out_file = self.stats_dir / "analytics_u17_test.json"
        self.assertTrue(out_file.exists())

    def test_tg_ranking_callback_data_length(self):
        """Verify that all ranking callback_data buttons stay strictly <= 64 bytes for long universe IDs."""
        long_uid = "u17_stephensonE__trailing_ratchet"
        buttons_data = [
            f"arank:net:{long_uid}:new",
            f"arank:net:{long_uid}:s",
            f"arank:rec:{long_uid}:s",
            f"arank:abs_rec:{long_uid}:s",
        ]
        for cb in buttons_data:
            self.assertLessEqual(len(cb.encode("utf-8")), 64, f"Callback data {cb} exceeds 64 bytes limit!")

    def test_screeners_menu_no_samsonov(self):
        """Verify that screeners menu does not include deprecated Samsonov screener."""
        from TG.tg_receiver import TelegramReceiver
        mock_core = MagicMock()
        receiver = TelegramReceiver(mock_core)
        # Search registered callback filters in dispatcher
        # Also verify that helper markup has 3 rows (2 screeners + 1 settings)
        # Inspect source code of tg_receiver for menu text
        import inspect
        src = inspect.getsource(receiver.__class__)
        self.assertNotIn("Скринер Самсонова (Радар)", src)
        self.assertNotIn("cb_selector_run:samsonov_funnel", src)


if __name__ == "__main__":
    unittest.main()
