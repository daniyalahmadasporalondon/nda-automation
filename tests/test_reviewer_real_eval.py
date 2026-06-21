"""Tests for the AI REVIEWER eval -- two layers.

LAYER 1 (key-gated, default-OFF): the REAL-PATH adversarial reviewer eval that
runs the ACTUAL OpenRouter reviewer (not a stub) over adversarial single-clause
NDAs and asserts the live model does not land in a forbidden bucket (e.g. an
unapproved governing law must not pass). See ``tests.reviewer_real_eval`` for
the rationale, the per-clause traps, and how to run it. It is skipped (never
fails) unless BOTH:
  * ``NDA_RUN_REAL_REVIEWER_EVAL`` is truthy, and
  * an ``OPENROUTER_API_KEY`` is configured,
so the default key-free / flag-free suite stays green and spends no tokens.

    NDA_RUN_REAL_REVIEWER_EVAL=1 OPENROUTER_API_KEY=sk-... \
        pytest tests/test_reviewer_real_eval.py -v

LAYER 2 (always-on, key-free): stub contract cases. These feed HAND-AUTHORED
assessor outputs through the real ``assess_nda_with_ai`` plumbing (via the
``InMemoryAssessmentReviewer`` seam) and assert the CONTRACT / GROUNDING /
DOWNGRADE behavior for the clauses with no automated coverage today -- above all
``signatures`` (which had ZERO coverage). They run green in CI and catch
plumbing/contract regressions; they do NOT test model judgment (that is Layer 1).
"""
from __future__ import annotations

import unittest
from copy import deepcopy
from typing import Any, Mapping

from nda_automation.ai_assessor import (
    AI_FIRST_ASSESSOR_MODE,
    InMemoryAssessmentReviewer,
    assess_nda_with_ai,
)
from nda_automation.evidence_grounding import (
    GROUNDING_ABSENCE,
    GROUNDING_GROUNDED,
    GROUNDING_UNGROUNDED,
    UNGROUNDED_REASON_CODE,
)
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH
from nda_automation.review_state import (
    CLAUSE_DECISION_FAIL,
    CLAUSE_DECISION_PASS,
    CLAUSE_DECISION_REVIEW,
)
from tests.reviewer_real_eval import (
    build_cases,
    format_report,
    real_reviewer_eval_enabled,
    run_eval,
    skip_reason,
)


# ---------------------------------------------------------------------------
# LAYER 1 -- real-path, key-gated, default-OFF
# ---------------------------------------------------------------------------
class RealReviewerEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        if not real_reviewer_eval_enabled():
            self.skipTest(skip_reason() or "real reviewer eval disabled")

    def test_cases_cover_every_clause_and_the_named_traps(self) -> None:
        """The adversarial cases exist, cover all six clauses, and forbid a verdict.

        Structure-only checks run BEFORE the live call so a malformed case set
        fails loudly rather than skewing the live run. ``signatures`` and
        ``non_circumvention`` -- the previously-uncovered clauses -- must be present.
        """
        cases = build_cases()
        self.assertGreaterEqual(len(cases), 8, "expected one adversarial case per clause + precision guards")
        clause_ids = {str(case.get("clause_id") or "") for case in cases}
        self.assertEqual(
            clause_ids,
            {
                "mutuality",
                "confidential_information",
                "governing_law",
                "term_and_survival",
                "non_circumvention",
                "signatures",
            },
            "every playbook clause must have at least one adversarial real-path case",
        )
        for case in cases:
            self.assertIn("source_text", case)
            self.assertTrue(case.get("forbidden_decisions"), f"{case['name']} must forbid a decision")

    def test_real_reviewer_resists_unsafe_verdicts(self) -> None:
        """The LIVE reviewer must not land any adversarial case in a forbidden bucket."""
        summary = run_eval()
        self.assertGreater(summary["total"], 0, "no real-path cases ran")
        self.assertEqual(
            summary["unsafe"],
            [],
            "Real reviewer produced an UNSAFE verdict:\n" + format_report(summary),
        )


# ---------------------------------------------------------------------------
# LAYER 2 -- always-on, key-free stub contract cases
# ---------------------------------------------------------------------------
#
# Hand-authored assessor outputs driven through the real assess_nda_with_ai
# plumbing. These pin the CONTRACT/GROUNDING/DOWNGRADE behavior (not model
# judgment) for the previously-uncovered clauses.

# A real, complete mutual signature block (so the signatures clause has source
# text to ground against) plus the rest of a minimal compliant NDA.
_SIGNATURE_BLOCK_PARAGRAPH = (
    "For Aspora Limited\nBy: _______________  Name: ____________  Title: Director  Date: __________\n"
    "For Counterparty Ltd\nBy: _______________  Name: ____________  Title: Director  Date: __________"
)

