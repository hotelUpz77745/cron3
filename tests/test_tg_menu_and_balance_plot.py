# ==============================================================================
# Path: tests/test_tg_menu_and_balance_plot.py
# Role: Комплексные юнит-тесты новой структуры меню Telegram, двухшагового Set Coins,
#       валидации тикеров (блокировка китайских иероглифов), переключения вселенных
#       и генерации графиков движения баланса (от стартового с учетом Net Profit).
# ==============================================================================

import csv
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import CallbackQuery, Message, User, Chat

import ANALYTICS.plotter as plotter
from TG.tg_receiver import TelegramReceiver, TGStates
from TG.universe_commands import (
    get_user_universe_id,
    set_user_universe_id,
    render_universes_page,
    setup_universe_handlers
)


class TestTGMenuAndBalancePlot(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cron3_menu_plot_test_")
        self.orig_images_dir = plotter.IMAGES_DIR
        self.test_images_dir = Path(self.temp_dir) / "images"
        self.test_images_dir.mkdir(parents=True, exist_ok=True)
        plotter.IMAGES_DIR = self.test_images_dir

        self.mock_bot_core = MagicMock()
        self.mock_bot_core.is_paused = False
        self.mock_bot_core.symbols = ["NEARUSDT", "PUMPUSDT", "GRASSUSDT"]
        self.mock_bot_core.analytics = MagicMock()
        self.mock_bot_core.analytics.leaderboard_engine = None

        strat1 = MagicMock()
        strat1.name = "Omni Zenith Apex"
        strat1.symbols = ["NEARUSDT", "PUMPUSDT"]
        strat1.is_active = True
        strat1.raw_meta = {"name": "Omni Zenith Apex", "invest_size": 10.0, "leverage": 10}

        strat2 = MagicMock()
        strat2.name = "Gamma Fortress 35"
        strat2.symbols = ["GRASSUSDT"]
        strat2.is_active = True
        strat2.raw_meta = {"name": "Gamma Fortress 35", "invest_size": 15.0, "leverage": 5}

        self.mock_registry = MagicMock()
        self.mock_registry.active_strategies = {
            "u96_omni_zenith_apex": strat1,
            "u6_symbiosis_gamma_fortress35": strat2
        }
        self.mock_registry.get_active_strategies.return_value = self.mock_registry.active_strategies
        self.mock_registry.get_all_active_symbols.return_value = ["NEARUSDT", "PUMPUSDT", "GRASSUSDT"]
        self.mock_bot_core.strategy_registry = self.mock_registry

        self.mock_bot = MagicMock()
        with patch("aiogram.Bot", return_value=self.mock_bot):
            self.tg = TelegramReceiver(self.mock_bot_core)

    def tearDown(self):
        plotter.IMAGES_DIR = self.orig_images_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # 1. ТЕСТЫ ГЛАВНОГО МЕНЮ (4 ряда, 10 кнопок)
    # =========================================================================
    def test_main_keyboard_layout_and_buttons(self):
        """Проверяет точную 4-рядную структуру главного меню (10 кнопок) с Risk System и Set Universes."""
        kb = self.tg._get_main_keyboard()
        rows = kb.keyboard

        self.assertEqual(len(rows), 4, "Главное меню должно содержать ровно 4 строки кнопок")

        row1_texts = [btn.text for btn in rows[0]]
        self.assertEqual(row1_texts, ["▶️ Start", "ℹ️ Status", "⏸️ Stop"])

        row2_texts = [btn.text for btn in rows[1]]
        self.assertEqual(row2_texts, ["🏆 Leaderboard", "🌌 Set Universes", "📊 Analytics"])

        row3_texts = [btn.text for btn in rows[2]]
        self.assertEqual(row3_texts, ["⚙️ Set Coins", "🛡️ Risk System", "📜 Logs"])

        row4_texts = [btn.text for btn in rows[3]]
        self.assertEqual(row4_texts, ["🚨 Close All"])

        all_buttons = [btn.text for r in rows for btn in r]
        self.assertEqual(len(all_buttons), 10, "Всего в главном меню должно быть ровно 10 кнопок")

    # =========================================================================
    # 2. ТЕСТЫ МЕНЮ SET COINS (Ровно 3 кнопки ReplyKeyboardMarkup)
    # =========================================================================
    def test_set_coins_keyboard_buttons(self):
        """Проверяет клавиатуру управления монетами: ровно 3 кнопки [Добавить] [Удалить] / [Назад]."""
        kb = self.tg._get_set_coins_keyboard()
        rows = kb.keyboard

        self.assertEqual(len(rows), 2)
        row1_texts = [btn.text for btn in rows[0]]
        self.assertEqual(row1_texts, ["➕ Добавить монету", "🗑️ Удалить монету"])

        row2_texts = [btn.text for btn in rows[1]]
        self.assertEqual(row2_texts, ["🔙 Назад"])

        all_buttons = [btn.text for r in rows for btn in r]
        self.assertEqual(len(all_buttons), 3)

    # =========================================================================
    # 3. ТЕСТ ВАЛИДАЦИИ СИМВОЛОВ (Зарубание китайских иероглифов и не-USDT)
    # =========================================================================
    def test_symbol_regex_validation(self):
        """Проверяет строгость регулярного выражения для тикеров: блокировка китайских иероглифов и мусора."""
        symbol_pattern = re.compile(r"^[A-Z0-9]+USDT$")

        # Допустимые валидные символы
        valid_symbols = ["BTCUSDT", "ETHUSDT", "NEARUSDT", "PUMPUSDT", "1000PEPEUSDT", "0GUSDT", "SOLUSDT"]
        for sym in valid_symbols:
            self.assertTrue(bool(symbol_pattern.match(sym)), f"Символ {sym} должен быть валидным")

        # Невалидные: китайские иероглифы
        chinese_symbols = ["龙虾USDT", "龙虾", "BTC龙虾USDT", "九USDT", "你好USDT"]
        for sym in chinese_symbols:
            self.assertFalse(bool(symbol_pattern.match(sym)), f"Символ с иероглифами {sym} обязан отсекаться")

        # Невалидные: не USDT, спецсимволы, пробелы, кириллица
        invalid_symbols = ["BTCBUSD", "BTC", "BTC-USDT", "ETH_USDT", "BTC USDT", "БИТКОИНUSDT", "PEPE!USDT"]
        for sym in invalid_symbols:
            self.assertFalse(bool(symbol_pattern.match(sym)), f"Невалидный символ {sym} обязан отсекаться")

    # =========================================================================
    # 4. ТЕСТЫ ГЕНЕРАЦИИ ГРАФИКА ДВИЖЕНИЯ БАЛАНСА (plotter.py)
    # =========================================================================
    def test_generate_balance_curve_with_trades(self):
        """Проверяет генерацию графика движения баланса монеты от стартового с учетом Net Profit по сделкам."""
        if not plotter.MATPLOTLIB_AVAILABLE:
            self.skipTest("matplotlib не установлен")

        # Создаем тестовый trades_ledger.txt с несколькими сделками по монете NEARUSDT
        csv_file = Path(self.temp_dir) / "trades_ledger.txt"
        with open(csv_file, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["Symbol", "Side", "Open Time", "Close Time", "PnL", "Balance"])
            writer.writerow(["NEARUSDT", "LONG", "2026-10-07 10:00:00", "2026-10-07 11:00:00", "1.50", "58.50"])
            writer.writerow(["BTCUSDT", "LONG", "2026-10-07 11:30:00", "2026-10-07 12:00:00", "0.80", "59.30"])
            writer.writerow(["NEARUSDT", "SHORT", "2026-10-07 12:30:00", "2026-10-07 13:00:00", "-0.50", "58.80"])
            writer.writerow(["NEARUSDT", "LONG", "2026-10-07 13:30:00", "2026-10-07 14:00:00", "2.00", "60.80"])

        # Создаем analytics.json со стартовым балансом 57.0 USDT
        json_file = Path(self.temp_dir) / "analytics.json"
        json_data = {
            "start_balance_usdt": 57.0,
            "cur_balance_usdt": 60.80,
            "per_coin": {
                "NEARUSDT": {
                    "net_profit_usdt": 3.00,
                    "trades": 3
                }
            }
        }
        json_file.write_text(json.dumps(json_data), encoding="utf-8")

        with patch("ANALYTICS.plotter._resolve_paths", return_value=(json_file, csv_file)):
            plot_path = plotter.generate_balance_curve("NEARUSDT")

            self.assertTrue(os.path.exists(plot_path), f"Файл графика {plot_path} должен существовать")
            self.assertTrue(plot_path.endswith(".png"))
            self.assertGreater(os.path.getsize(plot_path), 1000, "Файл графика не должен быть пустым")

    def test_generate_coin_analytics_delegates_to_balance_curve(self):
        """Проверяет, что вызов generate_coin_analytics возвращает график движения баланса."""
        if not plotter.MATPLOTLIB_AVAILABLE:
            self.skipTest("matplotlib не установлен")

        json_file = Path(self.temp_dir) / "analytics_empty.json"
        json_file.write_text(json.dumps({"start_balance_usdt": 100.0, "per_coin": {"ETHUSDT": {"net_profit_usdt": 5.0}}}), encoding="utf-8")
        csv_file = Path(self.temp_dir) / "empty_ledger.txt"

        with patch("ANALYTICS.plotter._resolve_paths", return_value=(json_file, csv_file)):
            plot_path = plotter.generate_coin_analytics("ETHUSDT")
            self.assertTrue(os.path.exists(plot_path))
            self.assertIn("balance_ethusdt", plot_path.lower())

    def test_generate_equity_curve_creates_file(self):
        """Проверяет генерацию глобальной кривой баланса (generate_equity_curve)."""
        if not plotter.MATPLOTLIB_AVAILABLE:
            self.skipTest("matplotlib не установлен")

        json_file = Path(self.temp_dir) / "analytics.json"
        json_file.write_text(json.dumps({"start_balance_usdt": 50.0}), encoding="utf-8")
        csv_file = Path(self.temp_dir) / "trades_ledger.txt"
        with open(csv_file, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["Symbol", "Side", "Open Time", "Close Time", "PnL", "Balance"])
            writer.writerow(["NEARUSDT", "LONG", "2026-10-07 10:00:00", "2026-10-07 11:00:00", "5.00", "55.00"])

        with patch("ANALYTICS.plotter._resolve_paths", return_value=(json_file, csv_file)):
            equity_path = plotter.generate_equity_curve(universe_id="u96_omni_zenith_apex")
            self.assertTrue(os.path.exists(equity_path))
            self.assertTrue(equity_path.endswith(".png"))

    # =========================================================================
    # 5. ТЕСТЫ КОМАНД ВСЕЛЕННЫХ И ПЕРЕКЛЮЧЕНИЯ (TG/universe_commands.py)
    # =========================================================================
    def test_user_universe_tracking_and_switching(self):
        """Проверяет сохранение и смену активной вселенной для пользователя."""
        user_id = 998877

        # По умолчанию если не установлена
        default_u = get_user_universe_id(user_id, self.mock_bot_core)
        self.assertIn(default_u, ["u96_omni_zenith_apex", "u6_symbiosis_gamma_fortress35"])

        # Смена вселенной
        set_user_universe_id(user_id, "u6_symbiosis_gamma_fortress35")
        current_u = get_user_universe_id(user_id, self.mock_bot_core)
        self.assertEqual(current_u, "u6_symbiosis_gamma_fortress35")

    def test_render_universes_page_pagination(self):
        """Проверяет пагинацию списка вселенных в render_universes_page."""
        user_id = 112233
        set_user_universe_id(user_id, "u96_omni_zenith_apex")
        text, kb = render_universes_page(
            user_id=user_id,
            bot_core=self.mock_bot_core,
            page=1
        )
        self.assertIn("СПИСОК ВСЕЛЕННЫХ", text)
        self.assertIsNotNone(kb)

        # Проверяем наличие кнопок навигации и кнопок вселенных
        cb_data_list = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]
        self.assertTrue(any(cb.startswith("use_univ:") for cb in cb_data_list))
        self.assertIn("analytics_back_to_univ", cb_data_list)

    # =========================================================================
    # 6. ТЕСТЫ СВЯЗКИ TG_RECEIVER И ВСЕЛЕННЫХ
    # =========================================================================
    def test_get_universe_analytics_resolution(self):
        """Проверяет вспомогательный метод _get_universe_analytics для текущего пользователя."""
        user_id = 12345
        set_user_universe_id(user_id, "u96_omni_zenith_apex")

        uid, uname, apath, lpath = self.tg._get_universe_analytics(user_id)
        self.assertEqual(uid, "u96_omni_zenith_apex")
        self.assertIn("Omni Zenith Apex", uname)
        self.assertTrue(str(apath).endswith(".json"))
        self.assertTrue(str(lpath).endswith(".txt"))


if __name__ == "__main__":
    unittest.main()
