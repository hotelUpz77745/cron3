# tests/test_close_all_sync.py
# Role: Unit tests for Close All iteration reset, leaderboard synchronization, and single-source runtime persistence

import unittest
import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
from TG.template_manager import TemplateManager
from CORE.STRATEGY.strategy_registry import StrategyRegistry


class TestCloseAllSyncAndRuntimeIsolation(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.runtime_dir = self.test_dir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_runtime_manager_writes_only_to_strategy_subdomain(self):
        """Проверка, что RuntimeFsmManager пишет строго в CFG/runtime/<strat_id>/<sym>.json и не дублирует в корень."""
        mock_registry = MagicMock()
        mock_registry.get_strategy_id_for_symbol.return_value = "u9_omni_zenith_apex"

        rm = RuntimeFsmManager(strategy_registry=mock_registry)
        rm.runtime_dir = self.runtime_dir

        path = rm.get_runtime_path("GRASSUSDT")
        expected_path = self.runtime_dir / "u9_omni_zenith_apex" / "grassusdt.json"
        self.assertEqual(path, expected_path)

        rm.caches["GRASSUSDT"] = {"LONG": {"invest_size": 400.0}}
        asyncio.run(rm.save_cache("GRASSUSDT"))

        # Файл под куполом должен существовать
        self.assertTrue(expected_path.exists())
        # В корне runtime/ файла быть НЕ ДОЛЖНО (никакого дублирования)
        root_file = self.runtime_dir / "grassusdt.json"
        self.assertFalse(root_file.exists(), "В корне runtime/ не должно быть дубликатов файлов монет!")

    def test_template_manager_does_not_mirror_to_root(self):
        """Проверка, что TemplateManager не пишет в корень runtime/ при применении настроек."""
        strat_dir = self.runtime_dir / "u9_omni_zenith_apex"
        strat_dir.mkdir(parents=True, exist_ok=True)
        target_file = strat_dir / "grassusdt.json"
        target_file.write_text(json.dumps({
            "LONG": {"enable": True, "invest_size": 400.0, "leverage": 20, "grid": {"1": {}}, "tp_map": {"1": {}}},
            "SHORT": {"enable": True, "invest_size": 400.0, "leverage": 20, "grid": {"1": {}}, "tp_map": {"1": {}}}
        }), encoding="utf-8")

        tm = TemplateManager()
        tm.runtime_dir = self.runtime_dir
        base_file = self.test_dir / "_base.json"
        base_file.write_text(json.dumps({
            "LONG": {"enable": True, "invest_size": 400.0, "leverage": 20, "grid": {"1": {}}, "tp_map": {"1": {}}},
            "SHORT": {"enable": True, "invest_size": 400.0, "leverage": 20, "grid": {"1": {}}, "tp_map": {"1": {}}}
        }), encoding="utf-8")
        tm.base_file = base_file

        success, msg = tm.apply_tg_template(
            user_json_str=json.dumps({
                "symbol": "GRASSUSDT",
                "strategy_id": "u9_omni_zenith_apex",
                "LONG": {"invest_size": 500.0}
            }),
            strategy_id="u9_omni_zenith_apex"
        )
        self.assertTrue(success, f"apply_tg_template failed: {msg}")

        # Проверяем, что в целевом файле обновилось
        updated_data = json.loads(target_file.read_text(encoding="utf-8"))
        self.assertEqual(updated_data["LONG"]["invest_size"], 500.0)

        # Проверяем, что корень чист
        root_file = self.runtime_dir / "grassusdt.json"
        self.assertFalse(root_file.exists(), "TemplateManager не должен зеркалировать в корень runtime/!")

    def test_reset_strategy_iteration_advances_cycle_and_resets_net(self):
        """Проверка, что reset_strategy_iteration корректно сбрасывает итерацию и переводит на новый цикл."""
        from CORE.bot import BotCore

        test_analytics_dir = self.test_dir / "ANALYTICS"
        test_stats_dir = test_analytics_dir / "STATS"
        test_stats_dir.mkdir(parents=True, exist_ok=True)
        test_cache_dir = self.test_dir / "CACHE"
        test_cache_dir.mkdir(parents=True, exist_ok=True)

        strat_id = "test_strat_isolated"
        initial_stats = {
            "universe_id": strat_id,
            "net_profit_usdt": 15.5,
            "realized_pnl_usdt": 18.0,
            "iteration_index": 2,
            "iteration_start_net_profit_usdt": 5.0,
            "iteration_net_profit_usdt": 10.5
        }
        (test_stats_dir / f"analytics_{strat_id}.json").write_text(json.dumps(initial_stats), encoding="utf-8")

        mock_bot = MagicMock()
        mock_bot.fsm_states = {"TESTUSDT": {"LONG": MagicMock(recent_closes=["close1"]), "SHORT": MagicMock(recent_closes=[])}}
        mock_bot.auto_closer = MagicMock(_last_close_ts={})

        strat_cfg = MagicMock()
        strat_cfg.symbols = ["TESTUSDT"]
        mock_bot.strategy_registry.active_strategies = {strat_id: strat_cfg}

        with patch("CORE.bot.ANALYTICS_DIR", test_analytics_dir), patch("CORE.bot.CACHE_DIR", test_cache_dir):
            res = asyncio.run(BotCore.reset_strategy_iteration(mock_bot, strat_id))

        self.assertIn("new_iteration", res)
        self.assertEqual(res["new_iteration"], 3)
        self.assertEqual(res["prev_iteration"], 2)
        self.assertEqual(res["base_net"], 15.5)
        self.assertEqual(mock_bot.fsm_states["TESTUSDT"]["LONG"].recent_closes, [])
        self.assertIn(strat_id, mock_bot.auto_closer._last_close_ts)

        # Проверяем, что файл в изолированной директории обновился
        saved_stats = json.loads((test_stats_dir / f"analytics_{strat_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_stats["iteration_index"], 3)
        self.assertEqual(saved_stats["iteration_start_net_profit_usdt"], 15.5)
        self.assertEqual(saved_stats["iteration_net_profit_usdt"], 0.0)

        # Проверяем, что iteration_state.json обновился
        saved_cache = json.loads((test_cache_dir / "iteration_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_cache[strat_id]["iteration_index"], 3)
        self.assertEqual(saved_cache[strat_id]["iteration_start_net_profit_usdt"], 15.5)


if __name__ == "__main__":
    unittest.main()
