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
        """Интеграционный тест: загрузка 3 лидеров из app.json и universes.json."""
        registry = StrategyRegistry()
        active_strats = registry.get_active_strategies()
        self.assertEqual(len(active_strats), 3)

        active_symbols = registry.get_all_active_symbols()
        self.assertIn("ZROUSDT", active_symbols)
        self.assertIn("PENGUUSDT", active_symbols)
        self.assertIn("SPXUSDT", active_symbols)

        strat_zro = registry.get_strategy_for_symbol("ZROUSDT")
        self.assertIsNotNone(strat_zro)
        self.assertEqual(strat_zro.strategy_id, "u0_fat_swing123")

        strat_pengu = registry.get_strategy_for_symbol("PENGUUSDT")
        self.assertIsNotNone(strat_pengu)
        self.assertEqual(strat_pengu.strategy_id, "u8_golden_macro_symbiosis")

        strat_spx = registry.get_strategy_for_symbol("SPXUSDT")
        self.assertIsNotNone(strat_spx)
        self.assertEqual(strat_spx.strategy_id, "u6_symbiosis_gamma_fortress123")

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


if __name__ == "__main__":
    unittest.main()
