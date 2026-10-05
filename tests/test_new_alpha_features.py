# ==============================================================================
# Path: tests/test_new_alpha_features.py
# Role: Комплексные юнит- и мок-тесты для LIFO Peeling, AlphaCoinSelector и RadarNavigator (23h window)
# ==============================================================================

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from POS_FSM.models import PositionState
from CORE.GRID.avg_manager import AverageManager
from CORE.SELECTOR.alpha_coin_selector import AlphaCoinSelector
from CORE.radar_navigator import RadarNavigator


class TestNewAlphaFeatures(unittest.IsolatedAsyncioTestCase):
    """Тестирование новых возможностей: LIFO Peeling, Alpha-Селектор и 23-часовое окно ротации."""

    def setUp(self):
        self.mock_bot = MagicMock()
        self.mock_bot.is_running = True
        self.mock_bot.symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"]
        self.mock_bot.delete_symbol = AsyncMock()
        self.mock_bot.add_symbol = AsyncMock()
        self.mock_bot.notifier.send_alert = AsyncMock()
        self.mock_bot.notifier.send_alert_with_keyboard = AsyncMock()

    # --------------------------------------------------------------------------
    # 1. ТЕСТЫ ALPHA COIN SELECTOR
    # --------------------------------------------------------------------------
    def test_alpha_selector_math_metrics(self):
        """Проверка математики: NATR, Wiggle Ratio, Alpha Score."""
        # Синтетические 24 свечи: High, Low, Close
        # Свеча с размахом 2.0 при Close 100 -> TR = 2.0%
        klines = []
        for i in range(24):
            open_p = 100.0 + (i % 2) * 0.5
            close_p = 100.0 + ((i + 1) % 2) * 0.5
            high_p = max(open_p, close_p) + 1.0
            low_p = min(open_p, close_p) - 1.0
            klines.append([0, open_p, high_p, low_p, close_p, 1000.0])

        metrics = AlphaCoinSelector.calculate_coin_metrics("TESTUSDT", klines)
        self.assertIsNotNone(metrics)
        self.assertGreater(metrics["natr"], 0.5)
        self.assertGreater(metrics["wiggle"], 0.5)
        self.assertTrue(metrics["eligible"])
        self.assertGreater(metrics["score"], 0.0)

    def test_alpha_selector_anti_parabolic_and_death_spiral(self):
        """Проверка фильтрации экстремальных пампов (>60%) и дампов (<-45%)."""
        # Памп-монета (+80% за 24 свечи)
        pump_klines = []
        for i in range(24):
            price = 100.0 + (i * 3.5)
            pump_klines.append([0, price, price + 1, price - 1, price, 5000])

        metrics_pump = AlphaCoinSelector.calculate_coin_metrics("PUMPUSDT", pump_klines)
        self.assertFalse(metrics_pump["eligible"])
        self.assertIn("parabolic_exhaustion_pump", metrics_pump["reject_reason"])

        # Дамп-монета (-50% за 24 свечи)
        dump_klines = []
        for i in range(24):
            price = 100.0 - (i * 2.2)
            dump_klines.append([0, price, price + 1, price - 1, price, 5000])

        metrics_dump = AlphaCoinSelector.calculate_coin_metrics("DUMPUSDT", dump_klines)
        self.assertFalse(metrics_dump["eligible"])
        self.assertIn("death_spiral_dump", metrics_dump["reject_reason"])

    def test_alpha_selector_zombie_filter(self):
        """Проверка отсева зомби-монет с затухшей волатильностью (NATR < 0.25%)."""
        flat_klines = []
        for i in range(24):
            flat_klines.append([0, 100.0, 100.05, 99.95, 100.0, 100])

        metrics_flat = AlphaCoinSelector.calculate_coin_metrics("FLATUSDT", flat_klines)
        self.assertFalse(metrics_flat["eligible"])
        self.assertIn("dead_volatility_zombie", metrics_flat["reject_reason"])

    @patch("CORE.SELECTOR.alpha_coin_selector.BinancePublic.get_klines")
    async def test_alpha_selector_select_top_coins(self, mock_get_klines):
        """Мок-тест select_top_coins с ранжированием по Alpha Score."""
        async def fake_klines(sym, interval, limit):
            # C1: идеальная волатильность (размах 10%)
            if sym == "COIN1USDT":
                return [{"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0} for _ in range(24)]
            # C2: чуть меньше размах (размах 6%)
            elif sym == "COIN2USDT":
                return [{"open": 10.0, "high": 10.3, "low": 9.7, "close": 10.0} for _ in range(24)]
            # C3: памп (не eligible)
            elif sym == "COIN3USDT":
                return [{"open": 10.0 + i * 5, "high": 11.0 + i * 5, "low": 9.0 + i * 5, "close": 10.0 + i * 5} for i in range(24)]
            return [{"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0} for _ in range(24)]

        mock_get_klines.side_effect = fake_klines
        custom_universe = ["COIN1USDT", "COIN2USDT", "COIN3USDT"]
        top_coins = await AlphaCoinSelector.select_top_coins(universe=custom_universe, target_count=2)

        self.assertEqual(len(top_coins), 2)
        self.assertEqual(top_coins[0]["symbol"], "COIN1USDT")
        self.assertEqual(top_coins[1]["symbol"], "COIN2USDT")
        self.assertTrue(top_coins[0]["score"] >= top_coins[1]["score"])

    # --------------------------------------------------------------------------
    # 2. ТЕСТЫ LIFO SUBGRID PEELING (AverageManager.process_peel)
    # --------------------------------------------------------------------------
    async def test_lifo_peeling_execution_on_rebound(self):
        """Проверка частичной LIFO-разгрузки верхнего колена на отскоке >= peel_tp_pct."""
        avg_mgr = AverageManager()

        state = PositionState(symbol="TESTUSDT", side="LONG")
        state.in_position = True
        state.total_volume = 20.0
        state.avg_entry_price = 90.0

        # Уровень 3 был исполнен по цене 80.0, объем 10.0, peel_tp_pct = 1.2%
        state.grid = {
            "0": {"is_active": True, "fill_price": 100.0, "fill_qty": 10.0, "peel_tp_pct": None},
            "1": {"is_active": False, "fill_price": None, "fill_qty": None, "peel_tp_pct": None},
            "2": {"is_active": False, "fill_price": None, "fill_qty": None, "peel_tp_pct": None},
            "3": {"is_active": True, "fill_price": 80.0, "fill_qty": 10.0, "peel_tp_pct": 1.2},
        }

        mock_client = AsyncMock()
        mock_client.make_order = AsyncMock(return_value=MagicMock(success=True))
        mock_runtime_mgr = MagicMock()
        mock_tp_manager = MagicMock()
        mock_tp_manager.place_take_profit = AsyncMock()
        spec_data = {"tickSize": "0.01", "stepSize": "0.01"}

        # Отскок: цена выросла с 80.0 до 81.2 (+1.5% >= 1.2%)
        current_price = 81.2
        await avg_mgr.process_peel(mock_client, mock_runtime_mgr, "TESTUSDT", "LONG", state, current_price, spec_data, mock_tp_manager)

        # 1. Проверяем отмену ордеров
        mock_client.cancel_orders_for_side.assert_called_once_with("TESTUSDT", "LONG")
        # 2. Проверяем маркет-сброс объема L3 (10.0)
        mock_client.make_order.assert_called_once()
        call_kwargs = mock_client.make_order.call_args[1]
        self.assertEqual(call_kwargs["symbol"], "TESTUSDT")
        self.assertEqual(call_kwargs["side"], "SELL")  # Закрываем часть LONG
        self.assertEqual(call_kwargs["market_type"], "MARKET")
        self.assertEqual(call_kwargs["qty"], 10.0)

        # 3. Проверяем состояние колена: сброшено
        self.assertFalse(state.grid["3"]["is_active"])
        self.assertIsNone(state.grid["3"]["fill_price"])
        self.assertIsNone(state.grid["3"]["fill_qty"])

        # 4. Проверяем сжатие позиции: сброс объема на 10.0
        self.assertEqual(state.total_volume, 10.0)

        # 5. Проверяем вызов перестановки тейка
        mock_tp_manager.place_take_profit.assert_called_once()

    async def test_lifo_peeling_ignored_when_rebound_insufficient(self):
        """Проверка: если отскок недостаточен (< peel_tp_pct), пилинг не выполняется."""
        avg_mgr = AverageManager()
        state = PositionState(symbol="TESTUSDT", side="LONG")
        state.in_position = True
        state.total_volume = 20.0
        state.avg_entry_price = 90.0
        state.grid = {
            "3": {"is_active": True, "fill_price": 80.0, "fill_qty": 10.0, "peel_tp_pct": 1.2}
        }
        mock_client = AsyncMock()
        mock_runtime_mgr = MagicMock()
        mock_tp_mgr = MagicMock()
        spec_data = {"tickSize": "0.01", "stepSize": "0.01"}

        # Отскок всего +0.5% (цена 80.4 при пороге 1.2% -> 80.96)
        current_price = 80.4
        await avg_mgr.process_peel(mock_client, mock_runtime_mgr, "TESTUSDT", "LONG", state, current_price, spec_data, mock_tp_mgr)

        mock_client.cancel_orders_for_side.assert_not_called()
        mock_client.make_order.assert_not_called()
        self.assertTrue(state.grid["3"]["is_active"])
        self.assertEqual(state.total_volume, 20.0)

    # --------------------------------------------------------------------------
    # 3. ТЕСТЫ RADAR NAVIGATOR (23H ROTATION WINDOW & ALPHA SELECTOR)
    # --------------------------------------------------------------------------
    async def test_radar_navigator_blocks_rotation_before_23_hours(self):
        """Проверка: ротация по храповику блокируется, если с прошлой ротации прошло < 23 часов."""
        cfg = {
            "radar_synergy": {
                "enabled": True,
                "mode": "auto",
                "selector_type": "alpha_coin_selector",
                "min_rotation_interval_hours": 23.0,
                "target_coins_count": 4,
            },
            "paths": {"radar_dir": "../cron3PapperUniverses"},
            "active_strategies": {
                "u6_symbiosis_gamma_fortress35": {
                    "enabled": True,
                    "symbols": ["GRASSUSDT", "ZROUSDT"]
                }
            }
        }

        with patch("CORE.radar_navigator._CFG", cfg):
            nav = RadarNavigator(self.mock_bot)
            strat_id = "u6_symbiosis_gamma_fortress35"

            # 1. Первая ротация в t0
            t0 = time.time()
            nav.last_rotation_ts_by_strat[strat_id] = t0

            # 2. Срабатывание храповика через 10 часов (< 23ч)
            with patch("time.time", return_value=t0 + (10.0 * 3600)):
                nav.alpha_selector.select_top_coins = AsyncMock()
                await nav.on_ratchet_closed(strat_id, "<=2.0%", is_stagnation=True)

                # Селектор не должен вызываться, ротация заблокирована 23-часовым фильтром
                nav.alpha_selector.select_top_coins.assert_not_called()
                self.mock_bot.delete_symbol.assert_not_called()

    async def test_radar_navigator_allows_rotation_after_23_hours(self):
        """Проверка: ротация успешно исполняется, если прошло >= 23 часов."""
        cfg = {
            "radar_synergy": {
                "enabled": True,
                "mode": "auto",
                "selector_type": "alpha_coin_selector",
                "min_rotation_interval_hours": 23.0,
                "target_coins_count": 2,
            },
            "paths": {"radar_dir": "../cron3PapperUniverses"},
            "active_strategies": {
                "u6_symbiosis_gamma_fortress35": {
                    "enabled": True,
                    "symbols": ["OLD1USDT", "OLD2USDT"]
                }
            }
        }

        with patch("CORE.radar_navigator._CFG", cfg):
            nav = RadarNavigator(self.mock_bot)
            strat_id = "u6_symbiosis_gamma_fortress35"

            # Настраиваем реестр стратегий в mock_bot
            mock_strat = MagicMock()
            mock_strat.symbols = ["OLD1USDT", "OLD2USDT"]
            self.mock_bot.strategy_registry.active_strategies = {strat_id: mock_strat}

            t0 = time.time()
            nav.last_rotation_ts_by_strat[strat_id] = t0

            # Имитируем прошествие 24 часов
            t_24h = t0 + (24.0 * 3600)
            mock_candidates = [
                {"symbol": "NEW1USDT", "eligible": True, "score": 95.0, "natr": 3.0, "wiggle": 0.8},
                {"symbol": "NEW2USDT", "eligible": True, "score": 90.0, "natr": 2.8, "wiggle": 0.75},
            ]

            with patch("time.time", return_value=t_24h):
                nav.alpha_selector.select_top_coins = AsyncMock(return_value=mock_candidates)
                await nav.on_ratchet_closed(strat_id, "<=2.0%", is_stagnation=True)

                # Селектор вызван, старые монеты удалены, новые добавлены
                nav.alpha_selector.select_top_coins.assert_called_once()
                self.assertEqual(self.mock_bot.delete_symbol.call_count, 2)
                self.assertEqual(self.mock_bot.add_symbol.call_count, 2)

                # Проверяем аргументы удаления и добавления
                deleted_symbols = [call[0][0] for call in self.mock_bot.delete_symbol.call_args_list]
                added_symbols = [call[0][0] for call in self.mock_bot.add_symbol.call_args_list]
                self.assertEqual(set(deleted_symbols), {"OLD1USDT", "OLD2USDT"})
                self.assertEqual(set(added_symbols), {"NEW1USDT", "NEW2USDT"})

                # Проверяем обновление метки времени последней ротации
                self.assertEqual(nav.last_rotation_ts_by_strat[strat_id], t_24h)


if __name__ == "__main__":
    unittest.main()
