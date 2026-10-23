# ==============================================================================
# Path: tests/test_architectural_invariants_and_cooldown.py
# Role: Институциональный комплексный аудит-тест:
#       1. Все кнопки Telegram (полный прогон через ООП-роутеры как dumb shells)
#       2. Аналитика: расчеты realized/unrealized PnL, комиссий, скорости и вывод
#       3. Кнопка автозакрытия и кулдаун до следующего тика сигнала (signal_heartbeat_tfr)
#       4. Автозапуск бота при auto_start: true
#       5. Ресторация рантаймов и синхронизация кэша с FSM
#       6. Чтение конфигов, 100% зеркальная симметрия u17/u96, клиентские leverage/notifications
#       7. Управление first_order_weight_regime через enabled: true/false
#       8. Произвольный signal_heartbeat_tfr (7m, 10m и т.д.) без хардкода 300s
#       9. Гонки состояний и идемпотентность блокировок
# ==============================================================================

import asyncio
import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Chat, Message, User
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from API.BINANCE.client_prod import BinanceClient
import shutil
import tempfile
from CORE._utils import Utils
from CORE.bot import BotCore
from CORE.strategy_registry import StrategyRegistry, StrategyConfig
from CORE.strategy_applicator import StrategyApplicator
from CORE.SERVICES.auto_closer import AutoCloser
from CORE.SERVICES.emergency_closer import EmergencyCloserService
from CORE.ENTRY.signal import TimeControl
from CORE.STRATEGY.first_order_regime import FirstOrderRegimeManager
from POS_FSM.models import PositionState
from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
from TG.tg_adapter import TelegramAdapter
from TG.keyboards import get_main_keyboard

ROOT_DIR = Path(__file__).resolve().parent.parent
CFG_DIR = ROOT_DIR / "CFG"
UNIVERSES_DIR = CFG_DIR / "universes"


