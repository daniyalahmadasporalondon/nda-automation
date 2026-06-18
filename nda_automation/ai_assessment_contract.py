from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
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

AI_ASSESSMENT_CONTRACT_VERSION = 3
# Contract versions this validator still ACCEPTS on the wire. v2 sent a single
# ``proposed_redline`` object; v3 sends a ``proposed_edits`` LIST and may use the
# sentence-level ``strike_span``/``replace_span`` sugar. Old stored matters and
# single-edit clauses keep parsing via the v2 compat shim (see A.3). The cleaned
# output always stamps the current version.
AI_ASSESSMENT_ACCEPTED_CONTRACT_VERSIONS = (2, 3)
AI_REDLINE_NO_CHANGE = "no_change"
# Sentence-level span actions (v3-only sugar). They are LOWERED at parse time to an
# ordinary ``replace_paragraph`` whose replacement is the paragraph with the span
# cut/substituted (see ``apply_span``), so NO downstream consumer learns a new
# action and no new index/identity rule is introduced.
AI_REDLINE_STRIKE_SPAN = "strike_span"
AI_REDLINE_REPLACE_SPAN = "replace_span"
AI_REDLINE_SPAN_ACTIONS = (AI_REDLINE_STRIKE_SPAN, AI_REDLINE_REPLACE_SPAN)
# Cap on the number of structured reasoning steps carried onto a parsed
# assessment. The reviewer method has five named steps (locate/read/apply/cite/
# decide); the cap leaves headroom for sub-steps while bounding a runaway list.
AI_ASSESSMENT_MAX_REASONING_STEPS = 8
AI_ASSESSMENT_DECISIONS = (CLAUSE_DECISION_PASS, CLAUSE_DECISION_FAIL, CLAUSE_DECISION_REVIEW)
AI_ASSESSMENT_ISSUE_TYPES = (
    ISSUE_TYPE_NONE,
    ISSUE_TYPE_MISSING,
    ISSUE_TYPE_PRESENT_BUT_WRONG,
    ISSUE_TYPE_UNCLEAR,
)
# Paragraph-level actions: the only actions any DOWNSTREAM consumer ever sees,
# because spans are lowered to ``replace_paragraph`` at parse time.
AI_ASSESSMENT_PARAGRAPH_REDLINE_ACTIONS = (
    AI_REDLINE_NO_CHANGE,
    REDLINE_REPLACE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_DELETE_PARAGRAPH,
)
# Wire-accepted actions: the four paragraph actions PLUS the two v3 span actions a
# fresh model response may emit (and which we lower before storing).
AI_ASSESSMENT_REDLINE_ACTIONS = (
    *AI_ASSESSMENT_PARAGRAPH_REDLINE_ACTIONS,
    AI_REDLINE_STRIKE_SPAN,
    AI_REDLINE_REPLACE_SPAN,
)

