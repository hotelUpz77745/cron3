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

    def test_mode2_toxicity_adaptive_relaxed_skips_l1(self):
        """Режим 2: При низкой токсичности (S < 0.35) порог L1 (13$) игнорируется, курок ждет L2 (21$)."""
        state = {}
        # Легкий рынок: S = 0.20, Net = 16.0$ (пробил старый L1=15.5$, но в легком рынке L1 отключен!)
        should_close, floor, info, _ = ToxicityEngine.evaluate_toxicity_adaptive(
            iter_net=16.0,
            iter_age_hours=2.0,
            ratchet_cfg=self.ratchet_cfg,
            state=state,
            toxicity_score=0.20,
            delta_recovery=0.90,
            scale_factor=1.0
        )
        # Не закрываемся, потому что L1 отключен, курок ждет L2 (24.5$)
        self.assertFalse(should_close)
        self.assertIsNone(floor)

    def test_mode3_radar_sync_23h(self):
        """Режим 3: Синхронизация с 23-часовым окном radar_synergy."""
        sync_cfg = {
            "rotation_window_hours": 23.0,
            "pre_rotation_lead_minutes": 45.0,
            "daily_target_quant_mult": 1.5,
            "quant_base_usdt": 10.0
        }
        # 1. Возраст 10 часов (< 22.25ч) -> синхронизация не срабатывает
        is_sync_early, _, _ = ToxicityEngine.evaluate_radar_sync(
            iter_net=18.0, iter_age_hours=10.0, sync_cfg=sync_cfg, scale_factor=1.0
        )
        self.assertFalse(is_sync_early)

        # 2. Возраст 22.5 часа (>= 22.25ч), но профит 10$ (< 15$) -> не закрываемся
        is_sync_low_profit, _, _ = ToxicityEngine.evaluate_radar_sync(
            iter_net=10.0, iter_age_hours=22.5, sync_cfg=sync_cfg, scale_factor=1.0
        )
        self.assertFalse(is_sync_low_profit)

        # 3. Возраст 22.5 часа, профит 16.5$ (>= 15$) -> фиксируем прибыль перед ротацией!
        is_sync_trigger, target, info = ToxicityEngine.evaluate_radar_sync(
            iter_net=16.5, iter_age_hours=22.5, sync_cfg=sync_cfg, scale_factor=1.0
        )
        self.assertTrue(is_sync_trigger)
        self.assertEqual(target, 15.0)
        self.assertIn("RADAR 23H PRE-ROTATION HARVEST", info)


if __name__ == "__main__":
    unittest.main()
