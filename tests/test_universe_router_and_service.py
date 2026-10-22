# tests/test_universe_router_and_service.py
# -*- coding: utf-8 -*-
"""
==============================================================================
Path: tests/test_universe_router_and_service.py
Role: Тесты UniverseRouter, UniverseSessionManager и AnalyticsReportService
==============================================================================
"""

import unittest
from unittest.mock import MagicMock
from pathlib import Path

from ANALYTICS.report_service import AnalyticsReportService
from TG.HANDLERS.universe_router import UniverseSessionManager, get_universe_router
from TG.universe_commands import (
    get_user_universe_id,
    set_user_universe_id,
    render_universes_page,
    render_strategy_control_panel,
)


class TestUniverseSessionManager(unittest.TestCase):
    def setUp(self):
        self.mgr = UniverseSessionManager(default_universe_id="u17_default")

    def test_default_fallback(self):
        """Без выбора и без strategy_registry возвращается дефолтный ID."""
        self.assertEqual(self.mgr.get_user_universe_id(12345), "u17_default")

    def test_set_and_get_user_universe(self):
        """Явная установка сохраняет контекст пользователя."""
        self.mgr.set_user_universe_id(12345, "u99_custom")
        self.assertEqual(self.mgr.get_user_universe_id(12345), "u99_custom")
        # Другой пользователь получает дефолт
        self.assertEqual(self.mgr.get_user_universe_id(67890), "u17_default")

    def test_get_from_strategy_registry_active(self):
        """Если в registry есть активные стратегии, берется первая."""
        bot_core = MagicMock()
        bot_core.strategy_registry.get_active_strategies.return_value = {
            "u16_chita": MagicMock(),
            "u17_stephenson": MagicMock(),
        }
        uid = self.mgr.get_user_universe_id(99999, bot_core=bot_core)
        self.assertEqual(uid, "u16_chita")


class TestUniverseAnalyticsReportService(unittest.TestCase):
    def setUp(self):
        self.svc = AnalyticsReportService()

    def test_render_universes_list(self):
        """Тест генерации списка вселенных с тумблерами переключения [ВКЛ / ВЫКЛ]."""
        active_strats = {
            "u1": {"name": "Alpha", "enabled": True, "mode": "classic"},
            "u2": {"name": "Beta", "enabled": False, "mode": "toxic"},
        }
        text, buttons = self.svc.render_universes_list(
            current_uid="u1",
            active_strats=active_strats,
            page=1,
            page_size=6,
        )
        self.assertIn("Управление стратегиями (куполами)", text)
        self.assertIn("Alpha", text)
        self.assertIn("Beta", text)
        self.assertIn("ВКЛЮЧЕНА", text)
        self.assertIn("ОТКЛЮЧЕНА", text)

        # Проверяем наличие кнопок-тумблеров
        callback_datas = [btn["callback_data"] for row in buttons for btn in row]
        self.assertIn("toggle_univ:u1:1", callback_datas)
        self.assertIn("toggle_univ:u2:1", callback_datas)
        self.assertIn("univ_page:1", callback_datas)

    def test_render_universe_control_panel(self):
        """Тест перенаправления устаревшей панели купола на тумблерный список."""
        s_cfg = {
            "mode": "toxic",
            "LONG": {"invest_size": 250.0},
            "symbols": ["BTCUSDT", "ETHUSDT"],
        }
        text, buttons = self.svc.render_universe_control_panel(
            uid="u17_test",
            s_cfg=s_cfg,
            page=1,
        )
        self.assertIn("Управление стратегиями (куполами)", text)
        self.assertIn("u17_test", text)
        callback_datas = [btn["callback_data"] for row in buttons for btn in row]
        self.assertTrue(any(cb.startswith("toggle_univ:") for cb in callback_datas))


class TestUniverseRouterAndFacade(unittest.TestCase):
    def test_get_universe_router(self):
        """Фабрика роутера создает aiogram 3 Router с именем universe_router."""
        bot_core = MagicMock()
        router = get_universe_router(bot_core)
        self.assertEqual(router.name, "universe_router")

    def test_facade_get_set_user_universe(self):
        """Фасадные функции в TG/universe_commands.py работают без ошибок."""
        set_user_universe_id(777, "u_test_facade")
        self.assertEqual(get_user_universe_id(777), "u_test_facade")

    def test_facade_render_functions(self):
        """render_universes_page и render_strategy_control_panel возвращают кортеж (str, InlineKeyboardMarkup)."""
        bot_core = MagicMock()
        text, kb = render_universes_page(user_id=101, bot_core=bot_core, page=1)
        self.assertIn("Управление стратегиями (куполами)", text)
        self.assertIsNotNone(kb.inline_keyboard)

        text2, kb2 = render_strategy_control_panel(user_id=101, bot_core=bot_core, uid="u17_stephensonE", page=1)
        self.assertIn("Управление стратегиями (куполами)", text2)
        self.assertIsNotNone(kb2.inline_keyboard)


if __name__ == "__main__":
    unittest.main()
