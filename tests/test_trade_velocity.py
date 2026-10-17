# tests/test_trade_velocity.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: tests/test_trade_velocity.py
Role: Институциональный юнит-тест анализатора скорости сделок (TradeVelocityEngine)
==============================================================================
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ANALYTICS.trade_velocity import TradeVelocityEngine


class TestTradeVelocityEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_trade_velocity_"))
        self.ledger_file = self.temp_dir / "trades_ledger.txt"

        self.app_json = self.temp_dir / "app.json"
        self.app_json.write_text(
            json.dumps({
                "active_strategies": {
                    "test_u": {
                        "enabled": True,
                        "symbols": ["BTCUSDT", "ETHUSDT"],
                    }
                }
            }),
            encoding="utf-8",
        )

        header = "ID;Symbol;Side;Open Time;Close Time;Entry Price;Close Price;Volume;Net Profit;Gross Profit;Commissions;Duration;Result;Exit Type\n"
        self.ledger_file.write_text(header, encoding="utf-8")

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_ledger_initialization(self):
        """Инициализация с пустым леджером дает состояние ACCUMULATING."""
        with patch("ANALYTICS.trade_velocity.DATA_DIR", self.temp_dir), \
             patch("ANALYTICS.trade_velocity.ANALYTICS_LEDGERS_DIR", self.temp_dir):
            engine = TradeVelocityEngine(universe_id="test_u", ledger_path=self.ledger_file)
            self.assertEqual(len(engine._trades_history), 0)
            metrics = engine.calculate_velocity()
            self.assertEqual(metrics["slice_trades_count"], 0)
            self.assertFalse(metrics["is_ready_for_alerts"])
            self.assertEqual(metrics["status"], "ACCUMULATING")

    def test_ledger_parsing_and_metrics_calculation(self):
        """Парсинг сделок из леджера и расчет метрик скорости."""
        now = time.time()
        rows = []
        for i in range(10):
            trade_ts = now - (i * 600)
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(trade_ts))
            sym = "BTCUSDT" if i % 2 == 0 else "ETHUSDT"
            rows.append(f"{i+1};{sym};LONG;{time_str};{time_str};50000;50500;0.1;50;50;1;10m;WIN;TP\n")

        with open(self.ledger_file, "a", encoding="utf-8") as f:
            f.writelines(rows)

        with patch("ANALYTICS.trade_velocity.DATA_DIR", self.temp_dir), \
             patch("ANALYTICS.trade_velocity.ANALYTICS_LEDGERS_DIR", self.temp_dir):
            engine = TradeVelocityEngine(universe_id="test_u", ledger_path=self.ledger_file)
            self.assertEqual(len(engine._trades_history), 10)

            engine.min_accumulation_hours = 0.0
            metrics = engine.calculate_velocity({"symbols": ["BTCUSDT", "ETHUSDT"]})

            self.assertTrue(metrics["is_ready_for_alerts"])
            self.assertEqual(metrics["slice_trades_count"], 10)
            self.assertIn("BTCUSDT", metrics["per_coin"])
            self.assertIn("ETHUSDT", metrics["per_coin"])
            self.assertEqual(metrics["per_coin"]["BTCUSDT"]["trades"], 5)
            self.assertEqual(metrics["per_coin"]["ETHUSDT"]["trades"], 5)

    def test_on_trade_closed_dynamically(self):
        """Динамическая регистрация новых закрытых сделок на лету через on_trade_closed."""
        with patch("ANALYTICS.trade_velocity.DATA_DIR", self.temp_dir), \
             patch("ANALYTICS.trade_velocity.ANALYTICS_LEDGERS_DIR", self.temp_dir):
            engine = TradeVelocityEngine(universe_id="test_u", ledger_path=self.ledger_file)
            engine.min_accumulation_hours = 0.0

            now_ms = int(time.time() * 1000)
            engine.on_trade_closed("ETHUSDT", now_ms - 10000)
            engine.on_trade_closed("ETHUSDT", now_ms)

            # Для предотвращения перечитывания пустого леджера симулируем запись в файл
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now_ms / 1000.0))
            with open(self.ledger_file, "a", encoding="utf-8") as f:
                f.write(f"1;ETHUSDT;LONG;{time_str};{time_str};3000;3050;1;50;50;1;10m;WIN;TP\n")
                f.write(f"2;ETHUSDT;LONG;{time_str};{time_str};3000;3050;1;50;50;1;10m;WIN;TP\n")

            metrics = engine.calculate_velocity({"symbols": ["ETHUSDT"]})
            self.assertGreaterEqual(metrics["slice_trades_count"], 2)

    def test_timeframe_window_filtering(self):
        """Сделки старше timeframe_hours отсекаются из расчетного окна методом _purge_old_trades."""
        now = time.time()
        with patch("ANALYTICS.trade_velocity.DATA_DIR", self.temp_dir), \
             patch("ANALYTICS.trade_velocity.ANALYTICS_LEDGERS_DIR", self.temp_dir):
            engine = TradeVelocityEngine(universe_id="test_u", ledger_path=self.ledger_file)
            engine.timeframe_hours = 1.0  # Окно 1 час

            now_ms = int(now * 1000)
            old_trade_ms = now_ms - (3 * 3600 * 1000)  # 3 часа назад
            fresh_trade_ms = now_ms - (30 * 60 * 1000)  # 30 минут назад

            engine._trades_history = [(old_trade_ms, "BTCUSDT"), (fresh_trade_ms, "BTCUSDT")]
            engine._purge_old_trades(now_ms)

            self.assertEqual(len(engine._trades_history), 1)
            self.assertEqual(engine._trades_history[0][0], fresh_trade_ms)

    def test_activity_ratio_status_thresholds(self):
        """Проверка градаций статусов активности: LOW_VOLATILITY, NORMAL, HIGH_VOLATILITY."""
        now = time.time()
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))

        with patch("ANALYTICS.trade_velocity.DATA_DIR", self.temp_dir), \
             patch("ANALYTICS.trade_velocity.ANALYTICS_LEDGERS_DIR", self.temp_dir):
            # 1. Низкая активность (1 сделка в леджере при benchmark 100)
            with open(self.ledger_file, "a", encoding="utf-8") as f:
                f.write(f"1;BTCUSDT;LONG;{time_str};{time_str};50000;50500;0.1;50;50;1;10m;WIN;TP\n")

            engine_low = TradeVelocityEngine(universe_id="test_u", ledger_path=self.ledger_file)
            engine_low.min_accumulation_hours = 0.0
            engine_low.benchmark_trades = 100.0
            engine_low.start_ts_ms = int((now - 24 * 3600) * 1000)

            metrics_low = engine_low.calculate_velocity({"symbols": ["BTCUSDT"]})
            self.assertEqual(metrics_low["status"], "LOW_VOLATILITY")
            self.assertEqual(metrics_low["status_desc"], "НЕДОГРЕВ")

            # 2. Высокая активность (добавляем 100 сделок)
            rows = [f"{i+2};BTCUSDT;LONG;{time_str};{time_str};50000;50500;0.1;50;50;1;10m;WIN;TP\n" for i in range(100)]
            with open(self.ledger_file, "a", encoding="utf-8") as f:
                f.writelines(rows)

            engine_high = TradeVelocityEngine(universe_id="test_u", ledger_path=self.ledger_file)
            engine_high.min_accumulation_hours = 0.0
            engine_high.benchmark_trades = 20.0
            engine_high.start_ts_ms = int((now - 24 * 3600) * 1000)

            metrics_high = engine_high.calculate_velocity({"symbols": ["BTCUSDT"]})
            self.assertEqual(metrics_high["status"], "HIGH_VOLATILITY")
            self.assertEqual(metrics_high["status_desc"], "ВОЛАТИЛЬНОСТЬ ЗАШИБИСЬ")


if __name__ == "__main__":
    unittest.main()
