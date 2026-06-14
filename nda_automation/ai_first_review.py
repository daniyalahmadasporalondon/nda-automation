from __future__ import annotations

import re
from copy import deepcopy
from collections.abc import Mapping, Sequence
from typing import Any

from .ai_assessment_contract import (
    AI_ASSESSMENT_CONTRACT_VERSION,
    AI_REDLINE_NO_CHANGE,
    _fold_typographic_glyphs,
    _normalize_quote_text,
    validate_ai_clause_assessments,
)
from .checker import REVIEW_ENGINE_VERSION, _build_redline_edits, load_playbook, validate_playbook
from .checks.common import (
    ISSUE_TYPE_LABELS,
    ISSUE_TYPE_MISSING,
    ISSUE_TYPE_NONE,
    ISSUE_TYPE_PRESENT_BUT_WRONG,
    ISSUE_TYPE_UNCLEAR,
    MAX_EVIDENCE_PARAGRAPHS,
    ClauseResult,
)
from .concept_classifier import classify_document_concepts
from .contract_structure import build_contract_structure
from .evidence_grounding import (
    GROUNDING_UNGROUNDED,
    build_citation,
    build_grounding,
    downgrade_ungrounded_finding,
)
from .reference_resolver import _resolve_reference_item, resolve_document_references
from .review_document import (
    Paragraph,
    align_document_paragraphs,
    split_document_paragraphs,
)
from .structure_validation import (
    should_validate_structure,
    structure_validation_enabled,
    validate_structure,
)
from .playbook_rules import normalize_playbook_policy
from .redline_rationale import attach_redline_rationales
from .playbook_runtime import playbook_snapshot_hash
from .review_state import (
    CLAUSE_DECISION_FAIL,
    CLAUSE_DECISION_PASS,
    CLAUSE_DECISION_REVIEW,
    aggregate_review_state,
    clause_review_state,
)
from .ai_verifier import apply_ai_verifier, refinalize_clause_grounding
from .review_result_contract import build_review_result, review_result_clause_counts

AI_FIRST_REVIEW_MODE = "ai_first_compat"

_DECISIONS = {CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW, CLAUSE_DECISION_FAIL}
_PLAYBOOK_RESULT_FIELDS = (
    "acceptable_language",
    "allowed_exclusions",
    "approved_laws",
    "check_trigger",
    "engine",
    "fallback",
    "instructions",
    "law_phrases",
    "longer_survival_carve_out_terms",
    "max_term_years",
    "one_way_terms",
    "preferred_law",
    "preferred_position",
    "rationale",
    "redline_template",
    "evidence_guidance",
    "exclusion_context_terms",
    "indefinite_terms",
    "semantic_signals",
    "rules",
    "standard_exclusions_template",
    "taxonomy_groups",
    "term_years",
    "type",
)