_NDA_WITH_SIGNATURES = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    '"Confidential Information" means non-public business, financial, technical, customer, '
    "supplier, pricing, market, product, proprietary and trade secret information disclosed by either party.",
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations survive for a fixed period of five years.",
    "Each party remains free to deal with third parties outside the Purpose.",
    _SIGNATURE_BLOCK_PARAGRAPH,
])

# Same NDA with NO signature block paragraph at all.
_NDA_WITHOUT_SIGNATURES = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    '"Confidential Information" means non-public business, financial, technical, customer, '
    "supplier, pricing, market, product, proprietary and trade secret information disclosed by either party.",
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations survive for a fixed period of five years.",
    "Each party remains free to deal with third parties outside the Purpose.",
])


def _assessment(
    clause_id: str,
    decision: str,
    *,
    issue_type: str | None = None,
    paragraph_id: str = "",
    quote: str = "",
    proposed_redline: Mapping[str, Any] | None = None,
    blocks_send: bool | None = None,
    confidence: float = 0.92,
) -> dict[str, Any]:
    """A single hand-authored clause assessment in the AI-first contract shape."""
    if issue_type is None:
        issue_type = "none" if decision == "pass" else "present_but_wrong"
    evidence: list[dict[str, Any]] = []
    if paragraph_id and quote:
        evidence.append({
            "paragraph_id": paragraph_id,
            "quote": quote,
            "relevance": "Supports the hand-authored verdict for this stub contract case.",
        })
    return {
        "clause_id": clause_id,
        "decision": decision,
        "issue_type": issue_type,
        "rationale": (
            f"{clause_id} assessed by the stub reviewer for the contract test; "
            "rationale text is long enough to satisfy the assessment contract validator."
        ),
        "evidence": evidence,
        "proposed_redline": proposed_redline or {"action": "no_change"},
        "confidence": confidence,
        "blocks_send": blocks_send if blocks_send is not None else decision == "review",
    }


