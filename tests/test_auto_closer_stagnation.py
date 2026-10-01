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

if __name__ == "__main__":
    unittest.main()
