import unittest
import time
from CORE.auto_closer import AutoCloser

class TestAutoCloserStagnation(unittest.TestCase):
    def test_match_rule_float(self):
        # Operators test
        self.assertTrue(AutoCloser.match_rule_float(">1.0", 1.5))
        self.assertFalse(AutoCloser.match_rule_float(">1.0", 0.9))
        self.assertTrue(AutoCloser.match_rule_float(">=0.5", 0.5))
        self.assertTrue(AutoCloser.match_rule_float("<4.0", 3.2))
        self.assertFalse(AutoCloser.match_rule_float("<4.0", 4.0))
        self.assertTrue(AutoCloser.match_rule_float("<=4.0", 4.0))
        self.assertTrue(AutoCloser.match_rule_float("==2.5", 2.5))
        self.assertFalse(AutoCloser.match_rule_float("==2.5", 2.55))

    def test_stagnation_velocity_card(self):
        pos_cfg = {
            "threshold": 21.0,
            "threshold_increment": 21.0,
            "stagnation": {
                "enabled": True,
                "min_floor": 7.0,
                "mode": "velocity",
                "stagnation_card": {
                    ">1.0": 21.0,
                    ">0.4": 12.0,
                    "<=0.4": 8.0
                }
            }
        }
        pos_cfg["stagnation"]["enabled"] = True

        # High velocity: should keep standard target 21.0
        analytics_high = {"net_velocity": 1.5, "peak_age_hours": 0.5}
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target(analytics_high, pos_cfg)
        self.assertEqual(target, 21.0)
        self.assertFalse(is_stag)

        # Medium velocity: should drop to 12.0
        analytics_med = {"net_velocity": 0.7, "peak_age_hours": 2.5}
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target(analytics_med, pos_cfg)
        self.assertEqual(target, 12.0)
        self.assertTrue(is_stag)
        self.assertIn("12.0", info)

        # Low velocity (stagnation): should drop to 8.0
        analytics_low = {"net_velocity": 0.2, "peak_age_hours": 5.0}
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target(analytics_low, pos_cfg)
        self.assertEqual(target, 8.0)
        self.assertTrue(is_stag)
        self.assertIn("8.0", info)

    def test_stagnation_min_floor_enforcement(self):
        pos_cfg = {
            "threshold": 21.0,
            "stagnation": {
                "enabled": True,
                "min_floor": 7.0,
                "mode": "velocity",
                "stagnation_card": {
                    "<=0.1": 4.0  # Attempt to set below floor
                }
            }
        }
        analytics = {"net_velocity": 0.05}
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target(analytics, pos_cfg)
        # Should be clamped to min_floor = 7.0
        self.assertEqual(target, 7.0)

    def test_flat_stagnation_card_without_outer_threshold(self):
        # Clean config with NO redundant outer threshold / threshold_increment
        pos_cfg = {
            "min_floor": 7.0,
            "mode": "velocity",
            "stagnation_card": {
                ">1.0": 21.0,
                ">0.4": 12.0,
                "<=0.4": 8.0
            }
        }
        # High velocity
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"net_velocity": 1.2}, pos_cfg)
        self.assertEqual(target, 21.0)
        self.assertFalse(is_stag)
        self.assertEqual(ref_top, 21.0)

        # Low velocity (stagnation)
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"net_velocity": 0.2}, pos_cfg)
        self.assertEqual(target, 8.0)
        self.assertTrue(is_stag)
        self.assertEqual(ref_top, 21.0)

    def test_stagnation_card_per_level_toggles(self):
        # 1. Intermediate level disabled via {"target": 12.0, "enabled": False}
        pos_cfg = {
            "enabled": True,
            "min_floor": 7.0,
            "mode": "velocity",
            "stagnation_card": {
                ">1.0": {"target": 21.0, "enabled": True},
                ">0.4": {"target": 12.0, "enabled": False},
                "<=0.4": {"target": 8.0, "enabled": True}
            }
        }
        # Velocity 0.7 matches disabled rule >0.4 -> should fall back to reference_top (21.0)
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"net_velocity": 0.7}, pos_cfg)
        self.assertEqual(target, 21.0)
        self.assertFalse(is_stag)
        self.assertEqual(ref_top, 21.0)

        # Velocity 0.2 matches <=0.4 (enabled) -> target 8.0
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"net_velocity": 0.2}, pos_cfg)
        self.assertEqual(target, 8.0)
        self.assertTrue(is_stag)

        # 2. Disabled via null / None
        pos_cfg_null = {
            "min_floor": 7.0,
            "mode": "velocity",
            "stagnation_card": {
                ">1.0": 21.0,
                ">0.4": None,  # Disabled
                "<=0.4": 8.0
            }
        }
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"net_velocity": 0.7}, pos_cfg_null)
        self.assertEqual(target, 21.0)
        self.assertFalse(is_stag)

        # 3. Master toggle disabled (enabled: False)
        pos_cfg_disabled = {
            "enabled": False,
            "min_floor": 7.0,
            "mode": "velocity",
            "stagnation_card": {
                ">1.0": 21.0,
                "<=0.4": 8.0
            }
        }
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"net_velocity": 0.2}, pos_cfg_disabled)
        self.assertIsNone(target)
        self.assertFalse(is_stag)

        # 4. All levels disabled -> returns None
        pos_cfg_all_off = {
            "min_floor": 7.0,
            "mode": "velocity",
            "stagnation_card": {
                ">1.0": {"target": 21.0, "enabled": False},
                "<=0.4": None
            }
        }
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"net_velocity": 0.2}, pos_cfg_all_off)
        self.assertIsNone(target)

    def test_iteration_telemetry_priority(self):
        # When iteration_net_velocity is provided, it must take precedence over lifetime net_velocity
        pos_cfg = {
            "min_floor": 7.0,
            "mode": "velocity",
            "stagnation_card": {
                ">1.0": 21.0,
                ">0.4": 12.0,
                "<=0.4": 8.0
            }
        }
        # Lifetime net_velocity is low (0.2), but current iteration velocity is high (1.5)
        analytics = {
            "net_velocity": 0.2,
            "iteration_net_velocity": 1.5,
            "iteration_net_profit_usdt": 5.0,
            "iteration_peak_age_hours": 0.2
        }
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target(analytics, pos_cfg)
        self.assertEqual(target, 21.0)
        self.assertFalse(is_stag)

    def test_percentage_velocity_rules_top_target(self):
        """Проверка работы правил с процентами от верхнего таргета (top_target: 21.0 -> 5%=1.05$/ч, 2%=0.42$/ч)."""
        pos_cfg = {
            "min_floor": 7.0,
            "mode": "velocity",
            "velocity_pct_of": "top_target",
            "stagnation_card": {
                ">5.0%": {"target": 21.0, "enabled": True},
                ">2.0%": {"target": 13.0, "enabled": True},
                "<=2.0%": {"target": 8.0, "enabled": True}
            }
        }

        # 1. Высокая скорость (1.5 $/ч > 1.05 $/ч) -> держим таргет 21.0
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"iteration_net_velocity": 1.5}, pos_cfg)
        self.assertEqual(target, 21.0)
        self.assertFalse(is_stag)
        self.assertEqual(ref_top, 21.0)
        self.assertIn("&gt;5.0%", info)
        self.assertIn("1.05$/h", info)

        # 2. Умеренная скорость (0.8 $/ч: между 0.42 и 1.05 $/ч) -> снижаем до 13.0 (Фибо)
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"iteration_net_velocity": 0.8}, pos_cfg)
        self.assertEqual(target, 13.0)
        self.assertTrue(is_stag)
        self.assertIn("&gt;2.0%", info)
        self.assertIn("0.42$/h", info)

        # 3. Затухание / стагнация (0.2 $/ч <= 0.42 $/ч) -> фиксируем базовый рэтчет 8.0
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"iteration_net_velocity": 0.2}, pos_cfg)
        self.assertEqual(target, 8.0)
        self.assertTrue(is_stag)
        self.assertIn("&lt;=2.0%", info)
        self.assertIn("0.42$/h", info)

    def test_scale_invariance_10x(self):
        """Проверка инвариантности к масштабированию объема в 10 раз (таргеты 210, 130, 80)."""
        pos_cfg_10x = {
            "min_floor": 70.0,
            "mode": "velocity",
            "velocity_pct_of": "top_target",
            "stagnation_card": {
                ">5.0%": {"target": 210.0, "enabled": True},
                ">2.0%": {"target": 130.0, "enabled": True},
                "<=2.0%": {"target": 80.0, "enabled": True}
            }
        }
        # 5% от 210 = 10.5 $/ч, 2% от 210 = 4.2 $/ч

        # Скорость 12.0 $/ч > 10.5 $/ч -> максимальный таргет 210.0
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"iteration_net_velocity": 12.0}, pos_cfg_10x)
        self.assertEqual(target, 210.0)
        self.assertFalse(is_stag)
        self.assertIn("10.50$/h", info)

        # Скорость 6.0 $/ч (между 4.2 и 10.5) -> таргет 130.0
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"iteration_net_velocity": 6.0}, pos_cfg_10x)
        self.assertEqual(target, 130.0)
        self.assertTrue(is_stag)
        self.assertIn("4.20$/h", info)

        # Скорость 2.0 $/ч <= 4.2 $/ч -> таргет 80.0
        target, is_stag, info, ref_top = AutoCloser.evaluate_stagnation_target({"iteration_net_velocity": 2.0}, pos_cfg_10x)
        self.assertEqual(target, 80.0)
        self.assertTrue(is_stag)

    def test_gradient_cascade_full_cycle(self):
        """Тестирование градиентного каскада 1h -> 6h -> 12h -> 24h."""
        pos_cfg = {
            "enabled": True,
            "mode": "velocity",
            "timeframes": {
                "1h": {
                    "enabled": True,
                    "min_age_hours": 0.0,
                    "card": {
                        ">3.0": {"target": 55.0, "enabled": True},
                        ">2.0": {"target": 34.0, "enabled": True},
                        ">1.0": {"target": 21.0, "enabled": True},
                        ">0.4": {"target": 12.0, "enabled": True},
                        "<=0.4": {"target": 8.0, "enabled": True}
                    }
                },
                "6h": {
                    "enabled": True,
                    "min_age_hours": 6.0,
                    "card": {
                        ">2.5": {"target": 34.0, "enabled": True},
                        ">1.5": {"target": 21.0, "enabled": True},
                        ">0.8": {"target": 13.0, "enabled": True},
                        ">0.3": {"target": 8.0, "enabled": True},
                        "<=0.3": {"target": 6.0, "enabled": True}
                    }
                },
                "12h": {
                    "enabled": True,
                    "min_age_hours": 12.0,
                    "card": {
                        ">2.0": {"target": 21.0, "enabled": True},
                        ">1.0": {"target": 13.0, "enabled": True},
                        ">0.5": {"target": 8.0, "enabled": True},
                        ">0.2": {"target": 5.5, "enabled": True},
                        "<=0.2": {"target": 4.5, "enabled": True}
                    }
                },
                "24h": {
                    "enabled": True,
                    "min_age_hours": 24.0,
                    "card": {
                        ">1.5": {"target": 13.0, "enabled": True},
                        ">0.8": {"target": 8.0, "enabled": True},
                        ">0.3": {"target": 5.0, "enabled": True},
                        "<=0.3": {"target": 3.8, "enabled": True}
                    }
                }
            }
        }

        now_ms = int(time.time() * 1000)

        # 1. Отрицательный Net: фиксация категорически запрещена
        analytics_neg = {
            "iteration_net_profit_usdt": -2.5,
            "iteration_net_velocity": 0.1,
            "iteration_start_ts": now_ms - int(30 * 3600 * 1000)
        }
        th, is_trig, info, ref = AutoCloser.evaluate_stagnation_target(analytics_neg, pos_cfg)
        self.assertFalse(is_trig)
        self.assertIsNone(th)

        # 2. Сегодняшний реальный кейс (28.8ч в позе, Net +4.74 USDT, скорость 0.2 $/ч):
        # 1h (порог 8.0) не взят, 6h (порог 6.0) не взят, 12h (порог 4.5) - взят!
        analytics_today_474 = {
            "iteration_net_profit_usdt": 4.74,
            "iteration_net_velocity": 0.2,
            "iteration_start_ts": now_ms - int(28.8 * 3600 * 1000)
        }
        th, is_trig, info, ref = AutoCloser.evaluate_stagnation_target(analytics_today_474, pos_cfg)
        self.assertTrue(is_trig)
        self.assertEqual(th, 4.5)
        self.assertIn("12H", info)

        # 3. Случай глубокой стагнации на 28ч при профите +3.90 USDT (скорость 0.15 $/ч):
        # 1h, 6h, 12h (порог 4.5) не взяты -> срабатывает 24h с порогом 3.8 USDT!
        analytics_today_390 = {
            "iteration_net_profit_usdt": 3.90,
            "iteration_net_velocity": 0.15,
            "iteration_start_ts": now_ms - int(28.8 * 3600 * 1000)
        }
        th, is_trig, info, ref = AutoCloser.evaluate_stagnation_target(analytics_today_390, pos_cfg)
        self.assertTrue(is_trig)
        self.assertEqual(th, 3.8)
        self.assertIn("24H", info)

        # 4. Проверка отключения таймфрейма ("enabled": False)
        # Отключаем 24h:
        pos_cfg["timeframes"]["24h"]["enabled"] = False
        pos_cfg["timeframes"]["12h"]["enabled"] = False
        # Теперь для профита 3.90$ срабатывания не будет:
        th, is_trig, info, ref = AutoCloser.evaluate_stagnation_target(analytics_today_390, pos_cfg)
        self.assertFalse(is_trig)

        # 5. Гипер-памп на старом цикле: не продешевить!
        # Даже если циклу 28 часов, при резком пампе (скорость 3.5 $/ч, Net +56.0 USDT)
        # 1h карта срабатывает первой и забирает 55.0 USDT, а не 3.8 USDT!
        pos_cfg["timeframes"]["12h"]["enabled"] = True
        pos_cfg["timeframes"]["24h"]["enabled"] = True
        analytics_pump = {
            "iteration_net_profit_usdt": 56.0,
            "iteration_net_velocity": 3.5,
            "iteration_start_ts": now_ms - int(28.8 * 3600 * 1000)
        }
        th, is_trig, info, ref = AutoCloser.evaluate_stagnation_target(analytics_pump, pos_cfg)
        self.assertTrue(is_trig)
        self.assertEqual(th, 55.0)
        self.assertIn("1H", info)

    def test_trailing_ratchet_age_gating_and_pullback(self):
        ratchet_cfg = {
            "enabled": True,
            "levels": [
                { "activation": 5.0,  "floor": 3.8,  "min_age_hours": 24.0, "enabled": True, "_desc": "Выныривание из ямы" },
                { "activation": 7.5,  "floor": 5.5,  "min_age_hours": 0.0,  "enabled": True, "_desc": "Локальный отскок" },
                { "activation": 11.0, "floor": 8.0,  "min_age_hours": 0.0,  "enabled": True, "_desc": "Уверенное движение" },
                { "activation": 17.0, "floor": 13.0, "min_age_hours": 0.0,  "enabled": True, "_desc": "Импульсный пробой" },
                { "activation": 27.5, "floor": 21.0, "min_age_hours": 0.0,  "enabled": True, "_desc": "Тренд средней силы" },
            ]
        }

        # 1. Молодой цикл (2 часа): Level 0 (min_age 24h) заблокирован возрастным цензом!
        # При Net = +5.2 USDT курок не взводится (молодой цикл не должен выходить рано на микро-профите)
        state_young = {}
        should_close, floor, info, is_stag = AutoCloser.evaluate_trailing_ratchet(
            iter_net=5.2, iter_age_hours=2.0, ratchet_cfg=ratchet_cfg, state=state_young
        )
        self.assertFalse(should_close)
        self.assertIsNone(floor)
        self.assertIn("RATCHET IDLE", info)

        # 2. Молодой цикл разгоняется до +7.6 USDT -> Level 1 (act 7.5, floor 5.5, min_age 0h) взводит пол 5.5!
        should_close, floor, info, is_stag = AutoCloser.evaluate_trailing_ratchet(
            iter_net=7.6, iter_age_hours=2.5, ratchet_cfg=ratchet_cfg, state=state_young
        )
        self.assertFalse(should_close)  # Пока растет - держим!
        self.assertEqual(floor, 5.5)
        self.assertEqual(state_young["armed_floor"], 5.5)
        self.assertEqual(state_young["active_level_idx"], 1)

        # 3. Дальнейший памп до +12.0 USDT -> Ступенька шагает на Level 2 (floor 8.0)!
        should_close, floor, info, is_stag = AutoCloser.evaluate_trailing_ratchet(
            iter_net=12.0, iter_age_hours=3.0, ratchet_cfg=ratchet_cfg, state=state_young
        )
        self.assertFalse(should_close)
        self.assertEqual(floor, 8.0)
        self.assertEqual(state_young["armed_floor"], 8.0)
        self.assertEqual(state_young["highest_net"], 12.0)

        # 4. Откат: Net снижается до +8.0 USDT -> Пол 8.0 срабатывает! Позиции закрываются в +8.0 USDT!
        should_close, floor, info, is_stag = AutoCloser.evaluate_trailing_ratchet(
            iter_net=8.0, iter_age_hours=3.2, ratchet_cfg=ratchet_cfg, state=state_young
        )
        self.assertTrue(should_close)
        self.assertEqual(floor, 8.0)
        self.assertIn("L2", info)

        # 5. Старый цикл (28.8 часов в просадке / стагнации):
        # Level 0 (min_age 12h) РАЗРЕШЕН! При Net = +5.1 USDT взводится аварийный пол 3.8 USDT!
        state_old = {}
        should_close, floor, info, is_stag = AutoCloser.evaluate_trailing_ratchet(
            iter_net=5.1, iter_age_hours=28.8, ratchet_cfg=ratchet_cfg, state=state_old
        )
        self.assertFalse(should_close)
        self.assertEqual(floor, 3.8)
        self.assertTrue(is_stag)

        # Откат старого цикла к 3.8 USDT -> Фиксация выхода из ямы!
        should_close, floor, info, is_stag = AutoCloser.evaluate_trailing_ratchet(
            iter_net=3.8, iter_age_hours=29.0, ratchet_cfg=ratchet_cfg, state=state_old
        )
        self.assertTrue(should_close)
        self.assertEqual(floor, 3.8)
        self.assertTrue(is_stag)
        self.assertIn("Выныривание из ямы", info)


if __name__ == "__main__":
    unittest.main()


