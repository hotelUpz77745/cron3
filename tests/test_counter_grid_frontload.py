import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

CRON3_DIR = Path(__file__).resolve().parent.parent
BT_DIR = CRON3_DIR.parent / "cron3TradeBacktest"
if str(CRON3_DIR) not in sys.path:
    sys.path.insert(0, str(CRON3_DIR))

from POS_FSM.models import PositionState
from CORE._utils import RiskCalculatingUtils
from CORE.STRATEGY.counter_grid_frontload import CounterGridFrontloader
from CORE.STRATEGY.strategy_config import StrategyConfig
from CORE.strategy_registry import StrategyRegistry
from CORE.strategy_applicator import StrategyApplicator
from CORE.trade_cycle_manager import TradeCycleManager
from CORE.ENTRY.signal import TimeControl
from CORE.GRID.avg_manager import AverageManager
from CORE.TP.tp_manager import TakeProfitManager
from CORE.TP.fallback_tp_manager import FallbackTpManager
from API.BINANCE.client_sim import SimulatedBinanceClient


def _build_zenith13_runtime(strat_id="u97_zenith13_hedge_shield", invest=1200.0):
    reg = StrategyRegistry(
        active_strategies_cfg={
            f"{strat_id}__trailing_ratchet": {
                "template": strat_id,
                "enabled": True,
                "symbols": ["BILLUSDT"],
                "auto_closing_mode": "trailing_ratchet",
                "leverage": 20,
                "margin_type": "CROSSED",
                "LONG": {"enabled": True, "default_invest_size": invest, "invest_size": {}},
                "SHORT": {"enabled": True, "default_invest_size": invest, "invest_size": {}},
            }
        }
    )
    strat = reg.get_strategy(f"{strat_id}__trailing_ratchet")
    applicator = StrategyApplicator()
    side_cfgs = applicator.build_side_configs(strat, symbol="BILLUSDT")
    return reg, strat, side_cfgs