AI_CLAUSE_ASSESSMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": AI_ASSESSMENT_CONTRACT_VERSION},
        "clause_id": {"type": "string"},
        # Optional, DISPLAY-ONLY chain-of-thought. Placed before `decision` so the
        # model records its work (locate -> read -> apply -> cite) BEFORE committing
        # to a verdict (genuine reasoning, not post-hoc justification). Parsed
        # fail-open: a missing/empty/malformed value is dropped and never changes the
        # decision/issue_type/evidence/confidence.
        "reasoning_steps": {
            "type": "array",
            "maxItems": AI_ASSESSMENT_MAX_REASONING_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string"},
                    "finding": {"type": "string"},
                },
                "required": ["step", "finding"],
                "additionalProperties": False,
            },
        },
        "decision": {"type": "string", "enum": list(AI_ASSESSMENT_DECISIONS)},
        "issue_type": {"type": "string", "enum": list(AI_ASSESSMENT_ISSUE_TYPES)},
        "rationale": {"type": "string"},
        "resolution_question": {"type": "string"},
        "suggested_redline": {"type": "string"},
        "recommended_option": {
            "type": "object",
            "properties": {
                "option": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["option", "reason"],
            "additionalProperties": False,
        },
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
        # Legacy single redline (v2). Still accepted and parsed into a 1-element
        # ``proposed_edits`` list so old stored matters and single-edit clauses keep
        # working without migration.
        "proposed_redline": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(AI_ASSESSMENT_REDLINE_ACTIONS)},
                "paragraph_id": {"type": "string"},
                "anchor_quote": {"type": "string"},
                "original_text": {"type": "string"},
                "replacement": {"type": ["string", "null"]},
                "text": {"type": "string"},
                "jurisdiction": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        # v3 multi-edit list. One edit per defective span. ``strike_span`` /
        # ``replace_span`` carry a verbatim ``anchor_quote`` and are lowered to a
        # whole-paragraph ``replace_paragraph`` at parse time.
        "proposed_edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(AI_ASSESSMENT_REDLINE_ACTIONS)},
                    "paragraph_id": {"type": "string"},
                    "anchor_quote": {"type": "string"},
                    "original_text": {"type": "string"},
                    "replacement": {"type": ["string", "null"]},
                    "text": {"type": "string"},
                    "jurisdiction": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
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
        # Either ``proposed_redline`` (v2) or ``proposed_edits`` (v3) satisfies the
        # redline requirement; the validator enforces "at least one" below rather
        # than via the static schema (JSON-Schema "anyOf" would reject neither).
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
    payload_version = AI_ASSESSMENT_CONTRACT_VERSION
    if "schema_version" in assessment:
        try:
            schema_version = int(assessment.get("schema_version"))
        except (TypeError, ValueError):
            schema_version = -1
        if schema_version not in AI_ASSESSMENT_ACCEPTED_CONTRACT_VERSIONS:
            accepted = ", ".join(str(v) for v in AI_ASSESSMENT_ACCEPTED_CONTRACT_VERSIONS)
            errors.append(f"{location}: schema_version must be one of {accepted}")
        else:
            payload_version = schema_version

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
    resolution_question = _optional_text(assessment, "resolution_question")
    suggested_redline = _optional_text(assessment, "suggested_redline")
    recommended_option = _optional_recommended_option(assessment, location, errors)
    # Display-only chain-of-thought. Parsed defensively and FAIL-OPEN: it never
    # contributes to `errors`, so it can never fail the assessment or alter the
    # decision/issue_type/evidence/confidence parsed above.
    reasoning_steps = _optional_reasoning_steps(assessment)
    confidence = _required_confidence(assessment, location, errors)
    blocks_send = _required_bool(assessment, "blocks_send", location, errors)
    evidence = _validated_evidence(assessment, paragraph_by_id, ordered_paragraphs, location, errors)
    proposed_edits, all_edits_degraded = _validated_proposed_edits(
        assessment,
        paragraph_by_id,
        location,
        errors,
        payload_version=payload_version,
        playbook_clause=clauses_by_id.get(clause_id),
    )
    # The first edit is the legacy "primary" redline that existing readers (e.g.
    # ``ai_first_assessment.proposed_redline_action``) consume; keep it in the
    # cleaned output so nothing downstream that still reads ``proposed_redline``
    # breaks. An empty list collapses to a no_change so the field is always present.
    proposed_redline = deepcopy(proposed_edits[0]) if proposed_edits else {"action": AI_REDLINE_NO_CHANGE}

    # One clause whose redline could neither be authored by the AI nor defaulted
    # from the Playbook (or whose only span(s) failed to anchor) must not discard
    # every other (correct) assessment. The validator collapsed each unusable edit
    # to a no-text no_change flag; keep the verdict actionable without violating the
    # decision<->action coupling below: a blank fail becomes a human-review flag
    # (never a silent pass), a blank review stays a review. The decision/rationale
    # survive so a human still sees the finding. Mirrors the historical singular
    # ``_degraded_no_text`` behaviour, generalized to "ALL edits degraded".
    if all_edits_degraded:
        rationale = _with_degrade_note(rationale)
        if decision == CLAUSE_DECISION_FAIL:
            decision = CLAUSE_DECISION_REVIEW
            blocks_send = True

    has_actionable_edit = any(
        edit.get("action") != AI_REDLINE_NO_CHANGE for edit in proposed_edits
    )

    if decision == CLAUSE_DECISION_PASS and issue_type != ISSUE_TYPE_NONE:
        errors.append(f"{location}: pass decisions must use issue_type none")
    if decision in {CLAUSE_DECISION_FAIL, CLAUSE_DECISION_REVIEW} and issue_type == ISSUE_TYPE_NONE:
        errors.append(f"{location}: fail/review decisions must not use issue_type none")
    if decision == CLAUSE_DECISION_PASS and has_actionable_edit:
        errors.append(f"{location}: pass decisions must use proposed_redline.action no_change")
    if decision == CLAUSE_DECISION_FAIL and not has_actionable_edit:
        errors.append(f"{location}: fail decisions require a proposed redline action")
    if decision == CLAUSE_DECISION_REVIEW and any(
        edit.get("action") not in AI_ASSESSMENT_PARAGRAPH_REDLINE_ACTIONS for edit in proposed_edits
    ):
        errors.append(f"{location}: review decisions have an unsupported proposed redline action")
    if decision == CLAUSE_DECISION_FAIL and issue_type != ISSUE_TYPE_MISSING and not evidence:
        errors.append(f"{location}: fail decisions require at least one valid evidence item unless issue_type is missing")
    if blocks_send is not None and blocks_send != (decision == CLAUSE_DECISION_REVIEW):
        errors.append(f"{location}: blocks_send must be true only for review decisions")

    cleaned: dict[str, Any] = {
        "schema_version": AI_ASSESSMENT_CONTRACT_VERSION,
        "clause_id": clause_id,
        "decision": decision,
        "issue_type": issue_type,
        "rationale": rationale,
        "evidence": evidence,
        "proposed_redline": proposed_redline,
        "proposed_edits": proposed_edits,
        "confidence": confidence,
        "blocks_send": bool(blocks_send),
        "validation_status": "contract_valid",
    }
    if resolution_question:
        cleaned["resolution_question"] = resolution_question
    if suggested_redline:
        cleaned["suggested_redline"] = suggested_redline
    if recommended_option:
        cleaned["recommended_option"] = recommended_option
    if reasoning_steps:
        cleaned["reasoning_steps"] = reasoning_steps
    return cleaned, errors


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


