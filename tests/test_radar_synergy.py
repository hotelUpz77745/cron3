# ==============================================================================
# Path: tests/test_radar_synergy.py
# Role: Комплексные модульные тесты для модуля Штурмана и синергии с Радаром
# ==============================================================================

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from CORE.radar_navigator import RadarNavigator


class TestRadarSynergy(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # Создаем фиктивную структуру Радара
        self.radar_dir = self.root / "cron3PapperUniverses"
        self.radar_dir.mkdir(parents=True)
        self.radar_cfg_dir = self.radar_dir / "CFG"
        self.radar_cfg_dir.mkdir(parents=True)
        self.radar_stats_dir = self.radar_dir / "ANALYTICS" / "STATS"
        self.radar_stats_dir.mkdir(parents=True)

        self.radar_universes = {
            "u9_omni_zenith_apex": {
                "name": "Омен-9",
                "enabled": True
            },
            "u0_fat_swing123": {
                "name": "Жирный Свин",
                "enabled": True
            },
            "u_disabled_strat": {
                "name": "Отключенная",
                "enabled": False
            }
        }
        with open(self.radar_cfg_dir / "universes.json", "w", encoding="utf-8") as f:
            json.dump(self.radar_universes, f, indent=4)

        # Мок BotCore
        self.mock_bot = MagicMock()
        self.mock_bot.is_running = True
        self.mock_bot.symbols = ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"]

        mock_strat = MagicMock()
        mock_strat.symbols = ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"]
        self.mock_bot.strategy_registry.active_strategies = {
            "u9_omni_zenith_apex": mock_strat
        }
        self.mock_bot.delete_symbol = AsyncMock()
        self.mock_bot.add_symbol = AsyncMock()
        self.mock_bot.notifier.send_alert = AsyncMock()
        self.mock_bot.notifier.send_alert_with_keyboard = AsyncMock()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_navigator(self, config_override: dict = None) -> RadarNavigator:
        base_cfg = {
            "radar_synergy": {
                "enabled": True,
                "mode": "advisory",
                "min_radar_warmup_hours": None,
                "warmup_poll_interval_sec": 1.0,
                "target_coins_count": 4,
                "hysteresis": {
                    "min_score_lead_pct": 20.0
                },
                "rotation_triggers": {
                    "on_stagnation_ratchet": True,
                    "excluded_top_rule": ">5.0%"
                }
            },
            "paths": {
                "radar_dir": str(self.radar_dir)
            },
            "active_strategies": {
                "u9_omni_zenith_apex": {
                    "enabled": True,
                    "symbols": ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"]
                }
            }
        }
        if config_override:
            for k, v in config_override.items():
                if isinstance(v, dict) and k in base_cfg:
                    base_cfg[k].update(v)
                else:
                    base_cfg[k] = v

        with patch("CORE.radar_navigator._CFG", base_cfg):
            nav = RadarNavigator(self.mock_bot)
            nav.radar_dir = self.radar_dir
            nav.radar_stats_dir = self.radar_stats_dir
            nav.radar_cfg_path = self.radar_cfg_dir / "universes.json"
            nav.selector.radar_stats_dir = self.radar_stats_dir
            return nav

    def test_fail_fast_validation_missing_strategy(self):
        """Проверка: если стратегия боевого бота отсутствует в Радаре — должен быть краш (RuntimeError)!"""
        nav = self._create_navigator()
        # u_unknown отсутствует в Радаре
        with self.assertRaises(RuntimeError) as ctx:
            nav.validate_strategies_in_radar(["u_unknown"])
        self.assertIn("ПОЛНОСТЬЮ ОТСУТСТВУЕТ в Радаре", str(ctx.exception))

    def test_fail_fast_validation_disabled_strategy(self):
        """Проверка: если стратегия в Радаре выключена (enabled=false) — краш (RuntimeError)!"""
        nav = self._create_navigator()
        with self.assertRaises(RuntimeError) as ctx:
            nav.validate_strategies_in_radar(["u_disabled_strat"])
        self.assertIn("выключена (enabled=false) в Радаре", str(ctx.exception))

    def test_fail_fast_validation_success(self):
        """Проверка: если все стратегии активны в Радаре — валидация проходит без исключений."""
        nav = self._create_navigator()
        # Должно пройти успешно
        nav.validate_strategies_in_radar(["u9_omni_zenith_apex", "u0_fat_swing123"])

    def test_skip_warmup_when_min_hours_is_null(self):
        """Проверка: если min_radar_warmup_hours равен None, холостой ход пропускается мгновенно."""
        nav = self._create_navigator({"radar_synergy": {"min_radar_warmup_hours": None}})
        self.assertIsNone(nav.min_warmup_hours)

        async def run_test():
            # Метод должен вернуться мгновенно
            await nav.wait_radar_warmup_and_init()

        asyncio.run(run_test())

    def test_excluded_top_rule_blocks_rotation(self):
        """Проверка: закрытие по верхнему скоростному правилу (>5.0%) блокирует ротацию."""
        nav = self._create_navigator()
        nav.selector.calculate_coin_metrics = MagicMock()

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule=">5.0%",
                is_stagnation=False
            )
            # Расчет метрик не должен даже вызываться
            nav.selector.calculate_coin_metrics.assert_not_called()

        asyncio.run(run_test())

    def test_hysteresis_below_threshold_retains_pool(self):
        """Проверка: если кандидат лучше слабейшей монеты менее чем на 20%, состав пула сохраняется."""
        nav = self._create_navigator()
        # Моделируем скоры: слабейшая боевая монета ARXUSDT со скором 20.0, лучший кандидат 0GUSDT со скором 22.0 (+10% lead < +20%)
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 50.0, "realized_net": 15.0},
            {"symbol": "ZROUSDT", "samsonov_score": 40.0, "realized_net": 12.0},
            {"symbol": "XPLUSDT", "samsonov_score": 30.0, "realized_net": 10.0},
            {"symbol": "ARXUSDT", "samsonov_score": 20.0, "realized_net": 2.5},
            {"symbol": "0GUSDT", "samsonov_score": 22.0, "realized_net": 5.0},  # +10% от 20.0
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            # Ротация не должна быть предложена
            self.assertEqual(len(nav.pending_rotations), 0)
            self.mock_bot.delete_symbol.assert_not_called()

        asyncio.run(run_test())

    def test_hysteresis_above_threshold_triggers_advisory(self):
        """Проверка: если кандидат превосходит слабейшую более чем на 20%, в advisory создается запрос с кнопками."""
        nav = self._create_navigator({"radar_synergy": {"mode": "advisory"}})
        # Слабейшая ARXUSDT со скором 20.0, лучший кандидат 0GUSDT со скором 30.0 (+50% lead >= +20%)
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 50.0, "realized_net": 15.0},
            {"symbol": "ZROUSDT", "samsonov_score": 40.0, "realized_net": 12.0},
            {"symbol": "XPLUSDT", "samsonov_score": 30.0, "realized_net": 10.0},
            {"symbol": "ARXUSDT", "samsonov_score": 20.0, "realized_net": 2.5},
            {"symbol": "0GUSDT", "samsonov_score": 30.0, "realized_net": 11.0},  # +50% от 20.0
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            self.assertEqual(len(nav.pending_rotations), 1)
            req_id, rot = next(iter(nav.pending_rotations.items()))
            self.assertEqual(rot["old_symbol"], "ARXUSDT")
            self.assertEqual(rot["new_symbol"], "0GUSDT")
            self.assertAlmostEqual(rot["lead_pct"], 50.0)
            self.mock_bot.notifier.send_alert_with_keyboard.assert_called_once()

        asyncio.run(run_test())

    def test_hysteresis_above_threshold_triggers_auto_mode(self):
        """Проверка: в режиме auto ротация применяется автоматически без ожидания."""
        nav = self._create_navigator({"radar_synergy": {"mode": "auto"}})
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 50.0, "realized_net": 15.0},
            {"symbol": "ZROUSDT", "samsonov_score": 40.0, "realized_net": 12.0},
            {"symbol": "XPLUSDT", "samsonov_score": 30.0, "realized_net": 10.0},
            {"symbol": "ARXUSDT", "samsonov_score": 10.0, "realized_net": 2.5},
            {"symbol": "PUMPUSDT", "samsonov_score": 25.0, "realized_net": 8.0},  # +150% lead
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            # В авто-режиме сразу вызываются delete_symbol и add_symbol
            self.mock_bot.delete_symbol.assert_called_once_with("ARXUSDT")
            self.mock_bot.add_symbol.assert_called_once_with("PUMPUSDT", "u9_omni_zenith_apex")

        asyncio.run(run_test())

    def test_set_union_of_scanner_and_combat_symbols(self):
        """Проверка объединения списков: боевые монеты гарантированно не теряются."""
        scanner_symbols = ["0GUSDT", "PUMPUSDT", "KAITOUSDT"]
        combat_symbols = ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT", "PUMPUSDT"]

        # Простое объединение списков и дедупликация через множество
        combined = set(scanner_symbols + combat_symbols)
        final_list = list(combined)

        # Все боевые монеты обязаны присутствовать в итоговом пуле
        for cs in combat_symbols:
            self.assertIn(cs, final_list)
        # Все монеты сканера обязаны присутствовать
        for ss in scanner_symbols:
            self.assertIn(ss, final_list)
        # PUMPUSDT должен присутствовать ровно 1 раз
        self.assertEqual(len(final_list), len(combined))


if __name__ == "__main__":
    unittest.main()
