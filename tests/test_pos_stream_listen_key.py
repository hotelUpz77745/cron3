# tests/test_pos_stream_listen_key.py
# Role: Unit tests for BinanceListenKeyManager and PositionStream message processing

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp

from POS_FSM.pos_stream import BinanceListenKeyManager, PositionStream


class TestBinanceListenKeyManager(unittest.IsolatedAsyncioTestCase):
    async def test_create_listen_key_success(self):
        mock_session = MagicMock()
        mock_post_resp = AsyncMock()
        mock_post_resp.json = AsyncMock(return_value={"listenKey": "test_listen_key_12345"})
        
        # Async context manager for post()
        mock_session.post.return_value.__aenter__.return_value = mock_post_resp
        mock_session.put = AsyncMock()

        mgr = BinanceListenKeyManager(api_key="mock_api_key", session=mock_session)
        key = await mgr.create()

        self.assertEqual(key, "test_listen_key_12345")
        self.assertEqual(mgr.listen_key, "test_listen_key_12345")
        mock_session.post.assert_called_once_with(
            "https://fapi.binance.com/fapi/v1/listenKey",
            headers={"X-MBX-APIKEY": "mock_api_key"},
        )
        mock_session.put.assert_called_once_with(
            "https://fapi.binance.com/fapi/v1/listenKey",
            headers={"X-MBX-APIKEY": "mock_api_key"},
        )

    async def test_create_listen_key_rejected(self):
        mock_session = MagicMock()
        mock_post_resp = AsyncMock()
        mock_post_resp.json = AsyncMock(return_value={"code": -2014, "msg": "API-key format invalid."})
        mock_session.post.return_value.__aenter__.return_value = mock_post_resp

        mgr = BinanceListenKeyManager(api_key="bad_api_key", session=mock_session)
        with self.assertRaises(ValueError) as ctx:
            await mgr.create()
        self.assertIn("API-key format invalid", str(ctx.exception))

    async def test_keepalive_start_and_stop(self):
        mock_session = MagicMock()
        mock_session.put = AsyncMock()
        mgr = BinanceListenKeyManager(api_key="mock_api_key", session=mock_session)
        mgr.KEEPALIVE_INTERVAL = 0.01  # Fast for test

        mgr.start_keepalive()
        self.assertIsNotNone(mgr._task)
        self.assertFalse(mgr._task.done())

        # Allow loop to execute iteration
        await asyncio.sleep(0.03)
        self.assertGreaterEqual(mock_session.put.call_count, 1)

        await mgr.stop()
        self.assertIsNone(mgr._task)


class TestPositionStream(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stop_flag = False
        self.mock_monitor = MagicMock()
        self.stream = PositionStream(
            api_key="test_key",
            stop_flag=lambda: self.stop_flag,
            monitor=self.mock_monitor,
            target_symbols={"BTCUSDT", "ETHUSDT"},
        )

    def test_init_and_register_callback(self):
        cb1 = MagicMock()
        cb2 = AsyncMock()
        self.stream.register_callback(cb1)
        self.stream.register_callback(cb2)
        self.assertEqual(len(self.stream.callbacks), 2)
        self.assertEqual(self.stream.target_symbols, {"BTCUSDT", "ETHUSDT"})

    def test_stop_flag_signal(self):
        self.stream.stop()
        self.assertTrue(self.stream._external_stop)
        self.assertFalse(self.stream.ready)

    async def test_handle_messages_dispatch_to_callbacks(self):
        mock_ws = AsyncMock()
        
        # Message 1: valid account update
        msg1 = MagicMock()
        msg1.type = aiohttp.WSMsgType.TEXT
        msg1.data = '{"e": "ACCOUNT_UPDATE", "T": 123456789}'

        # Message 2: non-text message (should be skipped)
        msg2 = MagicMock()
        msg2.type = aiohttp.WSMsgType.BINARY
        msg2.data = b"raw binary"

        # Message 3: bad json (should be skipped safely)
        msg3 = MagicMock()
        msg3.type = aiohttp.WSMsgType.TEXT
        msg3.data = "not-a-valid-json"

        # Message 4: stop by timeout or closed
        msg4 = MagicMock()
        msg4.type = aiohttp.WSMsgType.CLOSED

        mock_ws.receive = AsyncMock(side_effect=[msg1, msg2, msg3, msg4])
        self.stream.websocket = mock_ws

        received_events = []
        def sync_cb(data):
            received_events.append(data.get("e"))

        async def async_cb(data):
            received_events.append("async_" + data.get("e", ""))

        self.stream.register_callback(sync_cb)
        self.stream.register_callback(async_cb)

        with self.assertRaises(RuntimeError) as ctx:
            await self.stream._handle_messages()

        self.assertIn("ws_closed", str(ctx.exception))
        # Give asyncio tasks spawned for async_cb a moment to finish
        await asyncio.sleep(0.01)
        self.assertIn("ACCOUNT_UPDATE", received_events)
        self.assertIn("async_ACCOUNT_UPDATE", received_events)

    async def test_disconnect_clean_cleanup(self):
        mock_ws = AsyncMock()
        mock_mgr = AsyncMock()
        self.stream.websocket = mock_ws
        self.stream.listen_mgr = mock_mgr
        self.stream.is_connected = True
        self.stream.ready = True

        await self.stream._disconnect()

        self.assertFalse(self.stream.is_connected)
        self.assertFalse(self.stream.ready)
        self.assertIsNone(self.stream.websocket)
        self.assertIsNone(self.stream.listen_mgr)
        mock_ws.close.assert_awaited_once()
        mock_mgr.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
