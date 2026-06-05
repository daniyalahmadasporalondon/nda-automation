import unittest

from nda_automation.evidence_grounding import (
    GROUNDING_ABSENCE,
    GROUNDING_GROUNDED,
    GROUNDING_UNGROUNDED,
    UNGROUNDED_REASON_CODE,
    build_citation,
    build_grounding,
    classify_grounding,
    downgrade_ungrounded_finding,
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


if __name__ == "__main__":
    unittest.main()
