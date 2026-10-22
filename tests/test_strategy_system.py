# ============================================================
# FILE: tests/test_strategy_system.py
# ROLE: Unit and integration tests for StrategyRegistry, StrategyApplicator, and MomentumEngine
# ============================================================

import unittest
import unittest.mock
from pathlib import Path
from consts import _CFG, DATA_DIR
from CORE._utils import Utils
from CORE.STRATEGY.momentum_engine import MomentumEngine
from CORE.strategy_registry import StrategyConfig, StrategyRegistry
from CORE.strategy_applicator import StrategyApplicator, MIN_TAKE_PROFIT_PCT


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

        cfg_null = {"knife_blocker_threshold": None}
        self.assertFalse(MomentumEngine.is_knife_blocked(5, cfg_null))

        cfg_enabled = {"knife_blocker_enabled": True, "knife_blocker_threshold": 2}
        self.assertFalse(MomentumEngine.is_knife_blocked(1, cfg_enabled))
        self.assertTrue(MomentumEngine.is_knife_blocked(2, cfg_enabled))
        self.assertTrue(MomentumEngine.is_knife_blocked(3, cfg_enabled))

        cfg_threshold_only = {"knife_blocker_threshold": 1}
        self.assertTrue(MomentumEngine.is_knife_blocked(1, cfg_threshold_only))

    def test_smart_tp_multiplier(self):
        """Тестирование множителей динамического тейк-профита."""
        cfg = {
            "increments_tp": {
                "==1": 1.25,
                ">=2": 1.50
            }
        }
    def test_smart_size_dynamic_ema_multiplier(self):
        """Тестирование trend_determ (ema_trend): привязка к EMA-тренду."""
        cfg_dyn = {
            "trend_determ": "ema_trend",
            "profiting_size_weights": {
                "==0": 1.14,
                "==1": 1.14,
                "==2": 1.28,
                "==3": 1.42,
                ">=4": 1.0
            }
        }

        # 1. LONG по восходящему тренду (UP) -> умножение работает
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, cfg_dyn, side="LONG", trend_state="UP"), 1.14)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(1, cfg_dyn, side="LONG", trend_state="UP"), 1.14)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(2, cfg_dyn, side="LONG", trend_state="UP"), 1.28)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(3, cfg_dyn, side="LONG", trend_state="UP"), 1.42)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(4, cfg_dyn, side="LONG", trend_state="UP"), 1.00)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(5, cfg_dyn, side="LONG", trend_state="UP"), 1.00)

        # 2. SHORT по нисходящему тренду (DOWN) -> умножение работает
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, cfg_dyn, side="SHORT", trend_state="DOWN"), 1.14)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(2, cfg_dyn, side="SHORT", trend_state="DOWN"), 1.28)

        # 3. LONG при нисходящем тренде (DOWN) -> контртренд, строго 1.00x
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, cfg_dyn, side="LONG", trend_state="DOWN"), 1.00)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(2, cfg_dyn, side="LONG", trend_state="DOWN"), 1.00)

        # 4. SHORT при восходящем тренде (UP) -> контртренд, строго 1.00x
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, cfg_dyn, side="SHORT", trend_state="UP"), 1.00)

        # 5. При FLAT или UNSTABLE -> нейтральный объем 1.00x
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, cfg_dyn, side="LONG", trend_state="FLAT"), 1.00)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, cfg_dyn, side="LONG", trend_state="UNSTABLE"), 1.00)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, cfg_dyn, side="LONG", trend_state=None), 1.00)

        # 6. Режим без dynamic_sizing_mode (null / отсутствует) -> классический расчет без фильтра
        cfg_classic = {
            "dynamic_sizing_mode": None,
            "profiting_size_weights": {
                "==0": 1.14,
                "==1": 1.14,
                "==2": 1.28
            }
        }
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(0, cfg_classic, side="LONG", trend_state="DOWN"), 1.14)
        self.assertEqual(MomentumEngine.calculate_smart_size_multiplier(2, cfg_classic), 1.28)

    def test_get_ema_trend_state(self):
        """Тестирование изолированного расчета get_ema_trend_state с моком свечей."""
        import asyncio
        MomentumEngine.clear_cache()

        # Восходящие свечи
        up_closes = [100.0 + i * 2.0 for i in range(50)]
        fake_klines_up = [{"close": c} for c in up_closes]

        with unittest.mock.patch("CORE.STRATEGY.momentum_engine.BinancePublic.get_klines", unittest.mock.AsyncMock(return_value=fake_klines_up)):
            trend = asyncio.run(MomentumEngine.get_ema_trend_state("TESTUSDT", {"sma_fast": 10, "sma_slow": 30, "confirmation_candles": 5}))
            self.assertEqual(trend, "UP")

        # Проверка TTL-кэша (повторный вызов без обращения к API возвращает UP)
        trend_cached = asyncio.run(MomentumEngine.get_ema_trend_state("TESTUSDT", {}))
        self.assertEqual(trend_cached, "UP")

        # Нисходящие свечи для другого символа
        MomentumEngine.clear_cache()
        down_closes = [200.0 - i * 2.0 for i in range(50)]
        fake_klines_down = [{"close": c} for c in down_closes]
        with unittest.mock.patch("CORE.STRATEGY.momentum_engine.BinancePublic.get_klines", unittest.mock.AsyncMock(return_value=fake_klines_down)):
            trend_down = asyncio.run(MomentumEngine.get_ema_trend_state("DOWNUSDT", {}))
            self.assertEqual(trend_down, "DOWN")

    def test_strategy_registry_resolution_and_invariants(self):
        """Тестирование разрешения стратегий и Инварианта 1 (1:1)."""
        sample_active = {
            "u6_symbiosis_gamma_fortress35": {
                "enabled": True,
                "symbols": ["ARXUSDT", "LAUSDT"]
            },
            "u96_omni_zenith_apex": {
                "template": "u96_omni_zenith_apex",
                "enabled": True,
                "symbols": ["FARTCOINUSDT"]
            }
        }
        registry = StrategyRegistry(active_strategies_cfg=sample_active)

        self.assertEqual(len(registry.get_active_strategies()), 2)
        self.assertIn("ARXUSDT", registry.get_all_active_symbols())
        self.assertIn("LAUSDT", registry.get_all_active_symbols())
        self.assertIn("FARTCOINUSDT", registry.get_all_active_symbols())

        strat_arx = registry.get_strategy_for_symbol("ARXUSDT")
        self.assertIsNotNone(strat_arx)
        self.assertEqual(strat_arx.strategy_id, "u6_symbiosis_gamma_fortress35")
        self.assertEqual(strat_arx.invest_size, 600.0)

        # 1. На одном аккаунте (одинаковые ключи) одинаковые монеты ОБЯЗАНЫ выбрасывать ValueError!
        single_account_conflict = {
            "u6_symbiosis_gamma_fortress35": {
                "enabled": True,
                "api_key_name": "API_KEY_MAIN",
                "symbols": ["ARXUSDT"]
            },
            "u96_omni_zenith_apex": {
                "template": "u96_omni_zenith_apex",
                "enabled": True,
                "api_key_name": "API_KEY_MAIN",
                "symbols": ["ARXUSDT"]  # Конфликт на одном аккаунте!
            }
        }
        with self.assertRaises(ValueError) as ctx:
            StrategyRegistry(active_strategies_cfg=single_account_conflict)
        self.assertIn("КОНФЛИКТ В РАМКАХ ОДНОГО АККАУНТА", str(ctx.exception))

        # 2. На разных аккаунтах (разные ключи) дублирование символов легально и разрешено!
        multi_account_active = {
            "u6_symbiosis_gamma_fortress35": {
                "enabled": True,
                "api_key_name": "API_KEY_ACCOUNT_1",
                "symbols": ["ARXUSDT"]
            },
            "u96_omni_zenith_apex": {
                "template": "u96_omni_zenith_apex",
                "enabled": True,
                "api_key_name": "API_KEY_ACCOUNT_2",
                "symbols": ["ARXUSDT"]  # Разрешено на разных счетах!
            }
        }
        registry = StrategyRegistry(active_strategies_cfg=multi_account_active)
        self.assertIn("ARXUSDT", registry.symbol_to_strategies)
        self.assertEqual(len(registry.symbol_to_strategies["ARXUSDT"]), 2)
        strats = [s.strategy_id for s in registry.get_strategies_for_symbol("ARXUSDT")]
        self.assertIn("u6_symbiosis_gamma_fortress35", strats)
        self.assertIn("u96_omni_zenith_apex", strats)

        # 3. Кастомные списки монет и строковый mode для разных модификаций одной стратегии
        dual_mode_active = {
            "u16_chita__trailing_ratchet": {
                "template": "u16_chita",
                "enabled": True,
                "auto_closing_mode": "trailing_ratchet",
                "api_key_name": "API_KEY_ACC1",
                "symbols": ["NEARUSDT", "BTCUSDT"]
            },
            "u16_chita__toxic": {
                "template": "u16_chita",
                "enabled": True,
                "auto_closing_mode": "toxic",
                "api_key_name": "API_KEY_ACC2",
                "symbols": ["ZROUSDT", "ETHUSDT"]
            }
        }
        dual_reg = StrategyRegistry(active_strategies_cfg=dual_mode_active)
        self.assertEqual(len(dual_reg.get_active_strategies()), 2)
        strat_ratchet = dual_reg.active_strategies["u16_chita__trailing_ratchet"]
        strat_toxic = dual_reg.active_strategies["u16_chita__toxic"]
        self.assertEqual(strat_ratchet.mode, "trailing_ratchet")
        self.assertEqual(strat_toxic.mode, "toxic")
        self.assertEqual(strat_ratchet.symbols, ["NEARUSDT", "BTCUSDT"])
        self.assertEqual(strat_toxic.symbols, ["ZROUSDT", "ETHUSDT"])
        self.assertIn("trailing_ratchet", strat_ratchet.auto_closing_cfg["positive"])
        self.assertIn("toxicity_adaptive", strat_toxic.auto_closing_cfg["positive"])

    def test_strategy_applicator_rule11_min_tp(self):
        """Тестирование Инварианта 4 (Правило 11: TP строго >= 0.50%)."""
        applicator = StrategyApplicator()
        sample_meta = {
            "name": "Micro TP Scalper Test",
            "invest_size": 400.0,
            "leverage": 10,
            "min_tp_indent": 0.50,
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
        import tempfile
        from RUNTIME_FSM.runtime_builder import build_runtime_caches
        from c_utils import Utils

        registry = StrategyRegistry()
        test_syms = registry.get_all_active_symbols()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_rt_dir = Path(tmp_dir)
            build_runtime_caches(symbols=test_syms, registry=registry, target_runtime_dir=test_rt_dir)

            for sym in test_syms:
                strat = registry.get_strategy_for_symbol(sym)
                if strat:
                    cfg_path = test_rt_dir / strat.strategy_id / f"{sym.lower()}.json"
                else:
                    cfg_path = test_rt_dir / f"{sym.lower()}.json"
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
        import tempfile
        from RUNTIME_FSM.runtime_manager import RuntimeFsmManager
        from RUNTIME_FSM.runtime_builder import build_runtime_caches

        registry = StrategyRegistry()
        active_syms = registry.get_all_active_symbols()
        self.assertGreaterEqual(len(active_syms), 1)

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_rt_dir = Path(tmp_dir)
            mgr = RuntimeFsmManager(strategy_registry=registry)
            mgr.runtime_dir = test_rt_dir
            build_runtime_caches(symbols=active_syms, registry=registry, target_runtime_dir=test_rt_dir)

            for sym in active_syms:
                strat = registry.get_strategy_for_symbol(sym)
                if strat:
                    dome_path = test_rt_dir / strat.strategy_id / f"{sym.lower()}.json"
                    self.assertTrue(dome_path.exists(), f"Файл рантайма .json под куполом {strat.strategy_id} должен существовать: {dome_path}")
                    resolved = mgr.get_runtime_path(sym)
                    self.assertEqual(resolved, dome_path)

    def test_structured_analytics_and_leaderboard(self):
        """Тест структурированной аналитики в STATS/, LEDGERS/, и витрины leaderboard."""
        from ANALYTICS.analytics import AnalyticsManager
        from consts import ANALYTICS_STATS_DIR, ANALYTICS_LEDGERS_DIR, ANALYTICS_DIR
        import json

        registry = StrategyRegistry()
        analytics_mgr = AnalyticsManager(strategy_registry=registry)
        analytics_mgr._floating_synced = True

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
        self.assertIn("Mode", table_content)

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
            # Проверяем, что auto_closing сконфигурирован согласно режиму стратегии
            self.assertIn("positive", strat.auto_closing_cfg)
            self.assertGreater(strat.auto_closing_cfg["positive"].get("scale_money", 0), 0)
            self.assertIn("_desc_scale_money", strat.auto_closing_cfg["positive"])

    def test_auto_closing_custom_override(self):
        """Проверка, что кастомный auto_closing переопределяет базовый шаблон из app.json."""
        custom_ac = {
            "positive": {
                "enabled": True,
                "mode": "fixed_pnl",
                "threshold": 15.0
            }
        }
        strat = StrategyConfig(
            strategy_id="u_custom",
            raw_meta={"name": "Custom AC"},
            symbols=["TESTUSDT"],
            active_settings={"auto_closing": custom_ac}
        )
        self.assertEqual(strat.auto_closing_cfg["positive"]["mode"], "fixed_pnl")
        self.assertEqual(strat.auto_closing_cfg["positive"]["threshold"], 15.0)

    def test_notifier_strategy_dome_monitoring(self):
        """Тестирование изоляции флагов уведомлений между куполами стратегий."""
        import asyncio
        from CORE.SERVICES.notifier import NotifierManager

        class DummyBot:
            def __init__(self, reg):
                self.strategy_registry = reg

        registry = StrategyRegistry()
        bot = DummyBot(registry)
        notifier = NotifierManager(bot_core=bot)

        # Прогон check_thresholds с нейтральным PnL
        with unittest.mock.patch("c_utils.Utils.read_json_file", return_value={"net_profit_usdt": 5.0}):
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
        self.assertEqual(strat.invest_size, 600.0)
        self.assertEqual(strat.leverage, 20)

        self.assertTrue(strat.smart_size_cfg.get("knife_blocker_enabled"))
        self.assertEqual(len(raw_meta["LONG"]["grid"]), 9)
        self.assertEqual(len(raw_meta["SHORT"]["grid"]), 9)
        self.assertEqual(len(raw_meta["LONG"]["tp_map"]), 9)
        self.assertEqual(len(strat.grid), 9)
        self.assertEqual(len(strat.tp_map), 9)
        # Проверка демпфера тейка (Panic Damper) от 2.2% до 0.65%
        self.assertEqual(raw_meta["LONG"]["tp_map"]["0"]["indent"], 2.2)
        self.assertEqual(raw_meta["LONG"]["tp_map"]["8"]["indent"], 0.65)

    def test_u9_omni_zenith_apex_template(self):
        """Тест универсальной формулы Вселенной u96_omni_zenith_apex."""
        registry = StrategyRegistry()
        self.assertIn("u96_omni_zenith_apex", registry.universes_db)

        raw_meta = registry.universes_db["u96_omni_zenith_apex"]
        strat = StrategyConfig("u96_omni_zenith_apex", raw_meta, ["TESTUSDT"])

        # Проверка ключевых параметров асимметричного дуального режима
        self.assertFalse(strat.super_grid_enabled)
        self.assertEqual(strat.timeframe, "5m")
        self.assertEqual(strat.smart_grace_cfg["start_period_sec"], 57)
        self.assertEqual(strat.smart_grace_cfg["shift_interval_sec"], 13)

        # Knife Blocker & Smart Size
        self.assertTrue(strat.smart_size_cfg["knife_blocker_enabled"])
        self.assertEqual(strat.smart_size_cfg["knife_blocker_threshold"], 1)
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
            self.assertEqual(len(grid), 9)
            self.assertEqual(len(tp_map), 9)
            self.assertEqual(grid["7"]["indent"], -58.0)
            self.assertEqual(grid["8"]["indent"], -89.0)
            self.assertEqual(tp_map["0"]["indent"], 1.8)
            self.assertEqual(tp_map["5"]["indent"], 0.62)
            self.assertEqual(tp_map["6"]["indent"], 0.62)
            self.assertEqual(tp_map["7"]["indent"], 0.62)
            self.assertEqual(tp_map["8"]["indent"], 0.62)

            # Строгое соблюдение Правила 11
            for tp_step in tp_map.values():
                self.assertGreaterEqual(float(tp_step["indent"]), MIN_TAKE_PROFIT_PCT)
                self.assertGreaterEqual(float(tp_step["fallback_indent"]), MIN_TAKE_PROFIT_PCT)

            # Проверка суммы объемов сетки (ровно 100.00%)
            total_vol = sum(float(lvl["volume"]) for lvl in grid.values())
            self.assertEqual(round(total_vol, 2), 100.0)

    def test_asymmetric_per_side_overrides(self):
        """Тест поддержки асимметричных настроек (LONG vs SHORT) для бычьей/медвежьей фазы."""
        asymm_meta = {
            "name": "Bull Fortress Asymmetric",
            "leverage": 20,
            "LONG": {
                "invest_size": 600.0,
                "leverage": 25,
                "tp_map": {
                    "0": {"indent": 2.5, "fallback_indent": 3.0}
                }
            },
            "SHORT": {
                "invest_size": 300.0,
                "leverage": 10,
                "tp_map": {
                    "0": {"indent": 0.8, "fallback_indent": 1.0}
                }
            }
        }
        strat = StrategyConfig("u_asymm_test", asymm_meta, ["ASYMMUSDT"])
        applicator = StrategyApplicator()
        side_cfgs = applicator.build_side_configs(strat)

        # LONG side checks
        self.assertEqual(side_cfgs["LONG"]["invest_size"], 600.0)
        self.assertEqual(side_cfgs["LONG"]["leverage"], 25)
        self.assertEqual(side_cfgs["LONG"]["tp_map"]["0"]["indent"], 2.5)

        # SHORT side checks
        self.assertEqual(side_cfgs["SHORT"]["invest_size"], 300.0)
        self.assertEqual(side_cfgs["SHORT"]["leverage"], 10)
        self.assertEqual(side_cfgs["SHORT"]["tp_map"]["0"]["indent"], 0.8)

    def test_no_duplicate_sources_of_truth_in_universes(self):
        """Проверка отсутствия раздвоения источников истины (invest_size, super_grid_enabled) и отсутствия overrides."""
        registry = StrategyRegistry()
        for uid, u_meta in registry.universes_db.items():
            # 1. Отсутствие устаревших/дублирующих ключей на верхнем уровне
            self.assertNotIn("invest_size", u_meta, f"Strategy {uid} must not have root invest_size")
            self.assertNotIn("super_grid_enabled", u_meta, f"Strategy {uid} must not have root super_grid_enabled")
            self.assertNotIn("is_active", u_meta, f"Strategy {uid} must not have is_active in universes.json (controlled strictly in app.json)")
            self.assertNotIn("overrides", u_meta, f"Strategy {uid} must not have legacy overrides wrapper")

            # 2. Сайзинг (invest_size): для активных вселенных (CFG/universes) задается строго на уровне клиента (в app.json active_strategies)
            self.assertIn("LONG", u_meta, f"Strategy {uid} must have LONG dome")
            self.assertIn("SHORT", u_meta, f"Strategy {uid} must have SHORT dome")
            if uid in ("u17_stephensonE", "u17_stephensonEM5", "u17_stephensonY", "u96_omni_zenith_apex"):
                self.assertNotIn("invest_size", u_meta.get("LONG", {}), f"Active strategy {uid} must not have LONG.invest_size at strategy level")
                self.assertNotIn("invest_size", u_meta.get("SHORT", {}), f"Active strategy {uid} must not have SHORT.invest_size at strategy level")
                self.assertNotIn("enabled", u_meta.get("LONG", {}), f"Active strategy {uid} must not have LONG.enabled at strategy level")
                self.assertNotIn("enabled", u_meta.get("SHORT", {}), f"Active strategy {uid} must not have SHORT.enabled at strategy level")
                self.assertNotIn("margin_type", u_meta.get("LONG", {}), f"Active strategy {uid} must not have LONG.margin_type at strategy level")
                self.assertNotIn("margin_type", u_meta.get("SHORT", {}), f"Active strategy {uid} must not have SHORT.margin_type at strategy level")
                self.assertNotIn("trend_size_depender", u_meta, f"Active strategy {uid} must not have trend_size_depender in json (static is default)")
            elif "trend_size_depender" in u_meta:
                self.assertIsInstance(u_meta["trend_size_depender"], str, f"Strategy {uid} trend_size_depender must be string, not dict")

            # 3. Сетка и TP-карта строго под куполом стороны (LONG / SHORT)
            self.assertIn("grid", u_meta["LONG"], f"Strategy {uid} must have LONG.grid")
            self.assertIn("grid", u_meta["SHORT"], f"Strategy {uid} must have SHORT.grid")
            self.assertIn("tp_map", u_meta["LONG"], f"Strategy {uid} must have LONG.tp_map")
            self.assertIn("tp_map", u_meta["SHORT"], f"Strategy {uid} must have SHORT.tp_map")
            self.assertGreater(len(u_meta["LONG"]["grid"]), 0)
            self.assertGreater(len(u_meta["SHORT"]["grid"]), 0)

            # 4. Наличие super_grid на уровне стратегии (булевый флаг или словарь для tribute)
            self.assertIn("super_grid", u_meta, f"Strategy {uid} must have super_grid")
            if isinstance(u_meta["super_grid"], dict):
                self.assertIn("enabled", u_meta["super_grid"], f"Strategy {uid} must have super_grid.enabled")
                self.assertIsInstance(u_meta["super_grid"]["enabled"], bool)
            else:
                self.assertIsInstance(u_meta["super_grid"], bool, f"Strategy {uid} super_grid must be a boolean (True/False)")

    def test_randomized_shift_interval_resolution_and_caching(self):
        """Проверка работы рандомизированного shift_interval_sec: secrets.SystemRandom [5, 16] и кэширование внутри свечи."""
        from CORE.ENTRY.signal import TimeControl

        # 1. Проверка разрешения диапазона [5, 16]
        samples = [TimeControl.resolve_shift_interval([5, 16]) for _ in range(50)]
        for s in samples:
            self.assertGreaterEqual(s, 5.0)
            self.assertLessEqual(s, 16.0)
            self.assertIsInstance(s, float)
        # Проверяем наличие дробных квантов (не строго целые числа)
        has_fractional = any(s != int(s) for s in samples)
        self.assertTrue(has_fractional, "Рандомизация должна выдавать дробные кванты (uniform random float)!")

        # 2. Проверка инвертированного диапазона [16, 5]
        s_inv = TimeControl.resolve_shift_interval([16, 5])
        self.assertGreaterEqual(s_inv, 5.0)
        self.assertLessEqual(s_inv, 16.0)

        # 3. Проверка скаляра (обратная совместимость)
        self.assertEqual(TimeControl.resolve_shift_interval(13), 13.0)

        # 4. Проверка стабильности внутри одной свечи (кэширование ID свечи)
        tc = TimeControl(interval="5m", smart_grace_cfg={"shift_interval_sec": [5, 16], "start_period_sec": 150})
        tc._candle_shifts.clear()
        
        # Симулируем 10 тиков внутри одного 5m интервала
        res1, _ = tc.is_new_interval(0)
        cached_shift = list(tc._candle_shifts.values())[0]
        self.assertGreaterEqual(cached_shift, 5.0)
        self.assertLessEqual(cached_shift, 16.0)

        for _ in range(10):
            tc.is_new_interval(0)
            self.assertEqual(list(tc._candle_shifts.values())[0], cached_shift, "Сдвиг должен быть стабилен внутри одной свечи!")

        # 5. Проверка загрузки из u16_chita
        registry = StrategyRegistry()
        chita_meta = registry.universes_db.get("u16_chita")
        self.assertIsNotNone(chita_meta)
        chita_strat = StrategyConfig("u16_chita", chita_meta, ["STRKUSDT"])
        self.assertEqual(chita_strat.smart_grace_cfg["shift_interval_sec"], [5, 16])

    def test_shift_interval_triggers_before_candle_close(self):
        """
        Проверка, что shift_interval_sec вычитается от времени закрытия 5-минутной свечи,
        обеспечивая вход за 5..16 секунд ДО закрытия свечи (а не после ее начала).
        """
        from datetime import datetime, timezone
        from unittest.mock import patch
        from CORE.ENTRY.signal import TimeControl

        tc = TimeControl(interval="5m", smart_grace_cfg={"shift_interval_sec": 13, "start_period_sec": 150})
        # Граница 5-минутки: 300 секунд (12:05:00 UTC)
        # При shift = 13:
        # t = 280 (12:04:40, 20с до закрытия) -> False
        # t = 287 (12:04:47, ровно 13с до закрытия) -> True (старт окна!)
        # t = 295 (12:04:55, 5с до закрытия) -> True

        def make_dt(timestamp: float):
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)

        with patch("CORE.ENTRY.signal.datetime") as mock_dt:
            # 1. За 20 секунд до закрытия свечи -> False
            mock_dt.now.return_value = make_dt(280.0)
            is_sig, _ = tc.is_new_interval(0)
            self.assertFalse(is_sig, "За 20с до закрытия окно еще должно быть закрыто!")

            # 2. Ровно за 13 секунд до закрытия свечи -> True
            mock_dt.now.return_value = make_dt(287.0)
            is_sig, _ = tc.is_new_interval(0)
            self.assertTrue(is_sig, "Ровно за 13с до закрытия свечи окно должно открыться!")

            # 3. За 5 секунд до закрытия свечи -> True
            mock_dt.now.return_value = make_dt(295.0)
            is_sig, _ = tc.is_new_interval(0)
            self.assertTrue(is_sig, "За 5с до закрытия свечи окно должно быть открыто!")

    def test_alias_resolution_multi_strategy(self):
        """Проверка интеллектуального разрешения алиасов (u6, u96, u16, omen, copper_snake, chita)."""
        registry = StrategyRegistry()
        db = registry.universes_db

        # 1. Проверка алиасов Медного Змея
        self.assertEqual(registry.resolve_template_id("u6", db), "u6_symbiosis_gamma_fortress35")
        self.assertEqual(registry.resolve_template_id("copper_snake", db), "u6_symbiosis_gamma_fortress35")
        self.assertEqual(registry.resolve_template_id("medny_zmey", db), "u6_symbiosis_gamma_fortress35")
        self.assertEqual(registry.resolve_template_id("u6_gamma", db), "u6_symbiosis_gamma_fortress35")

        # 2. Проверка алиасов Омена-96
        self.assertEqual(registry.resolve_template_id("u96", db), "u96_omni_zenith_apex")
        self.assertEqual(registry.resolve_template_id("u9", db), "u96_omni_zenith_apex")
        self.assertEqual(registry.resolve_template_id("omen", db), "u96_omni_zenith_apex")
        self.assertEqual(registry.resolve_template_id("omni", db), "u96_omni_zenith_apex")

        # 3. Проверка алиасов Читы v3.0 и v3.1
        self.assertEqual(registry.resolve_template_id("u16", db), "u16_chita")
        self.assertEqual(registry.resolve_template_id("chita", db), "u16_chita")
        self.assertEqual(registry.resolve_template_id("cheetah", db), "u16_chita")
        self.assertEqual(registry.resolve_template_id("u16_chita_v31", db), "u16_chita_v31")
        self.assertEqual(registry.resolve_template_id("u16_v31", db), "u16_chita_v31")
        self.assertEqual(registry.resolve_template_id("chita_v31", db), "u16_chita_v31")
        self.assertEqual(registry.resolve_template_id("chita31", db), "u16_chita_v31")

        # 4. Проверка гибкого нейминга с клиентскими префиксами и суффиксами
        self.assertEqual(registry.resolve_template_id("u16_chita_v31_client_alex", db), "u16_chita_v31")
        self.assertEqual(registry.resolve_template_id("client_alex_u16_chita_v31", db), "u16_chita_v31")
        self.assertEqual(registry.resolve_template_id("client_alex_u16", db), "u16_chita")
        self.assertEqual(registry.resolve_template_id("friend_bob_u6", db), "u6_symbiosis_gamma_fortress35")
        self.assertEqual(registry.resolve_template_id("investor_omen", db), "u96_omni_zenith_apex")

        # 5. Проверка одновременной параллельной регистрации всех 4 боевых вселенных
        test_active_cfg = {
            "u16__classic": {"enabled": True, "symbols": ["STRKUSDT"]},
            "u16_v31__classic": {"enabled": True, "symbols": ["PUMPUSDT"]},
            "u6__classic": {"enabled": True, "symbols": ["ENAUSDT"]},
            "u96__classic": {"enabled": True, "symbols": ["NEARUSDT"]},
        }
        test_reg = StrategyRegistry()
        test_reg.active_cfg = test_active_cfg
        test_reg._resolve_and_validate()

        self.assertEqual(len(test_reg.active_strategies), 4)
        self.assertIn("u16__classic", test_reg.active_strategies)
        self.assertIn("u16_v31__classic", test_reg.active_strategies)
        self.assertIn("u6__classic", test_reg.active_strategies)
        self.assertIn("u96__classic", test_reg.active_strategies)

        self.assertEqual(test_reg.get_strategy_for_symbol("STRKUSDT").name, test_reg.universes_db["u16_chita"]["name"])
        self.assertEqual(test_reg.get_strategy_for_symbol("PUMPUSDT").name, test_reg.universes_db["u16_chita_v31"]["name"])
        self.assertEqual(test_reg.get_strategy_for_symbol("ENAUSDT").name, test_reg.universes_db["u6_symbiosis_gamma_fortress35"]["name"])
        self.assertEqual(test_reg.get_strategy_for_symbol("NEARUSDT").name, test_reg.universes_db["u96_omni_zenith_apex"]["name"])

    def test_client_level_invest_size_configuration(self):
        """Проверка задания invest_size на уровне клиента в app.json (active_strategies)."""
        app_data = Utils.read_json_file(DATA_DIR / "app.json")
        active_strats = app_data.get("active_strategies", {})
        self.assertIn("u17_stephensonE__trailing_ratchet", active_strats)
        client_cfg = active_strats["u17_stephensonE__trailing_ratchet"]

        # Проверяем отсутствие дублирующего корневого invest_size, default_invest_size и списка symbols
        self.assertNotIn("invest_size", client_cfg, "Корневой invest_size не должен дублироваться в app.json")
        self.assertNotIn("default_invest_size", client_cfg, "Корневой default_invest_size не должен дублироваться на верхнем уровне (задается строго под копытами сторон)")
        self.assertNotIn("symbols", client_cfg, "Список symbols не дублируется в app.json (монеты выводятся из ключей invest_size)")
        self.assertEqual(client_cfg.get("auto_closing_mode"), "trailing_ratchet")
        self.assertTrue(client_cfg["LONG"]["enabled"])
        self.assertTrue(client_cfg["SHORT"]["enabled"])
        exp_invest = client_cfg["LONG"]["default_invest_size"]
        self.assertEqual(exp_invest, 400.0)
        self.assertEqual(client_cfg["SHORT"]["default_invest_size"], exp_invest)
        self.assertIsInstance(client_cfg["LONG"]["invest_size"], dict)
        self.assertIsInstance(client_cfg["SHORT"]["invest_size"], dict)
        self.assertEqual(client_cfg["LONG"]["invest_size"]["ICNTUSDT"], exp_invest)
        self.assertEqual(client_cfg["SHORT"]["invest_size"]["ICNTUSDT"], exp_invest)
        self.assertNotIn("trend_size_depender", client_cfg, "trend_size_depender не должен прописываться явно при дефолтном static")

        # Проверяем, что StrategyConfig и StrategyApplicator корректно применяют его
        registry = StrategyRegistry()
        strat = registry.get_strategy("u17_stephensonE__trailing_ratchet")
        exp_symbols = list(client_cfg["LONG"]["invest_size"].keys())
        self.assertEqual(strat.symbols, exp_symbols)
        self.assertEqual(registry.get_all_active_symbols(), exp_symbols)
        self.assertEqual(strat.get_invest_size("LONG", "ICNTUSDT"), exp_invest)
        self.assertEqual(strat.get_invest_size("SHORT", "AVAUSDT"), exp_invest)
        self.assertEqual(strat.trend_size_depender, "static")

        applicator = StrategyApplicator()
        side_cfgs_icnt = applicator.build_side_configs(strat, symbol="ICNTUSDT")
        self.assertEqual(side_cfgs_icnt["LONG"]["invest_size"], exp_invest)
        self.assertEqual(side_cfgs_icnt["SHORT"]["invest_size"], exp_invest)
        self.assertNotIn("sizing_mode", side_cfgs_icnt["LONG"])
        self.assertNotIn("sizing_mode", side_cfgs_icnt["SHORT"])

        # Проверка генерации рантайма с per-symbol размерами
        rt_path = applicator.build_runtime_for_symbol("ICNTUSDT", strat)
        self.assertTrue(rt_path.exists())
        rt_data = Utils.read_json_file(rt_path)
        self.assertEqual(rt_data["LONG"]["invest_size"], exp_invest)
        self.assertEqual(rt_data["SHORT"]["invest_size"], exp_invest)

    def test_resolve_executed_grid_levels_null_volume_resilience(self):
        """Проверка устойчивости resolve_executed_grid_levels к volume: None в grid_0 при активной позиции."""
        registry = StrategyRegistry()
        strat = registry.get_strategy("u17_stephensonE__trailing_ratchet")
        applicator = StrategyApplicator()

        new_side = {
            "invest_size": 400.0,
            "grid": {
                "0": {"volume": None, "is_active": False, "is_armed": False},
                "1": {"volume": 8.0, "is_active": False, "is_armed": False},
                "2": {"volume": 10.0, "is_active": False, "is_armed": False}
            },
            "trailing_grid_state": {"armed_levels": {}}
        }
        exist_side = {
            "in_position": True,
            "total_volume": 50.0,
            "avg_entry_price": 1.2,
            "grid": {
                "0": {"is_active": True},
                "1": {"is_active": False}
            }
        }

        # Вызов не должен падать с TypeError: float() argument must be a string or a real number, not 'NoneType'
        applicator.resolve_executed_grid_levels(strat, "TESTUSDT", "LONG", new_side, exist_side)
        self.assertTrue(new_side["grid"]["0"]["is_active"])

    def test_runtime_builder_solvusdt_and_sandusdt(self):
        """Проверка генерации рантайма для вновь добавленных монет SOLVUSDT и SANDUSDT."""
        registry = StrategyRegistry()
        strat = registry.get_strategy("u17_stephensonE__trailing_ratchet")
        applicator = StrategyApplicator()

        for sym in ["SOLVUSDT", "SANDUSDT"]:
            rt_path = applicator.build_runtime_for_symbol(sym, strat)
            self.assertTrue(rt_path.exists(), f"Runtime for {sym} must exist")
            rt_data = Utils.read_json_file(rt_path)
            self.assertEqual(rt_data["LONG"]["invest_size"], 400.0)
            self.assertEqual(rt_data["SHORT"]["invest_size"], 400.0)
            # Уровень 0 должен иметь числовой объем (разрешенный из first_order_weight_regime)
            self.assertIsNotNone(rt_data["LONG"]["grid"]["0"]["volume"])
            self.assertIsNotNone(rt_data["SHORT"]["grid"]["0"]["volume"])
            self.assertGreater(rt_data["LONG"]["grid"]["0"]["volume"], 0)
            self.assertGreater(rt_data["SHORT"]["grid"]["0"]["volume"], 0)


if __name__ == "__main__":
    unittest.main()


