import unittest

from nda_automation.evidence_grounding import (
    GROUNDING_ABSENCE,
    GROUNDING_GROUNDED,
    GROUNDING_UNGROUNDED,
    UNGROUNDED_REASON_CODE,
    UNGROUNDED_REVIEW_CAVEAT,
    UNGROUNDED_REVIEW_REASON,
    build_citation,
    build_grounding,
    classify_grounding,
    downgrade_ungrounded_finding,
    refinalize_clause_grounding,
    ungrounded_review_reason,
)


def _quoted_record(quote="laws of california", *, paragraph_id="p3", start=10, end=27):
    return {
        "paragraph_id": paragraph_id,
        "matched_text": quote,
        "matched_terms": [quote],
        "match_spans": [{"start": start, "end": end, "text": quote, "term": quote}],
        "relevance": "Shows the governing-law jurisdiction.",
    }


def _unquoted_record(paragraph_id="p1"):
    # Mirrors how a clause with no cited quote falls back to the whole paragraph.
    return {
        "paragraph_id": paragraph_id,
        "matched_text": "Each party may disclose Confidential Information.",
        "matched_terms": [],
        "match_spans": [],
    }


class ClassifyGroundingTests(unittest.TestCase):
    def test_quote_evidence_grounds_a_finding(self):
        status = classify_grounding(
            decision="fail",
            issue_type="present_but_wrong",
            clause_type="required",
            structured_evidence=[_quoted_record()],
        )
        self.assertEqual(status, GROUNDING_GROUNDED)

    def test_missing_required_clause_is_legitimate_absence(self):
        status = classify_grounding(
            decision="fail",
            issue_type="missing",
            clause_type="required",
            structured_evidence=[],
        )
        self.assertEqual(status, GROUNDING_ABSENCE)

    def test_absent_prohibited_clause_pass_is_legitimate_absence(self):
        status = classify_grounding(
            decision="pass",
            issue_type="none",
            clause_type="prohibited",
            structured_evidence=[],
        )
        self.assertEqual(status, GROUNDING_ABSENCE)

    def test_pass_on_required_clause_without_quote_is_ungrounded(self):
        status = classify_grounding(
            decision="pass",
            issue_type="none",
            clause_type="required",
            structured_evidence=[_unquoted_record()],
        )
        self.assertEqual(status, GROUNDING_UNGROUNDED)

    def test_present_but_wrong_fail_without_quote_is_ungrounded(self):
        # A prohibited clause that is supposedly *present and wrong* must quote it.
        status = classify_grounding(
            decision="fail",
            issue_type="present_but_wrong",
            clause_type="prohibited",
            structured_evidence=[],
        )
        self.assertEqual(status, GROUNDING_UNGROUNDED)

    def test_verifier_cleared_pass_on_required_clause_is_absence(self):
        # The gap the verifier flagged: a refute-to-pass on a REQUIRED clause
        # clears its evidence; without the verifier marker this would be read as
        # ungrounded, but decision_source=ai_verifier makes it a legit absence.
        ungrounded = classify_grounding(
            decision="pass",
            issue_type="none",
            clause_type="required",
            structured_evidence=[],
        )
        self.assertEqual(ungrounded, GROUNDING_UNGROUNDED)

        absence = classify_grounding(
            decision="pass",
            issue_type="none",
            clause_type="required",
            structured_evidence=[],
            decision_source="ai_verifier",
        )
        self.assertEqual(absence, GROUNDING_ABSENCE)


class BuildGroundingTests(unittest.TestCase):
    def test_grounded_finding_counts_quoted_evidence(self):
        grounding = build_grounding(
            decision="fail",
            issue_type="present_but_wrong",
            clause_type="required",
            structured_evidence=[_quoted_record(), _unquoted_record()],
        )
        self.assertEqual(grounding["status"], GROUNDING_GROUNDED)
        self.assertEqual(grounding["evidence_count"], 1)
        self.assertTrue(grounding["grounded"])
        self.assertTrue(grounding["requires_quote"])

    def test_absence_finding_does_not_require_quote(self):
        grounding = build_grounding(
            decision="fail",
            issue_type="missing",
            clause_type="required",
            structured_evidence=[],
        )
        self.assertEqual(grounding["status"], GROUNDING_ABSENCE)
        self.assertEqual(grounding["evidence_count"], 0)
        self.assertFalse(grounding["requires_quote"])


class BuildCitationTests(unittest.TestCase):
    def test_citation_exposes_quote_and_offsets(self):
        citation = build_citation([_unquoted_record(), _quoted_record()])
        self.assertEqual(citation["quote"], "laws of california")
        self.assertEqual(citation["paragraph_id"], "p3")
        self.assertEqual(citation["start"], 10)
        self.assertEqual(citation["end"], 27)
        self.assertEqual(citation["relevance"], "Shows the governing-law jurisdiction.")

    def test_no_quote_yields_no_citation(self):
        self.assertIsNone(build_citation([_unquoted_record()]))


