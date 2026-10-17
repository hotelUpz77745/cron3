# tests/test_bot_bootstrap.py
# Role: Unit tests for BotBootstrapService (CORE/SERVICES/bot_bootstrap.py)

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.SERVICES.bot_bootstrap import BotBootstrapService


class TestBotBootstrapService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_bot = MagicMock()
        self.mock_bot.is_running = True
        self.mock_bot.symbols = ["BTCUSDT", "ETHUSDT"]
        self.mock_bot.strategy_registry = MagicMock()
        self.mock_bot.volatility_manager = MagicMock()
        self.mock_bot.volatility_manager.process_all = AsyncMock()
        self.mock_bot.runtime_manager = MagicMock()
        self.mock_bot.runtime_manager.caches = {}
        self.mock_bot.spec_manager = MagicMock()
        self.mock_bot.price_stream = MagicMock()
        self.mock_bot.price_stream.ready = asyncio.Event()
        self.mock_bot.price_stream.ready.set()
        self.mock_bot.price_stream.snapshot_received = asyncio.Event()
        self.mock_bot.price_stream.snapshot_received.set()
        self.mock_bot.price_stream.run = AsyncMock()
        self.mock_bot.prices = {"BTCUSDT": (50000.0, 1000.0), "ETHUSDT": (3000.0, 1000.0)}
        self.mock_bot.fsm_states = {}
        self.mock_bot.client = MagicMock()
        self.mock_bot.runtime_configs = {}
        self.mock_bot.leverage_manager = MagicMock()
        self.mock_bot.leverage_manager.set_leverage_and_margin = AsyncMock()
        self.mock_bot._reconcile_open_orders = AsyncMock()
        self.mock_bot.notifier = MagicMock()
        self.mock_bot.notifier.start = AsyncMock()
        self.mock_bot.notifier.stop = AsyncMock()
        self.mock_bot.watchdog = MagicMock()
        self.mock_bot.watchdog.start = AsyncMock()
        self.mock_bot.watchdog.stop = MagicMock()
        self.mock_bot.analytics = MagicMock()
        self.mock_bot.analytics.deep_sync_analytics = AsyncMock()
        self.mock_bot.spec_data = {"symbols": ["BTCUSDT", "ETHUSDT"]}

    @patch("CORE.SERVICES.bot_bootstrap.build_runtime_caches", return_value=False)
    @patch("CORE.SERVICES.bot_bootstrap.StrategyApplicator")
    async def test_prepare_game_loop_success(self, mock_applicator, mock_build_caches):
        res = await BotBootstrapService.prepare_game_loop(self.mock_bot)
        self.assertTrue(res)
        mock_build_caches.assert_called_once_with(self.mock_bot.symbols, registry=self.mock_bot.strategy_registry)
        self.mock_bot.volatility_manager.process_all.assert_awaited_once()
        self.mock_bot.volatility_manager.start.assert_called_once()
        self.mock_bot.runtime_manager.load_initial_caches.assert_called_once_with(self.mock_bot.symbols)

    async def test_prepare_game_loop_exits_if_not_running(self):
        self.mock_bot.is_running = False
        res = await BotBootstrapService.prepare_game_loop(self.mock_bot)
        self.assertFalse(res)

    @patch("CORE.SERVICES.bot_bootstrap.PositionMonitor")
    @patch("CORE.SERVICES.bot_bootstrap.PositionStream")
    @patch("CORE.SERVICES.bot_bootstrap.BinanceWsInterpreter")
    async def test_init_streams_and_fsm(self, mock_interpreter, mock_pos_stream, mock_monitor):
        mock_pos_stream_inst = MagicMock()
        mock_pos_stream_inst.ready = True
        mock_pos_stream_inst.start = AsyncMock()
        mock_pos_stream.return_value = mock_pos_stream_inst
        mock_monitor_inst = MagicMock()
        mock_monitor_inst.sync_from_rest = AsyncMock()
        mock_monitor.return_value = mock_monitor_inst

        self.mock_bot.runtime_manager.sync_with_fsm = AsyncMock()

        await BotBootstrapService.init_streams_and_fsm(self.mock_bot)

        self.mock_bot.spec_manager.start.assert_called_once()
        mock_monitor_inst.sync_from_rest.assert_awaited_once_with(self.mock_bot.client, self.mock_bot.symbols)
        self.mock_bot.runtime_manager.sync_with_fsm.assert_awaited_once()
        self.mock_bot._reconcile_open_orders.assert_awaited_once_with(force=True)
        self.mock_bot.notifier.start.assert_awaited_once()
        self.mock_bot.analytics.deep_sync_analytics.assert_awaited_once()
        self.mock_bot.analytics.start_realtime_tracker.assert_called_once()

        # Clean up spawned tasks
        if self.mock_bot._price_task:
            self.mock_bot._price_task.cancel()
        if self.mock_bot._pos_task:
            self.mock_bot._pos_task.cancel()
        if self.mock_bot._watchdog_task:
            self.mock_bot._watchdog_task.cancel()

    async def test_shutdown_game_loop(self):
        dummy_task = asyncio.create_task(asyncio.sleep(10))
        self.mock_bot._price_task = dummy_task
        self.mock_bot._pos_task = None
        self.mock_bot._watchdog_task = None
        self.mock_bot._key_clients = {}

        await BotBootstrapService.shutdown_game_loop(self.mock_bot)

        self.assertFalse(self.mock_bot.is_running)
        self.mock_bot.notifier.stop.assert_awaited_once()
        self.mock_bot.watchdog.stop.assert_called_once()
        self.mock_bot.spec_manager.stop.assert_called_once()
        self.mock_bot.price_stream.stop.assert_called_once()
        self.assertTrue(dummy_task.cancelled())


if __name__ == "__main__":
    unittest.main()
