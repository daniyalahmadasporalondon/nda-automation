from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import Mapping, Sequence
from typing import Any

from .ai_assessment_contract import AI_ASSESSMENT_CONTRACT_VERSION, AI_CLAUSE_ASSESSMENT_SCHEMA
from .playbook_rules import PLAYBOOK_RULES_VERSION, playbook_rules_for_ai
from .review_document import Paragraph, align_document_paragraphs, split_document_paragraphs

AI_ASSESSMENT_PROMPT_VERSION = 1
AI_ASSESSMENT_TASK = "ai_first_clause_assessment"
MAX_AI_ASSESSMENT_PARAGRAPHS = 120
MAX_AI_ASSESSMENT_CHARS = 60000

AI_ASSESSMENT_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "assessments": {
            "type": "array",
            "items": deepcopy(AI_CLAUSE_ASSESSMENT_SCHEMA),
        },
    },
    "required": ["assessments"],
    "additionalProperties": False,
}

AI_ASSESSMENT_SYSTEM_PROMPT = (
    "You are an AI legal reviewer for NDA hard-clause assessment. "
    "Use only the supplied playbook rules and document paragraphs. "
    "Do not invent clauses, jurisdictions, paragraph ids, or quote text. "
    "Return only schema-valid JSON."
)

AI_ASSESSMENT_INSTRUCTIONS = [
    "Return exactly one assessment for every playbook clause in the packet.",
    "Each assessment must match the supplied AI clause assessment schema.",
    "Use pass only when the supplied paragraphs satisfy the clause rules.",
    "Use fail when a required clause is missing, a clause is present but wrong, or a prohibited clause is present.",
    "Use review when evidence is ambiguous, conflicting, incomplete, conditional, or depends on unavailable document text.",
    "For missing required clauses, return decision fail with issue_type missing and evidence as an empty list.",
    "For absent prohibited clauses, return decision pass with issue_type none; evidence may be empty when no direct quote can prove absence.",
    "For pass and fail decisions supported by text, cite exact quote text from supplied paragraph ids.",
    "Never cite a quote unless the exact quote appears in the cited paragraph.",
    "For Governing Law, choose proposed_redline.jurisdiction only from the rule approved_options list.",
    "Set blocks_send true only for review decisions; set it false for pass and fail decisions.",
]

AI_ASSESSMENT_DECISION_POLICY: dict[str, object] = {
    "pass": (
        "The document satisfies the clause rules. For required clauses, cite supporting text when available. "
        "For prohibited clauses, absence may be enough when the rule allows zero pass evidence."
    ),
    "fail": (
        "The document does not satisfy the clause rules because required language is missing, language is present "
        "but wrong, or prohibited language is present."
    ),
    "review": (
        "A human should decide because the document text, clause scope, governing option, or evidence is unclear, "
        "conflicting, incomplete, or outside the supplied packet."
    ),
}


def build_ai_assessment_packet(
    source_text: str,
    *,
    playbook: Mapping[str, Any],
    paragraphs: Sequence[Paragraph] | None = None,
    provider: str = "",
    model: str = "",
    max_paragraphs: int = MAX_AI_ASSESSMENT_PARAGRAPHS,
    max_chars: int = MAX_AI_ASSESSMENT_CHARS,
) -> dict[str, Any]:
    document_paragraphs = _review_paragraphs(source_text or "", paragraphs)
    included_paragraphs = _fit_context_budget(document_paragraphs, max_paragraphs=max_paragraphs, max_chars=max_chars)
    rules_packet = playbook_rules_for_ai(playbook)
    return {
        "version": AI_ASSESSMENT_PROMPT_VERSION,
        "task": AI_ASSESSMENT_TASK,
        "provider": str(provider or ""),
        "model": str(model or ""),
        "document": {
            "paragraph_count": len(document_paragraphs),
            "included_paragraph_count": len(included_paragraphs),
            "omitted_paragraph_count": max(0, len(document_paragraphs) - len(included_paragraphs)),
            "context_budget": {
                "max_paragraphs": int(max_paragraphs),
                "max_chars": int(max_chars),
            },
        },
        "paragraphs": [_paragraph_record(paragraph) for paragraph in included_paragraphs],
        "playbook": {
            "rules_version": PLAYBOOK_RULES_VERSION,
            "clauses": deepcopy(rules_packet["clauses"]),
        },
        "output_contract": {
            "assessment_contract_version": AI_ASSESSMENT_CONTRACT_VERSION,
            "response_schema": deepcopy(AI_ASSESSMENT_RESPONSE_SCHEMA),
            "required_assessment_count": len(rules_packet["clauses"]),
        },
        "decision_policy": deepcopy(AI_ASSESSMENT_DECISION_POLICY),
        "instructions": list(AI_ASSESSMENT_INSTRUCTIONS),
    }


def build_ai_assessment_prompt(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": AI_ASSESSMENT_PROMPT_VERSION,
        "system": AI_ASSESSMENT_SYSTEM_PROMPT,
        "user": (
            "Assess every playbook clause against the supplied document paragraphs. "
            "Return only JSON matching the response schema.\n\n"
            + json.dumps(packet, ensure_ascii=False, indent=2)
        ),
        "response_schema": deepcopy(AI_ASSESSMENT_RESPONSE_SCHEMA),
    }


def _review_paragraphs(source_text: str, paragraphs: Sequence[Paragraph] | None) -> list[Paragraph]:
    if paragraphs is None:
        return split_document_paragraphs(source_text)
    if source_text:
        return align_document_paragraphs(list(paragraphs), source_text)
    return [deepcopy(paragraph) for paragraph in paragraphs]


def _fit_context_budget(
    paragraphs: Sequence[Paragraph],
    *,
    max_paragraphs: int,
    max_chars: int,
) -> list[Paragraph]:
    fitted: list[Paragraph] = []
    char_count = 0
    paragraph_limit = max(0, int(max_paragraphs))
    char_limit = max(0, int(max_chars))
    for paragraph in paragraphs[:paragraph_limit]:
        text = str(paragraph.get("text") or "")
        if char_limit and char_count + len(text) > char_limit and fitted:
            break
        fitted.append(paragraph)
        char_count += len(text)
    return fitted


def _paragraph_record(paragraph: Paragraph) -> dict[str, Any]:
    record = {
        "id": str(paragraph.get("id") or ""),
        "index": paragraph.get("index"),
        "text": str(paragraph.get("text") or ""),
    }
    for key in ["start", "end", "source_index", "source_part", "source_kind"]:
        if key in paragraph:
            record[key] = paragraph[key]
    return record
