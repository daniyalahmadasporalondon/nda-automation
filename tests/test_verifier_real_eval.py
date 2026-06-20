"""CI hook for the REAL-PATH adversarial verifier eval.

This is the key-gated, default-OFF layer that runs the ACTUAL DeepSeek verifier
(not the scripted ``_scripted_verifier`` stub) over adversarial findings and
asserts the live model RESISTS unsafe clears. See ``tests.verifier_real_eval``
for the rationale, the four failure modes, and how to run it.

It is skipped (never fails) unless BOTH:
  * ``NDA_RUN_REAL_VERIFIER_EVAL`` is truthy, and
  * an ``OPENROUTER_API_KEY`` is configured,
so the default key-free / flag-free suite stays green and spends no tokens.

Run it deliberately:
    NDA_RUN_REAL_VERIFIER_EVAL=1 OPENROUTER_API_KEY=sk-... \
        pytest tests/test_verifier_real_eval.py -v
"""
from __future__ import annotations

import unittest

from tests.verifier_real_eval import (
    build_cases,
    format_report,
    real_verifier_eval_enabled,
    run_eval,
    skip_reason,
)


class RealVerifierEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        if not real_verifier_eval_enabled():
            self.skipTest(skip_reason() or "real verifier eval disabled")

    def test_cases_are_well_formed(self) -> None:
        """The adversarial cases exist and cover the four named failure modes.

        Runs even structure-only checks before the live call so a malformed case
        set fails loudly rather than skewing the live run.
        """
        cases = build_cases()
        self.assertGreaterEqual(len(cases), 4, "expected the four named failure-mode cases")
        modes = {str(case.get("failure_mode") or "") for case in cases}
        self.assertEqual(
            modes,
            {"polarity_trap", "prompt_injection", "cross_section_carveout_borrow", "over_clear"},
            "the four audit-named failure modes must all be covered",
        )
        for case in cases:
            self.assertIn("finding", case)
            self.assertTrue(case.get("forbidden_decisions"), f"{case['name']} must forbid an unsafe decision")

    def test_real_verifier_resists_unsafe_clears(self) -> None:
        """The LIVE verifier must not wrongly affirm/clear any adversarial finding."""
        summary = run_eval()
        self.assertGreater(summary["total"], 0, "no real-path cases ran")
        self.assertEqual(
            summary["unsafe"],
            [],
            "Real verifier produced an UNSAFE clear:\n" + format_report(summary),
        )


if __name__ == "__main__":
    unittest.main()
