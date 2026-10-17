# tests/test_signal_time_control.py
# Role: Unit tests for TimeControl (CORE/ENTRY/signal.py) interval conversions, rules, and smart grace period

import unittest
from CORE.ENTRY.signal import TimeControl


class TestTimeControl(unittest.TestCase):
    def setUp(self):
        self.tc = TimeControl(interval="5m")

    def test_interval_to_seconds(self):
        self.assertEqual(TimeControl.interval_to_seconds("1m"), 60)
        self.assertEqual(TimeControl.interval_to_seconds("5m"), 300)
        self.assertEqual(TimeControl.interval_to_seconds("15m"), 900)
        self.assertEqual(TimeControl.interval_to_seconds("1h"), 3600)
        self.assertEqual(TimeControl.interval_to_seconds("1d"), 86400)
        # Default fallback for unknown interval
        self.assertEqual(TimeControl.interval_to_seconds("unknown"), 300)

    def test_match_rule(self):
        # >= rule
        self.assertTrue(TimeControl.match_rule(">=5", 5))
        self.assertTrue(TimeControl.match_rule(">=5", 7))
        self.assertFalse(TimeControl.match_rule(">=5", 4))

        # <= rule
        self.assertTrue(TimeControl.match_rule("<=3", 3))
        self.assertTrue(TimeControl.match_rule("<=3", 1))
        self.assertFalse(TimeControl.match_rule("<=3", 4))

        # == rule
        self.assertTrue(TimeControl.match_rule("==2", 2))
        self.assertFalse(TimeControl.match_rule("==2", 3))

        # > rule
        self.assertTrue(TimeControl.match_rule(">10", 11))
        self.assertFalse(TimeControl.match_rule(">10", 10))

        # < rule
        self.assertTrue(TimeControl.match_rule("<4", 3))
        self.assertFalse(TimeControl.match_rule("<4", 4))

        # Exact number rule
        self.assertTrue(TimeControl.match_rule("5", 5))
        self.assertFalse(TimeControl.match_rule("5", 6))

        # Invalid syntax handles gracefully
        self.assertFalse(TimeControl.match_rule("invalid", 5))

    def test_resolve_shift_interval_fixed_and_range(self):
        # Fixed integer or float
        self.assertEqual(TimeControl.resolve_shift_interval(15), 15.0)
        self.assertEqual(TimeControl.resolve_shift_interval(12.5), 12.5)

        # Range [low, high]
        val = TimeControl.resolve_shift_interval([5, 15])
        self.assertGreaterEqual(val, 5.0)
        self.assertLessEqual(val, 15.0)

        # Inverted range [high, low]
        val_inv = TimeControl.resolve_shift_interval([20, 10])
        self.assertGreaterEqual(val_inv, 10.0)
        self.assertLessEqual(val_inv, 20.0)

        # Invalid returns default
        val_bad = TimeControl.resolve_shift_interval("not_a_number")
        self.assertIsInstance(val_bad, float)

    def test_is_new_interval_window_timing(self):
        tc = TimeControl(interval="5m")
        # 5m = 300s. Let interval start at 1000 * 300 = 300000
        # If shift is 0, start_period_sec is 30.0
        custom_cfg = {
            "start_period_sec": 30.0,
            "shift_interval_sec": 0.0,
            "increments_card": {">=3": 15.0},
        }

        # Timestamp 300005: 5s into candle, grace is 30.0 -> is_signal is True
        is_signal, grace = tc.is_new_interval(closed_count=0, smart_grace_cfg=custom_cfg, current_timestamp=300005.0)
        self.assertTrue(is_signal)
        self.assertEqual(grace, 30.0)

        # Timestamp 300040: 40s into candle, grace is 30.0 -> is_signal is False
        is_signal, _ = tc.is_new_interval(closed_count=0, smart_grace_cfg=custom_cfg, current_timestamp=300040.0)
        self.assertFalse(is_signal)

        # With closed_count >= 3, grace becomes 30.0 + 15.0 = 45.0
        # Timestamp 300040 is now inside window (< 45.0) -> is_signal is True!
        is_signal, grace = tc.is_new_interval(closed_count=3, smart_grace_cfg=custom_cfg, current_timestamp=300040.0)
        self.assertTrue(is_signal)
        self.assertEqual(grace, 45.0)


if __name__ == "__main__":
    unittest.main()
