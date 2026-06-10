import unittest

from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH
from nda_automation.review_document import split_document_paragraphs
from nda_automation.review_result_assembly import (
    aggregate_clause_results,
    assemble_redline_edits,
    build_review_context,
    prepare_review_document,
    refinalize_verifier_changed_clauses,
    verify_evidence_trust,
)


class ReviewResultAssemblyTests(unittest.TestCase):
    def test_prepare_review_document_can_infer_text_from_extracted_paragraphs(self):
        source_text, paragraphs = prepare_review_document(
            "",
            [
                {"text": "First clause.", "source_index": 4},
                {"text": "Second clause.", "source_index": 5},
            ],
            infer_text_from_paragraphs=True,
        )

        self.assertEqual(source_text, "First clause.\n\nSecond clause.")
        self.assertEqual([paragraph["id"] for paragraph in paragraphs], ["p1", "p2"])
        self.assertEqual([paragraph["source_index"] for paragraph in paragraphs], [4, 5])
        self.assertEqual(paragraphs[0]["start"], 0)
        self.assertEqual(paragraphs[1]["start"], len("First clause.\n\n"))

    def test_prepare_review_document_preserves_paragraphs_without_inferred_text(self):
        original = {"id": "doc-p1", "text": "First clause.", "start": 50}

        source_text, paragraphs = prepare_review_document("", [original])

        self.assertEqual(source_text, "")
        self.assertEqual(paragraphs, [original])
        self.assertIsNot(paragraphs[0], original)

    def test_build_review_context_reuses_existing_result_context(self):
        paragraphs = split_document_paragraphs("This Agreement is mutual.")
        existing_context = {
            "contract_structure": {"sections": [{"id": "custom"}]},
            "reference_resolver": {"references": [{"id": "ref-1"}]},
            "concept_classifier": {"concepts_by_clause_id": {"mutuality": ["mutual"]}},
        }

        context = build_review_context(paragraphs, review_result=existing_context)

        self.assertIs(context["contract_structure"], existing_context["contract_structure"])
        self.assertIs(context["reference_resolver"], existing_context["reference_resolver"])
        self.assertIs(context["concept_classifier"], existing_context["concept_classifier"])

    def test_aggregate_clause_results_returns_review_contract_counts(self):
        summary = aggregate_clause_results([
            {"id": "pass_clause", "decision": "pass"},
            {"id": "review_clause", "decision": "review", "needs_review": True},
            {"id": "fail_clause", "decision": "fail"},
        ])

        self.assertEqual(summary["requirements_passed"], 1)
        self.assertEqual(summary["requirements_needs_review"], 1)
        self.assertEqual(summary["requirements_failed"], 1)
        self.assertEqual(summary["review_state"]["counts"]["pass"], 1)
        self.assertEqual(summary["review_state"]["counts"]["review"], 1)
        self.assertEqual(summary["review_state"]["counts"]["check"], 1)
        self.assertEqual(summary["overall_status"], summary["review_state"]["overall_status"])

    def test_assemble_redline_edits_attaches_rationale_to_redlined_clause(self):
        clauses = [{
            "id": "governing_law",
            "requirement": "governing law must be England and Wales.",
            "reason": "Governing law is California.",
            "citation": {"quote": "laws of California", "paragraph_id": "p1"},
        }]
        paragraphs = [{"id": "p1", "index": 1, "text": "Governed by the laws of California.", "start": 0, "end": 37}]

        def build_redlines(clause_results, document_paragraphs):
            self.assertIs(clause_results[0], clauses[0])
            self.assertEqual(document_paragraphs, paragraphs)
            return [{
                "id": "r1",
                "clause_id": "governing_law",
                "action": REDLINE_REPLACE_PARAGRAPH,
                "paragraph_id": "p1",
                "original_text": "Governed by the laws of California.",
                "replacement_text": "Governed by the laws of England and Wales.",
            }]

        edits = assemble_redline_edits(
            clauses,
            paragraphs,
            playbook_clauses_by_id={"governing_law": {"requirement": "governing law must be England and Wales."}},
            build_redline_edits=build_redlines,
        )

        self.assertEqual([edit["id"] for edit in edits], ["r1"])
        self.assertEqual(clauses[0]["redline_rationale"]["basis"], {"quote": "laws of California", "paragraph_id": "p1"})
        self.assertIn("Playbook requires", clauses[0]["redline_rationale"]["explanation"])

    def test_refinalize_verifier_changed_clauses_calls_finalizer_only_for_changed_records(self):
        clauses = [{"id": "changed"}, {"id": "unchanged"}, {"id": "not_listed"}]
        finalized = []

        refinalize_verifier_changed_clauses(
            clauses,
            {"records": [{"clause_id": "changed", "changed": True}, {"clause_id": "unchanged", "changed": False}]},
            lambda clause: finalized.append(clause["id"]),
        )

        self.assertEqual(finalized, ["changed"])

    def test_verify_evidence_trust_stamps_verified_marker(self):
        paragraph = split_document_paragraphs("Alpha")[0]
        result = {
            "overall_status": "meets_requirements",
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 0,
            "paragraphs": [paragraph],
            "clauses": [{
                "id": "alpha",
                "decision": "pass",
                "decision_reason": "ok",
                "matched_paragraph_ids": ["p1"],
                "matched_text": "Alpha",
                "evidence": ["Alpha"],
                "evidence_paragraphs": [paragraph],
                "structured_evidence": [{
                    "paragraph_id": "p1",
                    "text": "Alpha",
                    "start": 0,
                    "end": 5,
                    "reason_code": "alpha_pass",
                    "reason_codes": ["alpha_pass"],
                }],
                "audit_trace": {
                    "decision": "pass",
                    "decision_reason": "ok",
                    "reason_code": "alpha_pass",
                    "reason_codes": ["alpha_pass"],
                    "evidence_summary": {"paragraph_ids": ["p1"], "structured_evidence_count": 1},
                },
                "review_state": {
                    "decision": "pass",
                    "state": "pass",
                    "reason_code": "alpha_pass",
                    "reason_codes": ["alpha_pass"],
                },
                "reason_code": "alpha_pass",
                "reason_codes": ["alpha_pass"],
            }],
            "review_state": {
                "overall_status": "meets_requirements",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
        }

        verify_evidence_trust(result, "Alpha", error_message="bad evidence: ")

        self.assertEqual(result["evidence_trust"], {"status": "verified", "errors": []})


if __name__ == "__main__":
    unittest.main()
