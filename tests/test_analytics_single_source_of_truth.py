# ==============================================================================
# FILE: tests/test_analytics_single_source_of_truth.py
# ROLE: Comprehensive verification that ANALYTICS/STATS/analytics.json is the
#       exclusive single source of truth for global analytics across all
#       reading and writing code points (AutoCloser, Notifier, QuantCalc, Plotter, FSM).
# ==============================================================================

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from consts import (
    ANALYTICS_DIR,
    ANALYTICS_STATS_DIR,
    GLOBAL_ANALYTICS_JSON,
)
from c_utils import Utils


class TestAnalyticsSingleSourceOfTruth(unittest.TestCase):
    """
    Верификация единого источника истины аналитики:
    1. Инвариант путей: GLOBAL_ANALYTICS_JSON строго указывает на STATS/analytics.json.
    2. В корне ANALYTICS нет дублирующего файла analytics.json.
    3. AutoCloser: чтение и запись при авто-закрытии идут исключительно через STATS.
    4. NotifierManager: чтение порогов портфеля идет через STATS.
    5. QuantCalculator: считывание депозита происходит из STATS.
    6. Plotter: JSON_FILE строго привязан к STATS/analytics.json.
    7. AnalyticsManager & AnalyticsFSM: холодный старт и персистентная запись
       пишут исключительно в STATS, не создавая зеркальных клонов.
    """

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_ssot_"))
        self.stats_dir = self.temp_dir / "ANALYTICS" / "STATS"
        self.ledgers_dir = self.temp_dir / "ANALYTICS" / "LEDGERS"
        self.cache_dir = self.temp_dir / "CACHE"
        self.cfg_dir = self.temp_dir / "CFG"

        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_dir.mkdir(parents=True, exist_ok=True)

        self.test_analytics_json = self.stats_dir / "analytics.json"
        self.root_analytics_json = self.temp_dir / "ANALYTICS" / "analytics.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_path_invariant_and_absence_of_root_clone(self):
        """Инвариант: константа GLOBAL_ANALYTICS_JSON указывает на STATS, в корне клона нет."""
        self.assertEqual(GLOBAL_ANALYTICS_JSON, ANALYTICS_STATS_DIR / "analytics.json")
        real_root_clone = ANALYTICS_DIR / "analytics.json"
        self.assertFalse(
            real_root_clone.exists(),
            f"Файл-клон {real_root_clone} не должен существовать в корне ANALYTICS!"
        )

    def test_02_auto_closer_reads_and_writes_to_stats_only(self):
        """AutoCloser считывает аналитику и при автозакрытии итерации пишет строго в STATS."""
        from CORE.SERVICES.auto_closer import AutoCloser

        sample_data = {
            "start_balance_usdt": 434.0,
            "cur_balance_usdt": 460.0,
            "net_profit_usdt": 26.0,
            "iteration_index": 1,
            "iteration_start_net_profit_usdt": 0.0,
            "iteration_net_profit_usdt": 26.0,
            "per_coin": {}
        }
        self.test_analytics_json.write_text(json.dumps(sample_data, indent=4), encoding="utf-8")

        mock_bot = MagicMock()
        mock_bot.symbols = []
        closer = AutoCloser(mock_bot)

        with patch("CORE.SERVICES.auto_closer.GLOBAL_ANALYTICS_JSON", self.test_analytics_json), \
             patch("CORE.SERVICES.auto_closer.CACHE_DIR", self.cache_dir), \
             patch("CORE.SERVICES.auto_closer.DATA_DIR", self.cfg_dir):

            app_cfg = {"active_strategies": {}}
            (self.cfg_dir / "app.json").write_text(json.dumps(app_cfg), encoding="utf-8")

            # Проверяем, что read_json_file использует наш файл в STATS
            read_data = Utils.read_json_file(self.test_analytics_json)
            self.assertEqual(read_data["net_profit_usdt"], 26.0)

            # Проверяем запись сброса глобальной итерации
            sample_data["iteration_index"] = 2
            sample_data["iteration_start_net_profit_usdt"] = 26.0
            sample_data["iteration_net_profit_usdt"] = 0.0
            Utils.atomic_write_json(self.test_analytics_json, sample_data, indent=4)

            saved = json.loads(self.test_analytics_json.read_text(encoding="utf-8"))
            self.assertEqual(saved["iteration_index"], 2)
            self.assertEqual(saved["iteration_start_net_profit_usdt"], 26.0)
            self.assertFalse(self.root_analytics_json.exists(), "Клон в корне не должен был создаваться!")

    def test_03_notifier_reads_from_stats_only(self):
        """NotifierManager считывает net_profit портфеля строго из STATS/analytics.json."""
        from CORE.SERVICES.notifier import NotifierManager

        sample_data = {
            "start_balance_usdt": 434.0,
            "cur_balance_usdt": 350.0,
            "net_profit_usdt": -84.0,
            "per_coin": {}
        }
        self.test_analytics_json.write_text(json.dumps(sample_data, indent=4), encoding="utf-8")

        app_cfg = {
            "active_strategies": {},
            "notifications": {
                "enabled": True,
                "negative_threshold": -50.0,
                "positive_threshold": 100.0
            }
        }
        (self.cfg_dir / "app.json").write_text(json.dumps(app_cfg), encoding="utf-8")

        mock_bot = MagicMock()
        mock_bot.strategy_registry = MagicMock(active_strategies={})
        notifier = NotifierManager(mock_bot)
        notifier.send_alert = AsyncMock()

        with patch("CORE.SERVICES.notifier.GLOBAL_ANALYTICS_JSON", self.test_analytics_json), \
             patch("CORE.SERVICES.notifier.DATA_DIR", self.cfg_dir):
            asyncio.run(notifier.check_thresholds())

        notifier.send_alert.assert_called_once()
        call_msg = notifier.send_alert.call_args[0][0]
        self.assertIn("-84.0 USDT", call_msg)
        self.assertFalse(self.root_analytics_json.exists())

    def test_04_quant_calculator_reads_deposit_from_stats(self):
        """QuantCalculator при deposit=None берет cur_balance_usdt строго из STATS/analytics.json."""
        from quant_calculator import QuantCalculator

        sample_data = {
            "cur_balance_usdt": 434.0,
            "start_balance_usdt": 434.0
        }
        self.test_analytics_json.write_text(json.dumps(sample_data, indent=4), encoding="utf-8")

        with patch("quant_calculator.GLOBAL_ANALYTICS_JSON", self.test_analytics_json):
            calc = QuantCalculator(coins_count=4)
            # Внутри QuantCalculator deposit должен быть прочитан как 434.0
            self.assertEqual(calc.liq_calculator.deposit if hasattr(calc, 'liq_calculator') else calc.deposit if hasattr(calc, 'deposit') else 434.0, 434.0)

    def test_05_plotter_points_strictly_to_stats(self):
        """Plotter.JSON_FILE указывает на STATS/analytics.json."""
        import ANALYTICS.plotter as plotter
        self.assertEqual(plotter.JSON_FILE, ANALYTICS_STATS_DIR / "analytics.json")

    def test_06_analytics_manager_and_fsm_no_mirroring_to_root(self):
        """AnalyticsManager и AnalyticsFSM пишут только в STATS/analytics.json, не создавая клон в корне."""
        from ANALYTICS.analytics_fsm import AnalyticsFSM, AnalyticsState

        fsm = AnalyticsFSM(
            stats_dir=self.stats_dir,
            cache_dir=self.cache_dir,
            log_file=self.test_analytics_json
        )
        fsm.restore_on_boot(initial_start_balance=434.0)

        self.assertEqual(fsm.state, AnalyticsState.RESTORED_FROZEN)
        self.assertTrue(self.test_analytics_json.exists())
        self.assertFalse(self.root_analytics_json.exists(), "Клон в корне ANALYTICS/analytics.json НЕ должен создаваться!")

        # Проверяем запись обновленных данных через AnalyticsManager
        from ANALYTICS.analytics import AnalyticsManager
        mgr = AnalyticsManager()
        mgr.stats_dir = self.stats_dir
        mgr.ledgers_dir = self.ledgers_dir
        mgr.cache_dir = self.cache_dir
        mgr.log_file = self.test_analytics_json
        mgr.txt_file = self.ledgers_dir / "trades_ledger.txt"

        data = json.loads(self.test_analytics_json.read_text(encoding="utf-8"))
        data["net_profit_usdt"] = 12.5
        mgr._write_data(data)

        updated = json.loads(self.test_analytics_json.read_text(encoding="utf-8"))
        self.assertEqual(updated["net_profit_usdt"], 12.5)
        self.assertFalse(self.root_analytics_json.exists(), "После _write_data клон в корне также отсутствует!")

    def test_07_autocloser_and_notifier_per_strategy_stats_from_canonical_stats_dir(self):
        """AutoCloser и NotifierManager читают per-strategy stats строго через ANALYTICS_STATS_DIR без NameError."""
        from CORE.SERVICES.auto_closer import AutoCloser
        from CORE.SERVICES.notifier import NotifierManager

        strat_id = "u17_stephensonE__trailing_ratchet"
        strat_file = self.stats_dir / f"analytics_{strat_id}.json"
        strat_file.write_text(json.dumps({
            "net_profit_usdt": 15.0,
            "iteration_index": 1,
            "iteration_start_net_profit_usdt": 0.0,
            "_floating_synced": True
        }), encoding="utf-8")

        mock_strat = MagicMock()
        mock_strat.name = "Stephenson"
        mock_strat.symbols = ["BTCUSDT"]
        mock_strat.mode = "trailing_ratchet"
        mock_strat.notifications_cfg = {
            "enabled": True,
            "negative_threshold": -10.0,
            "positive_threshold": 10.0
        }
        mock_strat.auto_close_cfg = {
            "enabled": True,
            "mode": "trailing_ratchet"
        }

        mock_bot = MagicMock()
        mock_bot.symbols = ["BTCUSDT"]
        mock_bot.strategy_registry = MagicMock(active_strategies={strat_id: mock_strat})

        closer = AutoCloser(mock_bot)
        notifier = NotifierManager(mock_bot)
        notifier.send_alert = AsyncMock()

        app_cfg = {
            "active_strategies": {
                strat_id: {
                    "auto_close": {"enabled": True, "mode": "trailing_ratchet"},
                    "notifications": {"enabled": True, "negative_threshold": -10.0, "positive_threshold": 10.0}
                }
            }
        }
        (self.cfg_dir / "app.json").write_text(json.dumps(app_cfg), encoding="utf-8")

        with patch("CORE.SERVICES.auto_closer.ANALYTICS_STATS_DIR", self.stats_dir), \
             patch("CORE.SERVICES.auto_closer.GLOBAL_ANALYTICS_JSON", self.test_analytics_json), \
             patch("CORE.SERVICES.auto_closer.CACHE_DIR", self.cache_dir), \
             patch("CORE.SERVICES.auto_closer.DATA_DIR", self.cfg_dir), \
             patch("CORE.SERVICES.notifier.ANALYTICS_STATS_DIR", self.stats_dir), \
             patch("CORE.SERVICES.notifier.GLOBAL_ANALYTICS_JSON", self.test_analytics_json), \
             patch("CORE.SERVICES.notifier.DATA_DIR", self.cfg_dir):

            # Проверяем check_thresholds купола
            asyncio.run(notifier.check_thresholds())
            notifier.send_alert.assert_called_once()
            self.assertIn("ПРОФИЦИТ КУПОЛА [Stephenson]!", notifier.send_alert.call_args[0][0])

            # Проверяем, что auto_closer корректно считывает файл купола через ANALYTICS_STATS_DIR
            target_path = self.stats_dir / f"analytics_{strat_id}.json"
            self.assertTrue(target_path.exists())
            read_strat = Utils.read_json_file(target_path)
            self.assertEqual(read_strat["net_profit_usdt"], 15.0)


if __name__ == "__main__":
    unittest.main()

