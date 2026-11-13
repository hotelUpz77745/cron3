# ==============================================================================
# Path: tests/test_plotter_autoclose.py
# Role: Юнит-тесты отображения отметок автоклозера на графике эквити
# ==============================================================================

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import ANALYTICS.plotter as plotter


class TestPlotterAutocloseMarkers(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cache_dir = Path(self.temp_dir) / "CACHE"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.orig_cache_dir = plotter.CACHE_DIR
        plotter.CACHE_DIR = self.cache_dir

        self.images_dir = Path(self.temp_dir) / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.orig_images_dir = plotter.IMAGES_DIR
        plotter.IMAGES_DIR = self.images_dir

    def tearDown(self):
        plotter.CACHE_DIR = self.orig_cache_dir
        plotter.IMAGES_DIR = self.orig_images_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_autoclose_events_empty_when_no_file(self):
        events = plotter._load_autoclose_events("u96_omni_zenith13", 1435.0)
        self.assertEqual(events, [])

    def test_load_autoclose_events_filtering_and_parsing(self):
        history_file = self.cache_dir / "auto_close_history.json"
        now_ts = int(datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        history_data = {
            "u96_omni_zenith13__trailing_ratchet": [
                {
                    "iteration_index": 1,
                    "timestamp_ms": now_ts - 3600_000,
                    "close_type": "TRAILING RATCHET",
                    "iter_net": 12.5,
                    "strat_net": 12.5,
                    "balance": 1447.5,
                },
                {
                    "iteration_index": 2,
                    "timestamp_ms": now_ts,
                    "close_type": "TRAILING RATCHET",
                    "iter_net": 18.0,
                    "strat_net": 30.5,
                    "balance": 1465.5,
                },
            ],
            "other_strategy": [
                {
                    "iteration_index": 1,
                    "timestamp_ms": now_ts,
                    "close_type": "FIXED",
                    "iter_net": 5.0,
                    "strat_net": 5.0,
                    "balance": 505.0,
                }
            ],
        }
        history_file.write_text(json.dumps(history_data), encoding="utf-8")

        # Проверка фильтрации по префиксу вселенной
        events = plotter._load_autoclose_events("u96_omni_zenith13", 1435.0)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["iteration_index"], 1)
        self.assertAlmostEqual(events[0]["balance"], 1447.5)
        self.assertAlmostEqual(events[0]["iter_net"], 12.5)
        self.assertEqual(events[1]["iteration_index"], 2)
        self.assertAlmostEqual(events[1]["balance"], 1465.5)

        # Проверка глобального скоупа ALL
        events_all = plotter._load_autoclose_events("ALL", 1435.0)
        self.assertEqual(len(events_all), 3)

    def test_load_armed_target_floor(self):
        state_file = self.cache_dir / "auto_closer_state.json"
        state_data = {
            "u96_omni_zenith13__trailing_ratchet": {
                "active_level_idx": 1,
                "armed_floor": 16.0,
            }
        }
        state_file.write_text(json.dumps(state_data), encoding="utf-8")

        target = plotter._load_armed_target_floor("u96_omni_zenith13", 1435.0)
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target["target_floor"], 16.0)
        self.assertAlmostEqual(target["target_equity"], 1451.0)
        self.assertIn("1451.00", target["label"])

    def test_generate_equity_curve_with_autoclose_markers(self):
        if not plotter.MATPLOTLIB_AVAILABLE:
            self.skipTest("matplotlib не установлен")

        # Создаем тестовую историю автозакрытий
        history_file = self.cache_dir / "auto_close_history.json"
        t1 = int(datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        t2 = int(datetime(2026, 11, 13, 14, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        history_file.write_text(json.dumps({
            "u96_omni_zenith13": [
                {
                    "iteration_index": 1,
                    "timestamp_ms": t1,
                    "close_type": "TRAILING RATCHET",
                    "iter_net": 15.0,
                    "strat_net": 15.0,
                    "balance": 1450.0,
                },
                {
                    "iteration_index": 2,
                    "timestamp_ms": t2,
                    "close_type": "REGIME_ADAPTIVE",
                    "iter_net": 22.0,
                    "strat_net": 37.0,
                    "balance": 1472.0,
                },
            ]
        }), encoding="utf-8")

        # Создаем тестовое состояние вооруженного курка
        state_file = self.cache_dir / "auto_closer_state.json"
        state_file.write_text(json.dumps({
            "u96_omni_zenith13": {
                "armed_floor": 21.0,
            }
        }), encoding="utf-8")

        # Создаем тестовый json статистики
        json_file = Path(self.temp_dir) / "analytics_u96_omni_zenith13.json"
        json_file.write_text(json.dumps({
            "start_balance_usdt": 1435.0,
            "cur_balance_usdt": 1480.0,
            "net_profit_usdt": 45.0,
            "realized_pnl_net_usdt": 37.0,
            "unrealized_pnl_usdt": 8.0,
            "iteration_start_ts": t1 - 3600_000,
        }), encoding="utf-8")
        csv_file = Path(self.temp_dir) / "trades_ledger_u96_omni_zenith13.txt"
        csv_file.write_text("Symbol;Side;Open Time;Close Time;PnL;Balance\n", encoding="utf-8")

        with patch("ANALYTICS.plotter._resolve_paths", return_value=(json_file, csv_file)):
            plot_path = plotter.generate_equity_curve(universe_id="u96_omni_zenith13")
            self.assertTrue(os.path.exists(plot_path))
            self.assertGreater(os.path.getsize(plot_path), 5000)


if __name__ == "__main__":
    unittest.main()