class DowngradeUngroundedFindingTests(unittest.TestCase):
    def test_pass_is_downgraded_to_review_and_blocks_send(self):
        downgrade = downgrade_ungrounded_finding(
            decision="pass",
            issue_type="none",
            blocks_send=False,
            reason_codes=["ai_first_pass"],
        )
        self.assertEqual(downgrade["decision"], "review")
        self.assertEqual(downgrade["issue_type"], "unclear")
        self.assertTrue(downgrade["blocks_send"])
        self.assertTrue(downgrade["downgraded"])
        self.assertEqual(downgrade["downgraded_from"], "pass")
        self.assertEqual(downgrade["reason_codes"][0], UNGROUNDED_REASON_CODE)
        self.assertIn("ai_first_pass", downgrade["reason_codes"])

    def test_existing_review_keeps_primary_reason_code_and_appends_flag(self):
        downgrade = downgrade_ungrounded_finding(
            decision="review",
            issue_type="unclear",
            blocks_send=True,
            reason_codes=["ai_first_missing_assessment"],
        )
        self.assertEqual(downgrade["decision"], "review")
        self.assertFalse(downgrade["downgraded"])
        self.assertEqual(downgrade["reason_codes"][0], "ai_first_missing_assessment")
        self.assertEqual(downgrade["reason_codes"][-1], UNGROUNDED_REASON_CODE)

    def test_ungrounded_reason_code_is_not_duplicated(self):
        downgrade = downgrade_ungrounded_finding(
            decision="fail",
            issue_type="present_but_wrong",
            blocks_send=False,
            reason_codes=[UNGROUNDED_REASON_CODE, "ai_first_fail"],
        )
        self.assertEqual(downgrade["reason_codes"].count(UNGROUNDED_REASON_CODE), 1)

    def test_downgraded_reason_leads_with_substance_then_caveat(self):
        downgrade = downgrade_ungrounded_finding(
            decision="pass",
            issue_type="none",
            blocks_send=False,
            reason_codes=["ai_first_pass"],
            substantive_reason="The definition is appropriately broad",
        )
        # The model's own concern leads, the honest caveat follows, and the wording
        # never claims the document lacks the text.
        self.assertEqual(
            downgrade["reason"],
            f"The definition is appropriately broad. {UNGROUNDED_REVIEW_CAVEAT}",
        )
        self.assertNotIn("quotable text from the document", downgrade["reason"])

    def test_downgraded_reason_falls_back_when_no_substance(self):
        downgrade = downgrade_ungrounded_finding(
            decision="fail",
            issue_type="present_but_wrong",
            blocks_send=False,
            reason_codes=["ai_first_fail"],
            substantive_reason="   ",
        )
        self.assertEqual(downgrade["reason"], UNGROUNDED_REVIEW_REASON)

    def test_ungrounded_review_reason_composition(self):
        self.assertEqual(
            ungrounded_review_reason("The term is too long"),
            f"The term is too long. {UNGROUNDED_REVIEW_CAVEAT}",
        )
        # Already-punctuated substance is not double-punctuated.
        self.assertEqual(
            ungrounded_review_reason("The term is too long."),
            f"The term is too long. {UNGROUNDED_REVIEW_CAVEAT}",
        )
        # No substance -> standalone reason.
        self.assertEqual(ungrounded_review_reason(""), UNGROUNDED_REVIEW_REASON)


class RefinalizeClauseGroundingTests(unittest.TestCase):
    def test_verifier_cleared_pass_becomes_absence_and_drops_citation(self):
        # The verifier refuted a fail to a pass and cleared the disproven
        # evidence; the stale grounded citation must not survive.
        clause = {
            "decision": "pass",
            "issue_type": "none",
            "type": "required",
            "decision_source": "ai_verifier",
            "structured_evidence": [],
            "grounding": {"status": GROUNDING_GROUNDED, "evidence_count": 1},
            "citation": {"quote": "old disproven text", "paragraph_id": "p1"},
        }
        refinalize_clause_grounding(clause)
        self.assertEqual(clause["grounding"]["status"], GROUNDING_ABSENCE)
        self.assertEqual(clause["grounding"]["evidence_count"], 0)
        self.assertNotIn("citation", clause)

    def test_rederives_fresh_citation_from_current_evidence(self):
        clause = {
            "decision": "fail",
            "issue_type": "present_but_wrong",
            "type": "required",
            "decision_source": "ai",
            "structured_evidence": [_quoted_record(quote="laws of California", start=5, end=23)],
            "grounding": {"status": GROUNDING_UNGROUNDED, "evidence_count": 0},
        }
        refinalize_clause_grounding(clause)
        self.assertEqual(clause["grounding"]["status"], GROUNDING_GROUNDED)
        self.assertEqual(clause["citation"]["quote"], "laws of California")
        self.assertEqual(clause["citation"]["start"], 5)

    def test_lost_quote_drops_stale_citation_without_verifier_marker(self):
        # A non-verifier clause that lost its quote is genuinely ungrounded;
        # its old citation must be removed.
        clause = {
            "decision": "pass",
            "issue_type": "none",
            "type": "required",
            "decision_source": "ai",
            "structured_evidence": [_unquoted_record()],
            "citation": {"quote": "stale", "paragraph_id": "p1"},
        }
        refinalize_clause_grounding(clause)
        self.assertEqual(clause["grounding"]["status"], GROUNDING_UNGROUNDED)
        self.assertNotIn("citation", clause)

    def test_non_mapping_clause_is_returned_unchanged(self):
        self.assertEqual(refinalize_clause_grounding(None), None)


if __name__ == "__main__":
    unittest.main()
