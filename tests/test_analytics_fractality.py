import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from TG.tg_receiver import TelegramReceiver


class TestAnalyticsFractality(unittest.TestCase):
    def setUp(self):
        mock_bot = MagicMock()
        mock_bot.symbols = ['BTCUSDT', 'ETHUSDT']
        self.receiver = TelegramReceiver(mock_bot)

    def test_main_keyboard_no_leaderboard(self):
        main_kb = self.receiver._get_main_keyboard()
        kb_texts = [btn.text for row in main_kb.keyboard for btn in row]
        self.assertNotIn('🏆 Leaderboard', kb_texts)

    def test_big_board_fractal_portfolio_tools(self):
        bb_text, bb_kb = self.receiver._render_analytics_big_board(12345)
        bb_buttons = [btn.text for row in bb_kb.inline_keyboard for btn in row]
        bb_cbs = [btn.callback_data for row in bb_kb.inline_keyboard for btn in row]

        # No close button
        self.assertFalse(any('закрыть' in b.lower() for b in bb_buttons))
        # Big Board has exactly 3 buttons: Dome Analytics, Switch Dome, Leaderboard
        self.assertEqual(len(bb_cbs), 3)
        self.assertTrue(any(cb.startswith('analytics_dome:') for cb in bb_cbs))
        self.assertIn('univ_page:1', bb_cbs)
        self.assertIn('analytics_bb_leaderboard', bb_cbs)
        # Redundant buttons removed from Big Board
        self.assertNotIn('analytics_bb_global', bb_cbs)
        self.assertNotIn('analytics_bb_ranking', bb_cbs)
        self.assertNotIn('analytics_bb_coins', bb_cbs)
        self.assertNotIn('analytics_bb_refresh', bb_cbs)

    def test_portfolio_data_aggregation(self):
        summary, per_coin, last_trade = self.receiver._get_portfolio_all_analytics_data()
        self.assertIn('start_balance_usdt', summary)
        self.assertIn('cur_balance_usdt', summary)
        self.assertIn('net_profit_usdt', summary)
        self.assertIsInstance(per_coin, dict)

    def test_abs_recovery_factor_and_no_legacy_metrics(self):
        from ANALYTICS.metrics import AnalyticsMathEngine
        sample_data = {
            "start_balance_usdt": 100.0,
            "net_profit_usdt": 50.0,
            "max_drawdown_usdt": -10.0,
            "invest_size": 100.0,
            "per_coin": {
                "BTCUSDT": {
                    "total_trades": 5,
                    "winning_trades": 4,
                    "net_profit_usdt": 50.0,
                    "peak_net_profit_usdt": 100.0,
                    "max_drawdown": -20.0,
                    "max_drawdown_net_usdt": -20.0,
                    "first_trade_ts": 0,
                    "epoch_state": {"size": 100.0},
                    "DRME": 0.5,
                    "MDME": 0.2
                }
            }
        }
        AnalyticsMathEngine.calculate(sample_data)
        btc = sample_data["per_coin"]["BTCUSDT"]
        self.assertEqual(btc["recovery_factor"], 1.0)  # net 50 / abs_dd 50
        self.assertEqual(btc["abs_recovery_factor"], 2.0)  # peak 100 / abs_dd 50
        self.assertNotIn("DRME", btc)
        self.assertNotIn("MDME", btc)
        self.assertNotIn("epoch_state", btc)

    def test_universe_analytics_menu_structure(self):
        """Проверяет объединенное меню аналитики вселенной: Рейтинг наверху, Лидерборд и Смена вселенной на месте, нет Global Analytics."""
        text, kb = self.receiver._render_universe_analytics(12345)
        self.assertIn("АНАЛИТИКА КУПОЛА СТРАТЕГИИ", text)

        all_buttons = [btn.text for row in kb.inline_keyboard for btn in row]
        all_cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]

        # 1. Первая кнопка на самом верху - Рейтинг монет
        self.assertIn("Рейтинг монет", kb.inline_keyboard[0][0].text)
        self.assertTrue(kb.inline_keyboard[0][0].callback_data.startswith("analytics_ranking:"))

        # 2. Кнопка смены вселенной и лидерборд присутствуют
        self.assertIn("univ_page:1", all_cbs)
        self.assertIn("analytics_bb_leaderboard", all_cbs)

        # 3. Лишние кнопки убраны (Global Analytics, переход во вселенную, возврат к бигборду)
        self.assertFalse(any("global" in cb.lower() for cb in all_cbs))
        self.assertFalse(any(cb.startswith("analytics_dome:") for cb in all_cbs))
        self.assertFalse(any("биг борд" in btn.lower() for btn in all_buttons))

        # 4. Основные разделы аналитики доступны
        self.assertTrue(any("монетам" in btn.lower() for btn in all_buttons))
        self.assertTrue(any("эквити" in btn.lower() for btn in all_buttons))
        self.assertTrue(any("лента сделок" in btn.lower() for btn in all_buttons))
        self.assertTrue(any("отчет" in btn.lower() for btn in all_buttons))


if __name__ == '__main__':
    unittest.main()
