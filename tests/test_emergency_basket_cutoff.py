# tests/test_emergency_basket_cutoff.py
# -*- coding: utf-8 -*-
"""
==============================================================================
UNIT TESTS: Basket Equity Cutoff (Защитный стоп-кран корзины)
- Нормализация и валидация порога просадки (max_drawdown_from_start_pct)
- Защита от ложных срабатываний при сетевых сбоях и рестартах (_floating_synced, None)
- Срабатывание аварийного стоп-крана: сброс в кэш + перевод на холостые (is_paused=True, auto_start=False)
- Идемпотентность и блокировка входов на стыке сердцебиений
- Поведение Telegram-роутера: подтверждение снятия стоп-крана оператором и возобновление работы
- Паритет бэктестера (BacktestAutoClosingEvaluator)
==============================================================================
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from consts import CACHE_DIR, DATA_DIR
from c_utils import Utils
from CORE.SERVICES.emergency_stop_manager import EmergencyStopManager
from CORE.SERVICES.auto_closer import AutoCloser
from CORE.SERVICES.portfolio_closer import PortfolioCloser
from CORE.SERVICES.dome_closer_executor import DomeCloserExecutor


class TestEmergencyBasketCutoff(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cache_file = Path(self.temp_dir) / "iteration_state.json"
        self.app_json_file = Path(self.temp_dir) / "app.json"
        self.stats_dir = Path(self.temp_dir) / "STATS"
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        
        # Перенаправляем путь кэша в EmergencyStopManager на временный файл
        self._orig_cache_file = EmergencyStopManager.CACHE_FILE
        EmergencyStopManager.CACHE_FILE = self.cache_file

    def tearDown(self):
        EmergencyStopManager.CACHE_FILE = self._orig_cache_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. ТЕСТ НОРМАЛИЗАЦИИ И ВАЛИДАЦИИ ПОРОГА ПРОСАДКИ
    # -------------------------------------------------------------------------
    def test_normalization_and_validation_guard(self):
        """Проверяет корректность нормализации положительных/отрицательных чисел и None-безопасность."""
        # 1. Корректные положительные и отрицательные числа
        self.assertEqual(EmergencyStopManager.normalize_dd_pct(45.0), 45.0)
        self.assertEqual(EmergencyStopManager.normalize_dd_pct(-45.0), 45.0)
        self.assertEqual(EmergencyStopManager.normalize_dd_pct(45), 45.0)
        self.assertEqual(EmergencyStopManager.normalize_dd_pct(-45), 45.0)
        self.assertEqual(EmergencyStopManager.normalize_dd_pct(37.5), 37.5)
        self.assertEqual(EmergencyStopManager.normalize_dd_pct(-37.5), 37.5)
        self.assertEqual(EmergencyStopManager.normalize_dd_pct("45.0"), 45.0)
        self.assertEqual(EmergencyStopManager.normalize_dd_pct("-45.0"), 45.0)
        self.assertEqual(EmergencyStopManager.normalize_dd_pct("37.5"), 37.5)

        # 2. Некорректные значения и граничные условия (должны давать None - отключено)
        self.assertIsNone(EmergencyStopManager.normalize_dd_pct(None))
        self.assertIsNone(EmergencyStopManager.normalize_dd_pct(0))
        self.assertIsNone(EmergencyStopManager.normalize_dd_pct(0.0))
        self.assertIsNone(EmergencyStopManager.normalize_dd_pct(100.0))
        self.assertIsNone(EmergencyStopManager.normalize_dd_pct(120.0))
        self.assertIsNone(EmergencyStopManager.normalize_dd_pct(-150.0))
        self.assertIsNone(EmergencyStopManager.normalize_dd_pct("invalid"))
        self.assertIsNone(EmergencyStopManager.normalize_dd_pct(""))

    # -------------------------------------------------------------------------
    # 2. ТЕСТ ПЕРСИСТЕНТНОСТИ СОСТОЯНИЯ СТОП-КРАНА
    # -------------------------------------------------------------------------
    def test_emergency_stop_manager_lifecycle(self):
        """Проверяет полный жизненный цикл стоп-крана: неактивен -> активация -> персистентность -> сброс."""
        # 1. Изначально неактивен
        is_act, details = EmergencyStopManager.is_active()
        self.assertFalse(is_act)
        self.assertEqual(details, {})

        # 2. Активация
        payload = {
            "strategy_id": "u96_omni_zenith13",
            "cur_dd_pct": 45.2,
            "limit_pct": 45.0,
            "start_balance_usdt": 600.0,
            "current_equity_usdt": 328.8,
        }
        EmergencyStopManager.set_active(payload)

        # 3. Проверка активности из файла
        is_act, details = EmergencyStopManager.is_active()
        self.assertTrue(is_act)
        self.assertEqual(details["strategy_id"], "u96_omni_zenith13")
        self.assertEqual(details["cur_dd_pct"], 45.2)
        self.assertTrue("triggered_at" in details)

        # 4. Сброс оператором
        EmergencyStopManager.clear()
        is_act, details = EmergencyStopManager.is_active()
        self.assertFalse(is_act)

    # -------------------------------------------------------------------------
    # 3. ТЕСТ СРАБАТЫВАНИЯ СТОП-КРАНА В AutoCloser (_check_strategy_domes)
    # -------------------------------------------------------------------------
    async def test_autocloser_emergency_basket_cutoff_trigger(self):
        """Проверяет срабатывание аварийного сброса купола при достижении лимита просадки."""
        mock_bot = MagicMock()
        mock_bot.is_paused = False
        mock_bot.entry_blocked = False
        mock_bot.close_all_positions = AsyncMock()
        mock_bot.close_strategy_positions = AsyncMock()
        mock_bot.notifier = MagicMock()
        mock_bot.notifier.send_alert = AsyncMock()

        strat_cfg = MagicMock()
        strat_cfg.name = "U96 Zenith 13"
        strat_cfg.start_balance_usdt = 600.0
        strat_cfg.symbols = ["BILLUSDT", "ERAUSDT"]
        strat_cfg.auto_closing_cfg = {
            "negative": {
                "max_drawdown_from_start_pct": 45.0,
                "notify_telegram": True,
            }
        }

        mock_bot.strategy_registry = MagicMock()
        mock_bot.strategy_registry.active_strategies = {
            "u96_omni_zenith13__trailing_ratchet": strat_cfg
        }

        auto_closer = AutoCloser(mock_bot)
        app_data = {
            "app": {"auto_start": True},
            "active_strategies": {
                "u96_omni_zenith13__trailing_ratchet": {
                    "auto_closing": {
                        "negative": {
                            "max_drawdown_from_start_pct": 45.0,
                            "notify_telegram": True,
                        }
                    }
                }
            }
        }

        stat_file = self.stats_dir / "analytics_u96_omni_zenith13__trailing_ratchet.json"

        # Сценарий A: Просадка ниже лимита (Net = -200$ при старте 600$ -> 33.3% < 45.0%) -> СТОП НЕ СРАБАТЫВАЕТ
        safe_analytics = {
            "_floating_synced": True,
            "net_profit_usdt": -200.0,
            "start_balance_usdt": 600.0,
            "iteration_index": 1,
            "iteration_start_net_profit_usdt": 0.0,
        }
        Utils.write_json_file(stat_file, safe_analytics)

        with patch("CORE.SERVICES.auto_closer.ANALYTICS_STATS_DIR", self.stats_dir):
            await auto_closer._check_strategy_domes(
                app_data=app_data,
                app_json_path=self.app_json_file,
                analytics_data={},
                now_sec=1000.0,
                now_ms=1000000,
                fsm=None,
            )

        mock_bot.close_all_positions.assert_not_called()
        self.assertFalse(mock_bot.is_paused)
        self.assertTrue(app_data["app"]["auto_start"])
        self.assertFalse(EmergencyStopManager.is_active()[0])

        # Сценарий B: Просадка достигает 45% (Net = -270$ при старте 600$ -> 45.0% >= 45.0%) -> СТОП-КРАН СРАБАТЫВАЕТ
        breach_analytics = {
            "_floating_synced": True,
            "net_profit_usdt": -270.0,
            "start_balance_usdt": 600.0,
            "iteration_index": 1,
            "iteration_start_net_profit_usdt": 0.0,
        }
        Utils.write_json_file(stat_file, breach_analytics)

        with patch("CORE.SERVICES.auto_closer.ANALYTICS_STATS_DIR", self.stats_dir):
            await auto_closer._check_strategy_domes(
                app_data=app_data,
                app_json_path=self.app_json_file,
                analytics_data={},
                now_sec=1100.0,
                now_ms=1100000,
                fsm=None,
            )

        # 1. 100% сброс всех позиций в кэш
        mock_bot.close_all_positions.assert_called_once()
        # 2. Бот переведен на холостые
        self.assertTrue(mock_bot.is_paused)
        self.assertTrue(mock_bot.entry_blocked)
        # 3. auto_start сброшен в False
        self.assertFalse(app_data["app"]["auto_start"])
        # 4. Стоп-кран зафиксирован в кэше
        is_act, details = EmergencyStopManager.is_active()
        self.assertTrue(is_act)
        self.assertEqual(details["strategy_id"], "u96_omni_zenith13__trailing_ratchet")
        self.assertEqual(details["drawdown_pct"], 45.0)
        # 5. Экстренное уведомление в Telegram
        mock_bot.notifier.send_alert.assert_called_once()
        sent_msg = mock_bot.notifier.send_alert.call_args[0][0]
        self.assertIn("АВАРИЙНЫЙ СТОП-КРАН ПОРТФЕЛЯ", sent_msg)
        self.assertIn("ХОЛОСТЫЕ", sent_msg)

    # -------------------------------------------------------------------------
    # 4. ТЕСТ ЗАЩИТЫ ОТ ЛОЖНЫХ СРАБАТЫВАНИЙ ПРИ РЕСТАРТАХ И СЕТЕВЫХ СБОЯХ
    # -------------------------------------------------------------------------
    async def test_resilience_to_network_glitches_and_unsynced_stats(self):
        """Проверяет, что при _floating_synced=False или None net_profit_usdt стоп-кран НЕ стреляет ложно."""
        mock_bot = MagicMock()
        mock_bot.is_paused = False
        mock_bot.close_all_positions = AsyncMock()

        strat_cfg = MagicMock()
        strat_cfg.start_balance_usdt = 600.0
        strat_cfg.auto_closing_cfg = {"negative": {"max_drawdown_from_start_pct": 45.0}}

        mock_bot.strategy_registry = MagicMock()
        mock_bot.strategy_registry.active_strategies = {
            "u96_omni_zenith13__trailing_ratchet": strat_cfg
        }

        auto_closer = AutoCloser(mock_bot)
        stat_file = self.stats_dir / "analytics_u96_omni_zenith13__trailing_ratchet.json"

        # _floating_synced = False (REST API еще синхронизируется при рестарте)
        unsynced_analytics = {
            "_floating_synced": False,
            "net_profit_usdt": -500.0,
            "start_balance_usdt": 600.0,
        }
        Utils.write_json_file(stat_file, unsynced_analytics)

        with patch("CORE.SERVICES.auto_closer.ANALYTICS_STATS_DIR", self.stats_dir):
            await auto_closer._check_strategy_domes(
                app_data={"app": {"auto_start": True}},
                app_json_path=self.app_json_file,
                analytics_data={},
                now_sec=1000.0,
                now_ms=1000000,
                fsm=None,
            )
        mock_bot.close_all_positions.assert_not_called()
        self.assertFalse(mock_bot.is_paused)

        # net_profit_usdt = None
        none_analytics = {
            "_floating_synced": True,
            "net_profit_usdt": None,
            "start_balance_usdt": 600.0,
        }
        Utils.write_json_file(stat_file, none_analytics)

        with patch("CORE.SERVICES.auto_closer.ANALYTICS_STATS_DIR", self.stats_dir):
            await auto_closer._check_strategy_domes(
                app_data={"app": {"auto_start": True}},
                app_json_path=self.app_json_file,
                analytics_data={},
                now_sec=1100.0,
                now_ms=1100000,
                fsm=None,
            )
        mock_bot.close_all_positions.assert_not_called()
        self.assertFalse(mock_bot.is_paused)

    # -------------------------------------------------------------------------
    # 5. ТЕСТ СРАБАТЫВАНИЯ СТОП-КРАНА В PortfolioCloser
    # -------------------------------------------------------------------------
    async def test_portfolio_closer_emergency_basket_cutoff(self):
        """Проверяет срабатывание стоп-крана на уровне глобального портфеля."""
        mock_bot = MagicMock()
        mock_bot.is_paused = False
        mock_bot.entry_blocked = False
        mock_bot.close_all_positions = AsyncMock()
        mock_bot.notifier = MagicMock()
        mock_bot.notifier.send_alert = AsyncMock()

        auto_closer = AutoCloser(mock_bot)
        app_data = {
            "app": {"auto_start": True},
            "auto_closing": {
                "negative": {
                    "max_drawdown_from_start_pct": 37.5,
                    "notify_telegram": True
                }
            }
        }

        # Net = -230$ при старте 600$ -> 38.3% >= 37.5% -> СРАБАТЫВАЕТ
        global_analytics = {
            "_floating_synced": True,
            "net_profit_usdt": -230.0,
            "start_balance_usdt": 600.0,
            "iteration_index": 1,
            "iteration_start_net_profit_usdt": 0.0,
        }

        await PortfolioCloser.check(
            auto_closer_inst=auto_closer,
            app_data=app_data,
            app_json_path=self.app_json_file,
            analytics_data=global_analytics,
            now_sec=1000.0,
            now_ms=1000000,
            fsm=None,
        )

        mock_bot.close_all_positions.assert_called_once()
        self.assertTrue(mock_bot.is_paused)
        self.assertFalse(app_data["app"]["auto_start"])
        is_act, details = EmergencyStopManager.is_active()
        self.assertTrue(is_act)
        self.assertEqual(details["strategy_id"], "__global__")
        self.assertEqual(details["max_drawdown_limit_pct"], 37.5)

    # -------------------------------------------------------------------------
    # 6. ТЕСТ СНЯТИЯ СТОП-КРАНА ОПЕРАТОРОМ В TELEGRAM
    # -------------------------------------------------------------------------
    async def test_telegram_operator_reset_flow(self):
        """Проверяет предупреждение при нажатии Start и возобновление работы после подтверждения."""
        from TG.HANDLERS.base_router import BaseRouterHandler

        mock_bot = MagicMock()
        mock_bot.is_paused = True
        mock_bot.entry_blocked = True
        mock_bot.symbol_blocked_until = {"BILLUSDT": 9999999.0}
        mock_bot.strategy_blocked_until = {"u96_omni_zenith13": 9999999.0}

        handler = BaseRouterHandler(mock_bot)

        # 1. Активируем стоп-кран
        EmergencyStopManager.set_active({
            "strategy_id": "u96_omni_zenith13",
            "drawdown_pct": 45.0,
            "max_drawdown_limit_pct": 45.0,
            "start_balance_usdt": 600.0,
            "current_equity_usdt": 330.0,
        })
        self.assertTrue(EmergencyStopManager.is_active()[0])

        # 2. Оператор нажимает кнопку '▶️ Start' (вызов on_pre_start)
        mock_msg = MagicMock()
        mock_msg.answer = AsyncMock()
        mock_state = MagicMock()
        mock_state.clear = AsyncMock()

        await handler.on_pre_start(mock_msg, mock_state)

        # Должно прийти предупреждение о стоп-кране и клавиатура подтверждения
        mock_msg.answer.assert_called_once()
        warning_text = mock_msg.answer.call_args[0][0]
        self.assertIn("АКТИВЕН АВАРИЙНЫЙ СТОП-КРАН", warning_text)
        self.assertIn("45.00%", warning_text)
        # Бот все еще на паузе
        self.assertTrue(mock_bot.is_paused)

        # 3. Оператор подтверждает снятие стоп-крана (on_confirm_start)
        mock_confirm_msg = MagicMock()
        mock_confirm_msg.answer = AsyncMock()

        # Создаем временный app.json
        Utils.write_json_file(self.app_json_file, {"app": {"auto_start": False}})
        with patch("TG.HANDLERS.base_router.DATA_DIR", Path(self.temp_dir)):
            await handler.on_confirm_start(mock_confirm_msg, mock_state)

        # Стоп-кран должен быть снят
        self.assertFalse(EmergencyStopManager.is_active()[0])
        # Пауза снята, бот запущен
        self.assertFalse(mock_bot.is_paused)
        self.assertFalse(mock_bot.entry_blocked)
        self.assertEqual(len(mock_bot.symbol_blocked_until), 0)
        self.assertEqual(len(mock_bot.strategy_blocked_until), 0)

        # Ответ оператору подтверждает снятие стоп-крана
        mock_confirm_msg.answer.assert_called_once()
        res_text = mock_confirm_msg.answer.call_args[0][0]
        self.assertIn("Аварийный стоп-кран успешно снят", res_text)
        self.assertIn("Running", res_text)

        # app.json персистентно сохранил auto_start=True
        saved_app = Utils.read_json_file(self.app_json_file)
        self.assertTrue(saved_app["app"]["auto_start"])

    # -------------------------------------------------------------------------
    # 7. ТЕСТ ПАРИТЕТА В БЭКТЕСТЕРЕ (BacktestAutoClosingEvaluator)
    # -------------------------------------------------------------------------
    def test_backtest_evaluator_parity(self):
        """Проверяет, что BacktestAutoClosingEvaluator корректно детектирует превышение просадки."""
        backtest_dir = Path(__file__).resolve().parent.parent.parent / "cron3TradeBacktest"
        if not backtest_dir.exists():
            backtest_dir = Path(__file__).resolve().parent.parent / "cron3TradeBacktest"
        if str(backtest_dir) not in sys.path:
            sys.path.insert(0, str(backtest_dir))

        from ENGINE.auto_closing_evaluator import BacktestAutoClosingEvaluator

        fsm_states = {"MYXUSDT": {"SHORT": MagicMock(in_position=True)}}
        cfg = {
            "mode": "trailing_ratchet",
            "positive": {"enabled": True},
            "negative": {"max_drawdown_from_start_pct": 45.0}
        }

        # 1. Просадка 30% (Equity = 420$ при старте 600$) -> не срабатывает
        close, _, _ = BacktestAutoClosingEvaluator.evaluate(
            auto_closing_cfg=cfg,
            cur_equity=420.0,
            iter_start_equity=600.0,
            current_sim_time=1000.0,
            iter_start_time=500.0,
            auto_closer_state={},
            positions={},
            fsm_states=fsm_states,
            initial_balance=600.0,
        )
        self.assertFalse(close)

        # 2. Просадка 45% (Equity = 330$ при старте 600$) -> СРАБАТЫВАЕТ
        close, _, info = BacktestAutoClosingEvaluator.evaluate(
            auto_closing_cfg=cfg,
            cur_equity=330.0,
            iter_start_equity=600.0,
            current_sim_time=1000.0,
            iter_start_time=500.0,
            auto_closer_state={},
            positions={},
            fsm_states=fsm_states,
            initial_balance=600.0,
        )
        self.assertTrue(close)
        self.assertIn("EMERGENCY BASKET CUTOFF", info)


if __name__ == "__main__":
    unittest.main()
