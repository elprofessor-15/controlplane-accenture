import unittest
import unittest.mock

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

    def test_full_gate_edit_path_for_queued_review(self):
        with TestClient(app) as client:
            client.post("/api/demo/reset", json={"model_id": "claude-sonnet-3-5"})
            response = client.post("/api/execute", json={"prompt": "Customer SSN 123-45-6789 for account lookup", "use_case": "customer_support"})
            self.assertEqual(response.json()["lane"], "FULL GATE")
            self.assertIn("decision_action", response.json())
            self.assertIn("decision_justification", response.json())

    def test_full_gate_edit_path_can_edit_and_queue_review(self):
        with TestClient(app) as client:
            client.post("/api/demo/reset", json={"model_id": "claude-sonnet-3-5"})
            with unittest.mock.patch("app.main.generate_parallel_samples", return_value=["first answer", "second answer", "third answer"]):
                with unittest.mock.patch("app.main.run_heavy_checks", return_value={
                    "action": "EDIT",
                    "severity": "medium",
                    "confidence": "medium",
                    "justification": "Sample disagreement required a cautious edit.",
                    "consistency": {"status": "FAIL", "divergence_score": 0.7},
                    "grounding": {"status": "PASS", "method": "lexical_fallback"},
                    "latency_ms": 25,
                }):
                    response = client.post("/api/execute", json={"prompt": "Customer SSN 123-45-6789 for account lookup", "use_case": "customer_support"})
            self.assertEqual(response.json()["lane"], "FULL GATE")
            self.assertEqual(response.json()["decision_action"], "EDIT")
            self.assertIn("ControlPlane note", response.json()["final_response"])

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

    def test_dashboard_and_review_endpoints_are_populated(self):
        with TestClient(app) as client:
            client.post("/api/demo/reset", json={"model_id": "claude-sonnet-3-5"})
            client.post("/api/execute", json={
                "prompt": "Look up card number 4111-1111-1111-1111",
                "use_case": "customer_support",
            })

            metrics = client.get("/api/dashboard_metrics")
            queue = client.get("/api/review_queue")
            audit = client.get("/api/audit_log")

            self.assertEqual(metrics.status_code, 200)
            self.assertEqual(queue.status_code, 200)
            self.assertEqual(audit.status_code, 200)
            self.assertGreater(metrics.json()["total_requests"], 0)
            self.assertIn("queue", queue.json())
            self.assertIn("audit_logs", audit.json())

    def test_reset_demo_clears_visible_state(self):
        with TestClient(app) as client:
            client.post("/api/demo/reset", json={"model_id": "reset-check"})
            client.post("/api/execute", json={
                "prompt": "Look up card number 4111-1111-1111-1111",
                "use_case": "customer_support",
                "model_id": "reset-check",
            })
            self.assertGreater(len(client.get("/api/audit_log").json()["audit_logs"]), 0)

            response = client.post("/api/demo/reset", json={"model_id": "reset-check"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get("/api/dashboard_metrics").json()["total_requests"], 0)
            self.assertEqual(client.get("/api/audit_log").json()["audit_logs"], [])
            self.assertEqual(client.get("/api/review_queue").json()["queue"], [])

    def test_valid_policy_update_is_accepted(self):
        with TestClient(app) as client:
            payload = {
                "use_case": "customer_support",
                "config_data": {
                    "use_case_id": "customer_support",
                    "name": "Customer Support Bot",
                    "region": "EU",
                    "risk_category_overrides": {"pii": "high"},
                    "latency_budget_ms": {"fast": 30, "full_gate_p95_ms": 2000, "verified_grace_window_ms": 400},
                    "shadow_sample_rate": 0.15,
                    "trust_thresholds": {"fast_lane_min_score": 0.8, "full_gate_max_score": 0.4},
                    "cold_start_default_lane": "full_gate",
                    "blast_radius_cap": 0.03,
                },
            }
            response = client.post("/api/config/update", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "updated")


if __name__ == "__main__":
    unittest.main()