def _optional_text(assessment: Mapping[str, Any], key: str) -> str:
    if key not in assessment:
        return ""
    return str(assessment.get(key) or "").strip()


def _optional_recommended_option(
    assessment: Mapping[str, Any],
    location: str,
    errors: list[str],
) -> dict[str, str]:
    if "recommended_option" not in assessment:
        return {}
    option = assessment.get("recommended_option")
    if option in (None, ""):
        return {}
    option_location = f"{location}.recommended_option"
    if not isinstance(option, Mapping):
        errors.append(f"{option_location}: recommended_option must be an object")
        return {}
    for key in option:
        if str(key) not in {"option", "reason"}:
            errors.append(f"{option_location}: unsupported field {key}")
    recommended = str(option.get("option") or "").strip()
    reason = str(option.get("reason") or "").strip()
    if not recommended:
        errors.append(f"{option_location}: option must be non-empty text")
    if not reason:
        errors.append(f"{option_location}: reason must be non-empty text")
    return {"option": recommended, "reason": reason} if recommended and reason else {}


def _optional_reasoning_steps(assessment: Mapping[str, Any]) -> list[dict[str, str]]:
    """Parse the optional, DISPLAY-ONLY ``reasoning_steps`` array, FAIL-OPEN.

    The model is asked to record one entry per reviewer step (locate/read/apply/
    cite/decide) as ``{"step": <label>, "finding": <text>}`` BEFORE it decides. This
    is reviewer-facing chain-of-thought, never an input to the verdict, so it is
    parsed in total isolation from ``errors``:

    - missing, ``None``, or not a list -> ``[]`` (drop silently);
    - any element that is not an object, or lacks a non-empty step/finding -> that
      single element is skipped; the rest survive;
    - the list is capped at :data:`AI_ASSESSMENT_MAX_REASONING_STEPS` entries.

    It returns the cleaned ``[{"step", "finding"}]`` list and NEVER raises, so it
    cannot crash the assessment or change the parsed decision/evidence/confidence.
    """
    raw = assessment.get("reasoning_steps")
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in raw:
        if len(cleaned) >= AI_ASSESSMENT_MAX_REASONING_STEPS:
            break
        if not isinstance(item, Mapping):
            continue
        step = str(item.get("step") or "").strip()
        finding = str(item.get("finding") or "").strip()
        if not step or not finding:
            continue
        cleaned.append({"step": step, "finding": finding})
    return cleaned


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


