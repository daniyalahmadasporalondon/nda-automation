import unittest

from tests.review_eval import format_report, gate_failures, run_eval


class ReviewEvalGateTests(unittest.TestCase):
    """CI gate: the deterministic fixture eval must not regress.

    Fails the build on any false clear, any high-risk trap regression, or any AI
    dissent / invalid-AI output that fails to escalate. Ungated (needs-counsel)
    observations are reported but never break the build. This is regression
    coverage against authored traps, not a measure of real-world legal accuracy.
    """

    def test_fixture_eval_gate_holds(self):
        summary = run_eval()
        # Smoke check: the fixtures actually loaded and ran.
        self.assertGreater(summary["totals"]["cases"], 0)
        failures = gate_failures(summary)
        self.assertEqual(failures, [], "Review fixture eval regressed:\n" + format_report(summary))


if __name__ == "__main__":
    unittest.main()
