# ==============================================================================
# Path: tests/test_auto_closer_toxicity.py
# Role: Comprehensive tests for 3-mode AutoCloser (Classic, Toxicity Adaptive, Radar Sync)
# ==============================================================================

import unittest
from unittest.mock import MagicMock
from CORE.trailing_system import BaseTrailingSystem, ToxicityEngine
from CORE.auto_closer import AutoCloser


class TestAutoCloserToxicityAndModes(unittest.TestCase):
    def setUp(self):
        self.ratchet_cfg = {
            "enabled": True,
            "quant_base_usdt": 10.0,
            "quant_reference_size": 600.0,
            "toxicity_adaptive": {
                "panic_toxicity_threshold": 0.70,
                "emergency_exit_floor_mult": 0.30,
                "min_panic_recovery": 0.40,
                "relaxed_toxicity_threshold": 0.35,
                "relaxed_min_level_idx": 1
            },
            "levels": [
                {"activation": 8.0, "floor": 5.5, "min_age_hours": 24.0, "enabled": True, "_desc": "L0: Стагнация 24h"},
                {"activation": 15.5, "floor": 13.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L1: Институциональный старт"},
                {"activation": 24.5, "floor": 21.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L2: Фибо 21"},
                {"activation": 39.0, "floor": 34.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L3: Фибо 34"},
                {"activation": 63.5, "floor": 55.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L4: Фибо 55"},
                {"activation": 89.0, "floor": 89.0, "min_age_hours": 0.0, "enabled": True, "_desc": "L5: Мега-ралли"}
            ]
        }

    def test_delta_recovery_calculation(self):
        """Проверка расчета фактора снятия стресса просадки R_delta."""
        # 1. На самом дне ямы (floating = max_dd) -> recovery = 0%
        rec_bottom = ToxicityEngine.calculate_delta_recovery(current_floating_pnl=-100.0, max_drawdown_iteration=-100.0)
        self.assertAlmostEqual(rec_bottom, 0.0, places=3)

        # 2. Просадка сократилась вдвое (-50 из -100) -> recovery = 50%
        rec_half = ToxicityEngine.calculate_delta_recovery(current_floating_pnl=-50.0, max_drawdown_iteration=-100.0)
        self.assertAlmostEqual(rec_half, 0.5, places=3)

        # 3. Просадка полностью рассосалась (0 из -100) -> recovery = 100%
        rec_full = ToxicityEngine.calculate_delta_recovery(current_floating_pnl=0.0, max_drawdown_iteration=-100.0)
        self.assertAlmostEqual(rec_full, 1.0, places=3)

    def test_toxicity_score_calculation(self):
        """Проверка нормированного Toxicity Score S in [0.0, 1.0]."""
        # Высокий стресс: 80% маржи, 7-е колено (7/8), 80% float ratio, 0% recovery
        high_score = ToxicityEngine.calculate_toxicity_score(
            margin_load=0.80, max_depth_ratio=7.0 / 8.0, float_ratio=0.80, delta_recovery=0.0
        )
        self.assertGreaterEqual(high_score, 0.70)

        # Легкий рынок: 5% маржи, 1-е колено, 10% float ratio, 100% recovery
        low_score = ToxicityEngine.calculate_toxicity_score(
            margin_load=0.05, max_depth_ratio=1.0 / 8.0, float_ratio=0.10, delta_recovery=1.0
        )
        self.assertLess(low_score, 0.35)

    def test_quant_scaling_10x(self):
        """Проверка автоматического масштабирования порогов в 10 раз при увеличении сайза."""
        state_1x = {}
        # Базовый сайз 600 USDT (scale = 1.0): курок L1 = 15.5
        should_close, floor, info, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=12.0, iter_age_hours=1.0, ratchet_cfg=dict(self.ratchet_cfg, scale_factor=1.0), state=state_1x
        )
        self.assertFalse(should_close)
        self.assertIsNone(floor)

        state_10x = {}
        # Сайз 6000 USDT (scale = 10.0): курок L1 = 155.0. При Net = 120$ не должен взводиться!
        should_close_10x, floor_10x, _, _ = BaseTrailingSystem.evaluate_ratchet(
            iter_net=120.0, iter_age_hours=1.0, ratchet_cfg=dict(self.ratchet_cfg, scale_factor=10.0), state=state_10x
        )
        self.assertFalse(should_close_10x)
        self.assertIsNone(floor_10x)

        # При Net = 160.0$ на сайзе 6000 USDT взводится L1 (floor = 130.0$)
        BaseTrailingSystem.evaluate_ratchet(
            iter_net=160.0, iter_age_hours=1.0, ratchet_cfg=dict(self.ratchet_cfg, scale_factor=10.0), state=state_10x
        )
        self.assertEqual(state_10x.get("armed_floor"), 130.0)

    def test_mode2_toxicity_adaptive_panic_evacuation(self):
        """Режим 2: При высокой токсичности (S >= 0.70) и отскоке ямы бот эвакуирует портфель при +3.0$."""
        state = {}
        # Токсичный инвентарь: S = 0.75, отскок ямы R_delta = 0.50, Net = +3.5$
        should_close, floor, info, is_stag = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=3.5,
            iter_age_hours=2.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
            toxicity_score=0.75,
            delta_recovery=0.50,
            scale_factor=1.0
        )
        self.assertTrue(should_close)
        self.assertEqual(floor, 3.0)
        self.assertIn("TOXICITY EVACUATION", info)

    def test_mode2_toxicity_adaptive_relaxed_target(self):
        """Способ 2: При низкой токсичности (S < 0.35) бот ждет полноценный целевой фикс 15.0 USDT."""
        state = {}
        # 1. Легкий рынок: S = 0.20, Net = 12.0$ (< 15.0$) -> не закрываемся
        sc_low, fl_low, _, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=12.0,
            iter_age_hours=2.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
            toxicity_score=0.20,
            delta_recovery=0.90,
            scale_factor=1.0
        )
        self.assertFalse(sc_low)
        self.assertIsNone(fl_low)

        # 2. Net = 16.0$ (>= 15.0$) -> прямой сброс корзины в кэш
        sc_hit, fl_hit, info_hit, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=16.0,
            iter_age_hours=2.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
            toxicity_score=0.20,
            delta_recovery=0.90,
            scale_factor=1.0
        )
        self.assertTrue(sc_hit)
        self.assertEqual(fl_hit, 15.0)
        self.assertIn("TOXICITY LIGHT FIX", info_hit)

    def test_toxicity_adaptive_never_closes_at_negative(self):
        """Инвариант безопасности: в положительной ветке автоклозера закрытие при iter_net <= 0 категорически запрещено!"""
        state = {}
        # Даже при критической панике (S=0.95) и отскоке ямы (R_delta=0.80), если iter_net отрицательный - НИКАКОГО ЗАКРЫТИЯ
        should_close, floor, _, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=-5.0,
            iter_age_hours=2.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
            toxicity_score=0.95,
            delta_recovery=0.80,
            scale_factor=1.0,
        )
        self.assertFalse(should_close)
        self.assertIsNone(floor)

        # При нулевом профите также не закрываемся
        should_close_zero, floor_zero, _, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=0.0,
            iter_age_hours=2.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
            toxicity_score=0.95,
            delta_recovery=0.80,
            scale_factor=1.0,
        )
        self.assertFalse(should_close_zero)
        self.assertIsNone(floor_zero)

    def test_mode2_toxicity_adaptive_moderate_and_relaxed(self):
        """Способ 2: Проверка умеренной (0.35 <= S < 0.70) и легкой (S < 0.35) фиксации прибыли."""
        state = {}
        # 1. Умеренная токсичность: S = 0.50, R_delta = 0.30, целевой фикс = 8.0 USDT
        # При Net = 6.0 USDT (< 8.0) не закрываемся
        sc_mod_low, _, _, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=6.0, iter_age_hours=2.0, ratchet_cfg=self.ratchet_cfg, state=state,
            toxicity_score=0.50, delta_recovery=0.30, scale_factor=1.0
        )
        self.assertFalse(sc_mod_low)

        # При Net = 8.5 USDT (>= 8.0) фиксируем умеренный тейк без трейлинга
        sc_mod_hit, fl_mod, info_mod, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=8.5, iter_age_hours=2.0, ratchet_cfg=self.ratchet_cfg, state=state,
            toxicity_score=0.50, delta_recovery=0.30, scale_factor=1.0
        )
        self.assertTrue(sc_mod_hit)
        self.assertEqual(fl_mod, 8.0)
        self.assertIn("TOXICITY MODERATE FIX", info_mod)

        # 2. Легкая токсичность: S = 0.20 (инвентарь свободен), целевой фикс = 15.0 USDT
        # При Net = 12.0 USDT (< 15.0) не закрываемся
        sc_light_low, _, _, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=12.0, iter_age_hours=2.0, ratchet_cfg=self.ratchet_cfg, state=state,
            toxicity_score=0.20, delta_recovery=0.90, scale_factor=1.0
        )
        self.assertFalse(sc_light_low)

        # При Net = 15.5 USDT (>= 15.0) фиксируем легкий тейк
        sc_light_hit, fl_light, info_light, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=15.5, iter_age_hours=2.0, ratchet_cfg=self.ratchet_cfg, state=state,
            toxicity_score=0.20, delta_recovery=0.90, scale_factor=1.0
        )
        self.assertTrue(sc_light_hit)
        self.assertEqual(fl_light, 15.0)
        self.assertIn("TOXICITY LIGHT FIX", info_light)

    def test_mode4_fixed_net(self):
        """Способ 3: Фиксация по прямому фиксированному значению Net профита итерации (Fixed Net)."""
        fixed_cfg = {
            "enabled": True,
            "target_net_usdt": 15.0,
            "scale_by_invest_size": False
        }
        # 1. При Net = 14.5 USDT (< 15.0) -> не закрываемся
        is_fixed, trig, info = ToxicityEngine.evaluate_fixed_net(
            iter_net=14.5, fixed_cfg=fixed_cfg, scale_factor=1.0
        )
        self.assertFalse(is_fixed)
        self.assertIsNone(trig)

        # 2. При Net = 15.2 USDT (>= 15.0) -> мгновенный сброс в кэш!
        is_fixed_hit, trig_hit, info_hit = ToxicityEngine.evaluate_fixed_net(
            iter_net=15.2, fixed_cfg=fixed_cfg, scale_factor=1.0
        )
        self.assertTrue(is_fixed_hit)
        self.assertEqual(trig_hit, 15.0)
        self.assertIn("FIXED NET PROFIT TARGET", info_hit)

        # 3. С масштабированием сайза 10x (цель 150.0 USDT)
        fixed_cfg_scaled = {
            "enabled": True,
            "target_net_usdt": 15.0,
            "scale_by_invest_size": True
        }
        is_scale_low, _, _ = ToxicityEngine.evaluate_fixed_net(
            iter_net=120.0, fixed_cfg=fixed_cfg_scaled, scale_factor=10.0
        )
        self.assertFalse(is_scale_low)

        is_scale_hit, trig_scale, _ = ToxicityEngine.evaluate_fixed_net(
            iter_net=155.0, fixed_cfg=fixed_cfg_scaled, scale_factor=10.0
        )
        self.assertTrue(is_scale_hit)
        self.assertEqual(trig_scale, 150.0)


if __name__ == "__main__":
    unittest.main()
