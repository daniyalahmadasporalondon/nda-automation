import unittest

from nda_automation import decision_arbiter as arbiter


def _clause(det=None, ai_status=None, reason_code="", reason="", **fields):
    clause = dict(fields)
    if det is not None:
        clause["deterministic_decision"] = det
    if ai_status is not None:
        clause["ai_review_analysis"] = {"status": ai_status, "reason_code": reason_code, "reason": reason}
    return clause


class DecisionArbiterTests(unittest.TestCase):
    def test_deterministic_fail_is_never_softened_by_ai(self):
        # The fail-floor: no AI status moves a deterministic fail off "fail".
        for status in ("disagreement", "low_confidence", "invalid", "confirmed", "error"):
            verdict = arbiter.arbitrate(_clause("fail", status, reason_code="ai_semantic_disagreement"))
            self.assertEqual(verdict["decision"], "fail", status)
            self.assertEqual(verdict["source"], "deterministic", status)

    def test_deterministic_review_is_never_softened_by_ai(self):
        for status in ("confirmed", "disagreement", "low_confidence", "invalid", "error", None):
            verdict = arbiter.arbitrate(_clause("review", status))
            self.assertEqual(verdict["decision"], "review", str(status))
            self.assertEqual(verdict["source"], "deterministic", str(status))

    def test_pass_escalates_to_review_on_ai_concern(self):
        for status, code in (
            ("disagreement", "ai_semantic_disagreement"),
            ("low_confidence", "ai_confidence_below_threshold"),
            ("invalid", "ai_citation_validation_failed"),
        ):
            verdict = arbiter.arbitrate(_clause("pass", status, reason_code=code))
            self.assertEqual(verdict["decision"], "review", status)
            self.assertEqual(verdict["source"], "ai", status)
            self.assertEqual(verdict["reason_code"], code)

    def test_pass_stays_pass_when_ai_confirms_errors_or_is_absent(self):
        for status in ("confirmed", "error", None):
            verdict = arbiter.arbitrate(_clause("pass", status))
            self.assertEqual(verdict["decision"], "pass", str(status))
            self.assertEqual(verdict["source"], "deterministic", str(status))

    def test_derives_deterministic_decision_when_snapshot_absent(self):
        self.assertEqual(arbiter.arbitrate({"decision": "fail"})["decision"], "fail")
        self.assertEqual(arbiter.arbitrate({"passes": True})["decision"], "pass")
        self.assertEqual(arbiter.arbitrate({"passes": False})["decision"], "fail")
        self.assertEqual(arbiter.arbitrate({"needs_review": True})["decision"], "review")

    def test_unknown_or_empty_explicit_decision_fails_safe_to_review(self):
        for decision in ("", "unknown"):
            with self.subTest(decision=decision):
                verdict = arbiter.arbitrate({"decision": decision, "passes": True})

                self.assertEqual(verdict["decision"], "review")
                self.assertEqual(verdict["source"], "deterministic")

    def test_low_deterministic_confidence_is_review(self):
        verdict = arbiter.arbitrate({"passes": True, "semantic_confidence": 0.4})
        self.assertEqual(verdict["decision"], "review")

    def test_malformed_clause_fails_safe_to_review(self):
        verdict = arbiter.arbitrate({})
        self.assertEqual(verdict["decision"], "review")
        self.assertEqual(verdict["source"], "arbiter_default")


if __name__ == "__main__":
    unittest.main()
