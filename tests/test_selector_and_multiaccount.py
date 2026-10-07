# ============================================================
# FILE: tests/test_selector_and_multiaccount.py
# ROLE: Unit and integration tests for CoinSelectionService,
#       Multi-Account Client architecture, and Telegram Selector handlers
# ============================================================

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from CORE.SELECTOR.coin_selection_service import CoinSelectionService
from CORE.STRATEGY.strategy_registry import StrategyConfig, StrategyRegistry
from TG.tg_receiver import TelegramReceiver


class TestSelectorAndMultiAccount(unittest.TestCase):
    """Тестирование CoinSelectionService, Multi-Account клиентов и команды /selector."""

    def setUp(self):
        self.sample_active_cfg = {
            "u6_symbiosis_gamma_fortress35": {
                "enabled": True,
                "api_key_name": "API_KEY_ACCOUNT_1",
                "api_secret_name": "API_SECRET_ACCOUNT_1",
                "symbols": ["GRASSUSDT", "ZROUSDT"],
                "analytics": {"start_balance_usdt": 171.92}
            },
            "u16_titan_universal_alpha": {
                "enabled": True,
                "api_key_name": "API_KEY_ACCOUNT_2",
                "api_secret_name": "API_SECRET_ACCOUNT_2",
                "symbols": ["BTCUSDT"],
                "analytics": {"start_balance_usdt": 171.92}
            }
        }

    # ============================================================
    # 1. ТЕСТЫ МУЛЬТИАККАУНТНОСТИ
    # ============================================================
    def test_strategy_config_api_credentials_custom_env(self):
        """Проверка разрешения кастомных API ключей из .env для стратегии."""
        raw_meta = {
            "name": "Test Strategy",
            "invest_size": 400.0,
            "leverage": 20
        }
        with patch.dict(os.environ, {
            "API_KEY_ACCOUNT_1": "key_custom_1",
            "API_SECRET_ACCOUNT_1": "sec_custom_1"
        }):
            cfg = StrategyConfig(
                strategy_id="u6_test",
                raw_meta=raw_meta,
                symbols=["GRASSUSDT"],
                active_settings={
                    "api_key_name": "API_KEY_ACCOUNT_1",
                    "api_secret_name": "API_SECRET_ACCOUNT_1"
                }
            )
            key, sec = cfg.get_api_credentials()
            self.assertEqual(key, "key_custom_1")
            self.assertEqual(sec, "sec_custom_1")

    def test_strategy_config_api_credentials_suffix_fallback(self):
        """Проверка автоматического подхвата ключей по суффиксу API_KEY_<STRAT_ID>."""
        raw_meta = {
            "name": "Test Strategy",
            "invest_size": 400.0,
            "leverage": 20
        }
        with patch.dict(os.environ, {
            "API_KEY_U16_TITAN": "key_titan_16",
            "API_SECRET_U16_TITAN": "sec_titan_16"
        }):
            cfg = StrategyConfig(
                strategy_id="u16_titan",
                raw_meta=raw_meta,
                symbols=["GRASSUSDT"],
                active_settings={}
            )
            key, sec = cfg.get_api_credentials()
            self.assertEqual(key, "key_titan_16")
            self.assertEqual(sec, "sec_titan_16")

    def test_bot_core_multi_account_client_pool(self):
        """Проверка разделения инстансов BinanceClient по стратегиям и символам."""
        from CORE.bot import BotCore
        
        with patch.object(BotCore, "__init__", lambda s: None):
            bot = BotCore()
            bot.is_running = False
            bot.strategy_registry = StrategyRegistry(active_strategies_cfg=self.sample_active_cfg)
            bot.symbols = bot.strategy_registry.get_all_active_symbols()

            # Инициализируем пул клиентов
            with patch.dict(os.environ, {
                "API_KEY_ACCOUNT_1": "key_acc1",
                "API_SECRET_ACCOUNT_1": "sec_acc1",
                "API_KEY_ACCOUNT_2": "key_acc2",
                "API_SECRET_ACCOUNT_2": "sec_acc2"
            }):
                bot.clients = {}
                bot._key_clients = {}
                bot.client = MagicMock(name="default_client")
                bot._init_clients()

                # Проверяем, что создано 2 независимых клиента под два разных аккаунта
                self.assertIn("u6_symbiosis_gamma_fortress35", bot.clients)
                self.assertIn("u16_titan_universal_alpha", bot.clients)
                
                client_u6 = bot.get_client_for_strategy("u6_symbiosis_gamma_fortress35")
                client_u16 = bot.get_client_for_strategy("u16_titan_universal_alpha")
                self.assertNotEqual(client_u6, client_u16)

                # Проверяем get_client_for_symbol
                self.assertEqual(bot.get_client_for_symbol("GRASSUSDT"), client_u6)
                self.assertEqual(bot.get_client_for_symbol("BTCUSDT"), client_u16)

    # ============================================================
    # 2. ТЕСТЫ COIN SELECTION SERVICE
    # ============================================================
    def test_coin_selection_service_config_reading(self):
        """Проверка строго прямого чтения конфига app.json без неявных нулей/дефолтов."""
        service = CoinSelectionService()
        target_count = service.get_configured_target_count()
        selector_type = service.get_configured_selector_type()

        self.assertIsInstance(target_count, int)
        self.assertGreater(target_count, 0)
        self.assertIn(selector_type, ("alpha_coin_selector", "samsonov_funnel"))

    def test_coin_selection_service_alpha_top_coins(self):
        """Проверка работы get_alpha_top_coins с моком AlphaCoinSelector."""
        service = CoinSelectionService()
        mock_candidates = [
            {"symbol": "SOLUSDT", "score": 2.45, "natr": 1.2, "wiggle": 14.5, "move_pct": -2.1},
            {"symbol": "DOGEUSDT", "score": 1.95, "natr": 0.9, "wiggle": 11.2, "move_pct": 1.5},
        ]

        async def run_test():
            with patch("CORE.SELECTOR.alpha_coin_selector.AlphaCoinSelector.select_top_coins", new=AsyncMock(return_value=mock_candidates)):
                res = await service.get_alpha_top_coins(target_count=2)
                self.assertEqual(len(res), 2)
                self.assertEqual(res[0]["symbol"], "SOLUSDT")
                self.assertIn("is_active_in_bot", res[0])

        asyncio.run(run_test())

    def test_coin_selection_service_samsonov_top_coins(self):
        """Проверка работы get_samsonov_top_coins с моком SamsonovFunnelSelector."""
        service = CoinSelectionService()
        mock_funnel_targets = {
            "final_targets": [
                {"symbol": "GRASSUSDT", "trades": 120, "realized_net": 15.5, "peak_net": 18.0, "max_dd": 2.1, "resilience": 8.57},
                {"symbol": "ZROUSDT", "trades": 95, "realized_net": 11.2, "peak_net": 14.0, "max_dd": 1.8, "resilience": 7.78},
            ]
        }

        async def run_test():
            with patch("CORE.coin_selectors.SamsonovFunnelSelector.get_funnel_stages", return_value=mock_funnel_targets):
                res = await service.get_samsonov_top_coins(target_count=2, universe_id="u6_symbiosis_gamma_fortress35")
                self.assertEqual(len(res), 2)
                self.assertEqual(res[0]["symbol"], "GRASSUSDT")
                self.assertEqual(res[0]["score"], 8.57)

        asyncio.run(run_test())

    def test_format_telegram_report(self):
        """Проверка корректного форматирования отчета Telegram."""
        service = CoinSelectionService()
        sample_alpha_results = [
            {"symbol": "GRASSUSDT", "score": 2.512, "natr": 1.34, "wiggle": 15.2, "move_pct": -1.2, "is_active_in_bot": True, "current_strategy": "u6"},
            {"symbol": "NEARUSDT", "score": 1.890, "natr": 0.88, "wiggle": 10.5, "move_pct": 2.4, "is_active_in_bot": False, "current_strategy": None},
        ]
        report = service.format_telegram_report(sample_alpha_results, "alpha_coin_selector", 2)
        self.assertIn("Alpha Coin Selector", report)
        self.assertIn("GRASSUSDT", report)
        self.assertIn("🟢 В бою (u6)", report)
        self.assertIn("⚪ Свободна", report)

    # ============================================================
    # 3. ТЕСТЫ TELEGRAM КЛАВИАТУР
    # ============================================================
    def test_tg_keyboards_have_set_coins_and_selector(self):
        """Проверка наличия кнопок Set Coins и Selector в клавиатурах Telegram."""
        mock_bot_core = MagicMock()
        mock_bot_core.is_paused = False
        with patch("aiogram.Bot"), patch("TG.template_manager.TemplateManager"):
            tg = TelegramReceiver(mock_bot_core)
            main_kb = tg._get_main_keyboard()
            all_main_buttons = [btn.text for row in main_kb.keyboard for btn in row]
            self.assertIn("⚙️ Set Coins", all_main_buttons)
            self.assertIn("🎯 Selector", all_main_buttons)

            set_coins_kb = tg._get_set_coins_keyboard()
            all_set_buttons = [btn.text for row in set_coins_kb.keyboard for btn in row]
            self.assertIn("🎯 Selector", all_set_buttons)
            self.assertIn("➕ Add", all_set_buttons)
            self.assertIn("🗑️ Del", all_set_buttons)


if __name__ == "__main__":
    unittest.main()
