# ==============================================================================
# Path: tests/test_tp_order_reconciliation.py
# Role: Юнит-тесты механизма сверки и авто-восстановления TP ордеров (TakeProfitRestorer)
# ==============================================================================

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from POS_FSM.models import PositionState
from CORE.TP.take_profit_restorer import TakeProfitRestorer
from CORE.bot import BotCore


class TestTakeProfitOrderReconciliation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Создаем легковесный мок ядра BotCore без запуска тяжелых фоновых потоков
        with patch("CORE.bot.StrategyRegistry"), \
             patch("CORE.bot.RuntimeFsmManager"), \
             patch("CORE.bot.BinanceClient"), \
             patch("CORE.bot.CrossProcessHotPriceStream"), \
             patch("CORE.bot.SpecManager"), \
             patch("CORE.bot.LeverageManager"), \
             patch("CORE.bot.AverageManager"), \
             patch("CORE.bot.FallbackTpManager"), \
             patch("CORE.bot.TakeProfitManager"), \
             patch("CORE.bot.AnalyticsManager"), \
             patch("CORE.bot.NotifierManager"), \
             patch("CORE.bot.LoopWatchdog"), \
             patch("CORE.bot.PositionStream"):
            self.bot = BotCore()

        self.bot.symbols = ["STRKUSDT", "METUSDT"]
        self.bot.prices = {
            "STRKUSDT": (0.092, time.time()),
            "METUSDT": (0.425, time.time()),
        }
        self.bot.spec_data = {"symbols": {"STRKUSDT": {}, "METUSDT": {}}}

        # Инициализируем FSM состояния
        self.bot.fsm_states = {
            "STRKUSDT": {
                "LONG": PositionState(symbol="STRKUSDT", side="LONG"),
                "SHORT": PositionState(symbol="STRKUSDT", side="SHORT"),
            },
            "METUSDT": {
                "LONG": PositionState(symbol="METUSDT", side="LONG"),
                "SHORT": PositionState(symbol="METUSDT", side="SHORT"),
            },
        }

        self.bot.tp_manager.place_take_profit = AsyncMock(return_value=True)
        self.bot.client.fetch_open_orders = AsyncMock()
        self.bot.tp_restorer = TakeProfitRestorer(self.bot)

    async def test_reconcile_detects_and_restores_missing_tp(self):
        """Проверяем, что открытая позиция без TP ордера на бирже обнаруживается и TP выставляется."""
        state_strk_l = self.bot.fsm_states["STRKUSDT"]["LONG"]
        state_strk_l.in_position = True
        state_strk_l.total_volume = 421.2
        state_strk_l.avg_entry_price = 0.09275

        # На бирже нет ордеров по STRKUSDT LONG
        self.bot.client.fetch_open_orders.return_value = []

        stats = await self.bot._reconcile_open_orders(force=True)

        # place_take_profit должен быть вызван для STRKUSDT LONG
        self.bot.tp_manager.place_take_profit.assert_called_once()
        args = self.bot.tp_manager.place_take_profit.call_args[0]
        self.assertEqual(args[1], "STRKUSDT")
        self.assertEqual(args[2], "LONG")
        self.assertEqual(args[3], 0.092)
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["restored"], 1)

    async def test_reconcile_skips_when_tp_order_already_exists(self):
        """Проверяем, что если ордер уже есть на бирже с совпадающим объемом, дубликат НЕ выставляется."""
        state_strk_l = self.bot.fsm_states["STRKUSDT"]["LONG"]
        state_strk_l.in_position = True
        state_strk_l.total_volume = 421.2
        state_strk_l.avg_entry_price = 0.09275

        # На бирже уже есть активный ордер для STRKUSDT LONG с точно таким объемом
        self.bot.client.fetch_open_orders.return_value = [
            {"symbol": "STRKUSDT", "positionSide": "LONG", "orderId": 5351237446, "price": "0.09667", "origQty": "421.2"}
        ]

        stats = await self.bot._reconcile_open_orders(force=True)

        self.bot.tp_manager.place_take_profit.assert_not_called()
        self.assertEqual(stats["confirmed"], 1)
        self.assertEqual(stats["restored"], 0)

    async def test_reconcile_detects_volume_mismatch(self):
        """Проверяем, что если объем TP ордера не совпадает с позицией (например, после добора), TP обновляется."""
        state_strk_l = self.bot.fsm_states["STRKUSDT"]["LONG"]
        state_strk_l.in_position = True
        state_strk_l.total_volume = 800.0  # увеличили позицию
        state_strk_l.avg_entry_price = 0.09100

        # Старый ордер остался на 421.2
        self.bot.client.fetch_open_orders.return_value = [
            {"symbol": "STRKUSDT", "positionSide": "LONG", "orderId": 5351237446, "price": "0.09667", "origQty": "421.2"}
        ]

        stats = await self.bot._reconcile_open_orders(force=True)

        self.bot.tp_manager.place_take_profit.assert_called_once()
        self.assertEqual(stats["mismatch"], 1)
        self.assertEqual(stats["restored"], 1)

    async def test_reconcile_skips_flat_positions(self):
        """Проверяем, что по закрытым позициям (in_position=False или vol=0) TP не ставится."""
        state_strk_l = self.bot.fsm_states["STRKUSDT"]["LONG"]
        state_strk_l.in_position = False
        state_strk_l.total_volume = 0.0

        self.bot.client.fetch_open_orders.return_value = []

        stats = await self.bot._reconcile_open_orders(force=True)
        self.bot.tp_manager.place_take_profit.assert_not_called()
        self.assertEqual(stats["checked"], 0)

    async def test_reconcile_debounce_interval(self):
        """Проверяем debouncing: без force повторный вызов быстрее 15 секунд пропускается."""
        state_strk_l = self.bot.fsm_states["STRKUSDT"]["LONG"]
        state_strk_l.in_position = True
        state_strk_l.total_volume = 421.2

        self.bot.client.fetch_open_orders.return_value = []

        # Первый вызов (без force)
        await self.bot._reconcile_open_orders(force=False)
        self.assertEqual(self.bot.tp_manager.place_take_profit.call_count, 1)

        # Немедленный второй вызов должен быть отсечен debouncer'ом
        await self.bot._reconcile_open_orders(force=False)
        self.assertEqual(self.bot.tp_manager.place_take_profit.call_count, 1)

    async def test_reconcile_exception_handled_gracefully(self):
        """Проверяем, что сетевые сбои при получении ордеров не крашат цикл бота."""
        self.bot.client.fetch_open_orders.side_effect = RuntimeError("Binance API 502 Bad Gateway")

        try:
            stats = await self.bot._reconcile_open_orders(force=True)
            self.assertEqual(stats["restored"], 0)
        except Exception as e:
            self.fail(f"_reconcile_open_orders raised an unhandled exception: {e}")


if __name__ == "__main__":
    unittest.main()
