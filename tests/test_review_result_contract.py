from __future__ import annotations

from nda_automation.review_result_contract import (
    attach_document_source,
    build_review_result,
    build_proposed_change,
    extracted_text_from_paragraphs,
    review_result_clause_counts,
    review_result_paragraphs,
)


def test_attach_document_source_adds_canonical_source_metadata_and_warnings():
    paragraphs = [{"id": "p1", "text": "First."}, {"id": "p2", "text": "Second."}]
    result = {"clauses": []}

    updated = attach_document_source(
        result,
        filename="NDA.pdf",
        document_type="pdf",
        extracted_paragraphs=paragraphs,
        extraction_quality={"page_count": 2, "warnings": ["Scanned page skipped."]},
    )

    assert updated is result
    assert updated["extracted_text"] == "First.\n\nSecond."
    assert updated["source"] == {
        "filename": "NDA.pdf",
        "type": "pdf",
        "extracted_characters": len("First.\n\nSecond."),
        "extracted_paragraphs": 2,
        "extraction_quality": {"page_count": 2, "warnings": ["Scanned page skipped."]},
    }
    assert updated["review_warnings"] == ["Scanned page skipped."]


def test_extracted_text_from_paragraphs_uses_review_result_separator():
    assert extracted_text_from_paragraphs([{"text": "A"}, {"text": "B"}]) == "A\n\nB"


def test_build_review_result_owns_counts_metadata_and_evidence_stamp():
    clauses = [
        {"id": "pass", "decision": "pass", "reason_codes": []},
        {"id": "review", "decision": "review", "reason_codes": []},
        {"id": "fail", "decision": "fail", "reason_codes": []},
    ]

    result = build_review_result(
        source_text="NDA text.",
        review_engine_version=8,
        metadata_fields={"review_mode": "ai_first_compat"},
        review_state={
            "overall_status": "redline_required",
            "state": "check",
            "counts": {"pass": 1, "review": 1, "check": 1},
        },
        checked_at="2026-06-10T10:00:00+00:00",
        paragraphs=[{"id": "p1", "text": "NDA text."}],
        contract_structure={"sections": []},
        reference_resolver={"references": []},
        concept_classifier={"concepts": []},
        semantic_crosscheck={"status": "not_run"},
        ai_review={"status": "completed"},
        review_fields={"ai_first_review": {"status": "normalized"}},
        ai_verifier={"status": "disabled"},
        clauses=clauses,
        redline_edits=[],
        result_fields={"unmatched_sections": []},
    )

    assert result["review_engine_version"] == 8
    assert result["review_mode"] == "ai_first_compat"
    assert result["checked_at"] == "2026-06-10T10:00:00+00:00"
    assert result["requirements_passed"] == 1
    assert result["requirements_needs_review"] == 1
    assert result["requirements_failed"] == 1
    assert result["ai_first_review"] == {"status": "normalized"}
    assert result["unmatched_sections"] == []
    assert [change["clause_id"] for change in result["proposed_changes"]] == ["review", "fail"]
    assert result["evidence_trust"] == {"status": "verified", "errors": []}


def test_proposed_change_uses_concrete_replacement_redline():
    clause = {
        "id": "governing_law",
        "name": "Governing Law",
        "decision": "fail",
        "issue_type": "present_but_wrong",
        "issue_label": "Present but wrong",
        "rationale": "The Playbook requires approved governing law.",
        "citation": {"quote": "laws of California", "paragraph_id": "p3"},
        "confidence": 0.8,
    }
    redline = {
        "id": "r1",
        "clause_id": "governing_law",
        "paragraph_id": "p3",
        "action": "replace_paragraph",
        "original_text": "This Agreement shall be governed by the laws of California.",
        "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
    }

    proposed = build_proposed_change(clause, redline)

    assert proposed["action"] == "replace"
    assert proposed["source_text"] == "This Agreement shall be governed by the laws of California."
    assert proposed["proposed_text"] == "This Agreement shall be governed by the laws of England and Wales."
    assert proposed["evidence"] == {"quote": "laws of California", "paragraph_id": "p3"}
    assert proposed["safety"]["status"] == "proposed_redline_available"
    assert proposed["safety"]["requires_human_approval"] is True


def test_proposed_change_uses_missing_clause_insert_redline():
    clause = {
        "id": "signatures",
        "name": "Signatures",
        "decision": "fail",
        "issue_type": "missing",
        "what_to_fix": "Add signature blocks.",
    }
    redline = {
        "id": "r2",
        "clause_id": "signatures",
        "paragraph_id": "p9",
        "action": "insert_after_paragraph",
        "anchor_text": "Last paragraph.",
        "insert_text": "For [Party 1 legal name]\nBy:\nTitle:\nDate:",
    }

    proposed = build_proposed_change(clause, redline)

    assert proposed["action"] == "insert"
    assert proposed["source_text"] == "Last paragraph."
    assert proposed["proposed_text"].startswith("For [Party 1 legal name]")
    assert proposed["paragraph_id"] == "p9"


def test_proposed_change_marks_ambiguous_clause_as_human_choice():
    clause = {
        "id": "non_circumvention",
        "name": "Non-circumvention",
        "decision": "review",
        "issue_type": "unclear",
        "issue_label": "Needs review",
        "what_to_fix": "Confirm whether this language restricts ordinary course dealings.",
    }

    proposed = build_proposed_change(clause)

    assert proposed["action"] == "needs_human_choice"
    assert proposed["safety"]["status"] == "needs_human_choice"
    assert proposed["safety"]["requires_human_approval"] is True
    assert "Confirm whether" in proposed["safety"]["reason"]


def test_proposed_change_marks_failed_clause_without_safe_redline_as_comment_only():
    clause = {
        "id": "dynamic_clause",
        "name": "Dynamic Clause",
        "decision": "fail",
        "issue_type": "present_but_wrong",
        "reason": "The clause is wrong but has no safe fallback wording.",
    }

    proposed = build_proposed_change(clause)

    assert proposed["action"] == "comment_only"
    assert proposed["safety"]["status"] == "comment_only"


def test_review_result_clause_counts_ignores_non_final_decisions():
    assert review_result_clause_counts([
        {"decision": "pass"},
        {"decision": "review"},
        {"decision": "fail"},
        {"decision": "unknown"},
    ]) == {"passed": 1, "needs_review": 1, "failed": 1}


def test_review_result_paragraphs_returns_cleaned_paragraphs_or_none():
    assert review_result_paragraphs({"paragraphs": [{"id": "p1"}, "bad", {"id": "p2"}]}) == [
        {"id": "p1"},
        {"id": "p2"},
    ]
    assert review_result_paragraphs({"paragraphs": []}) is None
    assert review_result_paragraphs(None) is None
