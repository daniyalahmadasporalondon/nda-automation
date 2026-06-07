from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .checks.common import (
    ISSUE_TYPE_MISSING,
    ISSUE_TYPE_NONE,
    ISSUE_TYPE_PRESENT_BUT_WRONG,
    ISSUE_TYPE_UNCLEAR,
)
from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from .redline_defaults import playbook_redline_text
from .review_document import Paragraph
from .review_state import CLAUSE_DECISION_FAIL, CLAUSE_DECISION_PASS, CLAUSE_DECISION_REVIEW

AI_ASSESSMENT_CONTRACT_VERSION = 1
AI_REDLINE_NO_CHANGE = "no_change"
AI_ASSESSMENT_DECISIONS = (CLAUSE_DECISION_PASS, CLAUSE_DECISION_FAIL, CLAUSE_DECISION_REVIEW)
AI_ASSESSMENT_ISSUE_TYPES = (
    ISSUE_TYPE_NONE,
    ISSUE_TYPE_MISSING,
    ISSUE_TYPE_PRESENT_BUT_WRONG,
    ISSUE_TYPE_UNCLEAR,
)
AI_ASSESSMENT_REDLINE_ACTIONS = (
    AI_REDLINE_NO_CHANGE,
    REDLINE_REPLACE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_DELETE_PARAGRAPH,
)

AI_CLAUSE_ASSESSMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": AI_ASSESSMENT_CONTRACT_VERSION},
        "clause_id": {"type": "string"},
        "decision": {"type": "string", "enum": list(AI_ASSESSMENT_DECISIONS)},
        "issue_type": {"type": "string", "enum": list(AI_ASSESSMENT_ISSUE_TYPES)},
        "rationale": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paragraph_id": {"type": "string"},
                    "quote": {"type": "string"},
                    "relevance": {"type": "string"},
                },
                "required": ["quote", "relevance"],
                "additionalProperties": False,
            },
        },
        "proposed_redline": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(AI_ASSESSMENT_REDLINE_ACTIONS)},
                "paragraph_id": {"type": "string"},
                "text": {"type": "string"},
                "jurisdiction": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "blocks_send": {"type": "boolean"},
    },
    "required": [
        "clause_id",
        "decision",
        "issue_type",
        "rationale",
        "evidence",
        "proposed_redline",
        "confidence",
        "blocks_send",
    ],
    "additionalProperties": False,
}


class AIAssessmentContractError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = [str(error) for error in errors if str(error).strip()]
        super().__init__("AI assessment contract validation failed: " + "; ".join(self.errors))


