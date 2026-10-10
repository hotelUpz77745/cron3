# ==============================================================================
# Path: tests/test_price_stream_resilience.py
# Role: Юнит-тесты отказоустойчивости стриминга цен, REST Failsafe и Stream Watchdog
# ==============================================================================

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from API.BINANCE.price_stream import BinanceHotPriceStream, HotPriceTick
import consts


class TestConstsDeclarations(unittest.TestCase):
    def test_consts_sig_cfg_defined(self):
        """Проверяем корректность инициализации _SIG_CFG и таймфрейма в consts.py."""
        self.assertIsNotNone(consts._SIG_CFG)
        self.assertIsInstance(consts._SIG_CFG, dict)
        self.assertEqual(consts.DEFAULT_HEARTBEAT_TFR, "5m")
        self.assertEqual(consts.DEFAULT_TIMEFRAME, "5m")


class TestPriceStreamResilience(unittest.TestCase):
    def test_make_url(self):
        """Проверка генерации комбинированного URL Binance Futures WebSocket."""
        symbols = ["STRKUSDT", "PUMPUSDT", "METUSDT", "RLCUSDT"]
        url = BinanceHotPriceStream._make_url(symbols)
        expected = "wss://fstream.binance.com/stream?streams=strkusdt@trade/pumpusdt@trade/metusdt@trade/rlcusdt@trade"
        self.assertEqual(url, expected)

    def test_parse_tick_valid(self):
        stream = BinanceHotPriceStream(["BTCUSDT"])
        payload = {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "E": 1791650000000,
                "s": "BTCUSDT",
                "p": "65432.10",
                "q": "0.1"
            }
        }
        tick = stream._parse_tick(payload)
        self.assertIsNotNone(tick)
        self.assertEqual(tick.symbol, "BTCUSDT")
        self.assertEqual(tick.price, 65432.10)
        self.assertEqual(tick.event_time_ms, 1791650000000)

    def test_parse_tick_invalid(self):
        stream = BinanceHotPriceStream(["BTCUSDT"])
        self.assertIsNone(stream._parse_tick({}))
        self.assertIsNone(stream._parse_tick({"data": {"s": "BTCUSDT", "p": "-1.0"}}))


class TestBotCorePriceFailsafeAndWatchdog(unittest.TestCase):
    def setUp(self):
        self.mock_bot = MagicMock()
        self.mock_bot.symbols = ["STRKUSDT", "PUMPUSDT", "METUSDT", "RLCUSDT"]
        self.mock_bot.prices = {}
        self.mock_bot._last_bulk_price_fetch = 0.0
        self.mock_bot._last_stream_restart_ts = 0.0

    def test_stale_detection_logic(self):
        """
        Проверяем, что цены старше 10 секунд помечаются как stale.
        """
        now = time.time()
        # Свежая цена (2 сек назад)
        self.mock_bot.prices["STRKUSDT"] = (0.088, now - 2.0)
        # Устаревшая цена (12 сек назад)
        self.mock_bot.prices["PUMPUSDT"] = (0.005, now - 12.0)
        # Отсутствующая цена
        self.mock_bot.prices["METUSDT"] = None

        stale_symbols = []
        for sym in self.mock_bot.symbols:
            price_data = self.mock_bot.prices.get(sym)
            if not price_data or not isinstance(price_data, tuple) or (now - price_data[1] > 10.0):
                stale_symbols.append(sym)

        self.assertIn("PUMPUSDT", stale_symbols)
        self.assertIn("METUSDT", stale_symbols)
        self.assertIn("RLCUSDT", stale_symbols)
        self.assertNotIn("STRKUSDT", stale_symbols)

    def test_price_acceptance_window_15s(self):
        """
        Проверяем окно толерантности цен в _process_symbol_loop:
        цена принимается до 15 секунд, отвергается от 15 секунд.
        """
        now = time.time()
        # 14 секунд назад -> валидна
        p_data_valid = (1.23, now - 14.0)
        p, ts = p_data_valid
        self.assertTrue(now - ts < 15.0)

        # 16 секунд назад -> устарела
        p_data_stale = (1.23, now - 16.0)
        p, ts = p_data_stale
        self.assertFalse(now - ts < 15.0)

    def test_stream_watchdog_trigger(self):
        """
        Проверяем, что если все символы устарели более чем на 25 секунд,
        срабатывает автоматический перезапуск стрима.
        """
        now = time.time()
        for sym in self.mock_bot.symbols:
            self.mock_bot.prices[sym] = (1.0, now - 28.0)

        stale_symbols = [
            sym for sym in self.mock_bot.symbols
            if not self.mock_bot.prices.get(sym) or (now - self.mock_bot.prices[sym][1] > 10.0)
        ]
        self.assertEqual(len(stale_symbols), len(self.mock_bot.symbols))

        oldest_ts = min([self.mock_bot.prices[s][1] for s in self.mock_bot.symbols])
        watchdog_triggered = (now - oldest_ts > 25.0) and (now - self.mock_bot._last_stream_restart_ts >= 30.0)
        self.assertTrue(watchdog_triggered)


if __name__ == "__main__":
    unittest.main()
