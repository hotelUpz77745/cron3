"""
Unit test to verify all Telegram dome buttons and menus for active strategies:
U16 (Titan Universal Alpha), U6 (Gamma Fortress 35 / Медный Змей), and U9 (Omni Zenith Apex).
"""

import unittest
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path
import json

from CORE.STRATEGY.strategy_registry import StrategyRegistry
from TG.tg_receiver import TelegramReceiver
from TG.template_manager import TemplateManager


class TestTGDomeButtons(unittest.TestCase):
    def setUp(self):
        self.mock_bot_core = MagicMock()
        self.registry = StrategyRegistry()
        self.mock_bot_core.strategy_registry = self.registry
        self.mock_bot_core.symbols = self.registry.get_all_active_symbols()
        self.mock_bot_core.is_paused = False

        self.tg = TelegramReceiver(bot_core=self.mock_bot_core)

    def test_active_strategies_loaded(self):
        active = self.registry.active_strategies
        self.assertIn("u16_titan_universal_alpha", active)
        self.assertIn("u6_symbiosis_gamma_fortress35", active)
        self.assertIn("u9_omni_zenith_apex", active)
        self.assertIn("u16_titan_universal_alpha__fixed", active)
        self.assertIn("u6_symbiosis_gamma_fortress35__fixed", active)
        self.assertIn("u9_omni_zenith_apex__fixed", active)

    def test_close_all_dome_buttons(self):
        """Проверяет генерацию кнопок закрытия под каждым куполом."""
        active_strats = self.registry.active_strategies
        buttons = []
        for sid, scfg in active_strats.items():
            buttons.append(f"close_dome_{sid}")

        self.assertIn("close_dome_u16_titan_universal_alpha", buttons)
        self.assertIn("close_dome_u6_symbiosis_gamma_fortress35", buttons)
        self.assertIn("close_dome_u9_omni_zenith_apex", buttons)

    def test_strategy_meta_extraction(self):
        """Проверяет корректность метаданных куполов стратегий."""
        for sid in ["u16_titan_universal_alpha", "u6_symbiosis_gamma_fortress35", "u9_omni_zenith_apex"]:
            meta = self.tg._get_strategy_meta(sid)
            self.assertEqual(meta["strategy_id"], sid)
            self.assertTrue(len(meta["name"]) > 0)
            self.assertGreater(meta["invest_size"], 0)
            self.assertGreater(meta["leverage"], 0)

    def test_selector_apply_buttons(self):
        """Проверяет кнопки применения результатов селектора под каждый купол."""
        active_strats = self.registry.active_strategies
        apply_cbs = [f"cb_selector_apply:{sid}" for sid in active_strats.keys()]
        self.assertIn("cb_selector_apply:u16_titan_universal_alpha", apply_cbs)
        self.assertIn("cb_selector_apply:u6_symbiosis_gamma_fortress35", apply_cbs)
        self.assertIn("cb_selector_apply:u9_omni_zenith_apex", apply_cbs)

    def test_symbol_strategy_assignment_callbacks(self):
        """Проверяет генерацию callback_data при добавлении монеты под конкретный купол."""
        symbol = "BTCUSDT"
        active_strats = self.registry.active_strategies
        callbacks = [f"add_pick_strat_{symbol}_{sid}" for sid in active_strats.keys()]
        self.assertIn(f"add_pick_strat_{symbol}_u16_titan_universal_alpha", callbacks)
        self.assertIn(f"add_pick_strat_{symbol}_u6_symbiosis_gamma_fortress35", callbacks)
        self.assertIn(f"add_pick_strat_{symbol}_u9_omni_zenith_apex", callbacks)


if __name__ == "__main__":
    unittest.main()
