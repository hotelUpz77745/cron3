# ==============================================================================
# Path: tests/test_auto_closer_radar_rotation.py
# Role: Тестирование ротации монет при закрытии итерации (radar_rotation_on_close)
#       по симметричному скору с гистерезисом 35% и инварианту ручного пула
# ==============================================================================

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.radar_navigator import RadarNavigator


class TestAutoCloserRadarRotation(unittest.TestCase):
    """
    Комплексные тесты ротации обоймы по автоклозеру:
    1. Инвариант: ручной пул оператора не перезаписывается при старте.
    2. Симметричный Скор (Score-to-Score) Самсонова с порогом гистерезиса 35%.
    3. Симметричный Альфа-Скор (AlphaCoinSelector) с порогом 35%.
    4. Отклонение ротации при дельте < 35% (защита от чехарды).
    """

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.radar_dir = self.root / "cron3PapperUniverses"
        self.radar_cfg = self.radar_dir / "CFG" / "universes.json"
        self.radar_stats = self.radar_dir / "ANALYTICS" / "STATS"
        self.radar_cfg.parent.mkdir(parents=True, exist_ok=True)
        self.radar_stats.mkdir(parents=True, exist_ok=True)

        self.radar_cfg.write_text(json.dumps({
            "u9_omni_zenith_apex": {"enabled": True, "is_active": True}
        }), encoding="utf-8")

        self.mock_bot = MagicMock()
        self.mock_bot.is_running = True
        self.mock_bot.symbols = ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"]

        self.mock_strat = MagicMock()
        self.mock_strat.symbols = ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"]
        self.mock_strat.auto_closing_cfg = {
            "positive": {
                "enabled": True,
                "mode": "toxicity_adaptive",
                "radar_rotation_on_close": {
                    "enabled": True,
                    "selector_type": "samsonov_funnel",
                    "target_coins_count": 4,
                    "min_score_lead_pct": 35.0,
                    "quant_base_usdt": 10.0,
                    "quant_reference_size": 600.0,
                }
            }
        }
        self.mock_bot.strategy_registry.active_strategies = {
            "u9_omni_zenith_apex": self.mock_strat
        }
        self.mock_bot.delete_symbol = AsyncMock()
        self.mock_bot.add_symbol = AsyncMock()
        self.mock_bot.notifier.send_alert = AsyncMock()
        self.mock_bot.notifier.send_alert_with_keyboard = AsyncMock()

        self.test_cache = self.root / "CACHE"
        self.test_cache.mkdir(parents=True, exist_ok=True)
        self.patch_cache = patch("CORE.radar_journal.CACHE_DIR", self.test_cache)
        self.patch_ledgers = patch("CORE.radar_journal.ANALYTICS_LEDGERS_DIR", self.test_cache)
        self.patch_cache.start()
        self.patch_ledgers.start()

    def tearDown(self):
        self.patch_cache.stop()
        self.patch_ledgers.stop()
        self.temp_dir.cleanup()

    def _create_navigator(self, mode="auto") -> RadarNavigator:
        cfg = {
            "radar_synergy": {
                "enabled": True,
                "mode": mode,
                "selector_type": "samsonov_funnel",
                "target_coins_count": 4,
                "hysteresis": {"min_score_lead_pct": 35.0},
                "rotation_triggers": {"on_stagnation_ratchet": True, "excluded_top_rule": ">5.0%"},
                "advisory": {"timeout_sec": 120, "timeout_action": "skip"}
            },
            "paths": {"radar_dir": str(self.radar_dir)},
            "active_strategies": {
                "u9_omni_zenith_apex": {
                    "enabled": True,
                    "symbols": ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"]
                }
            }
        }
        with patch("CORE.radar_navigator._CFG", cfg):
            return RadarNavigator(self.mock_bot)

    def test_invariant_no_autocomplete_at_start(self):
        """Инвариант: старт бота не меняет монеты оператора и не ждет прогрева."""
        nav = self._create_navigator()
        app_json = self.root / "app.json"
        orig = {"active_strategies": {"u9_omni_zenith_apex": {"symbols": ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"]}}}
        app_json.write_text(json.dumps(orig), encoding="utf-8")

        with patch("CORE.radar_navigator.DATA_DIR", self.root):
            asyncio.run(nav.wait_radar_warmup_and_init())

        res = json.loads(app_json.read_text(encoding="utf-8"))
        self.assertEqual(res["active_strategies"]["u9_omni_zenith_apex"]["symbols"], orig["active_strategies"]["u9_omni_zenith_apex"]["symbols"])

    def test_samsonov_symmetric_score_rotation_above_35_pct(self):
        """Ротация Самсонова: кандидат со скором 28.0 обгоняет худшую монету (20.0) на +40% (>= 35%) -> замена."""
        nav = self._create_navigator(mode="auto")
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 60.0},
            {"symbol": "ZROUSDT", "samsonov_score": 50.0},
            {"symbol": "XPLUSDT", "samsonov_score": 40.0},
            {"symbol": "ARXUSDT", "samsonov_score": 20.0},   # худшая в строю
            {"symbol": "SOLUSDT", "samsonov_score": 28.0},   # кандидат: (28 - 20)/20 = +40% >= 35%
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run():
            await nav.on_ratchet_closed("u9_omni_zenith_apex", matched_rule="<=2.0%", is_stagnation=True)

        asyncio.run(run())
        self.mock_bot.delete_symbol.assert_called_once_with("ARXUSDT")
        self.mock_bot.add_symbol.assert_called_once_with("SOLUSDT", "u9_omni_zenith_apex")

    def test_samsonov_symmetric_score_rotation_rejected_below_35_pct(self):
        """Гистерезис: кандидат со скором 25.0 обгоняет худшую монету (20.0) на +25% (< 35%) -> замена отклонена."""
        nav = self._create_navigator(mode="auto")
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 60.0},
            {"symbol": "ZROUSDT", "samsonov_score": 50.0},
            {"symbol": "XPLUSDT", "samsonov_score": 40.0},
            {"symbol": "ARXUSDT", "samsonov_score": 20.0},   # худшая в строю
            {"symbol": "SOLUSDT", "samsonov_score": 25.0},   # кандидат: +25% < 35%
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run():
            await nav.on_ratchet_closed("u9_omni_zenith_apex", matched_rule="<=2.0%", is_stagnation=True)

        asyncio.run(run())
        self.mock_bot.delete_symbol.assert_not_called()
        self.mock_bot.add_symbol.assert_not_called()

    def test_alpha_selector_rotation_on_close(self):
        """AlphaCoinSelector под куполом: замена слабейшей монеты на лидера при дельте >= 35%."""
        nav = self._create_navigator(mode="auto")
        self.mock_strat.auto_closing_cfg["positive"]["radar_rotation_on_close"]["selector_type"] = "alpha_coin_selector"

        mock_alpha = [
            {"symbol": "GRASSUSDT", "eligible": True, "score": 80.0},
            {"symbol": "ZROUSDT", "eligible": True, "score": 70.0},
            {"symbol": "XPLUSDT", "eligible": True, "score": 60.0},
            {"symbol": "ARXUSDT", "eligible": True, "score": 20.0},   # худшая в строю
            {"symbol": "PUMPUSDT", "eligible": True, "score": 30.0},  # +50% >= 35%
        ]
        with patch.object(nav.alpha_selector, "select_top_coins", new=AsyncMock(return_value=mock_alpha)):
            async def run():
                await nav.on_ratchet_closed("u9_omni_zenith_apex", matched_rule="<=2.0%", is_stagnation=True)

            asyncio.run(run())

        self.mock_bot.delete_symbol.assert_called_once_with("ARXUSDT")
        self.mock_bot.add_symbol.assert_called_once_with("PUMPUSDT", "u9_omni_zenith_apex")

    def test_multi_coin_rotation_two_coins_replaced(self):
        """Мульти-ротация Top-N: 2 монеты выбывают и заменяются двумя лидерами с отрывом >= 35%."""
        nav = self._create_navigator(mode="auto")
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 60.0}, # Сохраняется (в Top-4)
            {"symbol": "ZROUSDT", "samsonov_score": 50.0},   # Сохраняется (в Top-4)
            {"symbol": "PEPEUSDT", "samsonov_score": 45.0},  # Новый кандидат #1 (в Top-4)
            {"symbol": "NEARUSDT", "samsonov_score": 40.0},  # Новый кандидат #2 (в Top-4)
            {"symbol": "XPLUSDT", "samsonov_score": 20.0},   # Выбывает #2 (40 vs 20 = +100% >= 35%)
            {"symbol": "ARXUSDT", "samsonov_score": 10.0},   # Выбывает #1 (45 vs 10 = +350% >= 35%)
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run():
            await nav.on_ratchet_closed("u9_omni_zenith_apex", matched_rule="<=2.0%", is_stagnation=True)

        asyncio.run(run())
        self.mock_bot.delete_symbol.assert_any_call("ARXUSDT")
        self.mock_bot.delete_symbol.assert_any_call("XPLUSDT")
        self.mock_bot.add_symbol.assert_any_call("PEPEUSDT", "u9_omni_zenith_apex")
        self.mock_bot.add_symbol.assert_any_call("NEARUSDT", "u9_omni_zenith_apex")
        self.assertEqual(self.mock_bot.delete_symbol.call_count, 2)
        self.assertEqual(self.mock_bot.add_symbol.call_count, 2)


if __name__ == "__main__":
    unittest.main()
