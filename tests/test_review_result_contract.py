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
    assert updated["source_fidelity"]["source_type"] == "pdf"
    assert updated["source_fidelity"]["capabilities"]["pdf_page_references"] is False


def test_attach_document_source_flags_and_gates_tracked_changes():
    # An all-pass clause set must never silently clear when the source carried
    # unresolved redlines: the marker + document-level gate force human review.
    paragraphs = [{"id": "p1", "text": "Five year term."}]
    result = {
        "clauses": [{"id": "term", "decision": "pass"}],
        "requirements_passed": 1,
        "requirements_failed": 0,
        "requirements_needs_review": 0,
        "overall_status": "meets_requirements",
    }

    updated = attach_document_source(
        result,
        filename="NDA.docx",
        document_type="docx",
        extracted_paragraphs=paragraphs,
        extraction_quality={
            "has_tracked_changes": True,
            "tracked_insertions": 1,
            "tracked_deletions": 1,
            "reviewed_state": "in_force_baseline",
            "warnings": [{"type": "docx_unresolved_tracked_changes", "message": "Unresolved tracked changes."}],
        },
    )

    assert updated["tracked_changes"]["has_tracked_changes"] is True
    assert updated["tracked_changes"]["reviewed_state"] == "in_force_baseline"
    assert updated["review_warnings"][0]["type"] == "docx_unresolved_tracked_changes"
    # The stored state is re-derived through the gate, not left at the build-time pass.
    assert updated["overall_status"] == "needs_review"
    assert updated["review_state"]["state"] == "review"
    assert updated["review_state"]["blocks_send"] is True
    assert updated["review_state"]["tracked_changes_forced_review"] is True


def test_attach_document_source_does_not_flag_clean_docx():
    paragraphs = [{"id": "p1", "text": "Five year term."}]
    result = {
        "clauses": [{"id": "term", "decision": "pass"}],
        "requirements_passed": 1,
        "requirements_failed": 0,
        "requirements_needs_review": 0,
        "overall_status": "meets_requirements",
    }

    updated = attach_document_source(
        result,
        filename="NDA.docx",
        document_type="docx",
        extracted_paragraphs=paragraphs,
        extraction_quality=None,
    )

    assert "tracked_changes" not in updated
    assert updated["overall_status"] == "meets_requirements"


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
        result_fields={},
    )

    assert result["review_engine_version"] == 8
    assert result["review_mode"] == "ai_first_compat"
    assert result["checked_at"] == "2026-06-10T10:00:00+00:00"
    assert result["requirements_passed"] == 1
    assert result["requirements_needs_review"] == 1
    assert result["requirements_failed"] == 1
    assert result["ai_first_review"] == {"status": "normalized"}
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
        "approved_positions": ["No non-circumvention restriction", "Narrow customer non-solicit only"],
        "recommended_option": {
            "option": "No non-circumvention restriction",
            "reason": "It matches the prohibited-clause playbook position.",
        },
        "resolution_question": "Is this restriction a prohibited non-circumvention clause?",
        "suggested_redline": "Delete the non-circumvention restriction.",
        "what_to_fix": "Confirm whether this language restricts ordinary course dealings.",
    }

    proposed = build_proposed_change(clause)

    assert proposed["action"] == "needs_human_choice"
    assert proposed["resolution_question"] == "Is this restriction a prohibited non-circumvention clause?"
    assert proposed["suggested_redline"] == "Delete the non-circumvention restriction."
    assert proposed["recommended_option"]["option"] == "No non-circumvention restriction"
    assert proposed["approved_alternatives"] == [
        "No non-circumvention restriction",
        "Narrow customer non-solicit only",
    ]
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


# --- Reading-order confidence carry (the "make degraded extraction loud" seam) -----

from nda_automation.review_result_contract import (  # noqa: E402
    READING_ORDER_RESULT_FIELD,
    carry_reading_order_signal,
    reading_order_signal,
)


