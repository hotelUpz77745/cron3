# ==============================================================================
# Path: tests/test_delisting_detector.py
# Role: Unit tests for DelistingDetector (Binance USDT-M delisting monitor)
# ==============================================================================

import os
import time
import unittest
from unittest.mock import AsyncMock, patch

from CORE.SERVICES.delisting_detector import DelistingDetector
from API.BINANCE.public import BinancePublic


class _FakeBot:
    """Минимальная заглушка BotCore: детектору нужен только список обоймы."""

    def __init__(self, symbols):
        self.symbols = list(symbols)


class TestDelistingDetector(unittest.IsolatedAsyncioTestCase):
    def _make(self, symbols=("BTCUSDT",)):
        return DelistingDetector(_FakeBot(symbols))

    def _install_recorders(self, det):
        """Подменяет реакции детектора на записывающие заглушки."""
        closed, warned = [], []

        async def fake_close(sym, info, time_left_sec):
            closed.append((sym, info.get("reason"), time_left_sec))

        async def fake_warn(sym, info, time_left_sec):
            warned.append((sym, info.get("reason"), time_left_sec))

        det._hard_close = fake_close  # type: ignore[assignment]
        det._warn = fake_warn  # type: ignore[assignment]
        return closed, warned

    async def test_status_change_triggers_hard_close(self):
        det = self._make(("BTCUSDT",))
        closed, warned = self._install_recorders(det)

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "STATUS_CHANGE", "status": "SETTLING", "delist_time_sec": time.time()}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0][0], "BTCUSDT")
        self.assertEqual(closed[0][1], "STATUS_CHANGE")
        self.assertEqual(warned, [])

    async def test_delivery_within_threshold_hard_close(self):
        det = self._make(("BTCUSDT",))
        closed, warned = self._install_recorders(det)
        now_sec = time.time()

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "DELIST_SCHEDULED", "status": "TRADING", "delist_time_sec": now_sec + 3 * 3600}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()

        self.assertEqual(len(closed), 1)
        self.assertEqual(warned, [])
        self.assertLessEqual(closed[0][2], det.hard_close_sec)

    async def test_delivery_beyond_threshold_warn_only(self):
        det = self._make(("BTCUSDT",))
        closed, warned = self._install_recorders(det)
        now_sec = time.time()

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "DELIST_SCHEDULED", "status": "TRADING", "delist_time_sec": now_sec + 10 * 3600}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()

        self.assertEqual(closed, [])
        self.assertEqual(len(warned), 1)
        self.assertGreater(warned[0][2], det.hard_close_sec)

    async def test_news_triggers_hard_close(self):
        det = self._make(("BTCUSDT",))
        closed, warned = self._install_recorders(det)

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "NEWS_DELIST", "status": "", "delist_time_sec": time.time()}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0][1], "NEWS_DELIST")
        self.assertIsNone(closed[0][2])
        self.assertEqual(warned, [])

    async def test_hard_close_is_idempotent(self):
        det = self._make(("BTCUSDT",))
        closed, _ = self._install_recorders(det)

        async def fake_signals(force_refresh=False):
            return {"BTCUSDT": {"reason": "STATUS_CHANGE", "status": "CLOSE", "delist_time_sec": time.time()}}

        det.get_delisted_symbols = fake_signals  # type: ignore[assignment]
        await det._check_once()
        await det._check_once()

        self.assertEqual(len(closed), 1)  # повторно не закрываем

    async def test_foreign_symbols_filtered_out(self):
        det = self._make(("BTCUSDT",))
        instruments = [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "deliveryDate": 4133404800000},
            {"symbol": "OTHERUSDT", "status": "CLOSE", "contractType": "PERPETUAL", "deliveryDate": 0},
        ]
        with patch.object(BinancePublic, "get_instruments", new=AsyncMock(return_value=instruments)):
            with patch.object(det, "_fetch_news_delistings", new=AsyncMock(return_value=set())):
                res = await det.get_delisted_symbols(force_refresh=True)

        self.assertNotIn("OTHERUSDT", res)   # чужая монета не мониторится
        self.assertNotIn("BTCUSDT", res)     # TRADING + далёкий deliveryDate -> нет сигнала

    async def test_news_signal_detected_from_scraper(self):
        det = self._make(("BTCUSDT", "ERAUSDT"))
        instruments = [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "deliveryDate": 4133404800000},
            {"symbol": "ERAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "deliveryDate": 4133404800000},
        ]
        with patch.object(BinancePublic, "get_instruments", new=AsyncMock(return_value=instruments)):
            with patch.object(det, "_fetch_news_delistings", new=AsyncMock(return_value={"ERAUSDT"})):
                res = await det.get_delisted_symbols(force_refresh=True)

        self.assertIn("ERAUSDT", res)
        self.assertEqual(res["ERAUSDT"]["reason"], "NEWS_DELIST")
        self.assertNotIn("BTCUSDT", res)

    async def test_backtest_is_noop(self):
        det = self._make(("BTCUSDT",))
        with patch.dict(os.environ, {"CRON3_BACKTEST": "1"}):
            res = await det.get_delisted_symbols(force_refresh=True)
        self.assertEqual(res, {})


if __name__ == "__main__":
    unittest.main()