def _all_pass_overrides(**overrides: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Baseline all-pass assessment per clause; override individual clauses by id."""
    # review_document paragraph ids are 1-indexed (p1..pN); the signature block is p6.
    base = {
        "mutuality": _assessment(
            "mutuality", "pass", paragraph_id="p1", quote="Each party may disclose Confidential Information"
        ),
        "confidential_information": _assessment(
            "confidential_information", "pass", paragraph_id="p2", quote='"Confidential Information" means non-public business'
        ),
        "governing_law": _assessment(
            "governing_law", "pass", paragraph_id="p3", quote="laws of England and Wales"
        ),
        "term_and_survival": _assessment(
            "term_and_survival", "pass", paragraph_id="p4", quote="fixed period of five years"
        ),
        "non_circumvention": _assessment("non_circumvention", "pass"),
        "signatures": _assessment(
            "signatures", "pass", paragraph_id="p6", quote="For Aspora Limited"
        ),
    }
    base.update(overrides)
    return base


def _response_from(by_clause: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {"assessments": [deepcopy(dict(a)) for a in by_clause.values()]}


def _run(source_text: str, by_clause: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    reviewer = InMemoryAssessmentReviewer(response=_response_from(by_clause))
    return assess_nda_with_ai(source_text, reviewer=reviewer)


def _clause(result: Mapping[str, Any], clause_id: str) -> dict[str, Any]:
    for clause in result.get("clauses") or []:
        if isinstance(clause, Mapping) and str(clause.get("id") or "") == clause_id:
            return dict(clause)
    raise AssertionError(f"clause {clause_id!r} not in result")


class ReviewerStubContractTests(unittest.TestCase):
    """Always-on, key-free contract/grounding/downgrade cases (no model judgment)."""

    def test_plumbing_smoke_all_pass_builds_ai_first_result(self) -> None:
        result = _run(_NDA_WITH_SIGNATURES, _all_pass_overrides())
        self.assertEqual(result["ai_first_review"]["mode"], AI_FIRST_ASSESSOR_MODE)
        self.assertEqual(result["ai_first_review"]["status"], "completed")
        self.assertEqual(result["requirements_failed"], 0)

    # ---- signatures: present + complete (was ZERO coverage) ----
    def test_signatures_present_grounded_pass_stays_pass(self) -> None:
        """A grounded pass on a present, complete signature block stays a grounded pass."""
        result = _run(_NDA_WITH_SIGNATURES, _all_pass_overrides())
        signatures = _clause(result, "signatures")
        self.assertEqual(signatures["decision"], CLAUSE_DECISION_PASS)
        self.assertEqual(signatures["grounding"]["status"], GROUNDING_GROUNDED)
        self.assertEqual(signatures["matched_paragraph_ids"], ["p6"])
        self.assertNotIn(UNGROUNDED_REASON_CODE, signatures.get("reason_codes") or [])

    def test_signatures_missing_block_fail_is_legitimate_absence(self) -> None:
        """A FAIL/missing on a document with no signature block stays a fail (absence).

        A ``missing`` fail carries no evidence quote but, per the assessment
        contract, still needs an ACTIONABLE redline (here an inserted block), so
        the fail is not auto-demoted for lack of a redline.
        """
        overrides = _all_pass_overrides(
            signatures=_assessment(
                "signatures",
                "fail",
                issue_type="missing",
                blocks_send=True,
                proposed_redline={
                    "action": "insert_after_paragraph",
                    "paragraph_id": "p5",
                    "text": (
                        "IN WITNESS WHEREOF, the parties have executed this Agreement.\n"
                        "For Aspora Limited\nBy: ____  Name: ____  Title: ____  Date: ____\n"
                        "For Counterparty Ltd\nBy: ____  Name: ____  Title: ____  Date: ____"
                    ),
                },
            )
        )
        result = _run(_NDA_WITHOUT_SIGNATURES, overrides)
        signatures = _clause(result, "signatures")
        # A required-but-missing clause is a legitimate quote-less absence; the fail
        # is preserved (NOT downgraded to review) and not flagged ungrounded.
        self.assertEqual(signatures["decision"], CLAUSE_DECISION_FAIL)
        self.assertEqual(signatures["grounding"]["status"], GROUNDING_ABSENCE)
        self.assertNotIn(UNGROUNDED_REASON_CODE, signatures.get("reason_codes") or [])

    def test_signatures_ungrounded_fail_is_downgraded_to_review(self) -> None:
        """A present_but_wrong FAIL with no quote can't be auto-verified -> review."""
        overrides = _all_pass_overrides(
            signatures=_assessment(
                "signatures",
                "fail",
                issue_type="present_but_wrong",
                blocks_send=True,
                # No evidence quote: the model claims the block is defective but
                # cites nothing groundable.
            )
        )
        result = _run(_NDA_WITH_SIGNATURES, overrides)
        signatures = _clause(result, "signatures")
        self.assertEqual(signatures["decision"], CLAUSE_DECISION_REVIEW)
        self.assertEqual(signatures["grounding"]["status"], GROUNDING_UNGROUNDED)
        self.assertIn(UNGROUNDED_REASON_CODE, signatures.get("reason_codes") or [])

    # ---- governing_law: grounded fail + redline plumbing ----
    def test_governing_law_grounded_fail_carries_redline(self) -> None:
        """A grounded governing-law fail preserves the fail and emits its redline."""
        source = _NDA_WITH_SIGNATURES.replace(
            "This Agreement shall be governed by the laws of England and Wales.",
            "This Agreement shall be governed by the laws of California.",
        )
        overrides = _all_pass_overrides(
            governing_law=_assessment(
                "governing_law",
                "fail",
                paragraph_id="p3",
                quote="laws of California",
                proposed_redline={
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p3",
                    "text": "This Agreement shall be governed by the laws of England and Wales.",
                    "jurisdiction": "England and Wales",
                },
            )
        )
        result = _run(source, overrides)
        governing_law = _clause(result, "governing_law")
        self.assertEqual(governing_law["decision"], CLAUSE_DECISION_FAIL)
        self.assertEqual(governing_law["decision_source"], "ai")
        self.assertEqual(governing_law["grounding"]["status"], GROUNDING_GROUNDED)
        redline = next(
            edit for edit in result["redline_edits"] if edit["clause_id"] == "governing_law"
        )
        self.assertEqual(redline["action"], REDLINE_REPLACE_PARAGRAPH)
        self.assertEqual(redline["paragraph_id"], "p3")

    # ---- confidential_information: ungrounded PASS is downgraded ----
    def test_confidential_information_ungrounded_pass_is_downgraded(self) -> None:
        """A pass the model could not ground in a quote is not trustworthy -> review."""
        overrides = _all_pass_overrides(
            confidential_information=_assessment(
                "confidential_information", "pass", issue_type="none"
                # No grounding quote on a present-clause pass.
            )
        )
        result = _run(_NDA_WITH_SIGNATURES, overrides)
        confidential = _clause(result, "confidential_information")
        self.assertEqual(confidential["decision"], CLAUSE_DECISION_REVIEW)
        self.assertEqual(confidential["grounding"]["status"], GROUNDING_UNGROUNDED)
        self.assertIn(UNGROUNDED_REASON_CODE, confidential.get("reason_codes") or [])


if __name__ == "__main__":
    unittest.main()
