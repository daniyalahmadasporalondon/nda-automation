"""Tests for the deterministic law<->forum mismatch detector (law_forum_check).

Three layers:

1. EVAL — the 4 documented mismatch cases (E&W-law/Cayman-courts,
   Delaware-law/India-arbitration, E&W-law/DIFC-courts, India-law/England-courts)
   must FLAG, and the 1 aligned control (E&W-law/E&W-courts) must stay clean.
   Run BOTH against the helper-unavailable fallback path AND against a STUBBED
   ``canonical_forum_for_law`` (the shared helper the foundation teammate owns),
   so the detector is proven correct regardless of merge order.

2. EXTRACTION — law/forum jurisdiction extraction, recital exclusion, garbage input.

3. ADDITIVE-ONLY OVERLAY — the anti-ghost contract: the overlay only ever ELEVATES
   a clean PASS to REVIEW; it never overrides/downgrades an AI fail or review, never
   force-FAILs, and fails safe (returns the input unchanged) on any error.
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from typing import Iterator

from nda_automation import law_forum_check as lfc

# ---------------------------------------------------------------------------
# Eval documents — inlined so the test is self-contained (mirror of the
# /tmp/judg-lawforum cases used for the documented eval).
# ---------------------------------------------------------------------------
_BODY = (
    "MUTUAL NON-DISCLOSURE AGREEMENT\n\n"
    "This Mutual Non-Disclosure Agreement is entered into between {cp} and Aspora "
    "Technologies Limited. Each party may act as a Discloser and as a Recipient.\n\n"
    "1. Confidential Information. \"Confidential Information\" means any and all "
    "non-public business information disclosed by either party.\n\n"
    "2. Use and Protection. The Recipient shall use the Confidential Information "
    "solely to evaluate the potential business relationship.\n\n"
    "3. Term and Survival. This Agreement remains in effect for two (2) years.\n\n"
    "4. Governing Law. {law}\n\n"
    "5. {forum_heading}. {forum}\n\n"
    "6. Signatures. Signed for and on behalf of {cp} by ____________.\n"
)

EVAL_CASES = {
    # name: (text, law_option_id, expect_mismatch)
    "m1_ew_law_cayman_courts": (
        _BODY.format(
            cp="Acme Corp",
            law="This Agreement shall be governed by and construed in accordance with the laws of England and Wales.",
            forum_heading="Jurisdiction and Venue",
            forum="The parties irrevocably submit to the exclusive jurisdiction of the courts of the Cayman Islands for any dispute arising out of or in connection with this Agreement.",
        ),
        "england_and_wales",
        True,
    ),
    "m2_delaware_law_bengaluru_arbitration": (
        _BODY.format(
            cp="Northwind Labs Inc.",
            law="This Agreement shall be governed by and construed in accordance with the laws of the State of Delaware, United States of America.",
            forum_heading="Dispute Resolution",
            forum="Any dispute arising out of or in connection with this Agreement shall be finally resolved by arbitration seated in Bengaluru, India, and the parties submit to the exclusive jurisdiction of the courts of Bengaluru, India.",
        ),
        "delaware",
        True,
    ),
    "m3_ew_law_difc_courts": (
        _BODY.format(
            cp="Helios Trading FZE",
            law="This Agreement shall be governed by and construed in accordance with the laws of England and Wales.",
            forum_heading="Jurisdiction",
            forum="The parties irrevocably submit to the exclusive jurisdiction of the DIFC Courts, Dubai International Financial Centre, for the resolution of any dispute.",
        ),
        "england_and_wales",
        True,
    ),
    "m4_india_law_england_courts": (
        _BODY.format(
            cp="Sapphire Retail Private Limited",
            law="This Agreement shall be governed by and construed in accordance with the laws of India.",
            forum_heading="Jurisdiction and Venue",
            forum="The parties irrevocably submit to the exclusive jurisdiction of the courts of England and Wales for any dispute.",
        ),
        "india",
        True,
    ),
    "c5_control_ew_law_ew_courts": (
        _BODY.format(
            cp="Brightwater Systems Limited",
            law="This Agreement shall be governed by and construed in accordance with the laws of England and Wales.",
            forum_heading="Jurisdiction and Venue",
            forum="The parties irrevocably submit to the exclusive jurisdiction of the courts of England and Wales for any dispute.",
        ),
        "england_and_wales",
        False,
    ),
}

# The canonical forum jurisdiction each approved law option pairs with -- mirrors
# the generation-side _COURT_FOR_OPTION_ID. Used by the stubbed shared helper.
_STUB_FORUM = {
    "england_and_wales": ("England and Wales", "the courts of England and Wales"),
    "delaware": ("Delaware", "the state and federal courts located in the State of Delaware"),
    "india": ("India", "the courts of Mumbai, India"),
    "difc": ("DIFC", "the DIFC Courts, Dubai"),
    "ontario_canada": ("Ontario, Canada", "the courts of the Province of Ontario, Canada"),
}


@contextmanager
def stubbed_helper() -> Iterator[None]:
    """Inject a stub ``governing_law_forum.canonical_forum_for_law`` for the duration.

    The foundation teammate owns the real helper; until it merges we prove the
    detector against a faithful stub of its documented contract so merge order
    doesn't gate this work.
    """
    import sys
    import types

    module = types.ModuleType("nda_automation.governing_law_forum")

    def canonical_forum_for_law(playbook: dict, law_option_id: str):  # noqa: ARG001
        pair = _STUB_FORUM.get(str(law_option_id or "").strip().lower())
        if not pair:
            return None
        return {
            "option_id": law_option_id,
            "law_label": pair[0],
            "forum_jurisdiction": pair[0],
            "court_name": pair[1],
        }

    module.canonical_forum_for_law = canonical_forum_for_law  # type: ignore[attr-defined]
    sys.modules["nda_automation.governing_law_forum"] = module
    try:
        yield
    finally:
        sys.modules.pop("nda_automation.governing_law_forum", None)


class EvalTests(unittest.TestCase):
    """The 4-mismatch + 1-control eval, both helper paths."""

    def test_eval_fallback_path_no_helper(self):
        # Helper not merged: detector falls back to the option id as the expected
        # forum (each approved law's forum is its own jurisdiction).
        for name, (text, law, expect) in EVAL_CASES.items():
            with self.subTest(case=name):
                finding = lfc.detect_mismatch(text, law, {})
                self.assertEqual(bool(finding), expect, f"{name}: finding={finding}")

    def test_eval_with_stubbed_shared_helper(self):
        with stubbed_helper():
            for name, (text, law, expect) in EVAL_CASES.items():
                with self.subTest(case=name):
                    finding = lfc.detect_mismatch(text, law, {"clauses": []})
                    self.assertEqual(bool(finding), expect, f"{name}: finding={finding}")

    def test_finding_names_both_jurisdictions(self):
        text, law, _ = EVAL_CASES["m1_ew_law_cayman_courts"]
        finding = lfc.detect_mismatch(text, law, {})
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding["reason_code"], lfc.REASON_CODE)
        self.assertEqual(finding["expected_forum"], "england_and_wales")
        self.assertEqual(finding["document_forum"], "cayman_islands")
        self.assertIn("England and Wales", finding["reason"])
        self.assertIn("Cayman Islands", finding["reason"])


class ExtractionTests(unittest.TestCase):
    def test_law_extraction(self):
        text, _, _ = EVAL_CASES["m4_india_law_england_courts"]
        self.assertEqual(lfc.extract_law_jurisdictions(text), {"india"})

    def test_forum_extraction(self):
        text, _, _ = EVAL_CASES["m4_india_law_england_courts"]
        self.assertEqual(lfc.extract_forum_jurisdictions(text), {"england_and_wales"})

    def test_difc_forum_extraction(self):
        text, _, _ = EVAL_CASES["m3_ew_law_difc_courts"]
        self.assertEqual(lfc.extract_forum_jurisdictions(text), {"difc"})

    def test_arbitration_seat_extraction(self):
        text, _, _ = EVAL_CASES["m2_delaware_law_bengaluru_arbitration"]
        self.assertEqual(lfc.extract_forum_jurisdictions(text), {"india"})

    def test_incorporation_recital_not_read_as_law(self):
        # A party recital "organized under the laws of India" must NOT be read as the
        # agreement's governing law -- only the operative governing-law sentence is.
        text = (
            "Northwind Labs, a company organized under the laws of India, and Aspora.\n"
            "Governing Law. This Agreement shall be governed by the laws of England and Wales.\n"
            "Jurisdiction. The courts of England and Wales have exclusive jurisdiction.\n"
        )
        self.assertEqual(lfc.extract_law_jurisdictions(text), {"england_and_wales"})
        # And the detector stays clean (law==forum, recital ignored).
        self.assertIsNone(lfc.detect_mismatch(text, "england_and_wales", {}))

    def test_empty_and_garbage_input_is_silent(self):
        self.assertEqual(lfc.extract_law_jurisdictions(""), set())
        self.assertEqual(lfc.extract_forum_jurisdictions(""), set())
        self.assertIsNone(lfc.detect_mismatch("", "england_and_wales", {}))
        self.assertIsNone(lfc.detect_mismatch("\x00\x01 garbage ￿", "india", {}))
        self.assertIsNone(lfc.detect_mismatch("no jurisdictions named here at all.", "delaware", {}))

    def test_no_law_option_is_silent(self):
        text, _, _ = EVAL_CASES["m1_ew_law_cayman_courts"]
        self.assertIsNone(lfc.detect_mismatch(text, "", {}))
        # An unapproved/unknown option id we have no expected forum for -> silent.
        self.assertIsNone(lfc.detect_mismatch(text, "narnia", {}))

    def test_no_recognizable_forum_is_silent(self):
        # A document with a governing law but no recognizable forum jurisdiction
        # must NOT flag (nothing to compare against -> no false positive).
        text = "Governing Law. This Agreement is governed by the laws of England and Wales.\n"
        self.assertIsNone(lfc.detect_mismatch(text, "england_and_wales", {}))


class _LFState:
    """Minimal review_state factory matching the real review_state shape we touch."""

    @staticmethod
    def make(state: str) -> dict:
        return {
            "version": 1,
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
            "reason_codes": ["existing_code"],
        }


def _matter_with_law_and_forum(law_value: str, forum_text_case: str) -> dict:
    """A matter whose review surfaces ``law_value`` and whose text is an eval case."""
    text, _, _ = EVAL_CASES[forum_text_case]
    return {
        "id": "mtest",
        "extracted_text": text,
        "review_result": {
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "pass",
                    "governing_law_analysis": {
                        "candidate_records": [
                            {"value": law_value, "approved": True, "needs_review": False}
                        ]
                    },
                }
            ]
        },
    }


class AdditiveOverlayTests(unittest.TestCase):
    """The anti-ghost contract: elevate-only, never override/downgrade/force-fail."""

    def test_clean_pass_elevated_to_review_on_mismatch(self):
        matter = _matter_with_law_and_forum("England and Wales", "m1_ew_law_cayman_courts")
        out = lfc.apply_lawforum_overlay(_LFState.make("pass"), matter)
        self.assertEqual(out["state"], "review")
        self.assertEqual(out["overall_status"], "needs_review")
        self.assertTrue(out["requires_human_review"])
        self.assertTrue(out["blocks_send"])
        self.assertTrue(out["law_forum_mismatch"])
        self.assertIn(lfc.REASON_CODE, out["reason_codes"])
        # Additive: the pre-existing reason code is preserved.
        self.assertIn("existing_code", out["reason_codes"])

    def test_aligned_control_pass_is_untouched(self):
        matter = _matter_with_law_and_forum("England and Wales", "c5_control_ew_law_ew_courts")
        state = _LFState.make("pass")
        out = lfc.apply_lawforum_overlay(state, matter)
        self.assertEqual(out["state"], "pass")
        self.assertNotIn("law_forum_mismatch", out)

    def test_never_downgrades_an_ai_fail(self):
        # Even with a real mismatch present, a CHECK (AI fail) is left untouched --
        # the detector never overrides/weakens a stronger AI verdict.
        matter = _matter_with_law_and_forum("England and Wales", "m1_ew_law_cayman_courts")
        check_state = _LFState.make("check")
        out = lfc.apply_lawforum_overlay(check_state, matter)
        self.assertEqual(out["state"], "check")
        self.assertEqual(out["overall_status"], "does_not_meet_requirements")
        self.assertNotIn("law_forum_mismatch", out)

    def test_never_overrides_an_ai_review(self):
        # An AI-review state is already the strongest "needs human" signal; the
        # overlay must not relabel or re-stamp it.
        matter = _matter_with_law_and_forum("England and Wales", "m1_ew_law_cayman_courts")
        review_state = _LFState.make("review")
        out = lfc.apply_lawforum_overlay(review_state, matter)
        self.assertEqual(out["state"], "review")
        # Untouched: it did not add the mismatch marker over the AI's own review.
        self.assertNotIn("law_forum_mismatch", out)
        self.assertEqual(out, review_state)

    def test_never_force_fails(self):
        # The overlay can only ever produce "review" (or leave state as-is); it must
        # never write "check"/force-fail, even on a mismatch.
        matter = _matter_with_law_and_forum("India", "m4_india_law_england_courts")
        out = lfc.apply_lawforum_overlay(_LFState.make("pass"), matter)
        self.assertNotEqual(out["state"], "check")
        self.assertEqual(out["state"], "review")

    def test_no_mismatch_leaves_pass_clean(self):
        matter = _matter_with_law_and_forum("England and Wales", "c5_control_ew_law_ew_courts")
        out = lfc.apply_lawforum_overlay(_LFState.make("pass"), matter)
        self.assertEqual(out["state"], "pass")

    def test_overlay_is_pure_does_not_mutate_input(self):
        matter = _matter_with_law_and_forum("England and Wales", "m1_ew_law_cayman_courts")
        state = _LFState.make("pass")
        snapshot = dict(state)
        lfc.apply_lawforum_overlay(state, matter)
        self.assertEqual(state, snapshot, "overlay must not mutate the input review_state")

    def test_failsafe_on_garbage_inputs(self):
        # Non-dict review_state -> returned unchanged.
        self.assertIsNone(lfc.apply_lawforum_overlay(None, {}))
        self.assertEqual(lfc.apply_lawforum_overlay("nope", {}), "nope")
        # Non-mapping matter -> no flag, state unchanged.
        state = _LFState.make("pass")
        self.assertEqual(lfc.apply_lawforum_overlay(state, None), state)
        self.assertEqual(lfc.apply_lawforum_overlay(state, "garbage"), state)

    def test_matter_without_law_is_silent(self):
        # A matter whose governing law can't be resolved -> no flag.
        matter = {"id": "x", "extracted_text": EVAL_CASES["m1_ew_law_cayman_courts"][0]}
        out = lfc.apply_lawforum_overlay(_LFState.make("pass"), matter)
        self.assertEqual(out["state"], "pass")

    def test_matter_without_text_is_silent(self):
        matter = _matter_with_law_and_forum("England and Wales", "m1_ew_law_cayman_courts")
        matter["extracted_text"] = ""
        out = lfc.apply_lawforum_overlay(_LFState.make("pass"), matter)
        self.assertEqual(out["state"], "pass")

    def test_detect_matter_mismatch_fail_safe_on_helper_exception(self):
        # If the shared helper throws, the detector swallows it and stays silent.
        import sys
        import types

        module = types.ModuleType("nda_automation.governing_law_forum")

        def boom(playbook, law_option_id):  # noqa: ANN001, ARG001
            raise RuntimeError("helper exploded")

        module.canonical_forum_for_law = boom  # type: ignore[attr-defined]
        sys.modules["nda_automation.governing_law_forum"] = module
        try:
            # With no helper-resolvable forum AND a forced exception, the approved
            # option falls back to its own-jurisdiction forum, so a real mismatch is
            # still detected deterministically -- prove it does not CRASH and yields
            # a valid result either way.
            matter = _matter_with_law_and_forum("England and Wales", "m1_ew_law_cayman_courts")
            out = lfc.apply_lawforum_overlay(_LFState.make("pass"), matter)
            self.assertIn(out["state"], {"pass", "review"})
        finally:
            sys.modules.pop("nda_automation.governing_law_forum", None)


if __name__ == "__main__":
    unittest.main()
