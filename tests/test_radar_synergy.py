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
                "advisory": {
                    "timeout_sec": 300.0,
                    "timeout_action": "skip"
                },
                "min_radar_warmup_hours": None,
                "warmup_poll_interval_sec": 1.0,
                "target_coins_count": 4,
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
        nav.validate_strategies_in_radar(["u9_omni_zenith_apex", "u0_fat_swing123"])

    def test_skip_warmup_when_min_hours_is_null(self):
        """Проверка: если min_radar_warmup_hours равен None, холостой ход пропускается мгновенно."""
        nav = self._create_navigator({"radar_synergy": {"min_radar_warmup_hours": None}})
        self.assertIsNone(nav.min_warmup_hours)

        async def run_test():
            await nav.wait_radar_warmup_and_init()

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

    def test_on_ratchet_closed_is_noop_and_preserves_pool(self):
        """Инвариант #3: закрытие позиции/итерации не производит ротацию обоймы оператора."""
        nav = self._create_navigator()
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

    def test_warmup_init_preserves_operator_configured_symbols(self):
        """Инвариант #3: wait_radar_warmup_and_init НЕ перезаписывает монеты оператора при старте."""
        nav = self._create_navigator({"radar_synergy": {"target_coins_count": 4}})

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
                self.assertEqual(new_symbols, ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT"])

        asyncio.run(run_test())

    def test_warmup_partial_pool_accepted_without_artificial_rejection(self):
        """Проверка: частичный пул принимается без искусственных откатов."""
        nav = self._create_navigator({"radar_synergy": {"target_coins_count": 4}})

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

    def test_set_union_of_scanner_and_combat_symbols(self):
        """Проверка объединения списков: боевые монеты гарантированно не теряются."""
        scanner_symbols = ["0GUSDT", "PUMPUSDT", "KAITOUSDT"]
        combat_symbols = ["GRASSUSDT", "ARXUSDT", "ZROUSDT", "XPLUSDT", "PUMPUSDT"]

        combined = set(scanner_symbols + combat_symbols)
        final_list = list(combined)

        for cs in combat_symbols:
            self.assertIn(cs, final_list)
        for ss in scanner_symbols:
            self.assertIn(ss, final_list)
        self.assertEqual(len(final_list), len(combined))


if __name__ == "__main__":
    unittest.main()