class TestArchitecturalInvariantsAndCooldown(unittest.IsolatedAsyncioTestCase):
    """Глубокий верификационный аудит архитектурных инвариантов и протокола Самсонова Н."""

    async def asyncSetUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_invariants_"))
        self.tmp_runtime_dir = self.temp_dir / "runtime"
        self.tmp_runtime_dir.mkdir(parents=True, exist_ok=True)
        self.registry = StrategyRegistry()

        from consts import TG_ALLOWED_USERS
        user_id = TG_ALLOWED_USERS[0] if TG_ALLOWED_USERS else 610822492
        self.test_user = User(id=user_id, is_bot=False, first_name="Tester", username="tester")
        self.test_chat = Chat(id=user_id, type="private")

    async def asyncTearDown(self):
        if hasattr(self, "temp_dir") and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # 1. ТЕСТИРОВАНИЕ ВСЕХ ТГ-КНОПОК ЧЕРЕЗ ООП-РОУТЕРЫ
    # =========================================================================
    async def test_all_tg_buttons_execution_via_oop_handlers(self):
        """Прогон всех кнопок Telegram через классы-роутеры без исключений."""
        from TG.HANDLERS.base_router import BaseRouterHandler
        from TG.HANDLERS.analytics_router import AnalyticsRouterHandler
        from TG.HANDLERS.universe_router import UniverseRouterHandler
        from TG.HANDLERS.screeners_router import ScreenersRouterHandler
        from TG.HANDLERS.positions_router import PositionsRouterHandler
        from TG.HANDLERS.coins_router import CoinsRouterHandler

        mock_bot_core = MagicMock()
        mock_bot_core.is_running = True
        mock_bot_core.is_paused = False
        mock_bot_core.strategy_registry = self.registry
        mock_bot_core.runtime_manager = MagicMock()
        mock_bot_core.close_all_positions = AsyncMock(return_value={"closed": 2})
        mock_bot_core.analytics = None

        # BaseRouterHandler (Start, Stop, Status, Logs)
        base_h = BaseRouterHandler(mock_bot_core)
        msg_mock = AsyncMock(spec=Message)
        msg_mock.from_user = self.test_user
        msg_mock.chat = self.test_chat
        msg_mock.answer = AsyncMock()

        state_mock = AsyncMock(spec=FSMContext)

        await base_h.start_cmd(msg_mock, state_mock)
        self.assertTrue(msg_mock.answer.called)

        await base_h.on_status(msg_mock, state_mock)
        self.assertTrue(msg_mock.answer.called)

        await base_h.on_logs_menu(msg_mock, state_mock)
        self.assertTrue(msg_mock.answer.called)

        await base_h.on_confirm_start(msg_mock, state_mock)
        self.assertFalse(mock_bot_core.is_paused)

        await base_h.on_stop_trade(msg_mock, state_mock)
        self.assertTrue(mock_bot_core.is_paused)

        # AnalyticsRouterHandler
        analytics_h = AnalyticsRouterHandler(mock_bot_core)
        await analytics_h.cmd_analytics(msg_mock, state_mock)
        self.assertTrue(msg_mock.answer.called)

        await analytics_h.show_leaderboard(msg_mock)
        self.assertTrue(msg_mock.answer.called)

        # ScreenersRouterHandler
        screeners_h = ScreenersRouterHandler(mock_bot_core)
        await screeners_h.btn_screeners(msg_mock, state_mock)
        self.assertTrue(msg_mock.answer.called)

        # PositionsRouterHandler (Close All)
        pos_h = PositionsRouterHandler(mock_bot_core)
        await pos_h.on_close_all(msg_mock, state_mock)
        self.assertTrue(msg_mock.answer.called)

        # UniverseRouterHandler
        univ_h = UniverseRouterHandler(mock_bot_core)
        await univ_h.cmd_universes(msg_mock, state_mock)
        self.assertTrue(msg_mock.answer.called)

        # CoinsRouterHandler
        coins_h = CoinsRouterHandler(mock_bot_core)
        await coins_h.on_set_coins(msg_mock, state_mock)
        self.assertTrue(msg_mock.answer.called)

    # =========================================================================
    # 2. РАСЧЕТЫ И ВЫВОДЫ АНАЛИТИКИ
    # =========================================================================
    def test_analytics_calculations_and_net_velocity(self):
        """Проверка математики и формул аналитики: PnL, ROI, Recovery Factor, Drawdown, Trade Velocity."""
        from ANALYTICS.metrics import AnalyticsMathEngine
        from ANALYTICS.trade_velocity import TradeVelocityEngine

        realized = 150.0
        unrealized = -30.0
        net = realized + unrealized

        data = {
            "start_balance_usdt": 1000.0,
            "realized_pnl_usdt": realized,
            "unrealized_pnl_usdt": unrealized,
            "net_profit_usdt": net,
            "total_fees_usdt": 5.0,
            "cur_balance_usdt": 1000.0,
            "peak_balance_usdt": 1200.0,
            "min_balance_usdt": 950.0,
            "max_drawdown_usdt": -50.0,
            "per_coin": {
                "ICNTUSDT": {
                    "realized_pnl_usdt": 150.0,
                    "unrealized_pnl_usdt": -30.0,
                    "net_profit_usdt": 120.0,
                    "total_grid_size_usdt": 400.0,
                    "current_grid_load_usdt": 120.0,
                    "peak_net_profit_usdt": 160.0,
                    "max_drawdown_net_usdt": -40.0,
                }
            }
        }

        AnalyticsMathEngine.calculate(data)

        # 1. Net profit = realized + unrealized
        self.assertEqual(data["net_profit_usdt"], 120.0)

        # 2. Current balance = start_balance + net_profit
        self.assertEqual(data["cur_balance_usdt"], 1120.0)

        # 3. ROI % = (cur_balance - start_balance) / start_balance * 100
        expected_roi = ((1120.0 - 1000.0) / 1000.0) * 100.0
        self.assertAlmostEqual(data["roi_pct"], expected_roi, places=2)

        # 4. Recovery factor = net_profit / abs(max_drawdown)
        expected_rf = 120.0 / 50.0
        self.assertAlmostEqual(data["recovery_factor"], expected_rf, places=2)

        # 5. Per-coin metrics
        coin_data = data["per_coin"]["ICNTUSDT"]
        self.assertEqual(coin_data["net_profit_usdt"], 120.0)

        # 6. Trade velocity engine verification
        tv_engine = TradeVelocityEngine(universe_id="test_uid")
        now_ms = int(time.time() * 1000)
        # 10 сделок за последние 2 часа
        tv_engine._trades_history = [
            (now_ms - int(i * 600 * 1000), "ICNTUSDT") for i in range(10)
        ]
        tv_engine.earliest_trade_ts_ms = now_ms - (2 * 3600 * 1000)
        velocity_data = tv_engine.calculate_velocity()
        self.assertIsInstance(velocity_data, dict)
        self.assertIn("speed_ratio", velocity_data)
        report_block = tv_engine.format_report_block(velocity_data)
        self.assertIsInstance(report_block, str)

    # =========================================================================
    # 3. КНОПКА АВТОЗАКРЫТИЯ И КУЛДАУН ДО СЛЕДУЮЩЕГО ТИКА СИГНАЛА
    # =========================================================================
    async def test_close_all_and_autocloser_signal_tick_cooldown(self):
        """
        Верификация: после Close All и срабатывания AutoCloser
        вход в новые позиции заблокирован ровно до следующего тика свечи signal_heartbeat_tfr.
        """
        mock_bot = BotCore()
        mock_bot.is_paused = False
        mock_bot.strategy_registry = self.registry
        mock_bot.runtime_manager = MagicMock()
        mock_bot.analytics = MagicMock()
        mock_bot.client = MagicMock()
        mock_bot.client.cancel_all_orders = AsyncMock()
        mock_bot.client.fetch_positions = AsyncMock(return_value=[])
        mock_bot.reset_strategy_iteration = AsyncMock(return_value={"strategy_id": "u17_stephensonE__trailing_ratchet", "new_iteration": 2})

        now = 1000.0
        mock_bot.current_sim_time = now
        # Для 5m (300s): свечи 900..1200. При now=1000 следующая свеча начинается в 1200 + buffer(5) = 1205.
        cooldown_5m = BotCore.calc_heartbeat_window_cooldown(now, 300)
        self.assertEqual(cooldown_5m, 1205.0)

        # Для 7m (420s): 1000 // 420 = 2 -> следующая свеча 3 * 420 = 1260 + buffer(5) = 1265.
        cooldown_7m = BotCore.calc_heartbeat_window_cooldown(now, 420)
        self.assertEqual(cooldown_7m, 1265.0)

        # Проверяем EmergencyCloserService.close_all
        symbols = ["ICNTUSDT", "AVAUSDT"]
        mock_bot.symbols = symbols
        with patch("time.time", return_value=now):
            await EmergencyCloserService.close_all(mock_bot, reset_iterations=True)

        self.assertTrue(mock_bot.entry_blocked)
        self.assertGreaterEqual(mock_bot.block_until_timestamp, now)
        for sym in symbols:
            self.assertGreaterEqual(mock_bot.symbol_blocked_until[sym], now)

        # Проверяем, что в пределах кулдауна TradeCycleManager блокирует вход
        mock_bot.current_sim_time = now + 50.0
        is_blocked, reason, rem = mock_bot.check_entry_block("ICNTUSDT")
        self.assertTrue(is_blocked)
        self.assertIn("cooldown active", reason)

        # После истечения кулдауна вход разблокируется
        mock_bot.current_sim_time = mock_bot.block_until_timestamp + 1.0
        is_blocked, reason, rem = mock_bot.check_entry_block("ICNTUSDT")
        self.assertFalse(is_blocked)

    # =========================================================================
    # 4. ПЕРЕЗАПУСК БОТА ПРИ "auto_start": true / false
    # =========================================================================
    def test_bot_auto_start_flag_behavior(self):
        """Проверка флага auto_start: при True бот в активном режиме, при False — в паузе."""
        with patch("CORE.bot._CFG", {"app": {"auto_start": True}}), patch("CORE.bot.TG_ENABLED", True):
            bot_auto = BotCore()
            self.assertFalse(bot_auto.is_paused, "При auto_start=True бот должен быть активен (is_paused=False)")

        with patch("CORE.bot._CFG", {"app": {"auto_start": False}}), patch("CORE.bot.TG_ENABLED", True):
            bot_manual = BotCore()
            self.assertTrue(bot_manual.is_paused, "При auto_start=False бот должен ждать запуска (is_paused=True)")

    # =========================================================================
    # 5. РЕСТОРАЦИЯ РАНТАЙМОВ И СИНХРОНИЗАЦИЯ КЭША С FSM
    # =========================================================================
    async def test_runtime_cache_fsm_synchronization_and_restoration(self):
        rm = RuntimeFsmManager(strategy_registry=self.registry)
        rm.runtime_dir = self.tmp_runtime_dir
        symbol = "TEST_SYNC_USDT"
        rm.caches[symbol] = {}

        st = PositionState(symbol=symbol, side="LONG")
        st.in_position = True
        st.total_volume = 125.5
        st.avg_entry_price = 0.1850
        st.grid["0"] = {"is_active": True, "price": 0.1850, "volume": 12.0}
        st.grid["1"] = {"is_active": False, "price": 0.1750, "volume": 8.0}

        fsm_states = {symbol: {"LONG": st, "SHORT": PositionState(symbol=symbol, side="SHORT")}}

        # Синхронизация в кэш
        await rm.sync_with_fsm(fsm_states, force_save=False)
        cache_long = rm.caches[symbol]["LONG"]

        self.assertEqual(cache_long["total_volume"], 125.5)
        self.assertEqual(cache_long["avg_entry_price"], 0.1850)
        self.assertTrue(cache_long["in_position"])
        self.assertTrue(cache_long["grid"]["0"]["is_active"])
        self.assertFalse(cache_long["grid"]["1"]["is_active"])

        # Восстановление в чистый стейт
        restored_st = PositionState(symbol=symbol, side="LONG")
        clean_fsm = {symbol: {"LONG": restored_st, "SHORT": PositionState(symbol=symbol, side="SHORT")}}
        rm.populate_fsm_from_cache(clean_fsm)

        self.assertEqual(restored_st.total_volume, 125.5)
        self.assertEqual(restored_st.avg_entry_price, 0.1850)
        self.assertTrue(restored_st.in_position)
        self.assertTrue(restored_st.grid["0"]["is_active"])

    # =========================================================================
    # 6. ЧТЕНИЕ КОНФИГОВ, 100% СИММЕТРИЯ U17/U96, КЛИЕНТСКИЕ LEVERAGE/NOTIFICATIONS
    # =========================================================================
    def test_config_reading_and_full_passport_symmetry(self):
        """
        Проверка:
        1. u17 и u96 не содержат leverage и notifications (перенесены на клиентский уровень app.json).
        2. u17 и u96 имеют 100% зеркальную симметрию полей и секций.
        3. super_grid представлен каноническим словарем с enabled: false.
        """
        u17_data = Utils.read_json_file(UNIVERSES_DIR / "u17_stephensonE.json")
        u96_data = Utils.read_json_file(UNIVERSES_DIR / "u96_omni_zenith_apex.json")

        # 1. Отсутствие leverage и notifications в паспортах
        self.assertNotIn("leverage", u17_data, "leverage должен быть strictly client-side в app.json")
        self.assertNotIn("leverage", u96_data, "leverage должен быть strictly client-side в app.json")
        self.assertNotIn("notifications", u17_data, "notifications должен быть strictly client-side в app.json")
        self.assertNotIn("notifications", u96_data, "notifications должен быть strictly client-side в app.json")

        # 2. Наличие leverage и notifications в app.json
        app_data = Utils.read_json_file(CFG_DIR / "app.json")
        for s_key in ("u17_stephensonE__trailing_ratchet", "u96_omni_zenith_apex__trailing_ratchet"):
            c_cfg = app_data["active_strategies"][s_key]
            self.assertIn("leverage", c_cfg)
            self.assertEqual(c_cfg["leverage"], 20)
            self.assertIn("notifications", c_cfg)
            self.assertTrue(c_cfg["notifications"]["enabled"])

        # 3. 100% зеркальная симметрия верхнего уровня
        self.assertEqual(set(u17_data.keys()), set(u96_data.keys()))

        # 4. super_grid в обоих куполах — канонический словарь
        for data in (u17_data, u96_data):
            self.assertIsInstance(data["super_grid"], dict)
            self.assertFalse(data["super_grid"]["enabled"])
            self.assertEqual(data["super_grid"]["timeframe"], "1w")
            self.assertEqual(data["super_grid"]["window"], 12)
            self.assertEqual(data["super_grid"]["multiplier"], 1.5)

        # 5. SHORT в обоих паспортах полностью детерминирован
        for data in (u17_data, u96_data):
            self.assertIn("grid", data["SHORT"])
            self.assertIn("tp_map", data["SHORT"])
            self.assertFalse(data["SHORT"].get("_inheritance_base_side", True))

        # 6. Проверка StrategyMetadataHelper: чтение client leverage из app.json
        from TG.strategy_helpers import StrategyMetadataHelper
        u17_meta = StrategyMetadataHelper.get_strategy_meta("u17_stephensonE__trailing_ratchet")
        self.assertEqual(u17_meta["leverage"], 20)
        self.assertIn("Stephenson", u17_meta["name"])

    # =========================================================================
    # 7. УПРАВЛЕНИЕ first_order_weight_regime ЧЕРЕЗ enabled: true / false
    # =========================================================================
    def test_first_order_weight_regime_enabled_flag(self):
        """Проверка: first_order_weight_regime управляется флагом 'enabled': true | false."""
        u17_data = Utils.read_json_file(UNIVERSES_DIR / "u17_stephensonE.json")
        u96_data = Utils.read_json_file(UNIVERSES_DIR / "u96_omni_zenith_apex.json")

        self.assertTrue(u17_data["first_order_weight_regime"]["enabled"])
        self.assertFalse(u96_data["first_order_weight_regime"]["enabled"])

        # FirstOrderRegimeManager при enabled=False возвращает default_vol без изменений
        disabled_regime = {"enabled": False, "mode": "bullish", "presets": {"bullish": {"LONG": 12.0}}}
        vol_disabled = FirstOrderRegimeManager.resolve_static_volume(disabled_regime, side="LONG", default_vol=9.0)
        self.assertEqual(vol_disabled, 9.0)

        # При enabled=True возвращает объем из пресета
        enabled_regime = {"enabled": True, "mode": "bullish", "presets": {"bullish": {"LONG": 12.0}}}
        vol_enabled = FirstOrderRegimeManager.resolve_static_volume(enabled_regime, side="LONG", default_vol=9.0)
        self.assertEqual(vol_enabled, 12.0)

    # =========================================================================
    # 8. ПРОИЗВОЛЬНЫЙ signal_heartbeat_tfr (7m, 10m И Т.Д.) БЕЗ ХАРДКОДА 300s
    # =========================================================================
    def test_signal_heartbeat_arbitrary_timeframes(self):
        """Проверка: signal_heartbeat_tfr поддерживает любые интервалы (1m, 7m, 10m, 1h) без дефолта на 300s."""
        self.assertEqual(TimeControl.interval_to_seconds("1m"), 60)
        self.assertEqual(TimeControl.interval_to_seconds("3m"), 180)
        self.assertEqual(TimeControl.interval_to_seconds("5m"), 300)
        self.assertEqual(TimeControl.interval_to_seconds("7m"), 420)
        self.assertEqual(TimeControl.interval_to_seconds("10m"), 600)
        self.assertEqual(TimeControl.interval_to_seconds("15m"), 900)
        self.assertEqual(TimeControl.interval_to_seconds("1h"), 3600)
        self.assertEqual(TimeControl.interval_to_seconds("45s"), 45)

        # Проверяем расчет StrategyConfig с произвольным 7m
        raw_7m = {
            "name": "u_custom_7m",
            "signal_heartbeat_tfr": "7m",
            "LONG": {"grid": {"0": {"indent": 0.0, "volume": 10.0}}},
            "SHORT": {"grid": {"0": {"indent": 0.0, "volume": 10.0}}}
        }
        strat_7m = StrategyConfig("u_custom_7m", raw_7m)
        self.assertEqual(strat_7m.signal_heartbeat_tfr, "7m")
        self.assertEqual(strat_7m.heartbeat_seconds, 420)

        # Проверяем расчет кулдауна для 7m
        now = 1000.0
        # 1000 // 420 = 2 -> следующая свеча 3 * 420 = 1260 -> 1260 + 5 = 1265.
        cd_7m = BotCore.calc_heartbeat_window_cooldown(now, strat_7m.heartbeat_seconds)
        self.assertEqual(cd_7m, 1265.0)

    # =========================================================================
    # 9. ГОНКИ СОСТОЯНИЙ И ИДЕМПОТЕНТНОСТЬ БЛОКИРОВОК
    # =========================================================================
    async def test_race_conditions_and_concurrent_close_locks(self):
        """Проверка параллельного вызова Close All и сигналов: отсутствие дедлоков и гонок."""
        mock_bot = BotCore()
        mock_bot.strategy_registry = self.registry
        mock_bot.runtime_manager = MagicMock()
        mock_bot.analytics = MagicMock()
        mock_bot.client = MagicMock()
        mock_bot.client.cancel_all_orders = AsyncMock()
        mock_bot.client.fetch_positions = AsyncMock(return_value=[])
        mock_bot.reset_strategy_iteration = AsyncMock(return_value={"strategy_id": "u17_stephensonE__trailing_ratchet", "new_iteration": 2})
        symbols = ["ICNTUSDT", "AVAUSDT"]
        mock_bot.symbols = symbols

        # Запускаем одновременное закрытие
        tasks = [
            EmergencyCloserService.close_all(mock_bot, reset_iterations=True),
            EmergencyCloserService.close_all(mock_bot, reset_iterations=True),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            self.assertNotIsInstance(res, Exception)
            self.assertIsInstance(res, dict)

        self.assertTrue(mock_bot.entry_blocked)


if __name__ == "__main__":
    unittest.main()
