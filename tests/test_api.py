import unittest

try:
    from fastapi.testclient import TestClient
    from app.main import app
except ModuleNotFoundError:
    TestClient = None
    app = None


@unittest.skipUnless(TestClient is not None, "FastAPI dependencies are not installed")
class ControlPlaneApiTests(unittest.TestCase):
    def test_each_lane_is_reachable_and_pii_is_not_returned(self):
        with TestClient(app) as client:
            client.post("/api/demo/reset", json={"model_id": "claude-sonnet-3-5"})
            fast = client.post("/api/execute", json={"prompt": "What are your store hours?", "use_case": "customer_support"})
            verified = client.post("/api/execute", json={"prompt": "Will this product work for someone with sensitive skin?", "use_case": "customer_support"})
            gated = client.post("/api/execute", json={"prompt": "Look up card number 4111-1111-1111-1111", "use_case": "customer_support"})
            self.assertEqual(fast.json()["lane"], "FAST")
            self.assertEqual(verified.json()["lane"], "VERIFIED")
            self.assertEqual(gated.json()["lane"], "FULL GATE")
            self.assertNotIn("4111-1111-1111-1111", gated.json()["final_response"])

    def test_policy_update_rejects_malformed_thresholds(self):
        with TestClient(app) as client:
            response = client.post("/api/config/update", json={
                "use_case": "customer_support",
                "config_data": {
                    "use_case_id": "customer_support", "name": "x", "region": "EU",
                    "risk_category_overrides": {}, "latency_budget_ms": {"fast": 0},
                    "shadow_sample_rate": 1.2, "trust_thresholds": {"fast_lane_min_score": 0.8, "full_gate_max_score": 0.4},
                    "cold_start_default_lane": "full_gate", "blast_radius_cap": 0.03,
                },
            })
            self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()