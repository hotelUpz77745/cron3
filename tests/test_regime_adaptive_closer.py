# ============================================================
# Path: tests/test_regime_adaptive_closer.py
# Role: Юнит-тесты и parity-проверки HRON-4 RegimeAdaptiveCloser (PEACE <-> STRESS)
# ============================================================

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_BT_ROOT = os.path.abspath(os.path.join(_ROOT, "..", "cron3TradeBacktest"))
if _BT_ROOT not in sys.path:
    sys.path.insert(0, _BT_ROOT)

from consts import CFG_PATH
from CORE.SERVICES.auto_closer_state_store import AutoCloserStateStore
from CORE.STRATEGY.auto_closing_builder import AutoClosingConfigBuilder
from CORE.STRATEGY.profit_closer_common import BucketedCloserRouter
from CORE.STRATEGY.regime_adaptive_closer import RegimeAdaptiveCloser
from CORE.strategy_registry import StrategyRegistry


def _load_u96() -> dict:
    return json.loads((Path(_ROOT) / "CFG" / "universes" / "u96_omni_zenith13.json").read_text(encoding="utf-8"))


class _DummyPos:
    def __init__(self, symbol: str, qty: float, entry_price: float, leverage: int = 20):
        self.symbol = symbol
        self.qty = qty
        self.entry_price = entry_price
        self.leverage = leverage


class _DummyFSM:
    def __init__(self, in_position: bool, cur_step: int):
        self.in_position = in_position
        self.cur_step = cur_step


