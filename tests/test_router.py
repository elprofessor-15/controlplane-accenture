import unittest

from app.router import determine_lane


CONFIG = {
    "trust_thresholds": {
        "fast_lane_min_score": 0.80,
        "full_gate_max_score": 0.40,
    },
    "cold_start_default_lane": "full_gate",
}


class LaneRouterTests(unittest.TestCase):
    def test_cold_start_always_gates(self):
        self.assertEqual(determine_lane("low", 1.0, True, False, CONFIG), "FULL GATE")

    def test_high_risk_always_gates(self):
        self.assertEqual(determine_lane("high", 1.0, False, False, CONFIG), "FULL GATE")

    def test_low_risk_trusted_model_is_fast(self):
        self.assertEqual(determine_lane("low", 0.81, False, False, CONFIG), "FAST")

    def test_medium_risk_is_verified(self):
        self.assertEqual(determine_lane("medium", 0.90, False, False, CONFIG), "VERIFIED")

    def test_low_trust_is_full_gate(self):
        self.assertEqual(determine_lane("low", 0.40, False, False, CONFIG), "FULL GATE")

    def test_deterministic_flag_escalates(self):
        self.assertEqual(determine_lane("low", 0.90, False, True, CONFIG), "VERIFIED")
        self.assertEqual(determine_lane("medium", 0.90, False, True, CONFIG), "FULL GATE")


if __name__ == "__main__":
    unittest.main()