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
from .reference_resolver import resolve_document_references
from .review_document import (
    Paragraph,
    align_document_paragraphs,
    split_document_paragraphs,
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
    reason_codes_for_clause,
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


def build_ai_first_review_result(
    source_text: str,
    assessments: Sequence[Mapping[str, Any]],
    *,
    paragraphs: Sequence[Paragraph] | None = None,
    checked_at: str | None = None,
    playbook: Mapping[str, Any] | None = None,
    ai_verifier: Any | None = None,
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
    matched_paragraphs = _matched_paragraphs(paragraphs, assessment)
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
        # re-derive the natural one only when absent so a verifier-owned escalation
        # code (e.g. ai_verifier_refute on a fail->review) is preserved.
        if not clause.get("reason_code") and not clause.get("reason_codes"):
            reason_codes = reason_codes_for_clause(clause, decision)
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


def _matched_paragraphs(paragraphs: list[Paragraph], assessment: Mapping[str, Any]) -> list[Paragraph]:
    paragraph_lookup = {str(paragraph.get("id") or ""): paragraph for paragraph in paragraphs}
    matched: list[Paragraph] = []
    seen: set[str] = set()
    for paragraph_id in _evidence_paragraph_ids(assessment, paragraphs):
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
                matched_id = _paragraph_id_for_quote(paragraphs, quote)
                if matched_id:
                    ids.append(matched_id)
    return _dedupe_ids(ids)


def _paragraph_id_for_quote(paragraphs: list[Paragraph], quote: str) -> str:
    # Match with the SAME normalization the contract grounds with (glyph-fold +
    # whitespace-collapse + lowercase), so a quote the contract accepted -- curly
    # quotes, double spaces -- still resolves to its paragraph here instead of being
    # silently dropped.
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
            paragraph_id = _paragraph_id_for_quote(matched_paragraphs, quote)
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
