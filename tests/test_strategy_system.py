# ============================================================
# FILE: tests/test_strategy_system.py
# ROLE: Unit and integration tests for StrategyRegistry, StrategyApplicator, and MomentumEngine
# ============================================================

import unittest
from pathlib import Path
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE.STRATEGY.strategy_registry import StrategyConfig, StrategyRegistry
from CORE.STRATEGY.strategy_applicator import StrategyApplicator, MIN_TAKE_PROFIT_PCT


class TestStrategySystem(unittest.TestCase):
    """Набор тестов для MomentumEngine, StrategyRegistry и StrategyApplicator."""

    def test_momentum_engine_rules(self):
        """Тестирование строгой математики MomentumEngine."""
        self.assertTrue(MomentumEngine.match_rule("==1", 1))
        self.assertFalse(MomentumEngine.match_rule("==1", 2))
        self.assertTrue(MomentumEngine.match_rule(">=2", 2))
        self.assertTrue(MomentumEngine.match_rule(">=2", 3))
        self.assertTrue(MomentumEngine.match_rule("<2", 1))
        self.assertFalse(MomentumEngine.match_rule("<2", 2))

    def test_smart_grace_calculation(self):
        """Тестирование расчета длительности Smart Grace окна."""
        cfg = {
            "start_period_sec": 57,
            "shift_interval_sec": 13,
            "eval_window_sec": 300,
            "increments_card": {
                "==1": 30,
                "==2": 90,
                "==3": 150,
                ">=4": 185
            }
        }
        self.assertEqual(MomentumEngine.calculate_smart_grace(0, cfg), 57.0)
        self.assertEqual(MomentumEngine.calculate_smart_grace(1, cfg), 87.0)
        self.assertEqual(MomentumEngine.calculate_smart_grace(2, cfg), 147.0)
        self.assertEqual(MomentumEngine.calculate_smart_grace(4, cfg), 242.0)

    def test_knife_blocker(self):
        """Тестирование блокировки входов Knife Blocker."""
        cfg_disabled = {"knife_blocker_enabled": False, "knife_blocker_threshold": 2}
        self.assertFalse(MomentumEngine.is_knife_blocked(5, cfg_disabled))

        cfg_enabled = {"knife_blocker_enabled": True, "knife_blocker_threshold": 2}
        self.assertFalse(MomentumEngine.is_knife_blocked(1, cfg_enabled))
        self.assertTrue(MomentumEngine.is_knife_blocked(2, cfg_enabled))
        self.assertTrue(MomentumEngine.is_knife_blocked(3, cfg_enabled))

    def test_smart_tp_multiplier(self):
        """Тестирование множителей динамического тейк-профита."""
        cfg = {
            "increments_tp": {
                "==1": 1.25,
                ">=2": 1.50
            }
        }
        self.assertEqual(MomentumEngine.calculate_smart_tp_multiplier(0, cfg), 1.00)
        self.assertEqual(MomentumEngine.calculate_smart_tp_multiplier(1, cfg), 1.25)
        self.assertEqual(MomentumEngine.calculate_smart_tp_multiplier(2, cfg), 1.50)

    def test_strategy_registry_resolution_and_invariants(self):
        """Тестирование разрешения стратегий и Инварианта 1 (1:1)."""
        sample_active = {
            "u6_symbiosis_beta_scalper": {
                "enabled": True,
                "symbols": ["ARXUSDT", "LAUSDT"]
            },
            "u0_fat_swing": {
                "enabled": True,
                "symbols": ["FARTCOINUSDT"]
            },
            "u0_tight_scalper": {
                "enabled": False,
                "symbols": ["BTCUSDT"]  # Отключена
            }
        }
        registry = StrategyRegistry(active_strategies_cfg=sample_active)

        self.assertEqual(len(registry.get_active_strategies()), 2)
        self.assertIn("ARXUSDT", registry.get_all_active_symbols())
        self.assertIn("LAUSDT", registry.get_all_active_symbols())
        self.assertIn("FARTCOINUSDT", registry.get_all_active_symbols())
        self.assertNotIn("BTCUSDT", registry.get_all_active_symbols())

        strat_arx = registry.get_strategy_for_symbol("ARXUSDT")
        self.assertIsNotNone(strat_arx)
        self.assertEqual(strat_arx.strategy_id, "u6_symbiosis_beta_scalper")
        self.assertEqual(strat_arx.invest_size, 400.0)

        # Проверка Инварианта 1: Коллизия тикеров обязана выбрасывать ValueError!
        conflict_active = {
            "u6_symbiosis_beta_scalper": {
                "enabled": True,
                "symbols": ["ARXUSDT"]
            },
            "u0_fat_swing": {
                "enabled": True,
                "symbols": ["ARXUSDT"]  # Дубликат!
            }
        }
        with self.assertRaises(ValueError) as ctx:
            StrategyRegistry(active_strategies_cfg=conflict_active)
        self.assertIn("КОНФЛИКТ ИНВАРИАНТА 1:1", str(ctx.exception))

    def test_strategy_applicator_rule11_min_tp(self):
        """Тестирование Инварианта 4 (Правило 11: TP строго >= 0.50%)."""
        applicator = StrategyApplicator()
        sample_meta = {
            "name": "Micro TP Scalper Test",
            "invest_size": 400.0,
            "leverage": 10,
            "overrides": {
                "tp_map": {
                    "0": {"indent": 0.20, "fallback_indent": 0.40},  # Слишком малый TP!
                    "1": {"indent": 0.80, "fallback_indent": 1.20}
                }
            }
        }
        strat = StrategyConfig("test_micro", sample_meta, ["TESTUSDT"])
        side_cfgs = applicator.build_side_configs(strat)

        for side in ("LONG", "SHORT"):
            tp_map = side_cfgs[side]["tp_map"]
            self.assertEqual(tp_map["0"]["indent"], MIN_TAKE_PROFIT_PCT)
            self.assertEqual(tp_map["0"]["fallback_indent"], MIN_TAKE_PROFIT_PCT)
            self.assertEqual(tp_map["1"]["indent"], 0.80)
            self.assertEqual(tp_map["1"]["fallback_indent"], 1.20)

    def test_default_active_strategies_load(self):
        """Интеграционный тест: загрузка активных лидеров из app.json и universes.json."""
        registry = StrategyRegistry()
        active_strats = registry.get_active_strategies()
        self.assertGreaterEqual(len(active_strats), 1)

        active_symbols = registry.get_all_active_symbols()
        self.assertGreaterEqual(len(active_symbols), 1)
        for sym in active_symbols:
            strat = registry.get_strategy_for_symbol(sym)
            self.assertIsNotNone(strat)
            self.assertIn(strat.strategy_id, active_strats)


    def test_runtime_builder_integration(self):
        """Интеграционный тест сборки рантаймов build_runtime_caches."""
        from RUNTIME_FSM.runtime_builder import build_runtime_caches, RUNTIME_DIR
        from c_utils import Utils

        registry = StrategyRegistry()
        build_runtime_caches(registry=registry)

        for sym in ["ZROUSDT", "PENGUUSDT", "SPXUSDT"]:
            cfg_path = RUNTIME_DIR / f"{sym.lower()}.json"
            self.assertTrue(cfg_path.exists(), f"Рантайм файл {cfg_path} должен существовать!")
            data = Utils.read_json_file(cfg_path)
            for side in ("LONG", "SHORT"):
                self.assertIn(side, data)
                self.assertIn("grid", data[side])
                self.assertIn("tp_map", data[side])
                # Проверка соблюдения Rule 11 (min TP >= 0.50%)
                for tp_step in data[side]["tp_map"].values():
                    if "indent" in tp_step and tp_step["indent"] is not None:
                        self.assertGreaterEqual(float(tp_step["indent"]), MIN_TAKE_PROFIT_PCT)
                    if "fallback_indent" in tp_step and tp_step["fallback_indent"] is not None:
                        self.assertGreaterEqual(float(tp_step["fallback_indent"]), MIN_TAKE_PROFIT_PCT)

    def test_runtime_under_strategy_domes(self):
        """Тест изоляции файлов рантайма под куполами стратегий (вселенных)."""
        from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
        from RUNTIME_FSM.runtime_builder import build_runtime_caches, RUNTIME_DIR

        registry = StrategyRegistry()
        build_runtime_caches(registry=registry)
        mgr = RuntimeFsmManager(strategy_registry=registry)

        expected_domes = {
            "ZROUSDT": ("u0_fat_swing123", "zrousdt.json"),
            "PENGUUSDT": ("u8_golden_macro_symbiosis", "penguusdt.json"),
            "SPXUSDT": ("u6_symbiosis_gamma_fortress123", "spxusdt.json"),
        }

        for sym, (strat_id, fname) in expected_domes.items():
            dome_path = RUNTIME_DIR / strat_id / fname
            self.assertTrue(dome_path.exists(), f"Файл рантайма под куполом {strat_id} должен существовать: {dome_path}")
            resolved = mgr.get_runtime_path(sym)
            self.assertEqual(resolved, dome_path)

    def test_structured_analytics_and_leaderboard(self):
        """Тест структурированной аналитики в STATS/, LEDGERS/, и витрины leaderboard."""
        from ANALYTICS.analytics import AnalyticsManager
        from consts import ANALYTICS_STATS_DIR, ANALYTICS_LEDGERS_DIR, ANALYTICS_DIR
        import json

        registry = StrategyRegistry()
        analytics_mgr = AnalyticsManager(strategy_registry=registry)

        # 1. Проверяем создание файлов аналитики по каждой активной стратегии
        for strat_id in registry.active_strategies.keys():
            strat_json = ANALYTICS_STATS_DIR / f"analytics_{strat_id}.json"
            strat_txt = ANALYTICS_LEDGERS_DIR / f"trades_ledger_{strat_id}.txt"
            self.assertTrue(strat_json.exists(), f"Файл аналитики {strat_json} должен существовать!")
            self.assertTrue(strat_txt.exists(), f"Леджер {strat_txt} должен существовать!")

        # 2. Обновляем статистику и проверяем leaderboard
        analytics_mgr._update_strategies_stats(analytics_mgr._read_data())

        lead_txt = ANALYTICS_DIR / "leaderboard.txt"
        self.assertTrue(lead_txt.exists(), "Витрина leaderboard.txt должна существовать!")
        table_content = lead_txt.read_text(encoding="utf-8")
        self.assertIn("STRATEGIES LEADERBOARD", table_content)

        portfolio_json = ANALYTICS_STATS_DIR / "analytics_all.json"
        self.assertTrue(portfolio_json.exists(), "Файл портфеля analytics_all.json должен существовать!")
        portfolio_data = json.loads(portfolio_json.read_text(encoding="utf-8"))
        self.assertIn("leaderboard", portfolio_data)
        registry = StrategyRegistry()
        self.assertEqual(portfolio_data["total_universes_active"], len(registry.get_active_strategies()))


    def test_strategy_dome_configurations(self):
        """Тестирование изолированных конфигураций под куполами стратегий (super_grid, analytics, notifications, auto_closing)."""
        registry = StrategyRegistry()
        for strat_id, strat in registry.active_strategies.items():
            self.assertIsNotNone(strat.super_grid_cfg)
            self.assertGreater(strat.start_balance_usdt, 0)
            self.assertIsNotNone(strat.notifications_cfg)
            self.assertIsNotNone(strat.auto_closing_cfg)
            self.assertTrue(strat.notifications_cfg.get("enabled"))

    def test_notifier_strategy_dome_monitoring(self):
        """Тестирование изоляции флагов уведомлений между куполами стратегий."""
        import asyncio
        from CORE.notifier import NotifierManager

        class DummyBot:
            def __init__(self, reg):
                self.strategy_registry = reg

        registry = StrategyRegistry()
        bot = DummyBot(registry)
        notifier = NotifierManager(bot_core=bot)

        # Прогон check_thresholds
        asyncio.run(notifier.check_thresholds())
        for strat_id in registry.active_strategies.keys():
            self.assertIn(strat_id, notifier.strat_flags)
            self.assertFalse(notifier.strat_flags[strat_id]["neg_flag"])
            self.assertFalse(notifier.strat_flags[strat_id]["pos_flag"])

    def test_u6_symbiosis_gamma_fortress35_template(self):
        """Тест шаблона u6_symbiosis_gamma_fortress35 (Медный Змей)."""
        registry = StrategyRegistry()
        self.assertIn("u6_symbiosis_gamma_fortress35", registry.universes_db)
        raw_meta = registry.universes_db["u6_symbiosis_gamma_fortress35"]
        strat = StrategyConfig("u6_symbiosis_gamma_fortress35", raw_meta, ["TESTUSDT"])
        self.assertEqual(strat.invest_size, 400.0)
        self.assertEqual(strat.leverage, 20)
        self.assertTrue(strat.smart_size_cfg.get("knife_blocker_enabled"))
        self.assertEqual(len(strat.overrides["grid"]), 9)
        self.assertEqual(len(strat.overrides["tp_map"]), 9)
        # Проверка демпфера тейка (Panic Damper) от 2.2% до 0.50%
        self.assertEqual(strat.overrides["tp_map"]["0"]["indent"], 2.2)
        self.assertEqual(strat.overrides["tp_map"]["8"]["indent"], 0.5)

    def test_u9_omni_zenith_apex_template(self):
        """Тест универсальной формулы Вселенной u9_omni_zenith_apex."""
        registry = StrategyRegistry()
        self.assertIn("u9_omni_zenith_apex", registry.universes_db)

        raw_meta = registry.universes_db["u9_omni_zenith_apex"]
        strat = StrategyConfig("u9_omni_zenith_apex", raw_meta, ["TESTUSDT"])

        # Проверка ключевых параметров асимметричного дуального режима
        self.assertFalse(strat.super_grid_enabled)
        self.assertEqual(strat.timeframe, "5m")
        self.assertEqual(strat.smart_grace_cfg["start_period_sec"], 57)
        self.assertEqual(strat.smart_grace_cfg["shift_interval_sec"], 13)

        # Knife Blocker & Smart Size
        self.assertTrue(strat.smart_size_cfg["knife_blocker_enabled"])
        self.assertEqual(strat.smart_size_cfg["knife_blocker_threshold"], 2)
        self.assertEqual(strat.smart_size_cfg["profiting_size_weights"][">=4"], 0.6)
        self.assertEqual(strat.smart_size_cfg["profiting_size_weights"]["==2"], 1.2)

        # Smart TP
        self.assertEqual(strat.smart_tp_cfg["increments_tp"][">=2"], 1.3)
        self.assertEqual(strat.smart_tp_cfg["increments_tp"][">4"], 1.6)

        # Сборка рантайм конфигурации и валидация Правила 11
        applicator = StrategyApplicator()
        side_cfgs = applicator.build_side_configs(strat)
        for side in ("LONG", "SHORT"):
            grid = side_cfgs[side]["grid"]
            tp_map = side_cfgs[side]["tp_map"]
            self.assertEqual(len(grid), 8)
            self.assertEqual(len(tp_map), 8)
            self.assertEqual(grid["7"]["indent"], -58.0)
            self.assertEqual(tp_map["0"]["indent"], 1.8)
            self.assertEqual(tp_map["7"]["indent"], 0.5)

            # Строгое соблюдение Правила 11
            for tp_step in tp_map.values():
                self.assertGreaterEqual(float(tp_step["indent"]), MIN_TAKE_PROFIT_PCT)
                self.assertGreaterEqual(float(tp_step["fallback_indent"]), MIN_TAKE_PROFIT_PCT)

            # Проверка суммы объемов сетки (не более 85% депозита)
            total_vol = sum(float(lvl["volume"]) for lvl in grid.values())
            self.assertLessEqual(total_vol, 85.0)


if __name__ == "__main__":
    unittest.main()
