"""Tests for the generalized additive review-overlay pipeline (review_overlays).

The pipeline factors the anti-ghost elevation contract out of law_forum_check and
runs the law/forum overlay PLUS a list of coverage detectors. These tests pin the
SHARED contract that every overlay obeys, independent of any specific detector:

  * Only a clean PASS is ever elevated -- to REVIEW.
  * A REVIEW or CHECK (a stronger verdict) is NEVER downgraded/overridden; a detector
    may only append its reason_code additively, never touch the state.
  * No detector ever force-FAILs (writes "check").
  * FAIL-SAFE: a raising detector (or garbage state) can never crash the pipeline.

A finding-producing and a raising stub detector are injected via the same
``review_overlays.DetectorFn`` shape the three real detectors implement, so the
contract is verified without depending on those modules being merged.
"""
from __future__ import annotations

import unittest
from unittest import mock

from nda_automation import review_overlays


def _state(state: str) -> dict:
    """A review_state with the fields the overlay reads/writes."""
    return {
        "state": state,
        "overall_status": {
            "pass": "meets_requirements",
            "review": "needs_review",
            "check": "does_not_meet_requirements",
        }.get(state, "pending_review"),
        "label": state.upper(),
        "tone": state,
        "requires_attention": state in {"review", "check"},
        "requires_human_review": state == "review",
        "blocks_send": state in {"review", "check"},
        "reason_codes": [],
    }


def _finding_detector(matter):
    return {"reason_code": "stub_finding", "message": "stub coverage gap"}


def _silent_detector(matter):
    return None


def _raising_detector(matter):
    raise RuntimeError("detector blew up")


class ApplyReviewOverlaysContract(unittest.TestCase):
    def _with_detectors(self, detectors):
        return mock.patch.object(
            review_overlays, "_coverage_detectors", lambda: list(detectors)
        )

    def test_clean_pass_elevated_to_review_on_finding(self):
        with self._with_detectors([_finding_detector]):
            out = review_overlays.apply_review_overlays(_state("pass"), {})
        self.assertEqual(out["state"], "review")
        self.assertEqual(out["overall_status"], "needs_review")
        self.assertTrue(out["requires_human_review"])
        self.assertTrue(out["blocks_send"])
        self.assertIn("stub_finding", out["reason_codes"])
        self.assertIn("stub coverage gap", out.get("overlay_review_reasons", []))

    def test_clean_pass_with_no_finding_is_untouched(self):
        state = _state("pass")
        with self._with_detectors([_silent_detector]):
            out = review_overlays.apply_review_overlays(state, {})
        self.assertEqual(out["state"], "pass")

    def test_never_downgrades_a_check(self):
        # An AI fail (check) is never weakened, even with a finding present. The
        # reason_code may be appended additively, but the state stays "check".
        with self._with_detectors([_finding_detector]):
            out = review_overlays.apply_review_overlays(_state("check"), {})
        self.assertEqual(out["state"], "check")
        self.assertEqual(out["overall_status"], "does_not_meet_requirements")
        self.assertIn("stub_finding", out["reason_codes"])

    def test_never_overrides_a_review(self):
        with self._with_detectors([_finding_detector]):
            out = review_overlays.apply_review_overlays(_state("review"), {})
        self.assertEqual(out["state"], "review")

    def test_never_force_fails(self):
        # The pipeline can only ever produce pass or review from a pass input --
        # never "check". No overlay path writes a force-FAIL.
        with self._with_detectors([_finding_detector]):
            out = review_overlays.apply_review_overlays(_state("pass"), {})
        self.assertNotEqual(out["state"], "check")

    def test_failsafe_raising_detector_does_not_crash(self):
        with self._with_detectors([_raising_detector]):
            out = review_overlays.apply_review_overlays(_state("pass"), {})
        # A raising detector leaves the state untouched.
        self.assertEqual(out["state"], "pass")

    def test_failsafe_raising_then_finding(self):
        # A raising detector must not stop a later good detector from elevating.
        with self._with_detectors([_raising_detector, _finding_detector]):
            out = review_overlays.apply_review_overlays(_state("pass"), {})
        self.assertEqual(out["state"], "review")
        self.assertIn("stub_finding", out["reason_codes"])

    def test_garbage_state_returned_unchanged(self):
        with self._with_detectors([_finding_detector]):
            self.assertIsNone(review_overlays.apply_review_overlays(None, {}))
            self.assertEqual(
                review_overlays.apply_review_overlays("nope", {}), "nope"
            )

    def test_pure_does_not_mutate_input(self):
        state = _state("pass")
        snapshot = dict(state)
        with self._with_detectors([_finding_detector]):
            review_overlays.apply_review_overlays(state, {})
        self.assertEqual(state, snapshot)

    def test_two_findings_both_codes_present(self):
        def _second(matter):
            return {"reason_code": "stub_two", "message": "second gap"}

        with self._with_detectors([_finding_detector, _second]):
            out = review_overlays.apply_review_overlays(_state("pass"), {})
        self.assertIn("stub_finding", out["reason_codes"])
        self.assertIn("stub_two", out["reason_codes"])

    def test_real_coverage_detectors_are_failsafe(self):
        # The real (lazily-imported) detector list must never raise, regardless of
        # which detector modules are present, when fed a garbage matter.
        out = review_overlays.apply_review_overlays(_state("pass"), {"junk": object()})
        self.assertIn(out["state"], {"pass", "review"})


if __name__ == "__main__":
    unittest.main()