class TestCounterGridFrontload(unittest.TestCase):
    def test_u97_passport_parity_and_fail_fast(self):
        cfg_dir = CRON3_DIR / "CFG" / "universes"
        tribute_dir = CRON3_DIR / "CFG" / "artifacts" / "classic_tribute"
        u96 = json.loads((cfg_dir / "u96_omni_zenith13.json").read_text(encoding="utf-8"))
        u97 = json.loads((tribute_dir / "u97_zenith13_hedge_shield.json").read_text(encoding="utf-8"))

        for k in u96:
            if k in ("name", "description", "counter_grid_frontload", "_auto_closing_modes_allowed"):
                continue
            if k == "auto_closing":
                positive_u96 = {
                    pk: pv
                    for pk, pv in u96["auto_closing"]["positive"].items()
                    if pk not in ("smoothed_drawdown", "ratchet_smoothed_drawdown", "ma_crossover", "regime_adaptive")
                }
                expected_ac = {**u96["auto_closing"], "positive": positive_u96}
                self.assertEqual(u97["auto_closing"], expected_ac, "Core 'auto_closing' must match u96_omni_zenith13")
                continue
            self.assertEqual(u97[k], u96[k], f"Field '{k}' must match u96_omni_zenith13 for clean comparison")

        self.assertIn("counter_grid_frontload", u96)
        self.assertFalse(u96["counter_grid_frontload"]["enabled"])
        self.assertEqual(u96["counter_grid_frontload"]["absorb_tiers"], {"==5": 1, "==6": 2, ">=7": 3})
        self.assertTrue(u96["counter_grid_frontload"]["preserve_l0_identity"])

        for extra_name in ("u96_omni_alligator.json", "u96_omni_zenith13atr.json"):
            extra = json.loads((tribute_dir / extra_name).read_text(encoding="utf-8"))
            self.assertIn("counter_grid_frontload", extra, f"{extra_name} must declare counter_grid_frontload for compat")
            self.assertFalse(extra["counter_grid_frontload"]["enabled"])
            self.assertEqual(extra["counter_grid_frontload"]["absorb_tiers"], {"==5": 1, "==6": 2, ">=7": 3})

        self.assertTrue(u97["counter_grid_frontload"]["enabled"])
        self.assertEqual(u97["counter_grid_frontload"]["absorb_tiers"], {"==5": 1, "==6": 2, ">=7": 3})
        self.assertTrue(u97["counter_grid_frontload"]["preserve_l0_identity"])

        bad_meta = copy.deepcopy(u97)
        bad_meta["counter_grid_frontload"] = {"enabled": True}
        with self.assertRaises(KeyError):
            StrategyConfig("bad_u97", bad_meta, symbols=["BILLUSDT"])

    def test_dynamic_cur_step_and_absorbed_marker(self):
        _, _, cfgs = _build_zenith13_runtime()
        st = PositionState(symbol="BILLUSDT", side="SHORT")
        st.update_from_dict({"grid": cfgs["SHORT"]["grid"], "tp_map": cfgs["SHORT"]["tp_map"]})
        self.assertEqual(st.cur_step, 0)

        st.grid["0"]["is_active"] = True
        self.assertEqual(st.cur_step, 0)

        st.grid["5"]["is_active"] = True
        self.assertEqual(st.cur_step, 5)

        st.grid["7"]["is_active"] = True
        self.assertEqual(st.cur_step, 7)

        # Round-trip & reset
        dumped = st.to_dict()
        self.assertEqual(dumped["cur_step"], 7)
        st2 = PositionState.from_dict(dumped)
        self.assertEqual(st2.cur_step, 7)

        # Absorbed L1..L3 must NOT inflate cur_step / get_current_grid_level
        st_l = PositionState(symbol="BILLUSDT", side="LONG")
        st_l.update_from_dict({"grid": cfgs["LONG"]["grid"], "tp_map": cfgs["LONG"]["tp_map"]})
        st_l.grid["0"]["is_active"] = True
        CounterGridFrontloader.apply_absorbed_levels(st_l, ["1", "2", "3"], current_time_ms=1000_000, preserve_l0_identity=True)
        self.assertEqual(st_l.cur_step, 0)
        self.assertEqual(RiskCalculatingUtils.get_current_grid_level(st_l.grid), "0")
        self.assertEqual(RiskCalculatingUtils.get_current_grid_level(st_l.grid, include_absorbed=True), "3")

        # Real L4 averaging DOES advance cur_step to 4
        st_l.grid["4"]["is_active"] = True
        self.assertEqual(st_l.cur_step, 4)
        self.assertEqual(RiskCalculatingUtils.get_current_grid_level(st_l.grid), "4")

        st_l.reset()
        self.assertEqual(st_l.cur_step, 0)
        for lvl in ("0", "1", "2", "3", "4"):
            self.assertFalse(st_l.grid[lvl]["is_active"])
            self.assertNotIn("_absorbed_to_l0", st_l.grid[lvl])

    def test_plan_entry_absorption_ratios_and_smart_size_cap(self):
        _, strat, cfgs = _build_zenith13_runtime()
        cgf = strat.counter_grid_frontload_cfg
        long_grid = cfgs["LONG"]["grid"]

        # Step 4 -> no absorption (7.0%)
        pct4, keys4 = CounterGridFrontloader.plan_entry_absorption(cgf, long_grid, 7.0, 1.0, True, 4)
        self.assertAlmostEqual(pct4, 7.0)
        self.assertEqual(keys4, [])

        # Step 5 -> L1 absorbed (7.0 + 7.7 = 14.70%)
        pct5, keys5 = CounterGridFrontloader.plan_entry_absorption(cgf, long_grid, 7.0, 1.0, True, 5)
        self.assertAlmostEqual(pct5, 14.70)
        self.assertEqual(keys5, ["1"])

        # Step 6 -> L1..L2 absorbed (7.0 + 7.7 + 8.47 = 23.17%)
        pct6, keys6 = CounterGridFrontloader.plan_entry_absorption(cgf, long_grid, 7.0, 1.0, True, 6)
        self.assertAlmostEqual(pct6, 23.17)
        self.assertEqual(keys6, ["1", "2"])

        # Step 7 / 8 -> L1..L3 absorbed (7.0 + 7.7 + 8.47 + 9.32 = 32.49%)
        for s in (7, 8):
            pct7, keys7 = CounterGridFrontloader.plan_entry_absorption(cgf, long_grid, 7.0, 1.0, True, s)
            self.assertAlmostEqual(pct7, 32.49)
            self.assertEqual(keys7, ["1", "2", "3"])

        # smart_size=1.2x must NEVER exceed 100% total grid budget (clamped at 32.49%)
        pct_s12, keys_s12 = CounterGridFrontloader.plan_entry_absorption(cgf, long_grid, 7.0, 1.2, True, 7)
        self.assertAlmostEqual(pct_s12, 32.49)
        self.assertEqual(keys_s12, ["1", "2", "3"])

        # smart_size=0.6x still shrinks native L0 while keeping absorbed exact
        pct_s06, keys_s06 = CounterGridFrontloader.plan_entry_absorption(cgf, long_grid, 7.0, 0.6, True, 7)
        self.assertAlmostEqual(pct_s06, round(7.0 * 0.6 + 25.49, 6))
        self.assertEqual(keys_s06, ["1", "2", "3"])

        # Disabled level skipped cleanly
        grid_dis = copy.deepcopy(long_grid)
        grid_dis["1"]["enabled"] = False
        pct_d, keys_d = CounterGridFrontloader.plan_entry_absorption(cgf, grid_dis, 7.0, 1.0, True, 5)
        self.assertAlmostEqual(pct_d, 7.0 + 8.47)
        self.assertEqual(keys_d, ["2"])

    def test_resolve_opp_step_uses_runtime_cache_fallback(self):
        _, _, cfgs = _build_zenith13_runtime()
        opp = PositionState(symbol="BILLUSDT", side="SHORT", in_position=True)
        opp.grid = {}  # empty state grid -> must check runtime cache
        rm = MagicMock()
        cache_grid = copy.deepcopy(cfgs["SHORT"]["grid"])
        cache_grid["0"]["is_active"] = True
        cache_grid["6"]["is_active"] = True
        rm.caches = {"BILLUSDT": {"SHORT": {"grid": cache_grid}}}
        self.assertEqual(CounterGridFrontloader.resolve_opp_step(opp, rm, "BILLUSDT", "SHORT"), 6)

        opp.in_position = False
        self.assertEqual(CounterGridFrontloader.resolve_opp_step(opp, rm, "BILLUSDT", "SHORT"), 0)

    def test_end_to_end_entry_tp_averaging_and_failure_rollback(self):
        async def _run():
            reg, strat, cfgs = _build_zenith13_runtime()
            client = SimulatedBinanceClient(start_balance_usdt=2000.0, fee_rate=0.0005, leverage=20)
            client.current_prices["BILLUSDT"] = 0.20

            bot = MagicMock()
            bot.strategy_registry = reg
            bot.current_sim_time = 1778500000.0
            bot.get_current_time = lambda: bot.current_sim_time
            bot.get_client_for_symbol = lambda s: client
            bot._get_symbol_lock = lambda s: asyncio.Lock()
            bot.spec_data = {"BILLUSDT": {"qty_precision": 1, "price_precision": 5, "step_size": 0.1, "tick_size": 0.00001}}
            bot.leverage_manager = MagicMock()
            bot.leverage_manager.set_leverage_and_margin = AsyncMock()
            bot.first_order_regime = MagicMock()
            bot.first_order_regime.evaluate_and_resolve_volume = AsyncMock(return_value=7.0)
            bot.brownian_noise = MagicMock()
            bot.brownian_noise.enabled = False
            bot.is_paused = False
            bot.entry_blocked = False
            bot.block_until_timestamp = 0.0
            bot.symbol_blocked_until = {}
            bot.strategy_blocked_until = {}
            bot._closing_symbols = set()
            bot.prices = {"BILLUSDT": (0.20, bot.current_sim_time)}

            st_long = PositionState(symbol="BILLUSDT", side="LONG")
            st_short = PositionState(symbol="BILLUSDT", side="SHORT")
            st_long.update_from_dict({"grid": cfgs["LONG"]["grid"], "tp_map": cfgs["LONG"]["tp_map"]})
            st_short.update_from_dict({"grid": cfgs["SHORT"]["grid"], "tp_map": cfgs["SHORT"]["tp_map"]})

            # Mark SHORT active at L7!
            st_short.set_in_position(True)
            for lvl in ("0", "1", "2", "3", "4", "5", "6", "7"):
                st_short.grid[lvl]["is_active"] = True
            self.assertEqual(st_short.cur_step, 7)

            bot.fsm_states = {"BILLUSDT": {"LONG": st_long, "SHORT": st_short}}
            bot.runtime_configs = {"BILLUSDT": copy.deepcopy(cfgs)}
            rm = MagicMock()
            rm.strategy_registry = reg
            rm.caches = bot.runtime_configs
            rm.bot_core = bot
            rm.save_cache = MagicMock(return_value=None)
            bot.runtime_manager = rm
            bot.tp_manager = TakeProfitManager(rm)
            bot.time_control = TimeControl(interval="5m", smart_grace_cfg=strat.smart_grace_cfg)
            bot.avg_manager = AverageManager()
            bot.peeling_manager = None
            bot.fallback_tp_manager = FallbackTpManager()

            def on_fill(order, fill_price, pos):
                st = bot.fsm_states[order.symbol][order.position_side]
                if pos.qty > 0:
                    st.avg_entry_price = pos.entry_price
                    st.total_volume = pos.qty
                    st.set_in_position(True)
            client.on_fill_callback = on_fill

            # Two closes inside same 5m candle -> smart_size=1.2x (capped!), smart_tp=1.3x!
            t0 = bot.current_sim_time
            st_long.recent_closes = [t0 - 20.0, t0 - 10.0]

            tcm = TradeCycleManager(bot)
            await tcm.process_signal("BILLUSDT", "LONG", cfgs["LONG"], current_price=0.20)

            # Volume must equal 32.49% of $1200 = $389.88 -> 1949.4 @ 0.20 (1.2x smart_size capped!)
            self.assertAlmostEqual(st_long.total_volume, 1949.4, places=1)
            self.assertTrue(st_long.grid["0"]["is_active"])
            for k in ("1", "2", "3"):
                self.assertTrue(st_long.grid[k]["is_active"])
                self.assertTrue(st_long.grid[k].get("_absorbed_to_l0"))
            self.assertFalse(st_long.grid["4"]["is_active"])
            # Preserved L0 identity -> cur_step is 0 AND TP is L0 (2.34% * 1.3 = 3.042%)!
            self.assertEqual(st_long.cur_step, 0)
            self.assertAlmostEqual(st_long.tp_current_indent, 3.042, places=3)

            # Check pullback averaging: must skip L1..L3 and execute L4 (-15% -> 0.17)
            await bot.avg_manager.process(client, rm, "BILLUSDT", "LONG", st_long, st_short, 0.18, bot.spec_data, bot.tp_manager)
            self.assertEqual(st_long.cur_step, 0)  # L2(-5.5%)/L3(-9%) skipped!

            await bot.avg_manager.process(client, rm, "BILLUSDT", "LONG", st_long, st_short, 0.17, bot.spec_data, bot.tp_manager)
            client.current_prices["BILLUSDT"] = 0.17
            await bot.avg_manager.process(client, rm, "BILLUSDT", "LONG", st_long, st_short, 0.17, bot.spec_data, bot.tp_manager)
            self.assertTrue(st_long.grid["4"]["is_active"])
            self.assertEqual(st_long.cur_step, 4)
            # TP now switches to L4 (min_tp 1.0% * 1.3 = 1.3%)
            self.assertAlmostEqual(st_long.tp_current_indent, 1.3, places=3)

            # Failed order rollback test: reset & reject make_order
            st_long.reset()
            orig_make = client.make_order
            async def fail_make(*args, **kwargs):
                return MagicMock(success=False, error_msg="Margin Rejected")
            client.make_order = fail_make
            await tcm.process_signal("BILLUSDT", "LONG", cfgs["LONG"], current_price=0.20)
            client.make_order = orig_make
            self.assertFalse(st_long.in_position)
            self.assertEqual(st_long._pending_absorbed_levels, [])
            for k in ("0", "1", "2", "3"):
                self.assertFalse(st_long.grid[k]["is_active"])
                self.assertNotIn("_absorbed_to_l0", st_long.grid[k])

        asyncio.run(_run())

    def test_corridor_calculator_skips_absorbed_levels(self):
        if not BT_DIR.exists():
            self.skipTest("cron3TradeBacktest not available")
        if str(BT_DIR) not in sys.path:
            sys.path.insert(0, str(BT_DIR))
        from ENGINE.corridor_calculator import CorridorCalculator

        _, strat, cfgs = _build_zenith13_runtime()
        st_l = PositionState(symbol="BILLUSDT", side="LONG")
        st_l.update_from_dict({"grid": cfgs["LONG"]["grid"], "tp_map": cfgs["LONG"]["tp_map"], "in_position": True})
        st_l.initial_entry_price = 0.20
        st_l.avg_entry_price = 0.20
        st_l.grid["0"]["is_active"] = True
        for k, price_val in (("1", 0.194), ("2", 0.189), ("3", 0.182), ("4", 0.170)):
            st_l.grid[k]["price"] = price_val
        CounterGridFrontloader.apply_absorbed_levels(st_l, ["1", "2", "3"], current_time_ms=1778500000000)

        st_s = PositionState(symbol="BILLUSDT", side="SHORT", in_position=True)
        fsm = {"BILLUSDT": {"LONG": st_l, "SHORT": st_s}}
        c_low, _, _ = CorridorCalculator.calculate_corridor(
            symbol="BILLUSDT", fsm_states=fsm, open_orders={}, prices={"BILLUSDT": (0.20, 1778500000.0)},
            current_sim_time=1778500000.0, strategy=strat, time_control=TimeControl(interval="5m", smart_grace_cfg=strat.smart_grace_cfg)
        )
        # c_low must be L4 (0.170), NOT L1 (0.194)!
        self.assertAlmostEqual(c_low, 0.170, places=5)


if __name__ == "__main__":
    unittest.main()
