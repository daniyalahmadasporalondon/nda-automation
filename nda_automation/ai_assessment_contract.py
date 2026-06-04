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
        "why_it_might_be_a_problem": {"type": "string"},
        "why_it_may_be_fine": {"type": "string"},
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
        "why_it_might_be_a_problem",
        "why_it_may_be_fine",
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
) -> dict[str, dict[str, Any]]:
    errors: list[str] = []
    cleaned_by_clause_id: dict[str, dict[str, Any]] = {}
    valid_clause_id_set = {str(clause_id).strip() for clause_id in valid_clause_ids if str(clause_id).strip()}
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
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
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

    paragraph_by_id = {
        str(paragraph.get("id") or ""): str(paragraph.get("text") or "")
        for paragraph in paragraphs
        if str(paragraph.get("id") or "")
    }
    valid_clause_id_set = {str(clause_id).strip() for clause_id in valid_clause_ids if str(clause_id).strip()}

    clause_id = _required_text(assessment, "clause_id", location, errors)
    if clause_id and clause_id not in valid_clause_id_set:
        errors.append(f"{location}: unknown clause_id {clause_id}")

    decision = _required_enum(assessment, "decision", AI_ASSESSMENT_DECISIONS, location, errors)
    issue_type = _required_enum(assessment, "issue_type", AI_ASSESSMENT_ISSUE_TYPES, location, errors)
    rationale = _required_text(assessment, "rationale", location, errors)
    why_problem = _required_text(assessment, "why_it_might_be_a_problem", location, errors)
    why_fine = _required_text(assessment, "why_it_may_be_fine", location, errors)
    confidence = _required_confidence(assessment, location, errors)
    blocks_send = _required_bool(assessment, "blocks_send", location, errors)
    evidence = _validated_evidence(assessment, paragraph_by_id, location, errors)
    proposed_redline = _validated_proposed_redline(assessment, paragraph_by_id, location, errors)

    if decision == CLAUSE_DECISION_PASS and issue_type != ISSUE_TYPE_NONE:
        errors.append(f"{location}: pass decisions must use issue_type none")
    if decision in {CLAUSE_DECISION_FAIL, CLAUSE_DECISION_REVIEW} and issue_type == ISSUE_TYPE_NONE:
        errors.append(f"{location}: fail/review decisions must not use issue_type none")
    if decision in {CLAUSE_DECISION_FAIL, CLAUSE_DECISION_REVIEW} and not why_problem:
        errors.append(f"{location}: fail/review decisions require why_it_might_be_a_problem")
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
        "why_it_might_be_a_problem": why_problem,
        "why_it_may_be_fine": why_fine,
        "evidence": evidence,
        "proposed_redline": proposed_redline,
        "confidence": confidence,
        "blocks_send": bool(blocks_send),
        "validation_status": "contract_valid",
    }, errors


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
        if paragraph_id:
            paragraph_text = paragraph_by_id.get(paragraph_id)
            if paragraph_text is None:
                errors.append(f"{item_location}: paragraph_id does not exist: {paragraph_id}")
                continue
            if not _quote_appears_in_text(quote, paragraph_text):
                errors.append(f"{item_location}: quote does not appear in paragraph {paragraph_id}")
                continue
        else:
            matching_paragraph_ids = _paragraph_ids_for_quote(quote, paragraph_by_id)
            if not matching_paragraph_ids:
                errors.append(f"{item_location}: quote does not appear in any reviewed paragraph")
                continue
            if len(matching_paragraph_ids) > 1:
                errors.append(f"{item_location}: quote matches multiple reviewed paragraphs; provide paragraph_id")
                continue
            paragraph_id = matching_paragraph_ids[0]
        cleaned.append({"paragraph_id": paragraph_id, "quote": quote, "relevance": relevance})
    return cleaned


def _validated_proposed_redline(
    assessment: Mapping[str, Any],
    paragraph_by_id: Mapping[str, str],
    location: str,
    errors: list[str],
) -> dict[str, str]:
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

    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_INSERT_AFTER_PARAGRAPH} and not text:
        errors.append(f"{location}: proposed_redline.text is required for {action}")
    if action in {REDLINE_REPLACE_PARAGRAPH, REDLINE_DELETE_PARAGRAPH}:
        if not paragraph_id:
            errors.append(f"{location}: proposed_redline.paragraph_id is required for {action}")
        elif paragraph_id not in paragraph_by_id:
            errors.append(f"{location}: proposed_redline.paragraph_id does not exist: {paragraph_id}")

    cleaned = {"action": action}
    if paragraph_id:
        cleaned["paragraph_id"] = paragraph_id
    if text:
        cleaned["text"] = text
    if jurisdiction:
        cleaned["jurisdiction"] = jurisdiction
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


def _normalize_quote_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()
