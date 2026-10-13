# ============================================================
# FILE: tests/test_analytics_fsm_position_sync.py
# ROLE: Unit test suite for FSM-based position transition detection & symbol-isolated income sync
# ============================================================

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import tempfile
import json
import shutil

from ANALYTICS.analytics import AnalyticsManager
from CORE.STRATEGY.strategy_registry import StrategyConfig


class TestAnalyticsFsmPositionSync(unittest.TestCase):
    """
    Юнит-тесты FSM-автомата отслеживания переходов позиций и изоляции аналитики:
    1. Инициализация FSM-состояния для активных монет бота.
    2. Детекция срезки/закрытия позиции на бирже и автоматический запуск сверки income.
    3. Полный иммунитет к внешним монетам (BTC, ETH) и балансу аккаунта.
    4. Защита от дублирования вызовов через _sync_locks.
    """

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.stats_dir = self.test_dir / "STATS"
        self.ledgers_dir = self.test_dir / "LEDGERS"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self.ledgers_dir.mkdir(parents=True, exist_ok=True)

        # Мок StrategyRegistry со строго определенными монетами бота
        self.mock_registry = MagicMock()
        mock_strat = MagicMock(spec=StrategyConfig)
        mock_strat.strategy_id = "u16_chita__classic"
        mock_strat.name = "U16 Chita"
        mock_strat.start_balance_usdt = 151.0
        mock_strat.analytics_cfg = {"start_balance_usdt": 151.0}
        mock_strat.symbols = ["STRKUSDT", "PUMPUSDT", "METUSDT"]
        self.mock_registry.active_strategies = {"u16_chita__classic": mock_strat}
        self.mock_registry.get_active_strategies.return_value = {"u16_chita__classic": mock_strat}
        self.mock_registry.get_all_active_symbols.return_value = ["STRKUSDT", "PUMPUSDT", "METUSDT"]

        self.analytics = AnalyticsManager(strategy_registry=self.mock_registry)
        self.analytics.stats_dir = self.stats_dir
        self.analytics.ledgers_dir = self.ledgers_dir
        self.analytics.log_file = self.stats_dir / "analytics.json"
        self.analytics.txt_file = self.ledgers_dir / "trades_ledger.txt"
        self.analytics.peels_ledger = self.ledgers_dir / "peels_ledger.txt"
        self.analytics.recycling_ledger = self.ledgers_dir / "recycling_ledger.txt"

        # Начальный JSON
        initial_data = {
            "start_balance_usdt": 151.0,
            "cur_balance_usdt": 151.0,
            "net_profit_usdt": 0.0,
            "realized_pnl_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "first_trade_ts": 1791555700000,
            "per_coin": {}
        }
        self.analytics.log_file.write_text(json.dumps(initial_data, indent=4), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_fsm_initializes_baseline_without_triggering_sync(self):
        """Проверка: первый такт FSM запоминает объемы открытых позиций без запуска sync."""
        mock_client = MagicMock()
        mock_client.fetch_account_info = AsyncMock(return_value=MagicMock(
            success=True,
            data={
                "positions": [
                    {"symbol": "STRKUSDT", "positionSide": "LONG", "positionAmt": "1413.0", "unrealizedProfit": "-10.0"},
                    {"symbol": "PUMPUSDT", "positionSide": "SHORT", "positionAmt": "-5000.0", "unrealizedProfit": "2.5"}
                ],
                "totalMarginBalance": "450.0"
            }
        ))

        self.analytics.record_finished_position = MagicMock()

        import asyncio
        data = self.analytics._read_data()
        asyncio.run(self.analytics._update_drawdowns(mock_client, data, is_lightweight=True))

        # FSM запомнил объемы
        self.assertEqual(self.analytics._symbol_fsm_positions.get(("STRKUSDT", "LONG")), 1413.0)
        self.assertEqual(self.analytics._symbol_fsm_positions.get(("PUMPUSDT", "SHORT")), -5000.0)
        # На первом такте срезки не было — record_finished_position НЕ вызывался
        self.analytics.record_finished_position.assert_not_called()

    def test_02_fsm_detects_position_reduction_and_triggers_income_sync(self):
        """Проверка: при сокращении позиции монеты бота FSM детектирует событие и инициирует income sync."""
        mock_client = MagicMock()
        # Базовое состояние FSM: STRK LONG был 2800.0
        self.analytics._symbol_fsm_positions[("STRKUSDT", "LONG")] = 2800.0

        # Биржа возвращает 1413.0 (срезка объема на 50%)
        mock_client.fetch_account_info = AsyncMock(return_value=MagicMock(
            success=True,
            data={
                "positions": [
                    {"symbol": "STRKUSDT", "positionSide": "LONG", "positionAmt": "1413.0", "unrealizedProfit": "-11.0"}
                ],
                "totalMarginBalance": "450.0"
            }
        ))

        self.analytics.record_finished_position = MagicMock()

        import asyncio
        data = self.analytics._read_data()
        asyncio.run(self.analytics._update_drawdowns(mock_client, data, is_lightweight=True))

        # FSM обновил объем
        self.assertEqual(self.analytics._symbol_fsm_positions.get(("STRKUSDT", "LONG")), 1413.0)
        # record_finished_position вызван для STRKUSDT LONG
        self.analytics.record_finished_position.assert_called_once_with(mock_client, "STRKUSDT", "LONG", 0, unittest.mock.ANY)

    def test_03_external_coins_completely_ignored_by_fsm(self):
        """Проверка: монеты не из пула бота (BTC, ETH) полностью игнорируются FSM и не запускают sync."""
        mock_client = MagicMock()
        # Биржа отдает позицию по BTCUSDT и ETHUSDT, которыми оператор торгует вручную
        mock_client.fetch_account_info = AsyncMock(return_value=MagicMock(
            success=True,
            data={
                "positions": [
                    {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.5", "unrealizedProfit": "+250.0"},
                    {"symbol": "ETHUSDT", "positionSide": "SHORT", "positionAmt": "-3.0", "unrealizedProfit": "-50.0"}
                ],
                "totalMarginBalance": "10000.0"
            }
        ))

        self.analytics.record_finished_position = MagicMock()

        import asyncio
        data = self.analytics._read_data()
        asyncio.run(self.analytics._update_drawdowns(mock_client, data, is_lightweight=True))

        # Внешние монеты не добавлены в FSM активных монет
        self.assertNotIn(("BTCUSDT", "LONG"), self.analytics._symbol_fsm_positions)
        self.assertNotIn(("ETHUSDT", "SHORT"), self.analytics._symbol_fsm_positions)
        self.analytics.record_finished_position.assert_not_called()

    def test_04_sync_lock_prevents_duplicate_reconciliations(self):
        """Проверка: если монета уже находится в _sync_locks, повторный вызов не дублируется."""
        mock_client = MagicMock()
        self.analytics._symbol_fsm_positions[("STRKUSDT", "LONG")] = 2800.0
        self.analytics._sync_locks.add(("STRKUSDT", "LONG"))  # Уже ждет debounce

        mock_client.fetch_account_info = AsyncMock(return_value=MagicMock(
            success=True,
            data={
                "positions": [
                    {"symbol": "STRKUSDT", "positionSide": "LONG", "positionAmt": "1413.0", "unrealizedProfit": "-11.0"}
                ],
                "totalMarginBalance": "450.0"
            }
        ))

        self.analytics.record_finished_position = MagicMock()

        import asyncio
        data = self.analytics._read_data()
        success = asyncio.run(self.analytics._update_drawdowns(mock_client, data, is_lightweight=True))

        # Легковесный цикл безопасно прерван (success=False), чтобы защитить экстремумы
        self.assertFalse(success)
        # Повторный record_finished_position не вызван
        self.analytics.record_finished_position.assert_not_called()


if __name__ == "__main__":
    unittest.main()
