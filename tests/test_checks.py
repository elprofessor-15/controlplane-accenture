import unittest

from app.checks.deterministic import redact_sensitive_text, run_deterministic_checks
from app.checks.heavy import evaluate_grounding, evaluate_self_consistency
from app.trust_math import calculate_ledger_delta


class SafetyCheckTests(unittest.TestCase):
    def test_redaction_masks_all_structured_pii(self):
        text = "Email me at person@example.com; SSN 000-12-3456; card 4111-1111-1111-1111."
        result = run_deterministic_checks("lookup", text, "pii")
        self.assertTrue(result["flagged"])
        self.assertNotIn("person@example.com", result["redacted_response"])
        self.assertNotIn("000-12-3456", result["redacted_response"])
        self.assertNotIn("4111-1111-1111-1111", result["redacted_response"])

    def test_clean_text_is_unchanged(self):
        self.assertEqual(redact_sensitive_text("The answer is ready."), "The answer is ready.")

    def test_reports_active_detection_method(self):
        result = run_deterministic_checks("Contact Jane Smith at jane@example.com.", "Jane Smith is the account owner.", "pii")
        self.assertIn(result["detection_method"], {"regex-only", "regex+NER"})
        self.assertIn(result["method"], {"regex-only", "regex+NER"})


class HeavyCheckTests(unittest.TestCase):
    def test_consistency_detects_disagreement(self):
        result = evaluate_self_consistency(["The policy allows five days.", "Escalate; evidence is insufficient.", "The policy allows five days."])
        self.assertEqual(result["status"], "FAIL")

    def test_grounding_returns_match_metadata(self):
        result = evaluate_grounding("Stores are open Monday through Saturday from 9 AM to 8 PM.", "customer_support")
        self.assertEqual(result["matched_doc"], "doc_cs_01")
        self.assertIn(result["method"], {"sentence_transformer", "lexical_fallback"})


class LedgerDeltaTests(unittest.TestCase):
    def test_recovery_grows_with_clean_streak_and_is_bounded(self):
        self.assertEqual(calculate_ledger_delta("CHECK_FAIL", "high", "high"), -0.20)
        self.assertEqual(calculate_ledger_delta("HUMAN_APPROVED", clean_streak=0), 0.02)
        self.assertEqual(calculate_ledger_delta("HUMAN_APPROVED", clean_streak=20), 0.05)


if __name__ == "__main__":
    unittest.main()