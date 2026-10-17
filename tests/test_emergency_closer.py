# tests/test_emergency_closer.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: tests/test_emergency_closer.py
Role: Институциональный юнит-тест сервиса экстренного закрытия позиций (EmergencyCloserService)
==============================================================================
"""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.SERVICES.emergency_closer import EmergencyCloserService
from POS_FSM.models import PositionState


class TestEmergencyCloserService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_emerg_closer_"))
        self.analytics_dir = self.temp_dir / "ANALYTICS"
        self.stats_dir = self.analytics_dir / "STATS"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.temp_dir / "CACHE"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.mock_bot = MagicMock()
        self.mock_bot.symbols = ["BTCUSDT", "ETHUSDT"]
        self.mock_bot._closing_symbols = set()
        self.mock_bot._closing_strategies = set()
        self.mock_bot._close_lock = asyncio.Lock()
        self.mock_bot._get_symbol_lock = lambda sym: asyncio.Lock()
        self.mock_bot.get_strategy_heartbeat_seconds = MagicMock(return_value=300)
        self.mock_bot.calc_heartbeat_window_cooldown = MagicMock(return_value=1800000305.0)

        btc_long = PositionState("BTCUSDT", "LONG")
        btc_long.in_position = True
        btc_long.total_volume = 0.5
        btc_short = PositionState("BTCUSDT", "SHORT")

        eth_long = PositionState("ETHUSDT", "LONG")
        eth_short = PositionState("ETHUSDT", "SHORT")

        self.mock_bot.fsm_states = {
            "BTCUSDT": {"LONG": btc_long, "SHORT": btc_short},
            "ETHUSDT": {"LONG": eth_long, "SHORT": eth_short},
        }

        positions_first = [
            {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.5"},
            {"symbol": "ETHUSDT", "positionSide": "SHORT", "positionAmt": "-2.0"},
            {"symbol": "SOLUSDT", "positionSide": "LONG", "positionAmt": "10.0"},  # монета вне обоймы
        ]
        self.mock_bot.client = MagicMock()
        self.mock_bot.client.fetch_positions = AsyncMock(side_effect=[positions_first, []])
        self.mock_bot.client.cancel_all_orders = AsyncMock()
        self.mock_bot.client.make_order = AsyncMock(return_value=MagicMock(success=True))

        self.mock_bot.get_client_for_symbol = MagicMock(return_value=self.mock_bot.client)
        self.mock_bot.get_client_for_strategy = MagicMock(return_value=self.mock_bot.client)

        self.mock_bot.runtime_manager = MagicMock()
        self.mock_bot.runtime_manager.reset_state_to_default = MagicMock()
        self.mock_bot.runtime_manager.save_cache = AsyncMock()

        self.mock_bot.avg_manager = MagicMock()
        self.mock_bot.avg_manager.reset = MagicMock()

        self.mock_bot.analytics = MagicMock()
        self.mock_bot.analytics.reset_global_iteration = MagicMock()
        self.mock_bot.analytics.leaderboard_engine = MagicMock()
        self.mock_bot.analytics.leaderboard_engine.generate_leaderboard.return_value = [{"dome": "u16"}]

        strat_cfg = MagicMock()
        strat_cfg.symbols = ["BTCUSDT", "ETHUSDT"]
        strat_cfg.name = "Chita Classic"
        strat_cfg.raw_meta = {}
        self.mock_bot.strategy_registry = MagicMock()
        self.mock_bot.strategy_registry.active_strategies = {"u16_chita__classic": strat_cfg}
        self.mock_bot.strategy_registry.get_all_active_symbols.return_value = ["BTCUSDT", "ETHUSDT"]

        self.mock_bot.reset_strategy_iteration = AsyncMock(return_value={"strategy_id": "u16_chita__classic", "new_iteration": 2})

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_calc_cooldown_bounds(self):
        """Проверка формулы кулдауна до границы свечи с буфером."""
        now = 1000.0
        heartbeat_sec = 300
        cd = EmergencyCloserService.calc_cooldown(now, heartbeat_sec)
        # Ближайшая граница 300s: 1200 + COOLDOWN_POST_CANDLE_BUFFER_SEC (5) = 1205.0
        self.assertGreater(cd, now)
        self.assertEqual(cd, 1205.0)

    def test_close_all_empty_symbols(self):
        """close_all с пустым списком символов возвращает пустой словарь."""
        self.mock_bot.symbols = []
        self.mock_bot.strategy_registry.get_all_active_symbols.return_value = []
        res = asyncio.run(EmergencyCloserService.close_all(self.mock_bot))
        self.assertEqual(res, {})

    def test_close_all_full_lifecycle(self):
        """close_all отменяет ордера, закрывает открытые позиции обоймы, сбрасывает FSM и запускает сброс итераций."""
        res = asyncio.run(EmergencyCloserService.close_all(self.mock_bot, reset_iterations=True))

        # 1. Проверка установки кулдауна
        self.assertTrue(self.mock_bot.entry_blocked)
        self.assertEqual(self.mock_bot.block_until_timestamp, 1800000305.0)
        self.assertEqual(self.mock_bot.symbol_blocked_until["BTCUSDT"], 1800000305.0)
        self.assertEqual(self.mock_bot.symbol_blocked_until["ETHUSDT"], 1800000305.0)

        # 2. Проверка отмены ордеров
        self.assertEqual(self.mock_bot.client.cancel_all_orders.await_count, 2)

        # 3. Проверка рыночных ордеров закрытия (BTCUSDT SELL и ETHUSDT BUY)
        self.assertGreaterEqual(self.mock_bot.client.make_order.await_count, 2)
        called_symbols = {c.kwargs.get("symbol") for c in self.mock_bot.client.make_order.await_args_list}
        self.assertIn("BTCUSDT", called_symbols)
        self.assertIn("ETHUSDT", called_symbols)
        self.assertNotIn("SOLUSDT", called_symbols)  # SOLUSDT не в обойме, не должен закрываться!

        # 4. Проверка сброса FSM и avg_manager
        self.assertGreaterEqual(self.mock_bot.runtime_manager.reset_state_to_default.call_count, 4)
        self.assertGreaterEqual(self.mock_bot.avg_manager.reset.call_count, 4)
        self.assertEqual(self.mock_bot.runtime_manager.save_cache.await_count, 2)

        # 5. Проверка сброса итерации стратегии и лидерборда
        self.mock_bot.reset_strategy_iteration.assert_called_once_with("u16_chita__classic")
        self.mock_bot.analytics.reset_global_iteration.assert_called_once()
        self.mock_bot.analytics.leaderboard_engine.save_portfolio.assert_called_once()

    def test_close_strategy_unknown_strategy(self):
        """close_strategy для несуществующей стратегии выходит без исключений."""
        res = asyncio.run(EmergencyCloserService.close_strategy(self.mock_bot, "non_existent_strategy"))
        self.assertIsNone(res)
        self.mock_bot.client.cancel_all_orders.assert_not_called()

    def test_close_strategy_success(self):
        """close_strategy изолированно закрывает позиции строго для монет указанной стратегии."""
        res = asyncio.run(EmergencyCloserService.close_strategy(self.mock_bot, "u16_chita__classic"))
        self.assertIsNone(res)

        # Проверка отмены ордеров для монет стратегии
        self.assertEqual(self.mock_bot.client.cancel_all_orders.await_count, 2)
        # Проверка отправки рыночных ордеров
        self.assertGreaterEqual(self.mock_bot.client.make_order.await_count, 2)
        # Проверка очистки блокировок
        self.assertNotIn("u16_chita__classic", self.mock_bot._closing_strategies)
        self.assertNotIn("BTCUSDT", self.mock_bot._closing_symbols)

    def test_reset_strategy_iteration_direct(self):
        """Прямой юнит-тест reset_strategy_iteration в изолированном окружении."""
        strat_id = "u16_chita__classic"
        initial_stats = {
            "universe_id": strat_id,
            "net_profit_usdt": 24.5,
            "realized_pnl_usdt": 30.0,
            "iteration_index": 5,
        }
        (self.stats_dir / f"analytics_{strat_id}.json").write_text(json.dumps(initial_stats), encoding="utf-8")

        mock_bot = MagicMock()
        mock_bot.analytics_dir = self.analytics_dir
        mock_bot.cache_dir = self.cache_dir
        mock_bot.fsm_states = {"BTCUSDT": {"LONG": MagicMock(recent_closes=["t1"]), "SHORT": MagicMock(recent_closes=[])}}
        mock_bot.auto_closer = MagicMock(_last_close_ts={})

        strat_cfg = MagicMock()
        strat_cfg.symbols = ["BTCUSDT"]
        strat_cfg.signal_heartbeat_tfr = "5m"
        mock_bot.strategy_registry.active_strategies = {strat_id: strat_cfg}
        mock_bot.strategy_registry.get_all_active_symbols.return_value = ["BTCUSDT"]
        mock_bot.get_strategy_heartbeat_seconds = MagicMock(return_value=300)
        mock_bot.calc_heartbeat_window_cooldown = MagicMock(return_value=1900000000.0)

        with patch("CORE.SERVICES.emergency_closer.ANALYTICS_DIR", self.analytics_dir), \
             patch("CORE.SERVICES.emergency_closer.CACHE_DIR", self.cache_dir):
            res = asyncio.run(EmergencyCloserService.reset_strategy_iteration(mock_bot, strat_id))

        self.assertEqual(res["new_iteration"], 6)
        self.assertEqual(res["prev_iteration"], 5)
        self.assertEqual(res["base_net"], 24.5)
        self.assertEqual(mock_bot.fsm_states["BTCUSDT"]["LONG"].recent_closes, [])
        self.assertTrue(mock_bot.entry_blocked)
        self.assertEqual(mock_bot.block_until_timestamp, 1900000000.0)


if __name__ == "__main__":
    unittest.main()
