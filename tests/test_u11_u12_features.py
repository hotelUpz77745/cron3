# ==============================================================================
# Path: tests/test_u11_u12_features.py
# Role: Unit and integration tests for U11 Dynamic ATR TP and U12 Cross-Side Profit Recycling
# ==============================================================================

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.ADVANCED.atr_provider import ATRProvider
from CORE.TP.dynamic_atr_tp_manager import DynamicATRTakeProfitManager
from CORE.HEDGE.cross_recycler import CrossSideProfitRecycler
from POS_FSM.models import PositionState
from CORE.TP.tp_manager import TakeProfitManager


class TestU11DynamicATRTP(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ATRProvider.clear_cache()

    async def test_atr_provider_calculation_and_caching(self):
        """Проверка корректности расчета ATR% и работы кеша по TTL."""
        mock_klines = []
        base_p = 100.0
        for i in range(16):
            mock_klines.append({
                "open_time": i * 900000,
                "open": base_p,
                "high": base_p + 2.0,
                "low": base_p - 1.0,
                "close": base_p + 1.0,
                "volume": 1000.0,
            })
            base_p += 1.0

        with patch("API.BINANCE.public.BinancePublic.get_klines", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_klines

            atr_pct = await ATRProvider.get_atr_pct(
                client=None, symbol="TESTUSDT", timeframe="1h", period=14, cache_ttl_sec=60, fallback_atr_pct=1.2
            )
            self.assertGreater(atr_pct, 0.0)
            mock_get.assert_called_once()

            mock_get.reset_mock()
            atr_cached = await ATRProvider.get_atr_pct(
                client=None, symbol="TESTUSDT", timeframe="1h", period=14, cache_ttl_sec=60, fallback_atr_pct=1.2
            )
            self.assertEqual(atr_pct, atr_cached)
            mock_get.assert_not_called()

    async def test_atr_provider_fallback_on_empty(self):
        """Проверка: если API недоступен, возвращается строго fallback_atr_pct из конфига."""
        with patch("API.BINANCE.public.BinancePublic.get_klines", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            atr_pct = await ATRProvider.get_atr_pct(
                client=None, symbol="FALLBACKUSDT", timeframe="1h", period=14, cache_ttl_sec=60, fallback_atr_pct=1.85
            )
            self.assertEqual(atr_pct, 1.85)

    async def test_dynamic_atr_tp_deescalation_and_floors(self):
        """Проверка деэскалации множителей и ограничений floor/ceiling."""
        cfg = {
            "enabled": True,
            "timeframe": "1h",
            "period": 14,
            "cache_ttl_sec": 120,
            "min_guaranteed_floor_pct": 0.65,
            "max_ceiling_pct": 4.5,
            "fallback_atr_pct": 1.2,
            "level_multipliers": {
                "0": 0.75,
                "1": 0.60,
                "2": 0.50,
                "3": 0.40,
                "4": 0.30,
                "8": 0.10,
            }
        }
        spec = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}

        with patch.object(ATRProvider, "get_atr_pct", new_callable=AsyncMock) as mock_atr:
            mock_atr.return_value = 2.0

            tp_price, tp_pct = await DynamicATRTakeProfitManager.calculate_tp_price(
                client=None, symbol="BTCUSDT", side="LONG", avg_entry_price=100.0,
                active_level="0", cfg=cfg, spec_data=spec
            )
            self.assertEqual(tp_pct, 1.50)
            self.assertEqual(tp_price, 101.50)

            tp_short, _ = await DynamicATRTakeProfitManager.calculate_tp_price(
                client=None, symbol="BTCUSDT", side="SHORT", avg_entry_price=100.0,
                active_level="0", cfg=cfg, spec_data=spec
            )
            self.assertEqual(tp_short, 98.50)

            tp_price_l8, tp_pct_l8 = await DynamicATRTakeProfitManager.calculate_tp_price(
                client=None, symbol="BTCUSDT", side="LONG", avg_entry_price=100.0,
                active_level="8", cfg=cfg, spec_data=spec
            )
            self.assertEqual(tp_pct_l8, 0.65)
            self.assertEqual(tp_price_l8, 100.65)

            mock_atr.return_value = 10.0
            tp_price_ceil, tp_pct_ceil = await DynamicATRTakeProfitManager.calculate_tp_price(
                client=None, symbol="BTCUSDT", side="LONG", avg_entry_price=100.0,
                active_level="0", cfg=cfg, spec_data=spec
            )
            self.assertEqual(tp_pct_ceil, 4.5)
            self.assertEqual(tp_price_ceil, 104.5)

    async def test_dynamic_atr_tp_integration_in_tp_manager(self):
        """Проверка: TakeProfitManager применяет динамический ATR тейк при постановке лимитного ордера."""
        runtime_mgr = MagicMock()
        mock_strat = MagicMock()
        mock_strat.dynamic_atr_tp_cfg = {
            "enabled": True,
            "timeframe": "1h",
            "period": 14,
            "cache_ttl_sec": 120,
            "min_guaranteed_floor_pct": 0.65,
            "max_ceiling_pct": 4.5,
            "fallback_atr_pct": 1.2,
            "level_multipliers": {"0": 0.75}
        }
        mock_strat.smart_tp_cfg = None
        runtime_mgr.strategy_registry.get_strategy_for_symbol.return_value = mock_strat

        tp_mgr = TakeProfitManager(runtime_mgr)
        client = MagicMock()
        client.cancel_orders_for_side = AsyncMock()
        client.make_order = AsyncMock(return_value=MagicMock(success=True, order_id=999))

        state = PositionState("BTCUSDT", "LONG")
        state.set_in_position(True)
        state.total_volume = 1.0
        state.avg_entry_price = 100.0
        state.grid = {"0": {"is_active": True}}
        state.tp_map = {"0": {"indent": 2.0, "fallback_indent": 2.5}}
        spec = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}

        with patch.object(ATRProvider, "get_atr_pct", new_callable=AsyncMock) as mock_atr:
            mock_atr.return_value = 2.0  # 2.0 * 0.75 = 1.5% TP indent

            res = await tp_mgr.place_take_profit(
                client=client,
                symbol="BTCUSDT",
                side="LONG",
                current_price=100.0,
                spec_data=spec,
                state=state
            )
            self.assertTrue(res)
            client.make_order.assert_called_once()
            call_kwargs = client.make_order.call_args[1]
            self.assertEqual(call_kwargs["price"], 101.50)
            self.assertEqual(call_kwargs["market_type"], "LIMIT")

    async def test_dynamic_atr_tp_missing_key_raises_key_error(self):
        """Проверка строгого протокола: отсутствие обязательного ключа вызывает немедленный KeyError."""
        incomplete_cfg = {
            "enabled": True,
            # намеренно отсутствует timeframe
            "period": 14,
            "cache_ttl_sec": 120,
            "min_guaranteed_floor_pct": 0.65,
            "max_ceiling_pct": 4.5,
            "fallback_atr_pct": 1.2,
            "level_multipliers": {"0": 0.75}
        }
        spec = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}
        with self.assertRaises(KeyError):
            await DynamicATRTakeProfitManager.calculate_tp_price(
                client=None, symbol="BTCUSDT", side="LONG", avg_entry_price=100.0,
                active_level="0", cfg=incomplete_cfg, spec_data=spec
            )

    async def test_strategy_config_startup_validation(self):
        """Проверка: StrategyConfig при запуске падает с KeyError если не задан обязательный параметр."""
        from CORE.STRATEGY.strategy_registry import StrategyConfig
        broken_raw = {
            "name": "Broken Strategy",
            "dynamic_atr_tp": {
                "enabled": True,
                # отсутствует fallback_atr_pct
                "timeframe": "1h",
                "period": 14,
                "cache_ttl_sec": 120,
                "min_guaranteed_floor_pct": 0.65,
                "max_ceiling_pct": 4.5,
                "level_multipliers": {"0": 0.75}
            }
        }
        with self.assertRaises(KeyError):
            StrategyConfig("u_broken", broken_raw, ["BTCUSDT"])


