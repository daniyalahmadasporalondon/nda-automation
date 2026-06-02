import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import ai_review
from nda_automation.checker import review_nda

ROOT = Path(__file__).resolve().parent.parent


def _pass_sample_text():
    return (ROOT / "samples" / "pass-nda.txt").read_text(encoding="utf-8")


def _first_citation(packet):
    paragraph = packet["paragraphs"][0]
    quote = str(paragraph["text"])[:80]
    return {
        "paragraph_id": paragraph["id"],
        "quote": quote,
        "relevance": "Supports the clause decision.",
    }


def _confirming_reviewer(packet):
    return {
        "decision": packet["deterministic_result"]["decision"],
        "confidence": 0.93,
        "reason": "The supplied evidence supports the deterministic decision.",
        "cited_spans": [_first_citation(packet)],
        "issues": [],
        "suggested_fix": "",
    }


class AIReviewTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {
                "NDA_AI_REVIEW_ENABLED": "",
                "NDA_AI_REVIEW_CLAUSES": "",
                "NDA_AI_REVIEW_THRESHOLD": "",
                "NDA_AI_PROVIDER": "gemini",
                "NDA_AI_MODEL": "gemini-2.5-flash",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def test_ai_review_is_disabled_by_default(self):
        result = review_nda(_pass_sample_text())

        self.assertEqual(result["ai_review"]["status"], "disabled")
        self.assertFalse(any("ai_review_analysis" in clause for clause in result["clauses"]))
        self.assertEqual(result["overall_status"], "meets_requirements")

    def test_ai_review_can_confirm_deterministic_passes(self):
        result = review_nda(_pass_sample_text(), ai_reviewer=_confirming_reviewer)

        self.assertEqual(result["ai_review"]["status"], "completed")
        self.assertEqual(result["ai_review"]["record_count"], 5)
        self.assertEqual(result["overall_status"], "meets_requirements")
        reviewed = [clause for clause in result["clauses"] if clause.get("ai_review_analysis")]
        self.assertEqual(len(reviewed), 5)
        self.assertTrue(all(clause["ai_review_analysis"]["status"] == "confirmed" for clause in reviewed))

    def test_ai_disagreement_escalates_to_review_without_auto_redline(self):
        def reviewer(packet):
            if packet["clause"]["id"] == "mutuality":
                return {
                    "decision": "fail",
                    "confidence": 0.91,
                    "reason": "The clause appears one-way.",
                    "cited_spans": [_first_citation(packet)],
                    "issues": ["possible_one_way_language"],
                    "suggested_fix": "Confirm whether obligations are reciprocal.",
                }
            return _confirming_reviewer(packet)

        result = review_nda(_pass_sample_text(), ai_reviewer=reviewer)
        mutuality = next(clause for clause in result["clauses"] if clause["id"] == "mutuality")

        self.assertEqual(result["overall_status"], "needs_review")
        self.assertEqual(mutuality["decision"], "review")
        self.assertEqual(mutuality["reason_code"], "ai_semantic_disagreement")
        self.assertTrue(mutuality["review_state"]["blocks_send"])
        self.assertEqual(mutuality["ai_review_analysis"]["ai_decision"], "fail")
        self.assertTrue(mutuality["ai_review_analysis"]["disagreement"])
        self.assertFalse([edit for edit in result["redline_edits"] if edit["clause_id"] == "mutuality"])

    def test_ai_low_confidence_escalates_to_review(self):
        def reviewer(packet):
            if packet["clause"]["id"] == "governing_law":
                response = _confirming_reviewer(packet)
                response["confidence"] = 0.42
                return response
            return _confirming_reviewer(packet)

        result = review_nda(_pass_sample_text(), ai_reviewer=reviewer)
        governing_law = next(clause for clause in result["clauses"] if clause["id"] == "governing_law")

        self.assertEqual(result["overall_status"], "needs_review")
        self.assertEqual(governing_law["decision"], "review")
        self.assertEqual(governing_law["reason_code"], "ai_confidence_below_threshold")
        self.assertIn("below the review threshold", governing_law["decision_reason"])

    def test_ai_invalid_citation_escalates_to_review(self):
        def reviewer(packet):
            if packet["clause"]["id"] == "term_and_survival":
                return {
                    "decision": "pass",
                    "confidence": 0.9,
                    "reason": "The term is compliant.",
                    "cited_spans": [{
                        "paragraph_id": packet["paragraphs"][0]["id"],
                        "quote": "This quote does not exist in the paragraph.",
                        "relevance": "Invalid citation.",
                    }],
                    "issues": [],
                    "suggested_fix": "",
                }
            return _confirming_reviewer(packet)

        result = review_nda(_pass_sample_text(), ai_reviewer=reviewer)
        term = next(clause for clause in result["clauses"] if clause["id"] == "term_and_survival")

        self.assertEqual(result["overall_status"], "needs_review")
        self.assertEqual(term["decision"], "review")
        self.assertEqual(term["reason_code"], "ai_citation_validation_failed")
        self.assertTrue(term["ai_review_analysis"]["validation_errors"])

    def test_gemini_request_body_uses_structured_json_response_format(self):
        packet = {
            "task": "semantic_clause_crosscheck",
            "clause": {"id": "mutuality"},
            "paragraphs": [{"id": "p1", "text": "Each party is bound."}],
        }

        body = ai_review._gemini_request_body(packet)
        encoded = json.dumps(body)

        self.assertEqual(body["generationConfig"]["temperature"], 0)
        self.assertEqual(
            body["generationConfig"]["responseFormat"]["text"]["mimeType"],
            "application/json",
        )
        self.assertIn("schema", body["generationConfig"]["responseFormat"]["text"])
        self.assertIn("semantic_clause_crosscheck", encoded)


if __name__ == "__main__":
    unittest.main()