class TestRegimeAdaptiveCloser(unittest.TestCase):
    def setUp(self):
        self.u96 = _load_u96()
        self.ra_cfg = self.u96["auto_closing"]["positive"]["regime_adaptive"]
        self.pos_cfg = AutoClosingConfigBuilder.build_mode_auto_closing(
            "regime_adaptive", self.u96["auto_closing"]
        )["positive"]

    def test_passport_ssot_and_builder(self):
        self.assertIn("regime_adaptive", self.u96["_auto_closing_modes_allowed"])
        self.assertEqual(self.u96["auto_closing"]["mode"], "regime_adaptive")
        self.assertNotIn("enabled", self.ra_cfg)
        self.assertNotIn("enabled", self.ra_cfg["peace"])
        self.assertNotIn("enabled", self.ra_cfg["stress"]["trailing_ratchet"])
        self.assertTrue(BucketedCloserRouter.is_supported("regime_adaptive"))
        self.assertEqual(self.pos_cfg["mode"], "regime_adaptive")
        self.assertIn("regime_adaptive", self.pos_cfg)

        u96_copy = json.loads(json.dumps(self.u96))
        reg = StrategyRegistry(
            active_strategies_cfg={
                "custom_ra_strategy": {
                    "template": "u96_omni_zenith13",
                    "enabled": True,
                    "symbols": ["ETHUSDT"],
                    "default_invest_size": 1200.0,
                }
            },
            universes_db={"u96_omni_zenith13": u96_copy},
        )
        sc = reg.active_strategies["custom_ra_strategy"]
        self.assertEqual(sc.auto_closing_mode, "regime_adaptive")
        self.assertEqual(sc.auto_closing_cfg["mode"], "regime_adaptive")

    def test_peace_ma_crossover_and_slope_filter(self):
        closer = RegimeAdaptiveCloser(self.ra_cfg, min_guaranteed_profit_usdt=5.0)
        state = {}
        t0 = 1_700_000_040.0
        # Разогрев 50 x 5 = 250 бакетов растущей прибыли -> перелом вниз
        for i in range(245):
            ok, _, _ = closer.evaluate(40.0 + i * 0.25, t0 + i * 60.0, state, scale_money=2.0, iter_age_hours=2.0)
            self.assertFalse(ok)
            self.assertEqual(state["regime"], "PEACE")

        # Добавляем ещё 8 растущих, затем резкий спад при высоком профите (> $40)
        for i in range(245, 255):
            ok, _, _ = closer.evaluate(95.0 + (i - 245) * 0.5, t0 + i * 60.0, state, scale_money=2.0, iter_age_hours=4.3)
            self.assertFalse(ok)

        # Падение на текущем профите выше $40 -> пересечение MA15 <= MA250
        triggered = False
        for i in range(255, 290):
            ok, trig, info = closer.evaluate(45.0, t0 + i * 60.0, state, scale_money=2.0, iter_age_hours=4.8)
            if ok:
                triggered = True
                self.assertIn("[REGIME:PEACE]", info)
                self.assertIsNotNone(trig)
                break
        self.assertTrue(triggered)

        # Вариант со slope-фильтром блокирует выход, если медленная MA еще растет
        slope_cfg = json.loads(json.dumps(self.ra_cfg))
        slope_cfg["peace"]["require_slow_ma_non_growing"] = True
        closer_slope = RegimeAdaptiveCloser(slope_cfg, min_guaranteed_profit_usdt=5.0)
        state_slope = {}
        for i in range(260):
            closer_slope.evaluate(10.0 + i * 0.5, t0 + i * 60.0, state_slope, scale_money=2.0, iter_age_hours=2.0)
        # Пока slow MA ещё растет из-за резкого ралли перед откатом, один откат до 45$ блокируется до разворота slow MA
        ok_early, _, _ = closer_slope.evaluate(45.0, t0 + 260 * 60.0, state_slope, scale_money=2.0, iter_age_hours=4.4)
        self.assertFalse(ok_early)

    def test_peace_to_stress_triggers_and_hysteresis(self):
        closer = RegimeAdaptiveCloser(self.ra_cfg, min_guaranteed_profit_usdt=5.0)
        t0 = 1_700_000_040.0

        # Trigger 1: lvl >= 4 (Early Stress winner config)
        st1 = {}
        closer.evaluate(5.0, t0, st1, scale_money=2.0, iter_age_hours=5.0, max_active_level=3, load_ratio=1.0)
        self.assertEqual(st1["regime"], "PEACE")
        closer.evaluate(5.0, t0 + 60.0, st1, scale_money=2.0, iter_age_hours=5.02, max_active_level=4, load_ratio=1.0)
        self.assertEqual(st1["regime"], "STRESS")

        # Trigger 2: load >= 2.2
        st2 = {}
        closer.evaluate(5.0, t0, st2, scale_money=2.0, iter_age_hours=5.0, max_active_level=1, load_ratio=2.21)
        self.assertEqual(st2["regime"], "STRESS")

        # Trigger 3: age >= 36h and lvl >= 2
        st3 = {}
        closer.evaluate(5.0, t0, st3, scale_money=2.0, iter_age_hours=35.9, max_active_level=2, load_ratio=1.0)
        self.assertEqual(st3["regime"], "PEACE")
        closer.evaluate(5.0, t0 + 600.0, st3, scale_money=2.0, iter_age_hours=50.0, max_active_level=1, load_ratio=1.0)
        self.assertEqual(st3["regime"], "PEACE")
        closer.evaluate(5.0, t0 + 660.0, st3, scale_money=2.0, iter_age_hours=36.0, max_active_level=2, load_ratio=1.0)
        self.assertEqual(st3["regime"], "STRESS")

        # Hysteresis hold 45 min + unload (lvl <= 1 & load <= 1.5)
        # Непрерывно проверяем, что profit_history продолжает копиться в STRESS!
        for k in range(1, 44):
            closer.evaluate(6.0, t0 + 660.0 + k * 60.0, st3, scale_money=2.0, iter_age_hours=10.0, max_active_level=1, load_ratio=1.2)
            self.assertEqual(st3["regime"], "STRESS")
        self.assertGreaterEqual(len(st3["profit_history"]), 43)

        # На 45-й минуте после разгрузки возвращается в PEACE и очищает regime_stress_state
        closer.evaluate(6.0, t0 + 660.0 + 45 * 60.0, st3, scale_money=2.0, iter_age_hours=11.0, max_active_level=1, load_ratio=1.2)
        self.assertEqual(st3["regime"], "PEACE")
        self.assertNotIn("regime_stress_state", st3)

    def test_stress_ratchet_chronos_and_positive_profit_guard(self):
        closer = RegimeAdaptiveCloser(self.ra_cfg, min_guaranteed_profit_usdt=5.0)
        t0 = 1_700_000_040.0
        state = {}

        # Заходим в STRESS по lvl=4 при scale_money=2.0 -> L0 activation=16$, floor=10$, L1 activation=26$, floor=16$
        ok, _, _ = closer.evaluate(26.5, t0, state, scale_money=2.0, iter_age_hours=10.0, max_active_level=4, load_ratio=2.0)
        self.assertFalse(ok)
        self.assertEqual(state["regime"], "STRESS")
        self.assertEqual(state["regime_stress_state"]["armed_floor"], 16.0)

        # При просадке ниже нуля Positive Profit Guard жестко блокирует срабатывание
        ok_neg, _, _ = closer.evaluate(-3.0, t0 + 60.0, state, scale_money=2.0, iter_age_hours=10.02, max_active_level=4, load_ratio=2.0)
        self.assertFalse(ok_neg)

        # Набирает +26.5 и откатывает к +15.8 (<= 16.0, > 0) -> STRESS Ratchet закрывает в плюс!
        closer.evaluate(26.5, t0 + 120.0, state, scale_money=2.0, iter_age_hours=10.03, max_active_level=4, load_ratio=2.0)
        ok_pull, floor_val, info = closer.evaluate(15.8, t0 + 180.0, state, scale_money=2.0, iter_age_hours=10.05, max_active_level=4, load_ratio=2.0)
        self.assertTrue(ok_pull)
        self.assertEqual(floor_val, 16.0)
        self.assertIn("STRESS", info)

        # Chronos 36h+ / +$8 * scale = +$8 (при scale_money=1.0)
        st_ch = {}
        ok_ch, ch_fl, ch_info = closer.evaluate(8.2, t0, st_ch, scale_money=1.0, iter_age_hours=36.5, max_active_level=4, load_ratio=1.0)
        self.assertTrue(ok_ch)
        self.assertEqual(ch_fl, 8.0)
        self.assertIn("STRESS", ch_info)

    def test_corridor_bounds_and_prod_backtest_parity(self):
        from ENGINE.auto_closing_evaluator import BacktestAutoClosingEvaluator

        ac_cfg = AutoClosingConfigBuilder.build_mode_auto_closing("regime_adaptive", self.u96["auto_closing"])
        start_ts = 1_700_000_000.0
        state_bt = {}
        state_prod = {}
        positions = {("SOLUSDT", "SHORT"): _DummyPos("SOLUSDT", qty=10.0, entry_price=200.0)}  # notional 2000
        fsm_lvl2 = {"SOLUSDT": {"SHORT": _DummyFSM(True, 2), "LONG": _DummyFSM(False, 0)}}
        # initial_balance = 1200 -> load_ratio = 2000/1200 = 1.667 (<2.2), но age 36h + lvl 2 включат STRESS!

        # 1) Тик 0: last_sample_bucket еще None -> коридор обязан разбудить сразу
        eq_l, eq_h, max_t, _ = BacktestAutoClosingEvaluator.get_equity_corridor(
            auto_closing_cfg=ac_cfg,
            cur_equity=1205.0,
            iter_start_equity=1200.0,
            current_sim_time=start_ts + 10.0,
            iter_start_time=start_ts,
            auto_closer_state=state_bt,
            positions=positions,
            fsm_states=fsm_lvl2,
            initial_balance=1200.0,
        )
        self.assertGreater(eq_l, eq_h)

        # Оценка на tick 0 (PEACE) в BACKTEST и PROD
        bt_ok, bt_tr, _ = BacktestAutoClosingEvaluator.evaluate(
            auto_closing_cfg=ac_cfg,
            cur_equity=1205.0,
            iter_start_equity=1200.0,
            current_sim_time=start_ts + 10.0,
            iter_start_time=start_ts,
            auto_closer_state=state_bt,
            positions=positions,
            fsm_states=fsm_lvl2,
            initial_balance=1200.0,
        )
        prod_ctx = RegimeAdaptiveCloser.extract_regime_context(
            fsm_states=fsm_lvl2,
            analytics_dict={
                "start_balance_usdt": 1200.0,
                "per_coin": {"SOLUSDT": {"current_grid_load_usdt": 2000.0}},
            },
            strat_cfg=SimpleNamespace(start_balance_usdt=1200.0),
        )
        prod_ok, prod_tr, _ = BucketedCloserRouter.evaluate(
            mode="regime_adaptive",
            pos_cfg=ac_cfg["positive"],
            iter_net=5.0,
            now_sec=start_ts + 10.0,
            iter_age_hours=10.0 / 3600.0,
            state=state_prod,
            scale_money=BacktestAutoClosingEvaluator.resolve_scale_money(ac_cfg["positive"], positions, 1200.0),
            extra_context=prod_ctx,
        )
        self.assertEqual((bt_ok, bt_tr), (prod_ok, prod_tr))
        self.assertEqual(state_bt["regime"], "PEACE")
        self.assertEqual(state_prod["regime"], "PEACE")

        # Когда возраст 35.99h при lvl=2, max_valid_t ограничен age=36h (не перескакивает 36h порог)
        t_near_stress = start_ts + 36.0 * 3600.0 - 5.0
        state_bt["last_sample_bucket"] = int(t_near_stress // 60)
        eq_l2, eq_h2, max_t2, _ = BacktestAutoClosingEvaluator.get_equity_corridor(
            auto_closing_cfg=ac_cfg,
            cur_equity=1205.0,
            iter_start_equity=1200.0,
            current_sim_time=t_near_stress,
            iter_start_time=start_ts,
            auto_closer_state=state_bt,
            positions=positions,
            fsm_states=fsm_lvl2,
            initial_balance=1200.0,
        )
        self.assertLess(eq_l2, 1205.0)
        self.assertLessEqual(max_t2, start_ts + 36.0 * 3600.0)

        # Персистентность STATE на диске через AutoCloserStateStore (regime, stress_state, profit_history)
        with tempfile.TemporaryDirectory() as tmp:
            store1 = AutoCloserStateStore(state_file=Path(tmp) / "ac_state.json")
            state_bt["regime"] = "STRESS"
            state_bt["regime_entered_at_sec"] = t_near_stress + 10.0
            state_bt["regime_stress_state"] = {"armed_floor": 16.0, "active_level_idx": 0}
            store1.maybe_persist("s1", 1, state_bt, force=True)

            # Повторный не-forced save должен увидеть изменение сигнатуры при обновлении stress armed_floor
            state_bt["regime_stress_state"]["armed_floor"] = 26.0
            saved_second = store1.maybe_persist("s1", 1, state_bt, force=False)
            self.assertTrue(saved_second)

            store2 = AutoCloserStateStore(state_file=Path(tmp) / "ac_state.json")
            loaded = store2.load_all()
            self.assertEqual(loaded["s1"]["regime"], "STRESS")
            self.assertEqual(loaded["s1"]["regime_stress_state"]["armed_floor"], 26.0)
            self.assertEqual(loaded["s1"]["profit_history"], state_bt["profit_history"])

    def test_regime_adaptive_v2_ratchet_smoothed_stress(self):
        ra_v2_cfg = json.loads(json.dumps(self.ra_cfg))
        ra_v2_cfg["stress"]["mode"] = "ratchet_smoothed_drawdown"
        ra_v2_cfg["stress"]["ratchet_smoothed_drawdown"] = {
            "sample_interval_sec": 60,
            "min_samples": 10,
            "ma_window_samples": 5,
            "max_drawdown_ratio": 0.22,
            "chronos_exit": {
                "enabled": False,
                "activation": 8.0,
                "floor": 8.0,
                "min_age_hours": 36.0,
            },
        }
        closer = RegimeAdaptiveCloser(ra_v2_cfg, min_guaranteed_profit_usdt=5.0)
        self.assertEqual(closer.stress_mode, "ratchet_smoothed_drawdown")
        self.assertIsNotNone(closer.stress_smoothed_closer)

        state = {}
        t0 = 1_700_000_000.0

        # Run in STRESS (max_active_level=4, load_ratio=2.5)
        # Warmup with 10 samples rising to $100
        for i in range(10):
            net = 50.0 + i * 5.0
            ok, trig, info = closer.evaluate(
                iter_net=net,
                now_sec=t0 + i * 60.0,
                state=state,
                scale_money=1.0,
                iter_age_hours=1.0,
                max_active_level=4,
                load_ratio=2.5,
            )
            self.assertFalse(ok)
            self.assertEqual(state["regime"], "STRESS")

        # Peak reached 95.0. Levels are L0 ($13->$8), L1 ($21->$13), L2 ($34->$21), L3 ($55->$34), L4 ($89->$55)
        # For peak ~95, floor is $55. Trigger = peak * (1 - 0.22) = 95 * 0.78 = 74.1 > 55.
        # Now simulate drawdown: next 5 samples at $60.
        triggered = False
        for i in range(10, 16):
            ok, trig, info = closer.evaluate(
                iter_net=60.0,
                now_sec=t0 + i * 60.0,
                state=state,
                scale_money=1.0,
                iter_age_hours=1.5,
                max_active_level=4,
                load_ratio=2.5,
            )
            if ok:
                triggered = True
                self.assertIn("STRESS", info)
                self.assertIn("SMOOTH_DD", info)
                break
        self.assertTrue(triggered, "Smoothed drawdown must trigger exit during STRESS")


if __name__ == "__main__":
    unittest.main()
