# tests/test_per_coin_stop_loss.py
# -*- coding: utf-8 -*-
"""
==============================================================================
MODULE: tests.test_per_coin_stop_loss
ROLE: Комплексные юнит-тесты для PerCoinStopLossManager:
      - Нормализация порогов (поддержка float, int, отрицательных чисел, None)
      - Расчет неблагоприятного отклонения цены (adverse move %) от цены L0
      - Рыночная ликвидация позиции (MARKET) и отмена ордеров сетки
      - Сброс FSM состояния и вызов delete_symbol (очистка app.json и диска)
      - Формирование и отправка Telegram-уведомления оператору
==============================================================================
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.SERVICES.per_coin_stop_loss import PerCoinStopLossManager
from POS_FSM.models import PositionState


class TestPerCoinStopLoss(unittest.IsolatedAsyncioTestCase):

    def test_normalize_threshold(self):
        """Проверка надежной нормализации пользовательского ввода порога."""
        self.assertIsNone(PerCoinStopLossManager.normalize_threshold(None))
        self.assertIsNone(PerCoinStopLossManager.normalize_threshold(False))
        self.assertIsNone(PerCoinStopLossManager.normalize_threshold(0))
        self.assertIsNone(PerCoinStopLossManager.normalize_threshold(0.0))
        self.assertIsNone(PerCoinStopLossManager.normalize_threshold("invalid"))

        # Положительные числа
        self.assertEqual(PerCoinStopLossManager.normalize_threshold(100), 100.0)
        self.assertEqual(PerCoinStopLossManager.normalize_threshold(100.0), 100.0)
        self.assertEqual(PerCoinStopLossManager.normalize_threshold("100.0"), 100.0)

        # Отрицательные числа (нормализация через abs)
        self.assertEqual(PerCoinStopLossManager.normalize_threshold(-100), 100.0)
        self.assertEqual(PerCoinStopLossManager.normalize_threshold(-50.5), 50.5)

    def test_extract_config(self):
        """Проверка извлечения конфигурации из объекта стратегии или словаря."""
        strat_dict = {
            "per_coin_stop_loss": {
                "max_adverse_from_entry_pct": 100.0,
                "notify_telegram": True,
            }
        }
        short_th, long_th, notify = PerCoinStopLossManager.extract_config(strat_dict)
        self.assertEqual(short_th, 100.0)
        self.assertIsNone(long_th)  # 100.0 >= 100 -> long disabled
        self.assertTrue(notify)

        # Раздельные пороги для SHORT и LONG
        strat_asym = {
            "per_coin_stop_loss": {
                "max_adverse_short_pct": 120.0,
                "max_adverse_long_pct": 85.0,
                "notify_telegram": True,
            }
        }
        s_th, l_th, n = PerCoinStopLossManager.extract_config(strat_asym)
        self.assertEqual(s_th, 120.0)
        self.assertEqual(l_th, 85.0)
        self.assertTrue(n)

        # Отключенный стоп-лосс
        strat_disabled = {
            "per_coin_stop_loss": {
                "max_adverse_from_entry_pct": None,
                "notify_telegram": False,
            }
        }
        thresh2_s, thresh2_l, notify2 = PerCoinStopLossManager.extract_config(strat_disabled)
        self.assertIsNone(thresh2_s)
        self.assertIsNone(thresh2_l)
        self.assertFalse(notify2)

    def test_evaluate_symbol_long(self):
        """Проверка расчета отклонения для позиции LONG."""
        st = PositionState(symbol="BTCUSDT", side="LONG")
        st.in_position = True
        st.initial_entry_price = 100.0

        # Цена упала на 10% (100 -> 90) -> не превышает порог 100%
        trig, adv, entry = PerCoinStopLossManager.evaluate_symbol("BTCUSDT", st, 90.0, 100.0)
        self.assertFalse(trig)
        self.assertAlmostEqual(adv, 10.0)
        self.assertEqual(entry, 100.0)

        # Цена упала до 0 (100 -> 0) -> достигла порога 100%
        trig, adv, entry = PerCoinStopLossManager.evaluate_symbol("BTCUSDT", st, 0.01, 99.9)
        self.assertTrue(trig)
        self.assertAlmostEqual(adv, 99.99)

        # Цена выросла (прибыль) -> adverse < 0
        trig, adv, entry = PerCoinStopLossManager.evaluate_symbol("BTCUSDT", st, 120.0, 100.0)
        self.assertFalse(trig)
        self.assertAlmostEqual(adv, -20.0)

    def test_evaluate_symbol_short(self):
        """Проверка расчета отклонения для позиции SHORT (наиболее критичный сценарий)."""
        st = PositionState(symbol="MUSDT", side="SHORT")
        st.in_position = True
        st.initial_entry_price = 0.50

        # Цена выросла на 50% (0.50 -> 0.75) -> не достигла порога 100%
        trig, adv, entry = PerCoinStopLossManager.evaluate_symbol("MUSDT", st, 0.75, 100.0)
        self.assertFalse(trig)
        self.assertAlmostEqual(adv, 50.0)

        # Цена удвоилась на 100% (0.50 -> 1.00) -> ТРИГГЕР СТОП-ЛОССА
        trig, adv, entry = PerCoinStopLossManager.evaluate_symbol("MUSDT", st, 1.00, 100.0)
        self.assertTrue(trig)
        self.assertAlmostEqual(adv, 100.0)

        # Цена улетела в космос на +237% (0.50 -> 1.685) -> ТРИГГЕР
        trig, adv, entry = PerCoinStopLossManager.evaluate_symbol("MUSDT", st, 1.685, 100.0)
        self.assertTrue(trig)
        self.assertAlmostEqual(adv, 237.0)

    def test_evaluate_symbol_fallback_to_avg_price(self):
        """Проверка корректного fallback на avg_entry_price, если initial_entry_price не задан."""
        st = PositionState(symbol="SOLUSDT", side="SHORT")
        st.in_position = True
        st.initial_entry_price = 0.0
        st.avg_entry_price = 200.0

        trig, adv, entry = PerCoinStopLossManager.evaluate_symbol("SOLUSDT", st, 400.0, 100.0)
        self.assertTrue(trig)
        self.assertEqual(entry, 200.0)
        self.assertAlmostEqual(adv, 100.0)

    async def test_execute_stop_loss_flow(self):
        """Проверка полного сквозного цикла исполнения стоп-лосса монеты."""
        mock_bot = MagicMock()
        mock_client = AsyncMock()
        mock_bot.get_client_for_symbol.return_value = mock_client
        mock_bot.client = mock_client

        mock_client.cancel_all_orders = AsyncMock()
        mock_client.fetch_positions = AsyncMock(return_value=[
            {"symbol": "MUSDT", "positionAmt": "-500.0", "positionSide": "SHORT"}
        ])
        mock_client.make_order = AsyncMock()

        st_short = PositionState(symbol="MUSDT", side="SHORT")
        st_short.in_position = True
        st_short.total_volume = -500.0
        st_short.initial_entry_price = 0.50
        mock_bot.fsm_states = {"MUSDT": {"SHORT": st_short, "LONG": PositionState(symbol="MUSDT", side="LONG")}}

        mock_bot.runtime_manager = MagicMock()
        mock_bot.avg_manager = MagicMock()
        mock_bot.delete_symbol = AsyncMock(return_value=True)
        mock_bot.notifier = MagicMock()
        mock_bot.notifier.send_alert = AsyncMock()

        res = await PerCoinStopLossManager.execute_stop_loss(
            bot_core=mock_bot,
            symbol="MUSDT",
            side="SHORT",
            adverse_pct=105.0,
            threshold_pct=100.0,
            entry0_price=0.50,
            cur_price=1.025,
            strat_id="u96_omni_zenith13",
            strat_name="Omen 13",
            notify_telegram=True
        )

        self.assertTrue(res)
        # 1. Проверяем отмену всех ордеров
        mock_client.cancel_all_orders.assert_called_once_with("MUSDT")
        # 2. Проверяем рыночное закрытие позиции (MARKET BUY для закрытия SHORT)
        mock_client.make_order.assert_called_once_with(
            symbol="MUSDT",
            qty=500.0,
            side="BUY",
            position_side="SHORT",
            market_type="MARKET"
        )
        # 3. Проверяем сброс FSM и AvgManager
        mock_bot.runtime_manager.reset_state_to_default.assert_called()
        mock_bot.avg_manager.reset.assert_called()
        # 4. Проверяем вызов delete_symbol
        mock_bot.delete_symbol.assert_called_once_with("MUSDT")
        # 5. Проверяем отправку Telegram-уведомления
        mock_bot.notifier.send_alert.assert_called_once()
        alert_text = mock_bot.notifier.send_alert.call_args[0][0]
        self.assertIn("STOP-LOSS PER COIN", alert_text)
        self.assertIn("MUSDT", alert_text)
        self.assertIn("+105.00%", alert_text)

    async def test_check_per_coin_stop_loss_integration(self):
        """Проверка метода сканирования check_per_coin_stop_loss."""
        mock_bot = MagicMock()
        mock_client = AsyncMock()
        mock_bot.get_client_for_symbol.return_value = mock_client
        mock_bot.client = mock_client
        mock_client.cancel_all_orders = AsyncMock()
        mock_client.fetch_positions = AsyncMock(return_value=[
            {"symbol": "PUMPUSDT", "positionAmt": "-100.0", "positionSide": "SHORT"}
        ])
        mock_client.make_order = AsyncMock()
        mock_bot.delete_symbol = AsyncMock(return_value=True)
        mock_bot.notifier = MagicMock()
        mock_bot.notifier.send_alert = AsyncMock()

        # Монета PUMPUSDT превысила 100%, монета SAFEUSDT осталась в норме
        mock_bot.prices = {
            "PUMPUSDT": (2.10, 1000.0),  # L0 = 1.0 -> adverse = +110%
            "SAFEUSDT": (1.05, 1000.0),  # L0 = 1.0 -> adverse = +5%
        }

        st_pump = PositionState(symbol="PUMPUSDT", side="SHORT")
        st_pump.in_position = True
        st_pump.initial_entry_price = 1.0
        st_pump.total_volume = -100.0

        st_safe = PositionState(symbol="SAFEUSDT", side="SHORT")
        st_safe.in_position = True
        st_safe.initial_entry_price = 1.0
        st_safe.total_volume = -50.0

        mock_bot.fsm_states = {
            "PUMPUSDT": {"SHORT": st_pump, "LONG": PositionState()},
            "SAFEUSDT": {"SHORT": st_safe, "LONG": PositionState()},
        }

        strat_cfg = MagicMock()
        strat_cfg.symbols = ["PUMPUSDT", "SAFEUSDT"]
        strat_cfg.name = "Test Strategy"
        strat_cfg.per_coin_stop_loss_cfg = {
            "max_adverse_from_entry_pct": 100.0,
            "notify_telegram": True,
        }

        mock_bot.strategy_registry = MagicMock()
        mock_bot.strategy_registry.active_strategies = {"test_strat": strat_cfg}

        liquidated = await PerCoinStopLossManager.check_per_coin_stop_loss(mock_bot)

        # Ликвидирован ТОЛЬКО PUMPUSDT
        self.assertEqual(liquidated, ["PUMPUSDT"])
        mock_bot.delete_symbol.assert_called_once_with("PUMPUSDT")


if __name__ == "__main__":
    unittest.main()