class ReassessClauseError(RuntimeError):
    """Raised when a single-clause re-assessment cannot proceed."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def reassess_single_clause(
    clause_id: str,
    source_text: str,
    *,
    paragraphs: Sequence[Paragraph] | None = None,
    edited_paragraphs: Sequence[Mapping[str, Any]] | None = None,
    reviewer: Any | None = None,
    playbook: Mapping[str, Any] | None = None,
    ai_verifier: Any | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """Re-run the AI-first assessment for a single playbook clause.

    ``source_text`` is the full document text (from the stored matter).
    If ``edited_paragraphs`` is supplied those paragraphs are overlaid onto the
    document before assessment so the reviewer sees the proposed edit (the caller
    provides the updated paragraph(s) keyed by their ``id``).

    Returns the updated clause result dict in the standard ClauseResult shape
    (same fields as a clause in ``build_ai_first_review_result``), plus a
    ``reassess_metadata`` key describing the run.

    Raises :class:`ReassessClauseError` on invalid input.
    """
    from .ai_assessor import AIAssessorError, configured_ai_assessment_reviewer
    from .ai_assessment_prompt import build_ai_assessment_packet
    from .ai_assessment_contract import AIAssessmentContractError, validate_ai_clause_assessments

    clause_id = str(clause_id or "").strip()
    if not clause_id:
        raise ReassessClauseError("clause_id is required.")

    text = source_text or ""
    review_playbook = deepcopy(playbook) if isinstance(playbook, Mapping) else load_playbook()
    validate_playbook(review_playbook)
    review_playbook = normalize_playbook_policy(review_playbook)
    playbook_clauses = [
        clause
        for clause in review_playbook.get("clauses", [])
        if isinstance(clause, dict) and str(clause.get("id") or "").strip()
    ]
    playbook_clauses_by_id = {str(clause.get("id") or ""): clause for clause in playbook_clauses}
    target_clause = playbook_clauses_by_id.get(clause_id)
    if target_clause is None:
        raise ReassessClauseError(f"clause_id {clause_id!r} is not a known playbook clause.", status=404)

    # Build document paragraphs, then overlay any edited paragraphs.
    document_paragraphs = _review_paragraphs(text, paragraphs)
    if edited_paragraphs:
        edit_map: dict[str, Mapping[str, Any]] = {}
        for paragraph in edited_paragraphs:
            if isinstance(paragraph, Mapping):
                pid = str(paragraph.get("id") or "").strip()
                if pid:
                    edit_map[pid] = paragraph
        if edit_map:
            document_paragraphs = [
                dict(edit_map[str(p.get("id") or "")]) if str(p.get("id") or "") in edit_map else p
                for p in document_paragraphs
            ]

    # Build a packet scoped to this one clause so the AI reviewer only assesses it.
    from .ai_review import _ai_review_settings
    settings = _ai_review_settings()
    configured_reviewer = reviewer
    if configured_reviewer is None:
        if not settings.get("enabled"):
            raise ReassessClauseError("AI-first assessment is disabled.", status=502)
        try:
            configured_reviewer = configured_ai_assessment_reviewer(settings)
        except AIAssessorError as error:
            raise ReassessClauseError(str(error), status=502) from error

    # Build a packet containing only the one target clause so the model is not
    # asked about unrelated clauses (faster, cheaper, tighter grounding).
    single_clause_playbook = deepcopy(review_playbook)
    single_clause_playbook["clauses"] = [deepcopy(target_clause)]
    # When edited_paragraphs were overlaid the document_paragraphs now contain
    # the edited text, which may not appear verbatim in `text`.  Pass an empty
    # source_text so build_ai_assessment_packet deep-copies the edited paragraphs
    # directly (no re-alignment against the original source text) and the AI
    # reviewer sees the proposed edits.
    packet_source_text = "" if edited_paragraphs else text
    packet = build_ai_assessment_packet(
        packet_source_text,
        playbook=single_clause_playbook,
        paragraphs=document_paragraphs,
        provider=str(settings.get("provider") or ""),
        model=str(settings.get("model") or ""),
    )
    try:
        raw_response = configured_reviewer(packet)
    except Exception as error:
        raise ReassessClauseError(f"AI single-clause assessment failed: {error}", status=502) from error

    if not isinstance(raw_response, Mapping) or not isinstance(raw_response.get("assessments"), list):
        raise ReassessClauseError("AI single-clause assessment returned an invalid response.", status=502)

    raw_assessments_list = raw_response["assessments"]
    try:
        assessment_by_clause_id = validate_ai_clause_assessments(
            raw_assessments_list,
            valid_clause_ids=[clause_id],
            paragraphs=document_paragraphs,
            playbook_clauses_by_id={clause_id: target_clause},
        )
    except AIAssessmentContractError as error:
        raise ReassessClauseError(f"AI assessment response failed validation: {error}", status=502) from error

    assessment = assessment_by_clause_id.get(clause_id)
    # Build context structures for this document.
    contract_structure = build_contract_structure(document_paragraphs)
    reference_resolver = resolve_document_references(document_paragraphs, contract_structure)
    concept_classifier = classify_document_concepts(document_paragraphs, contract_structure)
    review_context = {
        "contract_structure": contract_structure,
        "reference_resolver": reference_resolver,
        "concept_classifier": concept_classifier,
    }
    clause_result = _clause_result_from_assessment(
        target_clause,
        assessment,
        document_paragraphs,
        review_context,
    )
    # Run the verifier over this single result if enabled.
    clause_results_list: list[ClauseResult] = [clause_result]
    clause_results_list, ai_verifier_review = apply_ai_verifier(
        clause_results_list,
        source_text=text,
        verifier=ai_verifier,
        enabled=verify,
    )
    _refinalize_ai_first_verifier_changes(clause_results_list, ai_verifier_review, document_paragraphs)
    # Build redline for just this clause.
    redline_edits = _build_redline_edits(clause_results_list, document_paragraphs)
    attach_redline_rationales(
        clause_results_list,
        redline_edits,
        playbook_clauses_by_id={clause_id: target_clause},
    )
    updated_clause = clause_results_list[0]
    updated_clause["reassess_metadata"] = {
        "clause_id": clause_id,
        "feature": "review",
        "has_edited_paragraphs": bool(edited_paragraphs),
        "ai_verifier_ran": bool(verify),
    }
    return updated_clause


def build_ai_first_review_result(
    source_text: str,
    assessments: Sequence[Mapping[str, Any]],
    *,
    paragraphs: Sequence[Paragraph] | None = None,
    checked_at: str | None = None,
    playbook: Mapping[str, Any] | None = None,
    ai_verifier: Any | None = None,
    structure_validator: Any | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """Build the existing review_result contract from AI-first clause assessments.

    Phase 1 only normalizes already-produced assessments. It intentionally does
    not call an AI model or decide legal outcomes itself.

    After the assessments are normalized into clause results, an adversarial
    verifier pass (additive) justifies-or-refutes each escalated finding; refuted
    findings are downgraded and unsubstantiated ones flagged for human review. This
    is the SHIPPING path, so the verifier protects real product reviews here.
    """
    text = source_text or ""
    document_paragraphs = _review_paragraphs(text, paragraphs)
    playbook = deepcopy(playbook) if isinstance(playbook, Mapping) else load_playbook()
    validate_playbook(playbook)
    # Hash the playbook as published (before policy normalization) so this stamp's
    # hash equals playbook_runtime.active_hash, which is computed over the raw
    # active file. review_engine overwrites this with the runtime-backed stamp
    # (same hash, plus the runtime id); a direct caller still gets a usable one.
    playbook_version = _content_playbook_version(playbook)
    playbook = normalize_playbook_policy(playbook)
    playbook_clauses = [
        clause
        for clause in playbook.get("clauses", [])
        if isinstance(clause, dict) and str(clause.get("id") or "").strip()
    ]
    playbook_clause_ids = [str(clause.get("id") or "") for clause in playbook_clauses]
    playbook_clauses_by_id = {str(clause.get("id") or ""): clause for clause in playbook_clauses}
    assessment_by_clause_id = validate_ai_clause_assessments(
        assessments,
        valid_clause_ids=playbook_clause_ids,
        paragraphs=document_paragraphs,
        playbook_clauses_by_id=playbook_clauses_by_id,
    )
    contract_structure = build_contract_structure(document_paragraphs)
    # Optional, additive AI structure-validation post-pass (shipping path). OFF by
    # default behind NDA_STRUCTURE_VALIDATION_ENABLED so the feature ships dormant;
    # when enabled it is further gated to DOCX-sourced parses with sections and
    # demotes style-misuse false positives from the reference index before the
    # resolver consumes it. Never deletes paragraphs or touches genuine sections,
    # and never blocks on failure. The verdict is cached by document content, so an
    # enabled pass runs at most once per document.
    if (
        verify
        and structure_validation_enabled()
        and should_validate_structure(contract_structure, document_paragraphs)
    ):
        contract_structure = validate_structure(
            contract_structure,
            document_paragraphs,
            validator=structure_validator,
        )
    reference_resolver = resolve_document_references(document_paragraphs, contract_structure)
    concept_classifier = classify_document_concepts(document_paragraphs, contract_structure)
    review_context = {
        "contract_structure": contract_structure,
        "reference_resolver": reference_resolver,
        "concept_classifier": concept_classifier,
    }
    clause_results = [
        _clause_result_from_assessment(
            playbook_clause,
            assessment_by_clause_id.get(str(playbook_clause.get("id") or "")),
            document_paragraphs,
            review_context,
        )
        for playbook_clause in playbook_clauses
    ]
    # Adversarial verifier pass over the AI-first findings (the SHIPPING path).
    # Additive overlay: justify-or-refute each escalated finding, then re-finalize
    # the derived structures (reason codes, structured evidence, audit trace) for any
    # clause it rewrote so the evidence-trust contract still holds.
    clause_results, ai_verifier_review = apply_ai_verifier(
        clause_results,
        source_text=text,
        verifier=ai_verifier,
        enabled=verify,
    )
    _refinalize_ai_first_verifier_changes(clause_results, ai_verifier_review, document_paragraphs)
    counts = review_result_clause_counts(clause_results)
    review_state = aggregate_review_state(
        clause_results,
        pass_count=counts["passed"],
        review_count=counts["needs_review"],
        check_count=counts["failed"],
    )
    redline_edits = _build_redline_edits(clause_results, document_paragraphs)
    # Explain WHY each proposed redline exists, sourced from the Playbook clause
    # (requirement / fallback wording / instructions) and the clause's own
    # grounded citation. Only clauses that produced an edit get a rationale.
    attach_redline_rationales(
        clause_results,
        redline_edits,
        playbook_clauses_by_id={str(clause.get("id") or ""): clause for clause in playbook_clauses},
    )
    return build_review_result(
        source_text=text,
        review_engine_version=REVIEW_ENGINE_VERSION,
        metadata_fields={
            "review_mode": AI_FIRST_REVIEW_MODE,
            "playbook_version": playbook_version,
        },
        review_state=review_state,
        checked_at=checked_at,
        paragraphs=document_paragraphs,
        contract_structure=contract_structure,
        reference_resolver=reference_resolver,
        concept_classifier=concept_classifier,
        semantic_crosscheck={"status": "not_run", "mode": AI_FIRST_REVIEW_MODE},
        ai_review={
            "status": "completed",
            "mode": AI_FIRST_REVIEW_MODE,
            "assessment_contract_version": AI_ASSESSMENT_CONTRACT_VERSION,
            "record_count": len(assessment_by_clause_id),
            "missing_clause_ids": [
                str(clause.get("id") or "")
                for clause in playbook_clauses
                if str(clause.get("id") or "") not in assessment_by_clause_id
            ],
        },
        review_fields={
            "ai_first_review": {
                "status": "normalized",
                "assessment_contract_version": AI_ASSESSMENT_CONTRACT_VERSION,
                "assessment_count": len(assessment_by_clause_id),
                "playbook_clause_count": len(playbook_clauses),
            },
        },
        ai_verifier=ai_verifier_review,
        clauses=clause_results,
        redline_edits=redline_edits,
        evidence_error_prefix="AI-first review evidence validation failed",
    )


def _content_playbook_version(playbook: Mapping[str, Any]) -> dict[str, str]:
    """Compact provenance stamp derived from the playbook content alone.

    Used when ``build_ai_first_review_result`` is called directly (no runtime
    context). ``hash`` matches ``playbook_runtime.active_hash`` for the same
    published Playbook because both use ``playbook_snapshot_hash``. ``id`` is left
    blank here — only the active runtime assigns the published version id, and
    ``review_engine`` overwrites this stamp with the runtime-backed one.
    """
    name = str(playbook.get("name") or "").strip()
    version = str(playbook.get("version") or "").strip()
    if name and version:
        label = f"{name} v{version}"
    else:
        label = name or (f"v{version}" if version else "")
    return {
        "id": "",
        "hash": playbook_snapshot_hash(dict(playbook)),
        "label": label,
    }


def _review_paragraphs(source_text: str, paragraphs: Sequence[Paragraph] | None) -> list[Paragraph]:
    if paragraphs is None:
        return split_document_paragraphs(source_text)
    if source_text:
        return align_document_paragraphs(list(paragraphs), source_text)
    return [deepcopy(paragraph) for paragraph in paragraphs]


def _clause_result_from_assessment(
    playbook_clause: Mapping[str, Any],
    assessment: Mapping[str, Any] | None,
    paragraphs: list[Paragraph],
    review_context: Mapping[str, Any],
) -> ClauseResult:
    if assessment is None:
        assessment = {
            "decision": CLAUSE_DECISION_REVIEW,
            "issue_type": ISSUE_TYPE_UNCLEAR,
            "rationale": "AI assessment did not return a result for this playbook clause.",
            "evidence": [],
            "proposed_redline": {"action": AI_REDLINE_NO_CHANGE},
            "confidence": 0.0,
            "blocks_send": True,
            "reason_code": "ai_first_missing_assessment",
            "schema_version": AI_ASSESSMENT_CONTRACT_VERSION,
            "validation_status": "missing_assessment",
        }
    decision = _normalized_decision(assessment.get("decision"))
    issue_type = _normalized_issue_type(assessment.get("issue_type"), decision)
    reason = _assessment_text(
        assessment,
        "rationale",
        "reason",
        "finding",
        fallback=_default_reason(decision),
    )
    what_to_fix = _assessment_fix_text(assessment, decision)
    reason_codes = _reason_codes(assessment, decision)
    matched_paragraphs = _matched_paragraphs(
        paragraphs, assessment, _reference_index(review_context)
    )
    proposed_redline = assessment.get("proposed_redline")
    if not isinstance(proposed_redline, Mapping):
        proposed_redline = {"action": AI_REDLINE_NO_CHANGE}
    blocks_send = bool(assessment.get("blocks_send"))

    # Ground every finding in the exact source text it cites. A pass/fail the
    # model could not back with a quotable span (and that is not a legitimate
    # absence verdict) is not trustworthy: downgrade it to review so it blocks
    # an automatic send and a human resolves it.
    structured_evidence = _structured_evidence_records(
        {"id": str(playbook_clause["id"]), "decision": decision, "issue_type": issue_type},
        matched_paragraphs,
        assessment,
    )
    clause_type = str(playbook_clause.get("type") or "")
    grounding = build_grounding(
        decision=decision,
        issue_type=issue_type,
        clause_type=clause_type,
        structured_evidence=structured_evidence,
    )
    if grounding["status"] == GROUNDING_UNGROUNDED:
        downgrade = downgrade_ungrounded_finding(
            decision=decision,
            issue_type=issue_type,
            blocks_send=blocks_send,
            reason_codes=reason_codes,
            substantive_reason=reason,
        )
        decision = downgrade["decision"]
        issue_type = downgrade["issue_type"]
        blocks_send = downgrade["blocks_send"]
        reason_codes = downgrade["reason_codes"]
        if downgrade.get("downgraded"):
            reason = downgrade["reason"]
            what_to_fix = _default_fix(decision)
        # The model's original status reflected its (rejected) verdict; recompute.
        status = _normalized_status(None, decision, issue_type, playbook_clause)
    else:
        status = _normalized_status(assessment.get("status"), decision, issue_type, playbook_clause)
    citation = build_citation(structured_evidence)
    result: ClauseResult = {
        "id": str(playbook_clause["id"]),
        "name": str(playbook_clause.get("name") or playbook_clause["id"]),
        "requirement": str(playbook_clause.get("requirement") or ""),
        "status": status,
        "passes": decision == CLAUSE_DECISION_PASS,
        "decision": decision,
        "decision_source": "ai",
        "needs_review": decision == CLAUSE_DECISION_REVIEW,
        "issue_type": issue_type,
        "issue_label": _issue_label(assessment, issue_type, grounding["status"]),
        "what_to_fix": what_to_fix,
        "reason": reason,
        "finding": reason,
        "decision_reason": reason,
        "confidence": assessment.get("confidence"),
        "resolution_question": _assessment_resolution_question(assessment, decision, playbook_clause),
        "suggested_redline": _assessment_suggested_redline(assessment, proposed_redline, decision, what_to_fix),
        "recommended_option": _assessment_recommended_option(assessment, playbook_clause, decision),
        "blocks_send": blocks_send,
        "proposed_redline": deepcopy(proposed_redline),
        "reason_code": reason_codes[0],
        "reason_codes": reason_codes,
        "matched_paragraph_ids": [str(paragraph["id"]) for paragraph in matched_paragraphs],
        "matched_text": "\n\n".join(str(paragraph.get("text") or "") for paragraph in matched_paragraphs),
        "evidence": [
            str(paragraph.get("text") or "")
            for paragraph in matched_paragraphs[:MAX_EVIDENCE_PARAGRAPHS]
        ],
        "evidence_paragraphs": [
            _evidence_paragraph(paragraph)
            for paragraph in matched_paragraphs[:MAX_EVIDENCE_PARAGRAPHS]
        ],
    }
    for field in _PLAYBOOK_RESULT_FIELDS:
        if field in playbook_clause:
            result[field] = deepcopy(playbook_clause[field])
    # Re-derive structured evidence against the final clause (its decision/status
    # may have changed in the grounding downgrade above) so signal types align.
    result["structured_evidence"] = _structured_evidence_records(result, matched_paragraphs, assessment)
    result["grounding"] = grounding
    if citation is not None:
        result["citation"] = citation
    result["review_state"] = clause_review_state(result, decision)
    result["audit_trace"] = _audit_trace(result)
    result["ai_first_assessment"] = {
        "status": str(assessment.get("validation_status") or "normalized"),
        "schema_version": int(assessment.get("schema_version") or AI_ASSESSMENT_CONTRACT_VERSION),
        "confidence": assessment.get("confidence"),
        "resolution_question": result.get("resolution_question"),
        "suggested_redline": result.get("suggested_redline"),
        "recommended_option": result.get("recommended_option"),
        "blocks_send": blocks_send,
        "proposed_redline_action": str(proposed_redline.get("action") or ""),
        "evidence_count": len(result["matched_paragraph_ids"]),
        "grounding_status": grounding["status"],
    }
    if isinstance(review_context.get("contract_structure"), dict):
        result["structure_context"] = _structure_context_for_clause(str(result["id"]), review_context)
    return result


def _refinalize_ai_first_verifier_changes(
    clause_results: list[ClauseResult],
    verifier_review: Mapping[str, Any],
    document_paragraphs: list[Paragraph],
) -> None:
    """Re-derive the derived structures for any clause the verifier rewrote.

    Mirrors the AI-first finalization in ``_clause_result_from_assessment`` (not the
    checker's helpers): the verifier owns the new decision/reason on a clause it
    changed, but reason codes, structured evidence, and the audit trace are derived
    data this module owns. Re-running them keeps the evidence-trust contract intact.
    A refute-to-pass clears the disproven evidence, so structured evidence collapses
    to empty and the clause re-derives its natural "no violation" reason code.
    """
    changed_ids = {
        str(record.get("clause_id") or "")
        for record in verifier_review.get("records", [])
        if isinstance(record, Mapping) and record.get("changed")
    }
    if not changed_ids:
        return
    paragraphs_by_id = {str(paragraph.get("id") or ""): paragraph for paragraph in document_paragraphs}
    for clause in clause_results:
        if str(clause.get("id") or "") not in changed_ids:
            continue
        decision = str(clause.get("decision") or "")
        # The verifier drops the stale reason code when it clears a disproven finding;
        # re-derive the AI-first-owned one only when absent so a verifier-owned
        # escalation code (e.g. ai_verifier_refute on a fail->review) is preserved.
        #
        # Crucially, do NOT call the DETERMINISTIC checker's reason_codes_for_clause
        # here. This is an AI-first verifier pass, and some checkers keyword-scan the
        # clause's free-text reason to derive a code (e.g. term_and_survival flags
        # 'over'/'exceeds'/'indefinite'). The verifier's PASS rationale is reviewer
        # prose, not deterministic-checker output, so that scan can stamp a
        # fail-flavored code (term_survival_over_cap / indefinite_survival) onto a
        # clause the verifier just PASSED -- a ghost. The verifier's decision is
        # authoritative, so the cleared clause carries this module's own code.
        if not clause.get("reason_code") and not clause.get("reason_codes"):
            reason_codes = _ai_first_verifier_reason_codes(decision)
            clause["reason_code"] = reason_codes[0]
            clause["reason_codes"] = reason_codes
        matched_paragraphs = [
            paragraphs_by_id[paragraph_id]
            for paragraph_id in (str(pid) for pid in clause.get("matched_paragraph_ids") or [])
            if paragraph_id in paragraphs_by_id
        ]
        clause["structured_evidence"] = _structured_evidence_records(clause, matched_paragraphs, {})
        # Grounding/citation are owned by the evidence pass (#16); re-derive them from
        # the freshly rebuilt structured_evidence, before review_state/audit_trace.
        refinalize_clause_grounding(clause)
        clause["review_state"] = clause_review_state(clause, decision)
        clause["audit_trace"] = _audit_trace(clause)


def _ai_first_verifier_reason_codes(decision: str) -> list[str]:
    """Reason code owned by the AI-first module for a verifier-changed clause.

    Used when the verifier cleared a clause (refute -> pass) and dropped the stale
    reason code: the cleared finding carries an AI-first, verifier-owned code rather
    than one re-derived from a deterministic checker's keyword scan of the verifier's
    free-text rationale (which can ghost a fail-flavored code onto a passed clause).
    A verifier refute-to-pass means the engine's finding was disproven, so the code
    records exactly that; any other verifier-changed decision is an escalation.
    """
    if str(decision or "").strip().lower() == CLAUSE_DECISION_PASS:
        return ["ai_verifier_refute_cleared"]
    return ["ai_verifier_escalated"]


def _normalized_decision(value: object) -> str:
    decision = str(value or "").strip().lower()
    return decision if decision in _DECISIONS else CLAUSE_DECISION_REVIEW


def _normalized_issue_type(value: object, decision: str) -> str:
    issue_type = str(value or "").strip()
    if issue_type:
        return issue_type
    if decision == CLAUSE_DECISION_PASS:
        return ISSUE_TYPE_NONE
    if decision == CLAUSE_DECISION_FAIL:
        return ISSUE_TYPE_PRESENT_BUT_WRONG
    return ISSUE_TYPE_UNCLEAR


def _issue_label(assessment: Mapping[str, Any], issue_type: str, grounding_status: str) -> str:
    # A downgraded (ungrounded) finding discards the model's own label, which
    # described the verdict we just rejected.
    if grounding_status != GROUNDING_UNGROUNDED:
        model_label = str(assessment.get("issue_label") or "").strip()
        if model_label:
            return model_label
    return ISSUE_TYPE_LABELS.get(issue_type, "Needs review")


def _normalized_status(
    value: object,
    decision: str,
    issue_type: str,
    playbook_clause: Mapping[str, Any],
) -> str:
    status = str(value or "").strip()
    if status:
        return status
    if decision == CLAUSE_DECISION_PASS:
        return "not_present" if str(playbook_clause.get("type") or "") == "prohibited" else "match"
    if issue_type == ISSUE_TYPE_MISSING:
        return "not_present"
    return "check"


def _assessment_text(assessment: Mapping[str, Any], *keys: str, fallback: str) -> str:
    for key in keys:
        value = str(assessment.get(key) or "").strip()
        if value:
            return value
    return fallback


def _assessment_fix_text(assessment: Mapping[str, Any], decision: str) -> str:
    proposed_redline = assessment.get("proposed_redline")
    if isinstance(proposed_redline, Mapping):
        text = str(proposed_redline.get("text") or "").strip()
        if text:
            return text
    return _assessment_text(
        assessment,
        "what_to_fix",
        "proposed_fix",
        "suggested_fix",
        fallback=_default_fix(decision),
    )


def _default_reason(decision: str) -> str:
    if decision == CLAUSE_DECISION_PASS:
        return "AI assessment found this clause satisfies the playbook."
    if decision == CLAUSE_DECISION_FAIL:
        return "AI assessment found this clause does not satisfy the playbook."
    return "AI assessment marked this clause for human review."


def _default_fix(decision: str) -> str:
    if decision == CLAUSE_DECISION_PASS:
        return "No change needed."
    if decision == CLAUSE_DECISION_FAIL:
        return "Review the proposed redline."
    return "Confirm the clause position before sending."


def _assessment_resolution_question(
    assessment: Mapping[str, Any],
    decision: str,
    playbook_clause: Mapping[str, Any],
) -> str:
    question = str(assessment.get("resolution_question") or "").strip()
    if question:
        return question
    if decision != CLAUSE_DECISION_REVIEW:
        return ""
    clause_name = str(playbook_clause.get("name") or playbook_clause.get("id") or "this clause").strip()
    approved = _approved_option_labels(playbook_clause)
    if approved:
        return f"Which approved {clause_name} position should be used here?"
    return f"Should {clause_name} be accepted as drafted, or revised to match the playbook position?"


def _assessment_suggested_redline(
    assessment: Mapping[str, Any],
    proposed_redline: Mapping[str, Any],
    decision: str,
    what_to_fix: str,
) -> str:
    suggested = str(assessment.get("suggested_redline") or "").strip()
    if suggested:
        return suggested
    if decision != CLAUSE_DECISION_REVIEW:
        return ""
    proposed_text = str(proposed_redline.get("text") or "").strip()
    if proposed_text:
        return proposed_text
    fix = str(what_to_fix or "").strip()
    if fix and fix != _default_fix(decision):
        return fix
    return ""


def _assessment_recommended_option(
    assessment: Mapping[str, Any],
    playbook_clause: Mapping[str, Any],
    decision: str,
) -> dict[str, str]:
    raw = assessment.get("recommended_option")
    if isinstance(raw, Mapping):
        option = str(raw.get("option") or "").strip()
        reason = str(raw.get("reason") or "").strip()
        if option and reason:
            return {"option": option, "reason": reason}
    if decision != CLAUSE_DECISION_REVIEW:
        return {}
    approved = _approved_option_labels(playbook_clause)
    if not approved:
        return {}
    return {
        "option": approved[0],
        "reason": "This is the first approved playbook alternative available for reviewer confirmation.",
    }


def _approved_option_labels(playbook_clause: Mapping[str, Any]) -> list[str]:
    values: list[object] = []
    for key in ("approved_positions", "approved_options", "approved_laws", "allowed_exclusions"):
        raw = playbook_clause.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            values.extend(raw)
    fallback = playbook_clause.get("fallback")
    if isinstance(fallback, Mapping):
        raw_positions = fallback.get("approved_positions")
        if isinstance(raw_positions, Sequence) and not isinstance(raw_positions, (str, bytes)):
            values.extend(raw_positions)
    labels: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            label = str(value.get("label") or value.get("name") or value.get("id") or value.get("value") or "").strip()
        else:
            label = str(value or "").strip()
        if label and label not in seen:
            labels.append(label)
            seen.add(label)
    return labels


def _reason_codes(assessment: Mapping[str, Any], decision: str) -> list[str]:
    raw_codes = assessment.get("reason_codes")
    if isinstance(raw_codes, list):
        codes = [str(code).strip() for code in raw_codes if str(code).strip()]
        if codes:
            return codes
    code = str(assessment.get("reason_code") or "").strip()
    if code:
        return [code]
    return [f"ai_first_{decision}"]


def _matched_paragraphs(
    paragraphs: list[Paragraph],
    assessment: Mapping[str, Any],
    reference_index: Mapping[str, Any] | None = None,
) -> list[Paragraph]:
    paragraph_lookup = {str(paragraph.get("id") or ""): paragraph for paragraph in paragraphs}
    matched: list[Paragraph] = []
    seen: set[str] = set()
    evidence_ids = _evidence_paragraph_ids(assessment, paragraphs)
    if not evidence_ids:
        # The structured evidence/matched arrays are empty for this clause, but the
        # AI often names a section only in its prose narrative ("Paragraph 11 defines
        # Confidential Information ...", "see Schedule 3"). Without an anchor the
        # finding is ungrounded and shows no "In the document" / Jump affordance.
        # Resolve each prose reference against the document's REAL printed structure
        # (contract_structure's reference_index) so "Paragraph 11" lands on whatever
        # block the document actually prints as clause 11 -- never the bare 11th
        # physical block, which is only the same by coincidence. This never runs when
        # structured evidence exists, so it cannot override a real match, and a
        # reference that does not resolve to a real section seeds nothing.
        evidence_ids = _prose_anchor_paragraph_ids(assessment, paragraph_lookup, reference_index)
    for paragraph_id in evidence_ids:
        paragraph = paragraph_lookup.get(paragraph_id)
        if not paragraph or paragraph_id in seen:
            continue
        # The document title (Word "Title" style -- e.g. the heading literally
        # reading "Non-Disclosure Agreement") is the document's name, never
        # substantive clause content. The AI sometimes cites it as on-topic
        # "evidence" for confidentiality/mutuality/signatures clauses, which then
        # paints the title green in the review render. Drop it from every clause's
        # matched set so it is never cited or highlighted as clause evidence. Real
        # clause headings ("Confidentiality", "Term") use Heading styles, not Title,
        # so they are unaffected.
        if _is_document_title_paragraph(paragraph):
            continue
        matched.append(paragraph)
        seen.add(paragraph_id)
    return matched


# Prose references to a place in the document, resolved against the document's REAL
# printed structure (contract_structure.reference_index), not the physical block order:
#
#   "Paragraph 11", "para 11", "para. 11", "¶11", "Paragraphs 11"   -> printed clause 11
#   "Clause 5", "Section 2", "Article 3", "Schedule 3"              -> the printed section
#   "Annex A", "Annexure 2", "Appendix 4", "Schedule 3(a)"          -> the printed attachment
#
# Resolution is delegated to reference_resolver._resolve_reference_item -- the SAME
# routine the document-side reference resolver uses -- so the alias scheme, the kind-
# agnostic ``number:N`` body fallback, the Schedule<->Section namespace guard and the
# duplicate-number ambiguity handling all stay in one place and cannot diverge here.
# Each resolved reference seeds the printed section's FIRST paragraph id, so "Paragraph
# 11" lands on whatever block the document prints as clause 11 -- almost never the 11th
# physical block. A reference that does not resolve to a real section seeds NOTHING
# (accuracy-or-nothing; no block-index guess).
#
# A bare ``pN`` token is a DIRECT document paragraph id (review_document ids are
# ``p{index}``) and is validated against the real ids rather than routed through the
# printed-number index.
#
# Number forms accepted: digits ("11"), a single letter ("a"/"A"), roman ("iv"),
# optionally followed by a parenthetical sub-part ("3(a)").
#
# Keyword -> the kind passed to the shared resolver. "Paragraph"/"para"/"¶" is a numbered
# cross-reference with no kind of its own, so it is resolved as a body reference
# (``section``) and finds its target purely through the ``number:N`` fallback.
#
# "Exhibit" is an ATTACHMENT word (like Schedule/Annex/Appendix), but reference_resolver
# (read-only) knows no ``exhibit`` kind/namespace and the parser never emits an
# ``exhibit:N`` alias. To make "Exhibit N" obey the SAME attachment namespace guard as
# Schedule/Annex on BOTH sides, it is mapped here to an attachment kind (``appendix``):
# _resolve_reference_item then refuses to append the kind-agnostic ``number:N`` body
# fallback, so "Exhibit N" never borrows a body "Section N"/numbered heading. There is no
# ``appendix:N`` alias for a real exhibit either, so the net is: "Exhibit N" declines to
# bridge onto a Section-N -- the EXACT outcome the FE reaches (structureReferenceKind maps
# exhibit to an attachment kind for the same guard). FE and BE thus agree on "Exhibit N".
_PROSE_REFERENCE_KINDS = {
    "paragraph": "section",
    "paragraphs": "section",
    "para": "section",
    "para.": "section",
    "¶": "section",
    "clause": "clause",
    "clauses": "clause",
    "section": "section",
    "sections": "section",
    "article": "article",
    "articles": "article",
    "schedule": "schedule",
    "schedules": "schedule",
    "annex": "annex",
    "annexes": "annex",
    "annexure": "annexure",
    "annexures": "annexure",
    "appendix": "appendix",
    "appendices": "appendix",
    "exhibit": "appendix",
    "exhibits": "appendix",
}
_PROSE_REFERENCE_NUMBER_PART = r"(?:\d+|[ivxlcdm]+|[a-z])(?:\([a-z0-9]+\))?"
_PROSE_REFERENCE_RE = re.compile(
    r"(?P<keyword>paragraphs?|para\.?|¶|clauses?|sections?|articles?|schedules?|"
    r"annexures?|annexes?|annex|appendices|appendix|exhibits?)\s*"
    rf"(?P<number>{_PROSE_REFERENCE_NUMBER_PART})\b",
    re.IGNORECASE,
)
_PROSE_TOKEN_RE = re.compile(r"\bp(\d+)\b", re.IGNORECASE)


def _prose_anchor_paragraph_ids(
    assessment: Mapping[str, Any],
    paragraph_lookup: Mapping[str, Paragraph],
    reference_index: Mapping[str, Any] | None = None,
) -> list[str]:
    prose_parts = [
        str(assessment.get(key) or "")
        for key in ("rationale", "reason", "finding", "decision_reason")
    ]
    prose = " ".join(part for part in prose_parts if part)
    if not prose:
        return []

    ids: list[str] = []

    # 1. Structural/paragraph references resolved through the printed-structure index,
    #    via the shared document-reference resolver (single source of truth for alias
    #    resolution + namespace/ambiguity guards).
    alias_lookup, sections_by_id, ambiguous_alias_keys = _reference_index_maps(reference_index)
    for match in _PROSE_REFERENCE_RE.finditer(prose):
        kind = _PROSE_REFERENCE_KINDS.get(match.group("keyword").strip().lower())
        if kind is None:
            continue
        item = _resolve_reference_item(
            kind,
            match.group("number").strip().lower(),
            alias_lookup,
            ambiguous_alias_keys,
            sections_by_id,
        )
        section_id = item.get("section_id")
        if not isinstance(section_id, str) or not section_id:
            continue
        section_record = sections_by_id.get(section_id)
        # SOURCE-BACKED GUARD. The structure parser invents FALSE sections on flat /
        # PDF documents -- a street address ("145 Curtain Road") or a cover-table cell
        # ("2 year") gets scraped as a clause number, producing a ``number:145`` /
        # ``number:2`` section with NO source metadata (source=null/absent). Grounding a
        # finding to one of those would anchor the evidence to an ADDRESS. A section is
        # only trustworthy as a STRUCTURAL prose-reference target if it came from real
        # Word numbering/heading metadata, which contract_structure records by attaching
        # a non-empty ``source`` mapping (see _resolver_section_record / _source_metadata
        # -- a scraped-from-text section carries no ``source`` key at all). If the
        # resolved section is not source-backed, seed NOTHING for this reference
        # (accuracy-or-nothing). This guards ONLY the structural Paragraph/Clause/Section
        # /Article/Schedule/Exhibit/Annex/Appendix N path; the bare ``pN`` direct-id path
        # below never goes through section resolution and is unaffected.
        if not _section_is_source_backed(section_record):
            continue
        start_paragraph_id = _section_start_paragraph_id(section_record)
        if start_paragraph_id and start_paragraph_id in paragraph_lookup:
            ids.append(start_paragraph_id)

    # 2. Bare ``pN`` tokens are DIRECT document paragraph ids, validated against the
    #    real ids (not routed through the printed-number index).
    for number in _PROSE_TOKEN_RE.findall(prose):
        candidate = f"p{int(number)}"
        if candidate in paragraph_lookup:
            ids.append(candidate)

    return _dedupe_ids(ids)


def _reference_index(review_context: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(review_context, Mapping):
        return None
    contract_structure = review_context.get("contract_structure")
    if not isinstance(contract_structure, Mapping):
        return None
    reference_index = contract_structure.get("reference_index")
    return reference_index if isinstance(reference_index, Mapping) else None


def _reference_index_maps(
    reference_index: Mapping[str, Any] | None,
) -> tuple[dict[str, str], dict[str, dict[str, Any]], set[str]]:
    if not isinstance(reference_index, Mapping):
        return {}, {}, set()
    alias_lookup = {
        str(key): str(value)
        for key, value in (reference_index.get("alias_to_section_id") or {}).items()
        if isinstance(key, str) and isinstance(value, str)
    }
    sections_by_id = {
        str(key): dict(value)
        for key, value in (reference_index.get("sections_by_id") or {}).items()
        if isinstance(key, str) and isinstance(value, Mapping)
    }
    ambiguous = reference_index.get("ambiguous_alias_keys")
    ambiguous_keys = (
        {str(key) for key in ambiguous} if isinstance(ambiguous, (list, tuple, set)) else set()
    )
    return alias_lookup, sections_by_id, ambiguous_keys


def _section_is_source_backed(section_record: Any) -> bool:
    # A section is source-backed when contract_structure attached a non-empty ``source``
    # mapping to it (real Word numbering/heading/style metadata). _resolver_section_record
    # only copies ``source`` onto the reduced record when the original section carries a
    # ``source`` dict, so a section scraped from plain text (e.g. an address digit read as
    # a clause number) exposes no ``source`` key here. Anything else -- a missing key, a
    # null, or an empty mapping -- is treated as NOT source-backed.
    if not isinstance(section_record, Mapping):
        return False
    source = section_record.get("source")
    return isinstance(source, Mapping) and bool(source)


def _section_start_paragraph_id(section_record: Any) -> str | None:
    # The resolver section record (reference_index.sections_by_id) carries the ordered
    # paragraph_ids but not a separate start_paragraph_id, so the start is the first
    # paragraph id.
    if not isinstance(section_record, Mapping):
        return None
    paragraph_ids = section_record.get("paragraph_ids")
    if isinstance(paragraph_ids, Sequence) and not isinstance(paragraph_ids, (str, bytes)):
        for paragraph_id in paragraph_ids:
            if isinstance(paragraph_id, str) and paragraph_id:
                return paragraph_id
    return None


def _is_document_title_paragraph(paragraph: Mapping[str, Any]) -> bool:
    for key in ("style_id", "style_name"):
        if str(paragraph.get(key) or "").strip().casefold() == "title":
            return True
    return False


def _evidence_paragraph_ids(assessment: Mapping[str, Any], paragraphs: list[Paragraph]) -> list[str]:
    raw_ids = assessment.get("matched_paragraph_ids")
    ids: list[str] = [str(paragraph_id) for paragraph_id in raw_ids] if isinstance(raw_ids, list) else []
    evidence = assessment.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            paragraph_id = str(item.get("paragraph_id") or "").strip()
            if paragraph_id:
                ids.append(paragraph_id)
                continue
            quote = str(item.get("quote") or item.get("text") or "").strip()
            if quote:
                matched_id = _paragraph_id_for_quote(paragraphs, quote, preferred_ids=ids)
                if matched_id:
                    ids.append(matched_id)
    return _dedupe_ids(ids)


def _paragraph_id_for_quote(
    paragraphs: list[Paragraph],
    quote: str,
    *,
    preferred_ids: Sequence[str] = (),
) -> str:
    # Match with the SAME normalization the contract grounds with (glyph-fold +
    # whitespace-collapse + lowercase), so a quote the contract accepted -- curly
    # quotes, double spaces -- still resolves to its paragraph here instead of being
    # silently dropped. If the same boilerplate quote appears more than once, prefer
    # the paragraph id the model/result already cited instead of losing the anchor.
    normalized_quote = _normalize_quote_text(quote)
    if not normalized_quote:
        return ""
    matching_ids: list[str] = []
    for paragraph in paragraphs:
        text = str(paragraph.get("text") or "")
        if normalized_quote in _normalize_quote_text(text):
            paragraph_id = str(paragraph.get("id") or "")
            if paragraph_id:
                matching_ids.append(paragraph_id)
    preferred = [str(paragraph_id).strip() for paragraph_id in preferred_ids if str(paragraph_id).strip()]
    for paragraph_id in preferred:
        if paragraph_id in matching_ids:
            return paragraph_id
    return matching_ids[0] if len(matching_ids) == 1 else ""


def _dedupe_ids(ids: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in ids:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        deduped.append(cleaned)
        seen.add(cleaned)
    return deduped


def _evidence_paragraph(paragraph: Paragraph) -> Paragraph:
    return deepcopy(paragraph)


def _structured_evidence_records(
    clause: ClauseResult,
    matched_paragraphs: list[Paragraph],
    assessment: Mapping[str, Any],
) -> list[dict[str, Any]]:
    quotes_by_paragraph_id = _quotes_by_paragraph_id(assessment, matched_paragraphs)
    records: list[dict[str, Any]] = []
    for index, paragraph in enumerate(matched_paragraphs, start=1):
        paragraph_id = str(paragraph.get("id") or "")
        quote = quotes_by_paragraph_id.get(paragraph_id, "")
        match_spans = _quote_spans(paragraph, quote)
        records.append({
            "id": f"{clause['id']}:{paragraph_id or index}:ai_first",
            "clause_id": clause["id"],
            "paragraph_id": paragraph_id,
            "paragraph_index": paragraph.get("index"),
            "source_index": paragraph.get("source_index"),
            "source_part": paragraph.get("source_part"),
            "source_kind": paragraph.get("source_kind"),
            "start": paragraph.get("start"),
            "end": paragraph.get("end"),
            "text": str(paragraph.get("text") or ""),
            "matched_text": quote or str(paragraph.get("text") or ""),
            "matched_terms": [quote] if quote else [],
            "match_spans": match_spans,
            "signal_type": _signal_type(clause),
            "rule_bucket": clause.get("issue_type"),
            "counted": True,
            "decision": clause.get("decision"),
            "result_status": clause.get("status"),
            "issue_type": clause.get("issue_type"),
            "issue_label": clause.get("issue_label"),
            "decision_reason": clause.get("decision_reason"),
            "reason_code": clause.get("reason_code"),
            "reason_codes": clause.get("reason_codes"),
            "reason": clause.get("reason"),
        })
    return records


def _quotes_by_paragraph_id(
    assessment: Mapping[str, Any],
    matched_paragraphs: list[Paragraph],
) -> dict[str, str]:
    evidence = assessment.get("evidence")
    if not isinstance(evidence, list):
        return {}
    paragraph_ids = {str(paragraph.get("id") or "") for paragraph in matched_paragraphs}
    quotes: dict[str, str] = {}
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        quote = str(item.get("quote") or item.get("text") or "").strip()
        if not quote:
            continue
        paragraph_id = str(item.get("paragraph_id") or "").strip()
        if not paragraph_id:
            paragraph_id = _paragraph_id_for_quote(matched_paragraphs, quote, preferred_ids=paragraph_ids)
        if paragraph_id in paragraph_ids:
            quotes[paragraph_id] = quote
    return quotes


# Glyph-flexible character classes: a straight quote/hyphen in the model's quote
# must still locate the curly/dashed variant in the source DOCX, mirroring the
# contract's glyph folding so a quote the contract accepted keeps its highlight
# offsets here instead of silently losing them.
_CHAR_FLEX: dict[str, str] = {
    "'": "['‘’‚‛′]",
    '"': "[\"“”„‟″]",
    "-": "[-‐‑‒–—―−]",
}


def _flexible_quote_regex(quote: str) -> "re.Pattern[str] | None":
    """A whitespace- and glyph-tolerant regex for locating ``quote`` in source text.

    Tokens match literally (glyph-folded), whitespace runs match ``\\s+``, and the
    quote/dash glyph variants are accepted, so a quote the contract grounded against
    a curly-quoted, double-spaced paragraph still yields an offset span.
    """
    folded = _fold_typographic_glyphs(quote).strip()
    if not folded:
        return None
    parts = [
        "".join(_CHAR_FLEX.get(char, re.escape(char)) for char in token)
        for token in folded.split()
    ]
    if not parts:
        return None
    return re.compile(r"\s+".join(parts), re.IGNORECASE)


def _quote_spans(paragraph: Paragraph, quote: str) -> list[dict[str, Any]]:
    if not quote:
        return []
    text = str(paragraph.get("text") or "")
    paragraph_start = paragraph.get("start")
    if not isinstance(paragraph_start, int):
        return []
    # Fast path: exact case-insensitive substring (clean ASCII quotes).
    offset = text.casefold().find(quote.casefold())
    length = len(quote)
    if offset < 0:
        # Whitespace/glyph-tolerant fallback so curly quotes and collapsed spaces
        # (which the contract folds before grounding) still yield a highlight span.
        pattern = _flexible_quote_regex(quote)
        match = pattern.search(text) if pattern is not None else None
        if match is None:
            return []
        offset = match.start()
        length = match.end() - match.start()
    start = paragraph_start + offset
    end = start + length
    return [{"start": start, "end": end, "text": text[offset:offset + length], "term": quote}]


def _signal_type(clause: ClauseResult) -> str:
    decision = str(clause.get("decision") or "")
    if decision == CLAUSE_DECISION_REVIEW:
        return "review_evidence"
    if decision == CLAUSE_DECISION_FAIL:
        return "check_evidence"
    return "pass_evidence"


def _audit_trace(clause: ClauseResult) -> dict[str, Any]:
    paragraph_ids = list(clause.get("matched_paragraph_ids") or [])
    structured_evidence = list(clause.get("structured_evidence") or [])
    return {
        "version": 1,
        "clause_id": str(clause.get("id") or ""),
        "decision": str(clause.get("decision") or ""),
        "status": str(clause.get("status") or ""),
        "issue_type": str(clause.get("issue_type") or ""),
        "decision_reason": str(clause.get("decision_reason") or ""),
        "reason_code": str(clause.get("reason_code") or ""),
        "reason_codes": list(clause.get("reason_codes") or []),
        "evidence_summary": {
            "paragraph_ids": [str(paragraph_id) for paragraph_id in paragraph_ids],
            "structured_evidence_count": len(structured_evidence),
            "matched_paragraph_count": len(paragraph_ids),
            "analysis_signal_count": 0,
            "ignored_signal_count": 0,
            "review_signal_count": 1 if clause.get("decision") == CLAUSE_DECISION_REVIEW else 0,
            "matched_terms": [],
            "signal_counts": {_signal_type(clause): len(structured_evidence)} if structured_evidence else {},
        },
        "analysis_outputs": [],
        "analysis_signals": [],
        "steps": [
            {
                "name": "AI assessment normalization",
                "outcome": "normalized",
                "details": "AI-first assessment was normalized into the review result contract.",
            },
            {
                "name": "Decision",
                "outcome": str(clause.get("decision") or ""),
                "details": str(clause.get("decision_reason") or ""),
                "reason_code": str(clause.get("reason_code") or ""),
                "reason_codes": list(clause.get("reason_codes") or []),
            },
        ],
    }


def _structure_context_for_clause(clause_id: str, review_context: Mapping[str, Any]) -> dict[str, Any]:
    concept_classifier = review_context.get("concept_classifier")
    concept_records = concept_classifier.get("concepts_by_clause_id", {}) if isinstance(concept_classifier, dict) else {}
    contract_structure = review_context.get("contract_structure")
    sections = contract_structure.get("sections", []) if isinstance(contract_structure, dict) else []
    return {
        "clause_id": clause_id,
        "concepts": list(concept_records.get(clause_id, [])) if isinstance(concept_records, dict) else [],
        "sections": list(sections) if isinstance(sections, list) else [],
        "reference_count": 0,
    }
