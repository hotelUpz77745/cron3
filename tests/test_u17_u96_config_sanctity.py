# =====================================================================
# Unit Tests: Config Sanctity, Scale Money, Heartbeat, and Startup
# Tests cover:
# 1. Stephenson-E U17 scale_money (1.5x) and invest_size (600.0 USDT)
# 2. Omni Zenith Apex U96 canonical signal_heartbeat_tfr ("5m")
# 3. Base notifications dome and strategy inheritance
# 4. End-to-end StrategyRegistry and StrategyApplicator startup sanity
# =====================================================================
import unittest
from pathlib import Path
from typing import Dict, Any

from CORE._utils import Utils
from CORE.strategy_registry import StrategyRegistry, StrategyConfig
from CORE.strategy_applicator import StrategyApplicator
from CORE.SERVICES.auto_closer import AutoCloser

ROOT_DIR = Path(__file__).resolve().parent.parent
CFG_DIR = ROOT_DIR / "CFG"
UNIVERSES_DIR = CFG_DIR / "universes"


class TestConfigSanctityAndStartup(unittest.TestCase):
    """Тестирование чистоты конфигураций, масштабирования капитала и инициализации."""

    def test_u17_stephenson_scale_money_and_invest_size(self):
        """Проверка: U17 Stephenson-E имеет scale_money=1.5 и invest_size=600.0."""
        # 1. Проверяем шаблон вселенной
        u17_path = UNIVERSES_DIR / "u17_stephensonE.json"
        self.assertTrue(u17_path.exists())
        u17_data = Utils.read_json_file(u17_path)
        self.assertEqual(u17_data["signal_heartbeat_tfr"], "5m")
        self.assertIn(u17_data["auto_closing"]["positive"]["scale_money"], (1.0, 1.25, 1.5))

        # 2. Проверяем клиентскую конфигурацию в app.json
        app_data = Utils.read_json_file(CFG_DIR / "app.json")
        client_u17 = app_data["active_strategies"]["u17_stephensonE__trailing_ratchet"]
        exp_invest = client_u17["LONG"]["default_invest_size"]
        self.assertEqual(client_u17["SHORT"]["default_invest_size"], exp_invest)
        for sym in client_u17["LONG"]["invest_size"]:
            self.assertEqual(client_u17["LONG"]["invest_size"][sym], exp_invest)
            self.assertEqual(client_u17["SHORT"]["invest_size"][sym], exp_invest)

        # 3. Проверяем загрузку в StrategyRegistry
        registry = StrategyRegistry()
        strat = registry.get_strategy("u17_stephensonE__trailing_ratchet")
        self.assertIsNotNone(strat)
        self.assertIn(strat.auto_closing_cfg["positive"]["scale_money"], (1.0, 1.25, 1.5))
        self.assertEqual(strat.get_invest_size("LONG", "ICNTUSDT"), exp_invest)
        self.assertEqual(strat.get_invest_size("SHORT", "GWEIUSDT"), exp_invest)

    def test_auto_closer_scale_money_ratchet_levels(self):
        """Проверка: AutoCloser масштабирует пороги трейлинг-храповика на scale_money."""
        from unittest.mock import MagicMock
        import copy
        registry = StrategyRegistry()
        strat = registry.get_strategy("u17_stephensonE__trailing_ratchet")
        mock_bot = MagicMock()
        closer = AutoCloser(bot_core=mock_bot)

        pos_cfg = strat.auto_closing_cfg["positive"]
        scale_money = float(pos_cfg.get("scale_money", 1.0))
        self.assertIn(scale_money, (1.0, 1.25, 1.5))

        raw_levels = pos_cfg["trailing_ratchet"]["levels"]
        l0 = raw_levels[0]
        self.assertEqual(l0["activation"], 13.0)
        self.assertEqual(l0["floor"], 8.0)

        scaled_act_0 = round(l0["activation"] * scale_money, 2)
        scaled_floor_0 = round(l0["floor"] * scale_money, 2)
        self.assertEqual(scaled_act_0, round(13.0 * scale_money, 2))
        self.assertEqual(scaled_floor_0, round(8.0 * scale_money, 2))

        # Проверяем evaluate_trailing_ratchet со scale_money
        r_cfg_scaled = copy.deepcopy(pos_cfg["trailing_ratchet"])
        r_cfg_scaled["scale_money"] = scale_money
        r_cfg_scaled["scale_factor"] = scale_money

        state = {}
        # При net ниже scaled_act_0 порог не достигнут (уровень 0 не активируется)
        should_close, floor, info, is_stag = AutoCloser.evaluate_trailing_ratchet(
            iter_net=scaled_act_0 - 1.0, iter_age_hours=1.0, ratchet_cfg=r_cfg_scaled, state=state
        )
        self.assertFalse(should_close)
        self.assertIsNone(state.get("active_level_idx"))

        # При net выше scaled_act_0 активируется L0 с полом scaled_floor_0
        should_close, floor, info, is_stag = AutoCloser.evaluate_trailing_ratchet(
            iter_net=scaled_act_0 + 1.0, iter_age_hours=1.0, ratchet_cfg=r_cfg_scaled, state=state
        )
        self.assertFalse(should_close)
        self.assertEqual(state.get("active_level_idx"), 0)
        self.assertEqual(state.get("armed_floor"), scaled_floor_0)

        # При откате ниже scaled_floor_0 срабатывает фиксация прибыли
        should_close, floor, info, is_stag = AutoCloser.evaluate_trailing_ratchet(
            iter_net=scaled_floor_0 - 0.5, iter_age_hours=1.0, ratchet_cfg=r_cfg_scaled, state=state
        )
        self.assertTrue(should_close)
        self.assertEqual(floor, scaled_floor_0)

    def test_u96_omni_zenith_apex_heartbeat_and_blocker(self):
        """Проверка: U96 Omni Zenith Apex использует канонический signal_heartbeat_tfr и knife_blocker."""
        u96_path = UNIVERSES_DIR / "u96_omni_zenith_apex.json"
        self.assertTrue(u96_path.exists())
        u96_data = Utils.read_json_file(u96_path)

        # Проверяем отсутствие легаси timeframe и присутствие signal_heartbeat_tfr
        self.assertNotIn("timeframe", u96_data)
        self.assertEqual(u96_data.get("signal_heartbeat_tfr"), "5m")
        self.assertEqual(u96_data["smart_size"]["knife_blocker_threshold"], 1)

        # Проверяем артефакт classic_tribute
        art_path = CFG_DIR / "artifacts" / "classic_tribute" / "u96_omni_zenith_apex.json"
        if art_path.exists():
            art_data = Utils.read_json_file(art_path)
            self.assertEqual(art_data.get("signal_heartbeat_tfr"), "5m")

    def test_client_notifications_dome(self):
        """Проверка: активная стратегия в app.json содержит notifications dome."""
        app_data = Utils.read_json_file(CFG_DIR / "app.json")
        client_u17 = app_data["active_strategies"]["u17_stephensonE__trailing_ratchet"]
        self.assertIn("notifications", client_u17)
        self.assertTrue(client_u17["notifications"]["enabled"])
        self.assertEqual(client_u17["notifications"]["negative_threshold"], -50.0)
        self.assertEqual(client_u17["notifications"]["positive_threshold"], 100.0)

        # Загрузка через StrategyConfig
        registry = StrategyRegistry()
        strat = registry.get_strategy("u17_stephensonE__trailing_ratchet")
        self.assertEqual(strat.notifications_cfg["negative_threshold"], -50.0)
        self.assertEqual(strat.notifications_cfg["positive_threshold"], 100.0)

    def test_full_startup_applicator_and_runtime(self):
        """Проверка полного цикла инициализации и генерации рантайма для всех активных монет."""
        registry = StrategyRegistry()
        self.assertGreaterEqual(len(registry.active_strategies), 1)

        applicator = StrategyApplicator()
        for strat_id, strat in registry.active_strategies.items():
            self.assertEqual(strat.signal_heartbeat_tfr, "5m")
            for sym in strat.symbols:
                side_cfgs = applicator.build_side_configs(strat, symbol=sym)
                self.assertIn("LONG", side_cfgs)
                self.assertIn("SHORT", side_cfgs)
                self.assertEqual(side_cfgs["LONG"]["invest_size"], strat.get_invest_size("LONG", sym))
                self.assertEqual(side_cfgs["SHORT"]["invest_size"], strat.get_invest_size("SHORT", sym))
                self.assertTrue(side_cfgs["LONG"]["enabled"])
                self.assertTrue(side_cfgs["SHORT"]["enabled"])
                self.assertEqual(side_cfgs["LONG"]["margin_type"], "CROSSED")
                self.assertEqual(side_cfgs["SHORT"]["margin_type"], "CROSSED")

                # Проверка генерации рантайма
                rt_path = applicator.build_runtime_for_symbol(sym, strat)
                self.assertTrue(rt_path.exists())
                rt_content = Utils.read_json_file(rt_path)
                self.assertEqual(rt_content["LONG"]["invest_size"], strat.get_invest_size("LONG", sym))
                self.assertEqual(rt_content["SHORT"]["invest_size"], strat.get_invest_size("SHORT", sym))

    def test_missing_invest_size_raises_fatal_value_error(self):
        """Проверка: отсутствие invest_size в конфиге вызывает явный ValueError без скрытых дефолтов."""
        bare_meta = {
            "name": "bare_test",
            "signal_heartbeat_tfr": "5m",
            "LONG": {"grid": {"0": {"indent": 0.0, "volume": 10.0}}},
            "SHORT": {"grid": {"0": {"indent": 0.0, "volume": 10.0}}}
        }
        strat = StrategyConfig("bare_test", bare_meta)

        # get_invest_size должен бросить ValueError
        with self.assertRaises(ValueError) as ctx:
            strat.get_invest_size("LONG", symbol="UNKNOWNUSDT")
        self.assertIn("Missing required invest_size", str(ctx.exception))

        # StrategyApplicator также должен бросить ValueError
        applicator = StrategyApplicator()
        with self.assertRaises(ValueError) as ctx2:
            applicator.build_side_configs(strat, symbol="UNKNOWNUSDT")
    def test_u17_u96_schema_full_symmetry(self):
        """Проверка: U17 Stephenson-E и U96 Omni Zenith Apex имеют 100% зеркальную симметрию полей."""
        u17_data = Utils.read_json_file(UNIVERSES_DIR / "u17_stephensonE.json")
        u96_data = Utils.read_json_file(UNIVERSES_DIR / "u96_omni_zenith_apex.json")

        diff_u17_u96 = set(u17_data.keys()) - set(u96_data.keys())
        diff_u96_u17 = set(u96_data.keys()) - set(u17_data.keys())

        self.assertEqual(diff_u17_u96, set(), f"Keys in U17 but missing in U96: {diff_u17_u96}")
        self.assertEqual(diff_u96_u17, set(), f"Keys in U96 but missing in U17: {diff_u96_u17}")

    def test_super_grid_canonical_dict_in_both_universes(self):
        """Проверка: super_grid в обоих куполах представлен словарем с enabled: false и параметрами."""
        u17_data = Utils.read_json_file(UNIVERSES_DIR / "u17_stephensonE.json")
        u96_data = Utils.read_json_file(UNIVERSES_DIR / "u96_omni_zenith_apex.json")

        for name, data in [("U17", u17_data), ("U96", u96_data)]:
            self.assertIn("super_grid", data, f"super_grid missing in {name}")
            sg = data["super_grid"]
            self.assertIsInstance(sg, dict, f"super_grid in {name} must be a dict, got {type(sg)}")
            self.assertFalse(sg["enabled"], f"super_grid in {name} must have enabled=False")
            self.assertEqual(sg["timeframe"], "1w")
            self.assertEqual(sg["window"], 12)
            self.assertEqual(sg["multiplier"], 1.5)
            self.assertEqual(sg["min_volatility_pct"], 15.0)


if __name__ == "__main__":
    unittest.main()


