# tests/test_chaos_resilience.py
# -*- coding: utf-8 -*-
"""
Тесты Фазы 5: Стресс-тестирование и Chaos Engineering (Samsonov Protocol).
1. Сетевой сбой и таймаут биржи при усреднении (гарантированное освобождение pending_avg).
2. Повреждение JSON рантайм-файла на диске (отказоустойчивость и лог инцидента).
3. Защита от одновременных гонок и повторных усреднений при активном pending_avg.
4. Автоматическое восстановление сетки FSM из биржевой позиции при пустом/битом рантайме.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from consts import FLOAT_ZERO_EPSILON
from c_utils import Utils
from CORE.GRID.avg_manager import AverageManager
from CORE.GRID.fsm_grid_resolver import FSMGridResolver
from POS_FSM.models import PositionState


class TestChaosResilience(unittest.IsolatedAsyncioTestCase):
    async def test_network_timeout_during_averaging_releases_pending_avg(self):
        """
        Chaos 1: При возникновении сетевого таймаута или ошибки биржи в момент отправки
        ордера усреднения, state.pending_avg ОБЯЗАН быть сброшен в False (finally-блок),
        а уровень сетки откатан из is_active=True в False.
        """
        avg_mgr = AverageManager()

        state = PositionState(
            symbol="BTCUSDT",
            side="LONG",
            total_volume=0.0003,  # 18 USDT (covers L0: 20 USDT * 0.9 = 18 USDT, but does NOT cover L1: 50 USDT)
            avg_entry_price=60000.0,
            initial_entry_price=60000.0,
            eff_invest=100.0,
            in_position=True,
            pending_avg=False,
            grid={
                "0": {"indent": 0.0, "volume": 20.0, "is_active": True, "price": 60000.0},
                "1": {"indent": 2.5, "volume": 30.0, "is_active": False, "price": 58500.0},
            },
        )
        level_data = state.grid["1"]

        # Mock client, бросающий TimeoutError при попытке выставить маркет-ордер
        mock_client = MagicMock()
        mock_client.cancel_orders_for_side = AsyncMock(return_value=None)
        mock_client.make_order = AsyncMock(side_effect=asyncio.TimeoutError("Exchange gateway timeout (429/504)"))

        mock_rm = MagicMock()
        mock_rm.caches = {"BTCUSDT": {"LONG": {"invest_size": 100.0, "eff_invest": 100.0}}}
        mock_rm.strategy_registry = None
        spec_data = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "quantityPrecision": 3,
                    "pricePrecision": 2,
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    ],
                }
            ]
        }
        mock_tp_mgr = MagicMock()

        # Запуск с моделированием сбоя
        with self.assertRaises(asyncio.TimeoutError):
            await avg_mgr.process(
                client=mock_client,
                runtime_manager=mock_rm,
                symbol="BTCUSDT",
                side="LONG",
                state=state,
                opposite_state=PositionState(symbol="BTCUSDT", side="SHORT"),
                current_price=58000.0,
                spec_data=spec_data,
                tp_manager=mock_tp_mgr,
            )

        # Инвариант: pending_avg НЕ ДОЛЖЕН зависнуть в True!
        self.assertFalse(state.pending_avg, "pending_avg обязан быть сброшен в False после сбоя!")
        self.assertFalse(level_data["is_active"], "is_active должен откатиться в False при сбое ордера!")
        self.assertIsNotNone(level_data.get("failed_cooldown_until"), "Должен установиться кулдаун после сбоя ордера!")

    async def test_concurrent_averaging_rejected_when_pending_avg_active(self):
        """
        Chaos 2: При взведенном state.pending_avg=True любой параллельный вызов усреднения
        должен немедленно отвергаться без отправки ордеров на биржу.
        """
        avg_mgr = AverageManager()
        state = PositionState(
            symbol="ETHUSDT",
            side="LONG",
            total_volume=0.5,
            avg_entry_price=3000.0,
            initial_entry_price=3000.0,
            eff_invest=100.0,
            in_position=True,
            pending_avg=True,  # Взведен флаг усреднения
            grid={
                "0": {"indent": 0.0, "volume": 20.0, "is_active": True, "price": 3000.0},
                "1": {"indent": 2.0, "volume": 30.0, "is_active": False, "price": 2940.0},
            },
        )

        mock_client = MagicMock()
        mock_client.make_order = AsyncMock()

        await avg_mgr.process(
            client=mock_client,
            runtime_manager=MagicMock(),
            symbol="ETHUSDT",
            side="LONG",
            state=state,
            opposite_state=PositionState(symbol="ETHUSDT", side="SHORT"),
            current_price=2900.0,
            spec_data={},
            tp_manager=MagicMock(),
        )

        # Ордер не должен вызываться
        mock_client.make_order.assert_not_called()

    def test_corrupted_runtime_file_fallback(self):
        """
        Chaos 3: Повреждение JSON-файла конфигурации на диске.
        Utils.read_json_file должен безопасно возвращать {} без краша процесса.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            corrupted_file = Path(tmp_dir) / "corrupted_runtime.json"
            corrupted_file.write_text("{ broken json content: invalid syntax", encoding="utf-8")

            res = Utils.read_json_file(corrupted_file)
            self.assertEqual(res, {}, "Utils.read_json_file обязан вернуть пустой dict на поврежденный JSON!")

    def test_grid_reconciliation_safeguard_prevents_duplicate_reaccumulation(self):
        """
        Chaos 4: Если на бирже уже накоплен объем позиции, покрывающий планируемые уровни
        сетки (например, после рестарта с пустым рантаймом), FSMGridResolver обязан
        пометить уровни как is_active=True, а AverageManager заблокировать повторный закуп.
        """
        state = PositionState(
            symbol="SOLUSDT",
            side="LONG",
            total_volume=2.0,
            avg_entry_price=150.0,  # Notional = 300 USDT
            eff_invest=400.0,
            in_position=True,
            grid={
                "0": {"indent": 0.0, "volume": 25.0, "is_active": False},  # 100 USDT
                "1": {"indent": 2.0, "volume": 25.0, "is_active": False},  # 100 USDT (cum = 200)
                "2": {"indent": 4.0, "volume": 25.0, "is_active": False},  # 100 USDT (cum = 300)
                "3": {"indent": 6.0, "volume": 25.0, "is_active": False},  # 100 USDT (cum = 400)
            },
        )

        # Запуск универсального резолвера
        FSMGridResolver.resolve_position_state_grid(state, current_price=145.0)

        # L0, L1, L2 должны быть помечены active, т.к. 300 USDT notional покрывает первые 3 уровня (300 >= 300 * 0.9)
        self.assertTrue(state.grid["0"]["is_active"])
        self.assertTrue(state.grid["1"]["is_active"])
        self.assertTrue(state.grid["2"]["is_active"])
        self.assertFalse(state.grid["3"]["is_active"])


if __name__ == "__main__":
    unittest.main()