# A legitimate "..." trims the middle of ONE sentence or block (the extractor split
# a sentence / signature block into fine-grained paragraphs). It must NOT stitch
# fragments from different sentences or far-apart paragraphs -- that lets the model
# fabricate a prohibition (or a clean-looking quote) by eliding the contradicting
# middle, e.g. "shall not ... solicit" assembled from "shall not disclose" (one
# sentence) and "may solicit freely" (another). So each elided gap between
# consecutive segments is length-bounded AND may not cross a sentence boundary.
_MAX_ELIDED_GAP_CHARS = 200
_SENTENCE_BOUNDARY_IN_GAP = re.compile(r"[.?!]")


def _ellipsis_segments_appear_in_order(quote: str, text: str) -> bool:
    """True when the quote's ellipsis-separated segments all appear, in order, with
    each elided gap short and within a single sentence.

    Each non-empty segment must be found at or after the previous match position in
    the normalized text, so an elided span grounds without the elided middle -- but
    a gap that is too long, or that crosses a sentence boundary, is rejected as a
    fabricated stitch rather than a genuine elision.
    """
    segments = [segment for segment in _ellipsis_split(quote) if segment.strip()]
    if len(segments) < 2:
        return False
    normalized_text = _normalize_quote_text(text)
    position = 0
    previous_end: int | None = None
    for segment in segments:
        normalized_segment = _normalize_quote_text(segment)
        if not normalized_segment:
            continue
        found = normalized_text.find(normalized_segment, position)
        if found < 0:
            return False
        if previous_end is not None:
            elided_gap = normalized_text[previous_end:found]
            if len(elided_gap) > _MAX_ELIDED_GAP_CHARS:
                return False
            if _SENTENCE_BOUNDARY_IN_GAP.search(elided_gap):
                return False
        previous_end = found + len(normalized_segment)
        position = previous_end
    return True