class TestU12CrossSideProfitRecycling(unittest.IsolatedAsyncioTestCase):
    async def test_cross_side_recycler_guard_and_execution(self):
        """Проверка условий срабатывания и математики срезки Cross-Side Recycling."""
        u = MagicMock()
        u.universe_id = "u12_test"
        u.mark_dirty = MagicMock()
        u.client = MagicMock()
        u.client.make_order = AsyncMock(return_value=MagicMock(success=True))
        u.cross_recycling_cfg = {
            "enabled": True,
            "recycle_ratio": 0.50,
            "min_burn_level": 2,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 25.0
        }

        spec = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}
        short_state = PositionState("TESTUSDT", "SHORT")
        short_state.set_in_position(True)
        short_state.avg_entry_price = 100.0
        short_state.total_volume = 10.0
        short_state.grid = {
            "0": {"is_active": True},
            "1": {"is_active": True},
            "2": {"is_active": True},
            "3": {"is_active": True}
        }

        u.fsm_states = {"TESTUSDT": {"SHORT": short_state}}

        res = await CrossSideProfitRecycler.maybe_recycle(
            universe=u,
            symbol="TESTUSDT",
            winning_side="LONG",
            realized_profit=20.0,
            current_price=105.0,
            spec_data=spec
        )

        self.assertIsNotNone(res)
        self.assertEqual(res["burned_qty"], 2.0)
        self.assertEqual(res["burn_loss"], 10.0)
        self.assertEqual(short_state.total_volume, 8.0)
        u.client.make_order.assert_called_once()
        call_args = u.client.make_order.call_args[1]
        self.assertEqual(call_args["side"], "BUY")
        self.assertEqual(call_args["qty"], 2.0)
        self.assertEqual(call_args["position_side"], "SHORT")

    async def test_cross_side_recycler_bot_core_resolution(self):
        """Проверка: CrossSideProfitRecycler разрешает стратегию из BotCore runtime_manager."""
        bot = MagicMock()
        del bot.cross_recycling_cfg  # BotCore не имеет прямого cross_recycling_cfg
        del bot.universe_id         # BotCore не имеет universe_id

        mock_strat = MagicMock()
        mock_strat.strategy_id = "u12_bot_core_test"
        mock_strat.cross_recycling_cfg = {
            "enabled": True,
            "recycle_ratio": 0.50,
            "min_burn_level": 2,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 25.0
        }
        bot.runtime_manager.strategy_registry.get_strategy_for_symbol.return_value = mock_strat
        bot.runtime_manager.save_cache = AsyncMock()
        bot.tp_manager = MagicMock()
        bot.tp_manager.place_take_profit = AsyncMock()

        bot.client = MagicMock()
        bot.client.make_order = AsyncMock(return_value=MagicMock(success=True))

        spec = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}
        long_state = PositionState("BTCUSDT", "LONG")
        long_state.set_in_position(True)
        long_state.avg_entry_price = 100.0
        long_state.total_volume = 10.0
        long_state.grid = {
            "0": {"is_active": True},
            "1": {"is_active": True},
            "2": {"is_active": True},
        }

        bot.fsm_states = {"BTCUSDT": {"LONG": long_state}}

        # SHORT закрылся в плюс $20, LONG в просадке на цене 95.0 (-5.0%)
        res = await CrossSideProfitRecycler.maybe_recycle(
            universe=bot,
            symbol="BTCUSDT",
            winning_side="SHORT",
            realized_profit=20.0,
            current_price=95.0,
            spec_data=spec
        )

        self.assertIsNotNone(res)
        self.assertEqual(res["burned_qty"], 2.0)
        self.assertEqual(long_state.total_volume, 8.0)
        bot.client.make_order.assert_called_once()
        bot.tp_manager.place_take_profit.assert_called_once()
        bot.runtime_manager.save_cache.assert_called_once_with("BTCUSDT")

    async def test_cross_side_recycler_skips_when_level_too_low(self):
        """Проверка: рециркуляция НЕ срабатывает, если страдающая сторона на уровне < 2."""
        u = MagicMock()
        u.universe_id = "u12_test"
        u.client = MagicMock()
        u.client.make_order = AsyncMock()
        u.cross_recycling_cfg = {
            "enabled": True,
            "recycle_ratio": 0.50,
            "min_burn_level": 2,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 25.0
        }

        short_state = PositionState("TESTUSDT", "SHORT")
        short_state.set_in_position(True)
        short_state.avg_entry_price = 100.0
        short_state.total_volume = 10.0
        short_state.grid = {"0": {"is_active": True}, "1": {"is_active": True}}

        u.fsm_states = {"TESTUSDT": {"SHORT": short_state}}
        spec = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}

        res = await CrossSideProfitRecycler.maybe_recycle(
            universe=u,
            symbol="TESTUSDT",
            winning_side="LONG",
            realized_profit=20.0,
            current_price=105.0,
            spec_data=spec
        )
        self.assertIsNone(res)
        u.client.make_order.assert_not_called()

    async def test_cross_recycler_missing_key_raises_key_error(self):
        """Проверка строгого протокола: отсутствие обязательного ключа в recycling_cfg вызывает KeyError."""
        u = MagicMock()
        u.universe_id = "u12_test"
        u.cross_recycling_cfg = {
            "enabled": True,
            # намеренно отсутствует recycle_ratio
            "min_burn_level": 2,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 25.0
        }
        spec = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}
        with self.assertRaises(KeyError):
            await CrossSideProfitRecycler.maybe_recycle(
                universe=u, symbol="TESTUSDT", winning_side="LONG",
                realized_profit=20.0, current_price=100.0, spec_data=spec
            )

    async def test_bot_core_finished_positions_recycling_hook(self):
        """Проверка: BotCore._check_and_reset_finished_positions вызывает рециркуляцию при закрытии прибыльного плеча."""
        from CORE.bot import BotCore
        if not hasattr(BotCore, "_check_and_reset_finished_positions"):
            self.skipTest("BotCore._check_and_reset_finished_positions is specific to live bot (cron3)")

        bot = MagicMock(spec=BotCore)
        bot.client = MagicMock()
        bot.client.cancel_orders_for_side = AsyncMock()
        bot.client.make_order = AsyncMock(return_value=MagicMock(success=True))
        bot.analytics = MagicMock()
        bot.runtime_manager = MagicMock()
        bot.runtime_manager.save_cache = AsyncMock()
        bot.tp_manager = MagicMock()
        bot.tp_manager.place_take_profit = AsyncMock()
        bot.avg_manager = MagicMock()
        bot.spec_data = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001}
        bot.prices = {"BTCUSDT": (102.0, time.time())}

        mock_strat = MagicMock()
        mock_strat.strategy_id = "u12_recycler"
        mock_strat.cross_recycling_cfg = {
            "enabled": True,
            "recycle_ratio": 0.50,
            "min_burn_level": 2,
            "min_drawdown_pct": 3.0,
            "max_slice_pct": 25.0
        }
        bot.runtime_manager.strategy_registry.get_strategy_for_symbol.return_value = mock_strat

        # LONG закрылся в плюс (вход 100, цена 102, объем 10 -> прибыль +$20)
        long_state = PositionState("BTCUSDT", "LONG")
        long_state.is_finished = True
        long_state.last_closed_volume = 10.0
        long_state.last_closed_avg_price = 100.0

        # SHORT висит в просадке на уровне 3 (вход 95, цена 102 -> DD -7.3%, объем 10)
        short_state = PositionState("BTCUSDT", "SHORT")
        short_state.set_in_position(True)
        short_state.avg_entry_price = 95.0
        short_state.total_volume = 10.0
        short_state.grid = {
            "0": {"is_active": True},
            "1": {"is_active": True},
            "2": {"is_active": True},
            "3": {"is_active": True},
        }

        states = {"LONG": long_state, "SHORT": short_state}
        bot.fsm_states = {"BTCUSDT": states}

        # Вызываем боевой метод _check_and_reset_finished_positions
        await BotCore._check_and_reset_finished_positions(bot, "BTCUSDT", states, {"LONG": {}, "SHORT": {}})

        # Проверяем, что был отправлен ордер на сжигание для SHORT
        bot.client.make_order.assert_called_once()
        order_call = bot.client.make_order.call_args[1]
        self.assertEqual(order_call["position_side"], "SHORT")
        self.assertEqual(order_call["side"], "BUY")
        self.assertGreater(order_call["qty"], 0.0)

        # Проверяем, что для LONG был вызван сброс
        bot.runtime_manager.reset_state_to_default.assert_called_once_with("BTCUSDT", "LONG", long_state)


if __name__ == "__main__":
    unittest.main()