def validate_ai_clause_assessments(
    assessments: Sequence[Mapping[str, Any]],
    *,
    valid_clause_ids: Sequence[str],
    paragraphs: Sequence[Paragraph],
    playbook_clauses_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    errors: list[str] = []
    cleaned_by_clause_id: dict[str, dict[str, Any]] = {}
    valid_clause_id_set = {str(clause_id).strip() for clause_id in valid_clause_ids if str(clause_id).strip()}
    clauses_by_id = _playbook_clauses_by_id(playbook_clauses_by_id)
    if not isinstance(assessments, Sequence) or isinstance(assessments, (str, bytes)):
        raise AIAssessmentContractError(["assessments must be a list"])

    for index, assessment in enumerate(assessments):
        location = f"assessment[{index}]"
        if not isinstance(assessment, Mapping):
            errors.append(f"{location}: assessment must be an object")
            continue
        cleaned, assessment_errors = validate_ai_clause_assessment(
            assessment,
            valid_clause_ids=valid_clause_id_set,
            paragraphs=paragraphs,
            location=location,
            playbook_clauses_by_id=clauses_by_id,
        )
        errors.extend(assessment_errors)
        clause_id = str(cleaned.get("clause_id") or "").strip()
        if not clause_id or assessment_errors:
            continue
        if clause_id in cleaned_by_clause_id:
            errors.append(f"{location}: duplicate assessment for clause {clause_id}")
            continue
        cleaned_by_clause_id[clause_id] = cleaned

    if errors:
        raise AIAssessmentContractError(errors)
    return cleaned_by_clause_id


def validate_ai_clause_assessment(
    assessment: Mapping[str, Any],
    *,
    valid_clause_ids: Sequence[str] | set[str],
    paragraphs: Sequence[Paragraph],
    location: str = "assessment",
    playbook_clauses_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    clauses_by_id = _playbook_clauses_by_id(playbook_clauses_by_id)
    allowed_keys = set(AI_CLAUSE_ASSESSMENT_SCHEMA["properties"])
    for key in assessment:
        if str(key) not in allowed_keys:
            errors.append(f"{location}: unsupported field {key}")
    if "schema_version" in assessment:
        try:
            schema_version = int(assessment.get("schema_version"))
        except (TypeError, ValueError):
            schema_version = -1
        if schema_version != AI_ASSESSMENT_CONTRACT_VERSION:
            errors.append(f"{location}: schema_version must be {AI_ASSESSMENT_CONTRACT_VERSION}")

    # Preserve packet order: the reviewed paragraphs arrive as an ordered list,
    # and document-wide grounding needs a STABLE, deterministic concatenation. An
    # ordered list of (id, text) is the source of truth; paragraph_by_id is the
    # by-id lookup derived from it.
    ordered_paragraphs = [
        (str(paragraph.get("id") or ""), str(paragraph.get("text") or ""))
        for paragraph in paragraphs
        if str(paragraph.get("id") or "")
    ]
    paragraph_by_id = dict(ordered_paragraphs)
    valid_clause_id_set = {str(clause_id).strip() for clause_id in valid_clause_ids if str(clause_id).strip()}

    clause_id = _required_text(assessment, "clause_id", location, errors)
    if clause_id and clause_id not in valid_clause_id_set:
        errors.append(f"{location}: unknown clause_id {clause_id}")

    decision = _required_enum(assessment, "decision", AI_ASSESSMENT_DECISIONS, location, errors)
    issue_type = _required_enum(assessment, "issue_type", AI_ASSESSMENT_ISSUE_TYPES, location, errors)
    rationale = _required_text(assessment, "rationale", location, errors)
    confidence = _required_confidence(assessment, location, errors)
    blocks_send = _required_bool(assessment, "blocks_send", location, errors)
    evidence = _validated_evidence(assessment, paragraph_by_id, ordered_paragraphs, location, errors)
    proposed_redline = _validated_proposed_redline(
        assessment,
        paragraph_by_id,
        location,
        errors,
        playbook_clause=clauses_by_id.get(clause_id),
    )

    # One clause whose redline could neither be authored by the AI nor defaulted
    # from the Playbook must not discard every other (correct) assessment. The
    # validator already collapsed it to a no-text no_change flag; keep the verdict
    # actionable without violating the decision<->action coupling below: a blank
    # fail becomes a human-review flag (never a silent pass), a blank review stays
    # a review. The decision/rationale survive so a human still sees the finding.
    if proposed_redline.pop("_degraded_no_text", False):
        rationale = _with_degrade_note(rationale)
        if decision == CLAUSE_DECISION_FAIL:
            decision = CLAUSE_DECISION_REVIEW
            blocks_send = True

    if decision == CLAUSE_DECISION_PASS and issue_type != ISSUE_TYPE_NONE:
        errors.append(f"{location}: pass decisions must use issue_type none")
    if decision in {CLAUSE_DECISION_FAIL, CLAUSE_DECISION_REVIEW} and issue_type == ISSUE_TYPE_NONE:
        errors.append(f"{location}: fail/review decisions must not use issue_type none")
    if decision == CLAUSE_DECISION_PASS and proposed_redline.get("action") != AI_REDLINE_NO_CHANGE:
        errors.append(f"{location}: pass decisions must use proposed_redline.action no_change")
    if decision == CLAUSE_DECISION_FAIL and proposed_redline.get("action") == AI_REDLINE_NO_CHANGE:
        errors.append(f"{location}: fail decisions require a proposed redline action")
    if decision == CLAUSE_DECISION_REVIEW and proposed_redline.get("action") not in {AI_REDLINE_NO_CHANGE, REDLINE_REPLACE_PARAGRAPH, REDLINE_INSERT_AFTER_PARAGRAPH, REDLINE_DELETE_PARAGRAPH}:
        errors.append(f"{location}: review decisions have an unsupported proposed redline action")
    if decision == CLAUSE_DECISION_FAIL and issue_type != ISSUE_TYPE_MISSING and not evidence:
        errors.append(f"{location}: fail decisions require at least one valid evidence item unless issue_type is missing")
    if blocks_send is not None and blocks_send != (decision == CLAUSE_DECISION_REVIEW):
        errors.append(f"{location}: blocks_send must be true only for review decisions")

    return {
        "schema_version": AI_ASSESSMENT_CONTRACT_VERSION,
        "clause_id": clause_id,
        "decision": decision,
        "issue_type": issue_type,
        "rationale": rationale,
        "evidence": evidence,
        "proposed_redline": proposed_redline,
        "confidence": confidence,
        "blocks_send": bool(blocks_send),
        "validation_status": "contract_valid",
    }, errors


_DEGRADE_NOTE = (
    "No standard replacement wording was available for this clause, so it is "
    "flagged for human review instead of an automatic redline."
)


def _playbook_clauses_by_id(
    playbook_clauses_by_id: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(playbook_clauses_by_id, Mapping):
        return {}
    return {
        str(clause_id).strip(): clause
        for clause_id, clause in playbook_clauses_by_id.items()
        if str(clause_id).strip() and isinstance(clause, Mapping)
    }


def _with_degrade_note(rationale: str) -> str:
    rationale = str(rationale or "").strip()
    if not rationale:
        return _DEGRADE_NOTE
    if _DEGRADE_NOTE in rationale:
        return rationale
    return f"{rationale} {_DEGRADE_NOTE}"


def _required_text(assessment: Mapping[str, Any], key: str, location: str, errors: list[str]) -> str:
    if key not in assessment:
        errors.append(f"{location}: {key} is required")
        return ""
    value = str(assessment.get(key) or "").strip()
    if not value:
        errors.append(f"{location}: {key} must be non-empty text")
    return value


def _required_enum(
    assessment: Mapping[str, Any],
    key: str,
    allowed: Sequence[str],
    location: str,
    errors: list[str],
) -> str:
    if key not in assessment:
        errors.append(f"{location}: {key} is required")
        return ""
    value = str(assessment.get(key) or "").strip().lower()
    if value not in allowed:
        errors.append(f"{location}: {key} must be one of {', '.join(allowed)}")
    return value


def _required_confidence(assessment: Mapping[str, Any], location: str, errors: list[str]) -> float:
    if "confidence" not in assessment:
        errors.append(f"{location}: confidence is required")
        return -1.0
    try:
        confidence = float(assessment.get("confidence"))
    except (TypeError, ValueError):
        errors.append(f"{location}: confidence must be a number between 0 and 1")
        return -1.0
    if confidence < 0 or confidence > 1:
        errors.append(f"{location}: confidence must be a number between 0 and 1")
    return confidence


def _required_bool(assessment: Mapping[str, Any], key: str, location: str, errors: list[str]) -> bool | None:
    if key not in assessment:
        errors.append(f"{location}: {key} is required")
        return None
    value = assessment.get(key)
    if not isinstance(value, bool):
        errors.append(f"{location}: {key} must be a boolean")
        return None
    return value


def _validated_evidence(
    assessment: Mapping[str, Any],
    paragraph_by_id: Mapping[str, str],
    ordered_paragraphs: Sequence[tuple[str, str]],
    location: str,
    errors: list[str],
) -> list[dict[str, str]]:
    raw_evidence = assessment.get("evidence")
    if not isinstance(raw_evidence, list):
        errors.append(f"{location}: evidence must be a list")
        return []

    cleaned: list[dict[str, str]] = []
    allowed_keys = {"paragraph_id", "quote", "relevance"}
    for index, item in enumerate(raw_evidence[:12]):
        item_location = f"{location}.evidence[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{item_location}: evidence item must be an object")
            continue
        for key in item:
            if str(key) not in allowed_keys:
                errors.append(f"{item_location}: unsupported field {key}")
        quote = str(item.get("quote") or "").strip()
        relevance = str(item.get("relevance") or "").strip()
        paragraph_id = str(item.get("paragraph_id") or "").strip()
        if not quote:
            errors.append(f"{item_location}: quote is required")
            continue
        if not relevance:
            errors.append(f"{item_location}: relevance is required")

        # The cited paragraph still must exist if the model named one: a
        # paragraph_id pointing nowhere is a structural error (the model invented
        # an id), distinct from a quote that simply spans paragraph boundaries.
        if paragraph_id and paragraph_id not in paragraph_by_id:
            errors.append(f"{item_location}: paragraph_id does not exist: {paragraph_id}")
            continue

        # Grounding precedence (a -> b/c -> d -> e):
        #   a. grounds in the cited paragraph -> keep paragraph_id.
        #   b/c. else grounds DOCUMENT-WIDE (whole-quote or ellipsis-split span)
        #        -> re-anchor paragraph_id to the reviewed paragraph that holds
        #           the first segment.
        #   e. grounds nowhere -> a genuine fabrication: DROP this single item
        #      (do not raise, do not crash the document) and let GATE 2 downgrade
        #      the now-less-supported finding.
        resolved_id, error = _ground_evidence_quote(
            quote, paragraph_id, paragraph_by_id, ordered_paragraphs, item_location
        )
        if error is not None:
            # Structural ambiguity, not a span: a short quote-only phrase that
            # recurs whole in several paragraphs is genuinely ambiguous; keep the
            # long-standing "provide paragraph_id" guard rather than guessing.
            errors.append(error)
            continue
        if resolved_id is None:
            # Fabricated quote: omit it like a skipped item. A non-pass finding
            # that ends with zero grounded evidence is forced to human review by
            # the evidence-grounding resilience layer (GATE 2), never a silent
            # pass; this keeps the whole-document review alive instead of raising.
            continue
        cleaned.append({"paragraph_id": resolved_id, "quote": quote, "relevance": relevance})
    return cleaned


def _ground_evidence_quote(
    quote: str,
    cited_paragraph_id: str,
    paragraph_by_id: Mapping[str, str],
    ordered_paragraphs: Sequence[tuple[str, str]],
    item_location: str,
) -> tuple[str | None, str | None]:
    """Resolve a quote to a grounding paragraph_id.

    Returns ``(paragraph_id, None)`` when the quote grounds, ``(None, None)`` when
    it grounds nowhere (a fabrication to be dropped), or ``(None, error)`` when it
    is a structurally ambiguous quote-only phrase the model must disambiguate.

    (a) When the model cites a paragraph and the quote is a substring of it, keep
    that id. (b/c) Otherwise check the whole document (paragraph texts joined in
    packet order) for the quote as a contiguous span or as ellipsis-elided
    segments that appear in order. (d) On a document-wide match, re-anchor to the
    first reviewed paragraph (packet order) whose text contains the quote's first
    segment, so evidence still points at a real containing paragraph for redline
    anchoring.
    """
    # (a) Grounds directly in the paragraph the model cited.
    if cited_paragraph_id:
        cited_text = paragraph_by_id.get(cited_paragraph_id)
        if cited_text is not None and _quote_appears_in_text(quote, cited_text):
            return cited_paragraph_id, None
    else:
        # The model cited no paragraph. If the quote sits WHOLE inside one or more
        # single paragraphs, the existing precision guards apply: exactly one ->
        # accept it; several -> ambiguous, demand a paragraph_id. Only a quote that
        # fits NO single paragraph (i.e. it spans boundaries) falls through to the
        # document-wide span grounding below.
        single_paragraph_ids = _paragraph_ids_for_quote(quote, paragraph_by_id)
        if len(single_paragraph_ids) == 1:
            return single_paragraph_ids[0], None
        if len(single_paragraph_ids) > 1:
            return None, f"{item_location}: quote matches multiple reviewed paragraphs; provide paragraph_id"

    # (b/c) Document-wide grounding: the quote is real document text that crosses
    # the extractor's fine-grained paragraph boundaries (a full sentence, a whole
    # signature block) and may elide its middle with ``...``.
    doc_text = _document_text(ordered_paragraphs)
    if _quote_appears_in_text(quote, doc_text) or _ellipsis_segments_appear_in_order(quote, doc_text):
        # (d) Re-anchor to the first reviewed paragraph (packet order) that
        # contains the quote's first segment, deterministically.
        first_segment = _first_quote_segment(quote)
        anchor_id = _first_paragraph_containing(first_segment, ordered_paragraphs)
        if anchor_id is not None:
            return anchor_id, None
        # Defensive: the span grounds across paragraphs but no single paragraph
        # holds the first segment whole (it too crosses a boundary). Anchor to the
        # first reviewed paragraph so the citation still points somewhere real.
        if ordered_paragraphs:
            return ordered_paragraphs[0][0], None

    # (e) Grounds nowhere, even document-wide: a genuine fabrication.
    return None, None


def _document_text(ordered_paragraphs: Sequence[tuple[str, str]]) -> str:
    return "\n".join(text for _paragraph_id, text in ordered_paragraphs)


def _first_paragraph_containing(
    segment: str,
    ordered_paragraphs: Sequence[tuple[str, str]],
) -> str | None:
    if not segment:
        return None
    for paragraph_id, paragraph_text in ordered_paragraphs:
        if _quote_appears_in_text(segment, paragraph_text):
            return paragraph_id
    return None


def _first_quote_segment(quote: str) -> str:
    for segment in _ellipsis_split(quote):
        if segment.strip():
            return segment
    return quote


def _ellipsis_split(quote: str) -> list[str]:
    # Split on a literal ``...`` or the ellipsis glyph; the model uses either to
    # elide the middle of a span it is quoting.
    return [segment for segment in re.split(r"\.\.\.|…", str(quote or ""))]


def _ellipsis_segments_appear_in_order(quote: str, text: str) -> bool:
    """True when the quote's ellipsis-separated segments all appear, in order.

    Each non-empty segment must be found at or after the previous match position
    in the normalized text, so an elided span grounds without the elided middle.
    """
    segments = [segment for segment in _ellipsis_split(quote) if segment.strip()]
    if len(segments) < 2:
        return False
    normalized_text = _normalize_quote_text(text)
    position = 0
    for segment in segments:
        normalized_segment = _normalize_quote_text(segment)
        if not normalized_segment:
            continue
        found = normalized_text.find(normalized_segment, position)
        if found < 0:
            return False
        position = found + len(normalized_segment)
    return True


def _validated_proposed_redline(
    assessment: Mapping[str, Any],
    paragraph_by_id: Mapping[str, str],
    location: str,
    errors: list[str],
    *,
    playbook_clause: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    proposed_redline = assessment.get("proposed_redline")
    if not isinstance(proposed_redline, Mapping):
        errors.append(f"{location}: proposed_redline must be an object")
        return {"action": ""}
    allowed_keys = {"action", "paragraph_id", "text", "jurisdiction"}
    for key in proposed_redline:
        if str(key) not in allowed_keys:
            errors.append(f"{location}: proposed_redline has unsupported field {key}")

    action = str(proposed_redline.get("action") or "").strip()
    if action not in AI_ASSESSMENT_REDLINE_ACTIONS:
        errors.append(f"{location}: proposed_redline.action must be one of {', '.join(AI_ASSESSMENT_REDLINE_ACTIONS)}")
    text = str(proposed_redline.get("text") or "").strip()
    paragraph_id = str(proposed_redline.get("paragraph_id") or "").strip()
    jurisdiction = str(proposed_redline.get("jurisdiction") or "").strip()

    # The AI supplies judgment; the Playbook supplies the wording. The model
    # decides clauses well but routinely leaves proposed_redline.text blank when
    # flagging one — which used to reject the WHOLE document. When the AI omits
    # the replacement wording for a replace/insert, default it from the clause's
    # canonical Playbook template (governing_law derives it from the approved law).
    # An AI-authored, non-empty text remains an optional override and is kept as-is.
    degraded_no_text = False
    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_INSERT_AFTER_PARAGRAPH} and not text:
        template_text = playbook_redline_text(playbook_clause)
        if template_text:
            text = template_text
        else:
            # No Playbook wording is available for this clause (e.g. a prohibited
            # clause whose fix is deletion, mislabelled as a replace). Degrade just
            # THIS clause to a no-text flag instead of discarding every assessment;
            # the caller keeps the decision/rationale and routes it for human review.
            degraded_no_text = True
            action = AI_REDLINE_NO_CHANGE

    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH}:
        if not paragraph_id:
            errors.append(f"{location}: proposed_redline.paragraph_id is required for {action}")
        elif paragraph_id not in paragraph_by_id:
            errors.append(f"{location}: proposed_redline.paragraph_id does not exist: {paragraph_id}")

    cleaned: dict[str, Any] = {"action": action}
    if paragraph_id and action != AI_REDLINE_NO_CHANGE:
        cleaned["paragraph_id"] = paragraph_id
    if text:
        cleaned["text"] = text
    if jurisdiction:
        cleaned["jurisdiction"] = jurisdiction
    if degraded_no_text:
        cleaned["_degraded_no_text"] = True
    return cleaned


def _paragraph_ids_for_quote(quote: str, paragraph_by_id: Mapping[str, str]) -> list[str]:
    return [
        paragraph_id
        for paragraph_id, paragraph_text in paragraph_by_id.items()
        if _quote_appears_in_text(quote, paragraph_text)
    ]


def _quote_appears_in_text(quote: str, text: str) -> bool:
    normalized_quote = _normalize_quote_text(quote)
    normalized_text = _normalize_quote_text(text)
    return bool(normalized_quote and normalized_quote in normalized_text)


# Typographic glyphs an inbound real DOCX carries (curly quotes, dash variants,
# ellipsis char, non-breaking/thin spaces) that the model may echo straight while
# the extracted paragraph holds a different glyph for the same character. Folding
# them to a canonical ASCII form BEFORE whitespace-collapse + lowercase makes
# grounding robust without weakening the "the quote is real text" guarantee.
_QUOTE_GLYPH_TRANSLATION = {
    # Curly / typographic single quotes and primes -> straight apostrophe.
    ord("‘"): "'",  # left single quotation mark
    ord("’"): "'",  # right single quotation mark
    ord("‚"): "'",  # single low-9 quotation mark
    ord("‛"): "'",  # single high-reversed-9 quotation mark
    ord("′"): "'",  # prime
    # Curly / typographic double quotes -> straight double quote.
    ord("“"): '"',  # left double quotation mark
    ord("”"): '"',  # right double quotation mark
    ord("„"): '"',  # double low-9 quotation mark
    ord("‟"): '"',  # double high-reversed-9 quotation mark
    ord("″"): '"',  # double prime
    # Dash variants (en, em, figure, minus, hyphen variants) -> ASCII hyphen.
    ord("‐"): "-",  # hyphen
    ord("‑"): "-",  # non-breaking hyphen
    ord("‒"): "-",  # figure dash
    ord("–"): "-",  # en dash
    ord("—"): "-",  # em dash
    ord("―"): "-",  # horizontal bar
    ord("−"): "-",  # minus sign
    # Ellipsis char -> three ASCII dots (keeps ellipsis-split logic uniform).
    ord("…"): "...",  # horizontal ellipsis
    # Non-breaking / thin / narrow spaces -> plain space (whitespace-collapsed next).
    ord(" "): " ",  # no-break space
    ord(" "): " ",  # figure space
    ord(" "): " ",  # thin space
    ord(" "): " ",  # narrow no-break space
}


def _fold_typographic_glyphs(value: str) -> str:
    return str(value or "").translate(_QUOTE_GLYPH_TRANSLATION)


def _normalize_quote_text(value: str) -> str:
    folded = _fold_typographic_glyphs(value)
    return re.sub(r"\s+", " ", folded).strip().lower()