def clause_proposed_edits(clause: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Backward-compatible accessor for a clause's proposed edit list.

    Returns ``clause["proposed_edits"]`` when present (v3 clause results), else
    wraps the legacy singular ``clause["proposed_redline"]`` into a 1-element list
    (v2 stored matters). A no_change-only / missing redline yields ``[]`` so callers
    can treat "no edits" uniformly. NEVER raises.
    """
    if not isinstance(clause, Mapping):
        return []
    edits = clause.get("proposed_edits")
    if isinstance(edits, list):
        return [dict(edit) for edit in edits if isinstance(edit, Mapping)]
    single = clause.get("proposed_redline")
    if isinstance(single, Mapping):
        return [dict(single)]
    return []


def apply_span(paragraph_text: str, anchor_quote: str, replacement: str) -> str | None:
    """Apply a sentence-level span edit to a paragraph, returning the new text.

    Locates ``anchor_quote`` inside ``paragraph_text`` using the SAME typographic
    glyph-folding + whitespace-collapse normalization already used for evidence
    grounding (:func:`_normalize_quote_text`), then performs the cut on the
    ORIGINAL (un-normalized) text at the matched offsets so punctuation/casing is
    preserved. ``replacement`` substitutes the span (``replace_span``); an empty
    ``replacement`` strikes it (``strike_span``) and trims one adjacent connector
    space so the surrounding text reads cleanly.

    Returns the new paragraph text, or ``None`` when the anchor cannot be located
    verbatim (glyph-folded) — the caller degrades that edit to a no-op. NEVER
    raises: any unexpected failure also returns ``None`` (degrade-safe).
    """
    try:
        original = str(paragraph_text or "")
        anchor = str(anchor_quote or "")
        if not anchor.strip():
            return None
        match = _locate_span_offsets(original, anchor)
        if match is None:
            return None
        start, end = match
        replacement_text = str(replacement or "")
        if replacement_text:
            return original[:start] + replacement_text + original[end:]
        # Strike: drop the span and one adjacent connector space (prefer the
        # leading space so "A, B and C" -> "A and C" reads cleanly; fall back to
        # the trailing space at a paragraph start).
        before = original[:start]
        after = original[end:]
        if before.endswith(" "):
            before = before[:-1]
        elif after.startswith(" "):
            after = after[1:]
        result = before + after
        return result
    except Exception:  # pragma: no cover - degrade-safe guard
        return None


def _locate_span_offsets(original: str, anchor: str) -> tuple[int, int] | None:
    """Find ``anchor`` inside ``original`` and return ORIGINAL-text [start, end).

    Matches on glyph-folded text so curly quotes / dashes / nbsp in either string
    line up, but collapses NO whitespace (to keep a 1:1 character mapping between
    the folded and original strings, since folding is a per-character translation).
    Returns ``None`` when the folded anchor is empty or not a substring, when it
    matches NON-UNIQUELY (more than one occurrence — refuse to guess), or when the
    match is not WORD-BOUNDARY aligned (so "compete" cannot cut inside
    "competent"). Each of these degrades the span edit to a no-op upstream.
    """
    folded_original = _fold_typographic_glyphs(original)
    folded_anchor = _fold_typographic_glyphs(anchor).strip()
    if not folded_anchor:
        return None
    # _fold_typographic_glyphs only maps the ellipsis glyph to a 3-char string;
    # if either string contains one, character offsets would shift, so fall back to
    # a normalized-equality check that refuses to guess an offset.
    if "…" in original or "…" in anchor:
        return None
    # Every other fold is a 1:1 per-character translation, so offsets in
    # ``folded_original`` map directly onto ``original`` — the uniqueness and
    # word-boundary guards below run on the folded text but the returned offsets
    # index the original text without shift.
    index = folded_original.find(folded_anchor)
    if index < 0:
        return None
    end = index + len(folded_anchor)
    # Uniqueness guard (A1-01): a non-unique anchor would let ``find`` cut an
    # arbitrary first occurrence. If the folded anchor appears more than once,
    # refuse to guess which one the model meant and degrade to review/no-op.
    if folded_original.find(folded_anchor, index + 1) >= 0:
        return None
    # Word-boundary guard (A1-08): require the match to sit on token boundaries so
    # an anchor like "compete" cannot cut the interior of "competent". The
    # character immediately before/after the match must be a non-word character
    # (or the string start/end). Only enforced when the anchor itself begins/ends
    # on a word character — a punctuation-edged anchor has no boundary to honour.
    if folded_anchor[:1].isalnum() or folded_anchor[:1] == "_":
        if index > 0:
            prev_char = folded_original[index - 1]
            if prev_char.isalnum() or prev_char == "_":
                return None
    if folded_anchor[-1:].isalnum() or folded_anchor[-1:] == "_":
        if end < len(folded_original):
            next_char = folded_original[end]
            if next_char.isalnum() or next_char == "_":
                return None
    return index, end


def _validated_proposed_edits(
    assessment: Mapping[str, Any],
    paragraph_by_id: Mapping[str, str],
    location: str,
    errors: list[str],
    *,
    payload_version: int,
    playbook_clause: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Validate and normalize the proposed edit list (v2 singular or v3 list).

    Returns ``(edits, all_degraded)``: a list of cleaned paragraph-level edits
    (spans already lowered to ``replace_paragraph``), and a flag that is True only
    when at least one edit was present but EVERY actionable edit degraded to a
    no-op (so the caller downgrades the clause to human review).
    """
    raw_edits, source_error = _raw_proposed_edits(assessment, location, payload_version)
    if source_error is not None:
        errors.append(source_error)
        return [{"action": ""}], False
    if not raw_edits:
        # No redline supplied at all. Mirror the historical empty-object behaviour:
        # a single no_change edit, which the coupling checks treat as "no action".
        return [{"action": AI_REDLINE_NO_CHANGE}], False

    cleaned_edits: list[dict[str, Any]] = []
    actionable_count = 0
    degraded_count = 0
    for index, raw_edit in enumerate(raw_edits):
        edit_location = location if len(raw_edits) == 1 else f"{location}.proposed_edits[{index}]"
        cleaned, degraded = _validated_single_edit(
            raw_edit,
            paragraph_by_id,
            edit_location,
            errors,
            payload_version=payload_version,
            playbook_clause=playbook_clause,
        )
        cleaned_edits.append(cleaned)
        if degraded:
            degraded_count += 1
            actionable_count += 1
        elif cleaned.get("action") != AI_REDLINE_NO_CHANGE:
            actionable_count += 1

    all_edits_degraded = actionable_count > 0 and degraded_count == actionable_count
    return cleaned_edits, all_edits_degraded


def _raw_proposed_edits(
    assessment: Mapping[str, Any],
    location: str,
    payload_version: int,
) -> tuple[list[Mapping[str, Any]], str | None]:
    """Extract the raw edit list from a v2 (singular) or v3 (list) payload.

    Returns ``(edits, error)``. ``proposed_edits`` (v3) wins when present; otherwise
    the legacy singular ``proposed_redline`` is wrapped into a 1-element list. A
    structurally wrong shape yields ``([], error)``.
    """
    if "proposed_edits" in assessment:
        raw = assessment.get("proposed_edits")
        if not isinstance(raw, list):
            return [], f"{location}: proposed_edits must be a list"
        edits = [item for item in raw if isinstance(item, Mapping)]
        if len(edits) != len(raw):
            return [], f"{location}: proposed_edits items must be objects"
        return edits, None
    if "proposed_redline" in assessment:
        single = assessment.get("proposed_redline")
        if not isinstance(single, Mapping):
            return [], f"{location}: proposed_redline must be an object"
        return [single], None
    return [], None


def _validated_single_edit(
    proposed_redline: Mapping[str, Any],
    paragraph_by_id: Mapping[str, str],
    location: str,
    errors: list[str],
    *,
    payload_version: int,
    playbook_clause: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Validate a single edit object and lower any span action.

    Returns ``(cleaned_edit, degraded)``. ``degraded`` is True when the edit was an
    actionable redline that could not be realized (no Playbook wording, or a span
    whose anchor was not found) and was collapsed to a no_change no-op.
    """
    allowed_keys = {
        "action",
        "paragraph_id",
        "anchor_quote",
        "original_text",
        "replacement",
        "text",
        "jurisdiction",
        "rationale",
    }
    for key in proposed_redline:
        if str(key) not in allowed_keys:
            errors.append(f"{location}: proposed edit has unsupported field {key}")

    action = str(proposed_redline.get("action") or "").strip()
    if action not in AI_ASSESSMENT_REDLINE_ACTIONS:
        errors.append(
            f"{location}: action must be one of {', '.join(AI_ASSESSMENT_REDLINE_ACTIONS)}"
        )
    # Span actions are v3-only sugar; a v2 payload may never use them.
    if action in AI_REDLINE_SPAN_ACTIONS and payload_version < 3:
        errors.append(f"{location}: {action} requires schema_version 3")

    paragraph_id = str(proposed_redline.get("paragraph_id") or "").strip()
    jurisdiction = str(proposed_redline.get("jurisdiction") or "").strip()
    rationale = str(proposed_redline.get("rationale") or "").strip()
    # ``replacement`` is the v3 name; ``text`` is the legacy alias kept for compat.
    replacement_raw = proposed_redline.get("replacement")
    if replacement_raw is None:
        replacement_raw = proposed_redline.get("text")
    text = str(replacement_raw or "").strip()
    anchor_quote = str(proposed_redline.get("anchor_quote") or "").strip()

    degraded_no_text = False
    # Span provenance preserved on the lowered edit so the redline builder can
    # COALESCE several spans on the SAME paragraph by re-composing each cut onto the
    # original paragraph text (a single full-paragraph replacement per span would
    # otherwise clobber its sibling). Only set for an actually-lowered span.
    span_action = ""
    span_anchor = ""
    span_replacement = ""

    # ---- Span lowering (strike_span / replace_span -> replace_paragraph) ----
    if action in AI_REDLINE_SPAN_ACTIONS:
        lowered = _lower_span_edit(
            action,
            paragraph_id,
            anchor_quote,
            text,
            paragraph_by_id,
            location,
            errors,
        )
        if lowered is None:
            # Anchor not found / structurally unusable: degrade THIS edit to a
            # no-op. The caller downgrades the clause to review if ALL edits degrade.
            cleaned: dict[str, Any] = {"action": AI_REDLINE_NO_CHANGE}
            if rationale:
                cleaned["rationale"] = rationale
            return cleaned, True
        span_action = action
        span_anchor = anchor_quote
        span_replacement = text if action == AI_REDLINE_REPLACE_SPAN else ""
        action = REDLINE_REPLACE_PARAGRAPH
        text = lowered

    # The AI supplies judgment; the Playbook supplies the wording. The model
    # decides clauses well but routinely leaves the replacement text blank when
    # flagging one — which used to reject the WHOLE document. When the AI omits the
    # replacement wording for a replace/insert, default it from the clause's
    # canonical Playbook template (governing_law derives it from the approved law).
    # An AI-authored, non-empty text remains an optional override and is kept as-is.
    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_INSERT_AFTER_PARAGRAPH} and not text:
        template_text = playbook_redline_text(playbook_clause)
        if template_text:
            text = template_text
        else:
            # No Playbook wording is available for this clause (e.g. a prohibited
            # clause whose fix is deletion, mislabelled as a replace). Degrade just
            # THIS edit to a no-op instead of discarding every assessment.
            degraded_no_text = True
            action = AI_REDLINE_NO_CHANGE

    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH}:
        if not paragraph_id:
            errors.append(f"{location}: paragraph_id is required for {action}")
        elif paragraph_id not in paragraph_by_id:
            errors.append(f"{location}: paragraph_id does not exist: {paragraph_id}")

    cleaned = {"action": action}
    if paragraph_id and action != AI_REDLINE_NO_CHANGE:
        cleaned["paragraph_id"] = paragraph_id
    if text:
        cleaned["text"] = text
    if jurisdiction:
        cleaned["jurisdiction"] = jurisdiction
    if rationale:
        cleaned["rationale"] = rationale
    # Carry span provenance so same-paragraph spans can be re-composed during
    # redline coalescing. Display/consumers that only read action+text ignore it.
    if span_action:
        cleaned["span_action"] = span_action
        cleaned["span_anchor_quote"] = span_anchor
        if span_action == AI_REDLINE_REPLACE_SPAN:
            cleaned["span_replacement"] = span_replacement
    return cleaned, degraded_no_text


def _lower_span_edit(
    action: str,
    paragraph_id: str,
    anchor_quote: str,
    replacement: str,
    paragraph_by_id: Mapping[str, str],
    location: str,
    errors: list[str],
) -> str | None:
    """Lower a span edit to the replacement text for a whole-paragraph replace.

    Returns the new paragraph text (paragraph with the span cut/substituted), or
    ``None`` when the edit is structurally unusable (missing/unknown paragraph_id,
    missing anchor_quote, or an anchor that does not appear verbatim). A structural
    contract violation (missing/unknown id, missing anchor) records an ``errors``
    entry; an unanchorable-but-well-formed span does NOT (it degrades to review).
    """
    if not paragraph_id:
        errors.append(f"{location}: paragraph_id is required for {action}")
        return None
    paragraph_text = paragraph_by_id.get(paragraph_id)
    if paragraph_text is None:
        errors.append(f"{location}: paragraph_id does not exist: {paragraph_id}")
        return None
    if not anchor_quote:
        errors.append(f"{location}: anchor_quote is required for {action}")
        return None
    span_replacement = replacement if action == AI_REDLINE_REPLACE_SPAN else ""
    new_text = apply_span(paragraph_text, anchor_quote, span_replacement)
    if new_text is None:
        # Well-formed but the anchor could not be located verbatim: a no-op
        # degrade, NOT a contract error (the clause is downgraded to review).
        return None
    if new_text == paragraph_text:
        # The span resolved to a no-change (e.g. replace with identical text):
        # treat as a degrade so it does not masquerade as an actionable redline.
        return None
    return new_text


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
