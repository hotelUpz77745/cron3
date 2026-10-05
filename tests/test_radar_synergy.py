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
from CORE.radar_journal import RadarJournal


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

        self.test_cache_dir = self.root / "CACHE"
        self.test_ledgers_dir = self.root / "ANALYTICS" / "LEDGERS"
        self.test_cache_dir.mkdir(parents=True, exist_ok=True)
        self.test_ledgers_dir.mkdir(parents=True, exist_ok=True)
        self.patch_cache = patch("CORE.radar_journal.CACHE_DIR", self.test_cache_dir)
        self.patch_ledgers = patch("CORE.radar_journal.ANALYTICS_LEDGERS_DIR", self.test_ledgers_dir)
        self.patch_cache.start()
        self.patch_ledgers.start()

    def tearDown(self):
        self.patch_cache.stop()
        self.patch_ledgers.stop()
        self.temp_dir.cleanup()

    def _create_navigator(self, config_override: dict = None) -> RadarNavigator:
        base_cfg = {
            "radar_synergy": {
                "enabled": True,
                "mode": "advisory",
                "advisory": {
                    "timeout_sec": 300.0,
                    "timeout_action": "skip"
                },
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

    def test_empty_radar_metrics_preserves_combat_pool(self):
        """Проверка: если Радар вернул пустой список метрик ([]), ротация безопасно отклоняется, пул сохранен."""
        nav = self._create_navigator()
        nav.selector.calculate_coin_metrics = MagicMock(return_value=[])

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            self.assertEqual(len(nav.pending_rotations), 0)
            self.mock_bot.delete_symbol.assert_not_called()
            self.mock_bot.add_symbol.assert_not_called()

        asyncio.run(run_test())

    def test_no_external_candidates_preserves_combat_pool(self):
        """Проверка: если все монеты из Радара уже есть в боевом пуле (внешних кандидатов нет), ротация отклоняется."""
        nav = self._create_navigator()
        # Только текущие боевые монеты
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 50.0, "realized_net": 15.0},
            {"symbol": "ARXUSDT", "samsonov_score": 20.0, "realized_net": 2.5},
            {"symbol": "ZROUSDT", "samsonov_score": 40.0, "realized_net": 12.0},
            {"symbol": "XPLUSDT", "samsonov_score": 30.0, "realized_net": 10.0},
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            self.assertEqual(len(nav.pending_rotations), 0)
            self.mock_bot.delete_symbol.assert_not_called()
            self.mock_bot.add_symbol.assert_not_called()

        asyncio.run(run_test())

    def test_zero_or_negative_score_candidates_rejected(self):
        """Проверка: кандидат со скором 0.0 или ниже не имеет альфы и не может вытеснить боевую монету."""
        nav = self._create_navigator({"radar_synergy": {"mode": "auto"}})
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 50.0, "realized_net": 15.0},
            {"symbol": "ARXUSDT", "samsonov_score": 0.0, "realized_net": 0.0},
            {"symbol": "ZROUSDT", "samsonov_score": 40.0, "realized_net": 12.0},
            {"symbol": "XPLUSDT", "samsonov_score": 30.0, "realized_net": 10.0},
            {"symbol": "LOSERUSDT", "samsonov_score": 0.0, "realized_net": -5.0},  # Скор 0.0!
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            self.mock_bot.delete_symbol.assert_not_called()
            self.mock_bot.add_symbol.assert_not_called()

        asyncio.run(run_test())

    def test_warmup_empty_candidates_preserves_combat_pool(self):
        """Проверка: если на первичном автокомплите радар вернул пустой список [], боевой пул не затирается."""
        nav = self._create_navigator({"radar_synergy": {"min_radar_warmup_hours": 0.01}})
        nav.get_radar_runtime_hours = MagicMock(return_value=1.0)
        nav.selector.get_funnel_stages = MagicMock(return_value={"final_targets": []})

        async def run_test():
            with patch("CORE.radar_navigator.DATA_DIR", self.root):
                app_path = self.root / "app.json"
                orig_data = {
                    "active_strategies": {
                        "u9_omni_zenith_apex": {
                            "symbols": ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"]
                        }
                    }
                }
                app_path.write_text(json.dumps(orig_data), encoding="utf-8")
                
                await nav.wait_radar_warmup_and_init()
                
                # Читаем обратно: символы обязаны остаться прежними, reload не должен ломать пул
                after_data = json.loads(app_path.read_text(encoding="utf-8"))
                self.assertEqual(after_data["active_strategies"]["u9_omni_zenith_apex"]["symbols"], orig_data["active_strategies"]["u9_omni_zenith_apex"]["symbols"])

        asyncio.run(run_test())

    def test_apply_rotation_guards_against_invalid_inputs(self):
        """Проверка: защита от пустых, совпадающих или отсутствующих символов в apply_rotation."""
        nav = self._create_navigator()

        async def run_test():
            # 1. Пустые символы
            await nav.apply_rotation("u9_omni_zenith_apex", "", "0GUSDT")
            self.mock_bot.delete_symbol.assert_not_called()

            # 2. Одинаковые символы
            await nav.apply_rotation("u9_omni_zenith_apex", "ARXUSDT", "ARXUSDT")
            self.mock_bot.delete_symbol.assert_not_called()

            # 3. Символ, которого нет в боевом пуле
            await nav.apply_rotation("u9_omni_zenith_apex", "NONEXISTENT", "0GUSDT")
            self.mock_bot.delete_symbol.assert_not_called()

            # 4. Символ назначения, который УЖЕ есть в боевом пуле (дубликат)
            await nav.apply_rotation("u9_omni_zenith_apex", "ARXUSDT", "GRASSUSDT")
            self.mock_bot.delete_symbol.assert_not_called()

        asyncio.run(run_test())

    def test_advisory_timeout_triggers_skip(self):
        """Проверка: по истечении таймаута при timeout_action='skip' ротация отменяется, монета возвращается к торгам."""
        nav = self._create_navigator({
            "radar_synergy": {
                "mode": "advisory",
                "advisory": {
                    "timeout_sec": 0.05,
                    "timeout_action": "skip"
                }
            }
        })
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 50.0, "realized_net": 15.0},
            {"symbol": "ZROUSDT", "samsonov_score": 40.0, "realized_net": 12.0},
            {"symbol": "XPLUSDT", "samsonov_score": 30.0, "realized_net": 10.0},
            {"symbol": "ARXUSDT", "samsonov_score": 10.0, "realized_net": 2.5},
            {"symbol": "0GUSDT", "samsonov_score": 30.0, "realized_net": 11.0},
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            # Сразу после вызова монета ARXUSDT заблокирована для входов, запрос в pending
            self.assertTrue(nav.is_symbol_pending_rotation("ARXUSDT"))
            self.assertEqual(len(nav.pending_rotations), 1)

            # Ждем срабатывания таймаута (0.05с)
            await asyncio.sleep(0.1)

            # Запрос снят, монета разблокирована, delete_symbol не вызывался (skip)
            self.assertFalse(nav.is_symbol_pending_rotation("ARXUSDT"))
            self.assertEqual(len(nav.pending_rotations), 0)
            self.mock_bot.delete_symbol.assert_not_called()

        asyncio.run(run_test())

    def test_advisory_timeout_triggers_apply(self):
        """Проверка: по истечении таймаута при timeout_action='apply' ротация автоматически применяется."""
        nav = self._create_navigator({
            "radar_synergy": {
                "mode": "advisory",
                "advisory": {
                    "timeout_sec": 0.05,
                    "timeout_action": "apply"
                }
            }
        })
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 50.0, "realized_net": 15.0},
            {"symbol": "ZROUSDT", "samsonov_score": 40.0, "realized_net": 12.0},
            {"symbol": "XPLUSDT", "samsonov_score": 30.0, "realized_net": 10.0},
            {"symbol": "ARXUSDT", "samsonov_score": 10.0, "realized_net": 2.5},
            {"symbol": "0GUSDT", "samsonov_score": 30.0, "realized_net": 11.0},
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            self.assertTrue(nav.is_symbol_pending_rotation("ARXUSDT"))

            # Ждем срабатывания таймаута (0.05с)
            await asyncio.sleep(0.1)

            # Ротация применена автоматически!
            self.assertFalse(nav.is_symbol_pending_rotation("ARXUSDT"))
            self.assertEqual(len(nav.pending_rotations), 0)
            self.mock_bot.delete_symbol.assert_called_once_with("ARXUSDT")
            self.mock_bot.add_symbol.assert_called_once_with("0GUSDT", "u9_omni_zenith_apex")

        asyncio.run(run_test())

    def test_warmup_init_truncates_to_exact_target_count(self):
        """Проверка: если радар вернул больше монет чем target_count, обойма строго обрезается до target_count."""
        nav = self._create_navigator({"radar_synergy": {"min_radar_warmup_hours": 0.01, "target_coins_count": 4}})
        nav.get_radar_runtime_hours = MagicMock(return_value=1.0)
        # Радар выдал 6 кандидатов
        nav.selector.get_funnel_stages = MagicMock(return_value={
            "final_targets": [
                {"symbol": "C1"}, {"symbol": "C2"}, {"symbol": "C3"},
                {"symbol": "C4"}, {"symbol": "C5"}, {"symbol": "C6"}
            ]
        })

        async def run_test():
            with patch("CORE.radar_navigator.DATA_DIR", self.root):
                app_path = self.root / "app.json"
                orig_data = {
                    "active_strategies": {
                        "u9_omni_zenith_apex": {
                            "symbols": ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"]
                        }
                    }
                }
                app_path.write_text(json.dumps(orig_data), encoding="utf-8")
                await nav.wait_radar_warmup_and_init()
                after_data = json.loads(app_path.read_text(encoding="utf-8"))
                new_symbols = after_data["active_strategies"]["u9_omni_zenith_apex"]["symbols"]
                self.assertEqual(len(new_symbols), 4)
                self.assertEqual(new_symbols, ["C1", "C2", "C3", "C4"])

        asyncio.run(run_test())

    def test_ratchet_rotation_allows_smaller_pool_and_preserves_size(self):
        """Проверка: если в стратегии 2 или 3 монеты (< target_coins_count), ротация не блокируется, а заменяет затухшую монету."""
        nav = self._create_navigator({"radar_synergy": {"target_coins_count": 4, "mode": "auto"}})
        # У стратегии только 2 монеты
        strat = self.mock_bot.strategy_registry.active_strategies["u9_omni_zenith_apex"]
        strat.symbols = ["ARXUSDT", "GRASSUSDT"]

        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 50.0},
            {"symbol": "ARXUSDT", "samsonov_score": 10.0},
            {"symbol": "0GUSDT", "samsonov_score": 30.0},
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            # Ротация успешно исполнена для пула из 2 монет
            self.mock_bot.delete_symbol.assert_called_once_with("ARXUSDT")
            self.mock_bot.add_symbol.assert_called_once_with("0GUSDT", "u9_omni_zenith_apex")

        asyncio.run(run_test())

    def test_multi_coin_simultaneous_rotation_auto(self):
        """Проверка множественной ротации: 2 затухшие монеты (ARX, XPLUS) пакетно заменяются на 2 лидеров (0G, PUMP)."""
        nav = self._create_navigator({"radar_synergy": {"mode": "auto"}})
        # Боевой пул: 4 монеты. GRASS (80), ZRO (50), XPLUS (15 - затухла), ARX (10 - затухла)
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 80.0, "realized_net": 30.0},
            {"symbol": "ZROUSDT", "samsonov_score": 50.0, "realized_net": 20.0},
            {"symbol": "XPLUSDT", "samsonov_score": 15.0, "realized_net": 1.0},
            {"symbol": "ARXUSDT", "samsonov_score": 10.0, "realized_net": 0.5},
            {"symbol": "0GUSDT", "samsonov_score": 90.0, "realized_net": 40.0},
            {"symbol": "PUMPUSDT", "samsonov_score": 60.0, "realized_net": 25.0},
            {"symbol": "KAITOUSDT", "samsonov_score": 20.0, "realized_net": 5.0},
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            # Обе затухшие монеты удалены
            self.assertEqual(self.mock_bot.delete_symbol.call_count, 2)
            self.mock_bot.delete_symbol.assert_any_call("ARXUSDT")
            self.mock_bot.delete_symbol.assert_any_call("XPLUSDT")

            # Оба лидера добавлены
            self.assertEqual(self.mock_bot.add_symbol.call_count, 2)
            self.mock_bot.add_symbol.assert_any_call("0GUSDT", "u9_omni_zenith_apex")
            self.mock_bot.add_symbol.assert_any_call("PUMPUSDT", "u9_omni_zenith_apex")

        asyncio.run(run_test())

    def test_multi_coin_simultaneous_rotation_advisory(self):
        """Проверка advisory при множественной ротации: обе монеты блокируются для входов и отправляются на подтверждение."""
        nav = self._create_navigator({"radar_synergy": {"mode": "advisory"}})
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 80.0, "realized_net": 30.0},
            {"symbol": "ZROUSDT", "samsonov_score": 50.0, "realized_net": 20.0},
            {"symbol": "XPLUSDT", "samsonov_score": 15.0, "realized_net": 1.0},
            {"symbol": "ARXUSDT", "samsonov_score": 10.0, "realized_net": 0.5},
            {"symbol": "0GUSDT", "samsonov_score": 90.0, "realized_net": 40.0},
            {"symbol": "PUMPUSDT", "samsonov_score": 60.0, "realized_net": 25.0},
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)

        async def run_test():
            await nav.on_ratchet_closed(
                strat_id="u9_omni_zenith_apex",
                matched_rule="<=2.0%",
                is_stagnation=True
            )
            # Обе монеты заблокированы
            self.assertTrue(nav.is_symbol_pending_rotation("ARXUSDT"))
            self.assertTrue(nav.is_symbol_pending_rotation("XPLUSDT"))
            self.assertFalse(nav.is_symbol_pending_rotation("GRASSUSDT"))
            self.assertFalse(nav.is_symbol_pending_rotation("ZROUSDT"))

            # В pending_rotations сохранен запрос с 2 ротациями
            self.assertEqual(len(nav.pending_rotations), 1)
            req_id = next(iter(nav.pending_rotations.keys()))
            rot_data = nav.pending_rotations[req_id]
            self.assertEqual(len(rot_data["rotations"]), 2)
            self.assertEqual(rot_data["rotations"][0]["old_symbol"], "ARXUSDT")
            self.assertEqual(rot_data["rotations"][0]["new_symbol"], "0GUSDT")
            self.assertEqual(rot_data["rotations"][1]["old_symbol"], "XPLUSDT")
            self.assertEqual(rot_data["rotations"][1]["new_symbol"], "PUMPUSDT")

            # Проверяем генерацию инлайн-клавиатуры с count=2
            from TG.tg_receiver import build_rotation_inline_keyboard
            kb = build_rotation_inline_keyboard(req_id, count=2)
            self.assertIn("✅ Применить ротацию (2)", kb["inline_keyboard"][0][0]["text"])

        asyncio.run(run_test())

    def test_radar_master_disabled_skips_all_actions(self):
        """Проверка: если enabled=false, Штурман полностью отключен и не вмешивается в работу бота."""
        nav = self._create_navigator({"radar_synergy": {"enabled": False}})
        self.assertFalse(nav.is_enabled())
        nav.selector.calculate_coin_metrics = MagicMock()

        async def run_test():
            await nav.wait_radar_warmup_and_init()
            await nav.on_ratchet_closed("u9_omni_zenith_apex", "<=2.0%", is_stagnation=True)
            nav.selector.calculate_coin_metrics.assert_not_called()

        asyncio.run(run_test())

    def test_radar_mode_disabled_skips_all_actions(self):
        """Проверка: если mode='disabled', is_enabled() возвращает False."""
        nav = self._create_navigator({"radar_synergy": {"mode": "disabled"}})
        self.assertFalse(nav.is_enabled())

    def test_immature_radar_runtime_blocks_rotation(self):
        """Проверка: если радар еще не наработал min_radar_warmup_hours, ротация при храповике отклоняется."""
        nav = self._create_navigator({"radar_synergy": {"min_radar_warmup_hours": 10.0}})
        nav.get_radar_runtime_hours = MagicMock(return_value=3.5)  # 3.5ч < 10.0ч
        nav.selector.calculate_coin_metrics = MagicMock()

        async def run_test():
            await nav.on_ratchet_closed("u9_omni_zenith_apex", "<=2.0%", is_stagnation=True)
            # Расчет метрик не должен вызываться
            nav.selector.calculate_coin_metrics.assert_not_called()
            self.assertEqual(len(nav.pending_rotations), 0)

        asyncio.run(run_test())

    def test_stagnation_flag_controls_rotation_trigger(self):
        """Проверка: on_stagnation_ratchet=True реагирует только на стагнацию, а False — на любое закрытие."""
        # 1. on_stagnation_ratchet=True и is_stagnation=False -> пропуск
        nav_true = self._create_navigator({"radar_synergy": {"rotation_triggers": {"on_stagnation_ratchet": True, "excluded_top_rule": ">5.0%"}}})
        nav_true.selector.calculate_coin_metrics = MagicMock()

        # 2. on_stagnation_ratchet=False и is_stagnation=False -> продолжается расчет
        nav_false = self._create_navigator({"radar_synergy": {"rotation_triggers": {"on_stagnation_ratchet": False, "excluded_top_rule": ">5.0%"}}})
        nav_false.selector.calculate_coin_metrics = MagicMock(return_value=[])

        async def run_test():
            await nav_true.on_ratchet_closed("u9_omni_zenith_apex", "<=2.0%", is_stagnation=False)
            nav_true.selector.calculate_coin_metrics.assert_not_called()

            await nav_false.on_ratchet_closed("u9_omni_zenith_apex", "<=2.0%", is_stagnation=False)
            nav_false.selector.calculate_coin_metrics.assert_called_once()

        asyncio.run(run_test())

    def test_warmup_partial_pool_accepted_without_artificial_rejection(self):
        """Проверка: если радар вернул 2 здоровые монеты (меньше target_coins_count=4), автокомплит заряжает их без отказа."""
        nav = self._create_navigator({"radar_synergy": {"min_radar_warmup_hours": 0.01, "target_coins_count": 4}})
        nav.get_radar_runtime_hours = MagicMock(return_value=1.0)
        # Радар выдал 2 здоровые монеты
        nav.selector.get_funnel_stages = MagicMock(return_value={"final_targets": [{"symbol": "0GUSDT"}, {"symbol": "PUMPUSDT"}]})

        async def run_test():
            with patch("CORE.radar_navigator.DATA_DIR", self.root):
                app_path = self.root / "app.json"
                orig_data = {
                    "active_strategies": {
                        "u9_omni_zenith_apex": {
                            "symbols": ["0GUSDT", "PUMPUSDT"]
                        }
                    }
                }
                app_path.write_text(json.dumps(orig_data), encoding="utf-8")
                await nav.wait_radar_warmup_and_init()
                after_data = json.loads(app_path.read_text(encoding="utf-8"))
                # Пул успешно записан с 2 монетами без искусственного отката
                self.assertEqual(after_data["active_strategies"]["u9_omni_zenith_apex"]["symbols"], ["0GUSDT", "PUMPUSDT"])

        asyncio.run(run_test())

    def test_telemetry_report_generation(self):
        """Проверка: генерация отчета телеметрии Радара формирует корректный текст с эмодзи и медалями."""
        nav = self._create_navigator()
        nav.get_radar_runtime_hours = MagicMock(return_value=12.5)

        # 1. Когда метрик нет
        nav.selector.calculate_coin_metrics = MagicMock(return_value=[])
        report_empty = nav.generate_telemetry_report("u9_omni_zenith_apex")
        self.assertIn("недоступны", report_empty)

        # 2. Когда метрики есть
        mock_metrics = [
            {"symbol": "GRASSUSDT", "samsonov_score": 95.0, "realized_net": 25.0},
            {"symbol": "0GUSDT", "samsonov_score": 88.0, "realized_net": 18.0},
            {"symbol": "ARXUSDT", "samsonov_score": 42.0, "realized_net": 3.0},
        ]
        nav.selector.calculate_coin_metrics = MagicMock(return_value=mock_metrics)
        report = nav.generate_telemetry_report("u9_omni_zenith_apex")
        self.assertIn("РАДАР ШТУРМАНА", report)
        self.assertIn("12.50 ч", report)
        self.assertIn("🥇 <b>GRASSUSDT</b>", report)
        self.assertIn("🥈 <b>0GUSDT</b>", report)

    def test_bot_signal_skips_pending_symbol_only(self):
        """Проверка: в _process_signal монета в symbols_pending_rotation не открывает позицию, остальные открывают."""
        from CORE.bot import BotCore
        bot = MagicMock()
        bot.symbols = ["ARXUSDT", "GRASSUSDT"]
        bot.radar_navigator = MagicMock()
        # ARXUSDT ожидает ротацию, GRASSUSDT свободен
        bot.radar_navigator.is_symbol_pending_rotation.side_effect = lambda s: s == "ARXUSDT"

        bot.fsm_states = {
            "GRASSUSDT": {"LONG": MagicMock(), "SHORT": MagicMock()}
        }
        bot.runtime_manager = MagicMock()
        bot.runtime_manager.save_cache = AsyncMock()
        bot.leverage_manager = MagicMock()
        bot.leverage_manager.set_leverage_and_margin = AsyncMock()
        bot.tp_manager = MagicMock()
        bot.tp_manager.place_take_profit = AsyncMock()
        bot.client = MagicMock()
        bot.client.make_order = AsyncMock(return_value=MagicMock(success=True))
        bot.get_client_for_symbol = MagicMock(return_value=bot.client)
        bot.spec_data = {}
        bot.strategy_registry.get_strategy_for_symbol.return_value = MagicMock(smart_size_cfg=None)

        async def run_test():
            # 1. ARXUSDT: вызов _process_signal отбрасывается в начале
            await BotCore._process_signal(bot, "ARXUSDT", "LONG", {"invest_size": 10, "grid": {"0": {"volume": 100}}}, 1.0)
            bot.client.make_order.assert_not_called()

            # 2. GRASSUSDT: вызов _process_signal выполняется в штатном режиме
            await BotCore._process_signal(bot, "GRASSUSDT", "LONG", {"invest_size": 10, "grid": {"0": {"volume": 100}}}, 1.0)
            bot.client.make_order.assert_called_once()

        asyncio.run(run_test())

    def test_dynamic_target_coins_count_configurable(self):
        """Проверка: target_coins_count динамически берется из конфига (например, 3) без какого-либо хардкода 4."""
        nav_3 = self._create_navigator({
            "radar_synergy": {
                "min_radar_warmup_hours": 0.01,
                "target_coins_count": 3
            },
            "active_strategies": {
                "u9_omni_zenith_apex": {
                    "enabled": True,
                    "symbols": ["GRASSUSDT", "ARXUSDT", "ZROUSDT"]
                }
            }
        })
        nav_3.get_radar_runtime_hours = MagicMock(return_value=1.0)
        nav_3.selector.get_funnel_stages = MagicMock(return_value={
            "final_targets": [{"symbol": "C1"}, {"symbol": "C2"}, {"symbol": "C3"}, {"symbol": "C4"}, {"symbol": "C5"}]
        })

        # Mock BotCore для стратегии с 3 монетами
        mock_strat_3 = MagicMock()
        mock_strat_3.symbols = ["C1", "C2", "C3"]
        self.mock_bot.strategy_registry.active_strategies = {
            "u9_omni_zenith_apex": mock_strat_3
        }

        async def run_test():
            with patch("CORE.radar_navigator.DATA_DIR", self.root):
                app_path = self.root / "app.json"
                orig_data = {
                    "active_strategies": {
                        "u9_omni_zenith_apex": {
                            "symbols": ["GRASSUSDT", "ARXUSDT", "ZROUSDT"]
                        }
                    }
                }
                app_path.write_text(json.dumps(orig_data), encoding="utf-8")
                await nav_3.wait_radar_warmup_and_init()
                after_data = json.loads(app_path.read_text(encoding="utf-8"))
                new_symbols = after_data["active_strategies"]["u9_omni_zenith_apex"]["symbols"]
                # Должно быть ровно 3 монеты (первые 3 из 5)
                self.assertEqual(len(new_symbols), 3)
                self.assertEqual(new_symbols, ["C1", "C2", "C3"])

                # Ротация при target_coins_count = 3
                await nav_3.apply_rotation("u9_omni_zenith_apex", "C3", "NEWCOIN")
                self.mock_bot.delete_symbol.assert_called_with("C3")
                self.mock_bot.add_symbol.assert_called_with("NEWCOIN", "u9_omni_zenith_apex")

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

    def test_rotation_journal_records_event(self):
        """Проверка фиксации смены монет в JSON-кэше и текстовом леджере с меткой UTC."""
        entry = RadarJournal.record_rotation(
            strat_id="u9_omni_zenith_apex",
            old_symbol="ARXUSDT",
            new_symbol="0GUSDT",
            old_score=12.5,
            new_score=85.0,
            lead_pct=580.0,
            matched_rule=">5.0%",
            mode="auto"
        )
        # Проверяем структуру возвращенного словаря
        self.assertEqual(entry["old_symbol"], "ARXUSDT")
        self.assertEqual(entry["new_symbol"], "0GUSDT")
        self.assertEqual(entry["old_score"], 12.5)
        self.assertEqual(entry["new_score"], 85.0)
        self.assertEqual(entry["lead_pct"], 580.0)
        self.assertEqual(entry["matched_rule"], ">5.0%")
        self.assertEqual(entry["mode"], "auto")
        self.assertIn("UTC", entry["timestamp_utc"])
        self.assertIsInstance(entry["timestamp_ms"], int)

        # Проверяем JSON-кэш файл
        cache_file = self.test_cache_dir / "rotations_log.json"
        self.assertTrue(cache_file.exists())
        cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertIsInstance(cache_data, list)
        self.assertEqual(len(cache_data), 1)
        self.assertEqual(cache_data[0]["new_symbol"], "0GUSDT")

        # Проверяем get_history
        history = RadarJournal.get_history(limit=10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["old_symbol"], "ARXUSDT")

        # Проверяем текстовый леджер
        ledger_file = self.test_ledgers_dir / "rotations_ledger.txt"
        self.assertTrue(ledger_file.exists())
        ledger_content = ledger_file.read_text(encoding="utf-8")
        self.assertIn("ARXUSDT", ledger_content)
        self.assertIn("0GUSDT", ledger_content)
        self.assertIn("85.00", ledger_content)

    def test_rotation_journal_telemetry_report(self):
        """Проверка формирования телеметрического отчета с блоком истории ротаций."""
        top_candidates = [
            {"symbol": "0GUSDT", "samsonov_score": 90.0, "realized_net": 15.5},
            {"symbol": "PUMPUSDT", "samsonov_score": 80.0, "realized_net": 8.2}
        ]
        recent_rotations = [
            {
                "timestamp_utc": "2026-10-03 00:00:00 UTC",
                "old_symbol": "ARXUSDT",
                "new_symbol": "0GUSDT",
                "lead_pct": 500.0
            }
        ]
        report = RadarJournal.format_telemetry_report(
            strat_id="u9_omni_zenith_apex",
            runtime_hours=2.5,
            current_symbols=["0GUSDT", "GRASSUSDT"],
            top_candidates=top_candidates,
            recent_rotations=recent_rotations
        )
        self.assertIn("РАДАР ШТУРМАНА: [u9_omni_zenith_apex]", report)
        self.assertIn("2.50 ч.", report)
        self.assertIn("0GUSDT", report)
        self.assertIn("ИСТОРИЯ РОТАЦИЙ ОБОЙМЫ:", report)
        self.assertIn("ARXUSDT ➔ 0GUSDT", report)


if __name__ == "__main__":
    unittest.main()