def _degraded_reading_order_quality(*, garbled=False, columns=2, confidence=0.4):
    """An extraction-quality report whose reading-order block is DEGRADED, shaped
    exactly like nda_automation.pdf_text._reading_order_signal emits it."""
    reasons = ["fragmented_or_letterspaced_text"] if garbled else ["column_reconstructed"]
    warning_type = "pdf_fragmented_text" if garbled else "pdf_reading_order_uncertain"
    return {
        "page_count": 2,
        "warnings": [
            {"type": "pdf_sparse_text", "message": "unrelated warning stays out of scope"},
            {"type": warning_type, "message": "Check the original source."},
        ],
        "reading_order": {
            "reading_order_confidence": confidence,
            "columns_detected": columns,
            "reorder_applied": True,
            "garbled": garbled,
            "degraded": True,
            "reasons": reasons,
        },
    }


def _clean_reading_order_quality():
    return {
        "page_count": 1,
        "warnings": [],
        "reading_order": {
            "reading_order_confidence": 1.0,
            "columns_detected": 1,
            "reorder_applied": False,
            "garbled": False,
            "degraded": False,
            "reasons": [],
        },
    }


def test_reading_order_signal_returns_block_only_when_present():
    assert reading_order_signal(_clean_reading_order_quality())["columns_detected"] == 1
    assert reading_order_signal({"warnings": []}) is None  # docx / no reading order
    assert reading_order_signal(None) is None
    assert reading_order_signal({"reading_order": "bad"}) is None


def test_carry_reading_order_degraded_sets_field_and_lifts_only_its_own_warning():
    result = {"clauses": []}
    quality = _degraded_reading_order_quality()

    block = carry_reading_order_signal(result, quality)

    assert block is quality["reading_order"]
    assert result[READING_ORDER_RESULT_FIELD]["degraded"] is True
    # ONLY the reading-order warning is lifted; the unrelated pdf_sparse_text is NOT.
    lifted_types = [w["type"] for w in result["review_warnings"]]
    assert lifted_types == ["pdf_reading_order_uncertain"]


def test_carry_reading_order_garbled_lifts_fragmented_warning():
    result = {"clauses": []}
    carry_reading_order_signal(result, _degraded_reading_order_quality(garbled=True))
    assert [w["type"] for w in result["review_warnings"]] == ["pdf_fragmented_text"]
    assert result[READING_ORDER_RESULT_FIELD]["garbled"] is True


def test_carry_reading_order_clean_sets_field_but_adds_no_warning():
    result = {"clauses": []}
    block = carry_reading_order_signal(result, _clean_reading_order_quality())
    assert block["degraded"] is False
    # Field present (provenance) but NO banner-driving warning: a clean single-column
    # NDA must surface nothing to the reviewer -- warning fatigue would gut the feature.
    assert result[READING_ORDER_RESULT_FIELD]["reading_order_confidence"] == 1.0
    assert "review_warnings" not in result


def test_carry_reading_order_missing_block_is_a_no_op():
    result = {"clauses": []}
    assert carry_reading_order_signal(result, {"page_count": 1, "warnings": []}) is None
    assert READING_ORDER_RESULT_FIELD not in result
    assert "review_warnings" not in result


def test_carry_reading_order_is_idempotent_and_dedupes_the_warning():
    result = {"clauses": []}
    quality = _degraded_reading_order_quality()
    carry_reading_order_signal(result, quality)
    carry_reading_order_signal(result, quality)  # calling twice must not double it
    assert [w["type"] for w in result["review_warnings"]] == ["pdf_reading_order_uncertain"]


def test_attach_document_source_rides_reading_order_field_without_doubling_warning():
    # The eager path lifts ALL warnings via _append_extraction_warnings AND sets the
    # first-class field via carry_reading_order_signal -- the reading-order warning must
    # appear exactly once, not twice.
    result = {"clauses": []}
    attach_document_source(
        result,
        filename="NDA.pdf",
        document_type="pdf",
        extracted_paragraphs=[{"id": "p1", "text": "Body."}],
        extraction_quality=_degraded_reading_order_quality(),
    )
    assert result[READING_ORDER_RESULT_FIELD]["degraded"] is True
    ro_warnings = [w for w in result["review_warnings"] if w["type"] == "pdf_reading_order_uncertain"]
    assert len(ro_warnings) == 1
    # And it is retrievable at the seam the eager path also stores it under.
    assert result["source"]["extraction_quality"]["reading_order"]["columns_detected"] == 2
