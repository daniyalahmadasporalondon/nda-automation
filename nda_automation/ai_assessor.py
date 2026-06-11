from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from copy import deepcopy
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .ai_assessment_contract import (
    AI_ASSESSMENT_CONTRACT_VERSION,
    AIAssessmentContractError,
    validate_ai_clause_assessments,
)
from .ai_assessment_prompt import (
    AI_ASSESSMENT_PROMPT_VERSION,
    build_ai_assessment_packet,
    build_ai_assessment_prompt,
)
from .ai_first_review import build_ai_first_review_result
from .ai_review import (
    DEFAULT_AI_TIMEOUT_SECONDS,
    DEFAULT_OPENROUTER_MODEL,
    OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
    _ai_review_settings,
    _configured_api_key,
    _sanitize_model_name,
    _trusted_https_context,
)
from .checker import load_playbook, validate_playbook
from .openrouter_usage import record_openrouter_usage
from .review_document import Paragraph, align_document_paragraphs, split_document_paragraphs
from .review_state import REVIEW_STATE_CHECK, REVIEW_STATE_REVIEW

AI_ASSESSOR_VERSION = 1
AI_FIRST_ASSESSOR_MODE = "ai_first_assessor"


class AIAssessorError(RuntimeError):
    pass


@runtime_checkable
class AIAssessmentReviewer(Protocol):
    def __call__(self, packet: dict[str, Any]) -> dict[str, Any] | None:
        ...


class OpenRouterAIAssessmentReviewer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_OPENROUTER_MODEL,
        timeout_seconds: int = DEFAULT_AI_TIMEOUT_SECONDS,
    ) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise AIAssessorError("OpenRouter API key is not configured.")
        self.api_key = cleaned_key
        self.model = _sanitize_model_name(model or DEFAULT_OPENROUTER_MODEL)
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS))
        # Provenance: set on a successful call so the result attributes the model
        # that ACTUALLY produced the verdict, not just the configured settings (which
        # would silently misreport once a fallback/override provider is introduced).
        self.last_success_provider = ""
        self.last_success_model = ""

    def __call__(self, packet: dict[str, Any]) -> dict[str, Any] | None:
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
            data=json.dumps(openrouter_ai_assessment_request_body(packet, model=self.model)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "nda-automation/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=_trusted_https_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:500]
            raise AIAssessorError(f"OpenRouter API returned HTTP {error.code}: {message}") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise AIAssessorError(f"OpenRouter API request failed: {error}") from error
        record_openrouter_usage(payload, feature="assessor", model=self.model)
        parsed = _parse_provider_response_text(_openrouter_response_text(payload), provider="OpenRouter")
        self.last_success_provider = "openrouter"
        self.last_success_model = self.model
        return parsed


class InMemoryAssessmentReviewer:
    def __init__(self, *, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.packets: list[dict[str, Any]] = []

    def __call__(self, packet: dict[str, Any]) -> dict[str, Any] | None:
        self.packets.append(deepcopy(packet))
        if self.error is not None:
            raise self.error
        if callable(self.response):
            return self.response(packet)
        return deepcopy(self.response) if isinstance(self.response, dict) else self.response


# Env var that swaps the real AI provider for the deterministic stub reviewer
# below. STRICTLY a test seam: unset in production (and in every code path that
# does not export it), so the real OpenRouter reviewer is always used unless a
# test explicitly opts in. Lets AI-first integration tests exercise the dynamic
# (engine=="dynamic") clause pipeline end to end without a live API key.
AI_ASSESSMENT_STUB_ENV = "NDA_AI_ASSESSMENT_STUB"

# Paragraph language that marks a prohibited non-circumvention / introduced-party
# / exclusive-dealing restriction — the same kinds the dynamic non_circumvention
# clause exists to remove. Mirrors the prohibited intent, not the engine's matcher.
_STUB_PROHIBITED_PATTERN = re.compile(
    r"circumvent|introduced part|deal directly|non-?solicit|exclusiv|"
    r"(?:hire|recruit|poach|retain).{0,80}introduced|introduced.{0,80}(?:hire|recruit|poach|retain)",
    re.IGNORECASE,
)
_STUB_FREEDOM_PRESERVING_PATTERN = re.compile(
    r"nothing\b.{0,120}\brestricts?\b.{0,120}\bdeal|"
    r"\bshall\s+not\s+be\s+(?:restricted|prevented)\s+from\b|"
    r"\bdoes\s+not\s+create\b.{0,120}\bnon[-\s]?circumvention\b|"
    r"\bno\s+non[-\s]?circumvention\b.{0,80}\b(?:obligation|restriction|is|are|exists?|created)\b",
    re.IGNORECASE,
)
_STUB_LAWFUL_CIRCUMVENTION_PATTERN = re.compile(
    r"\bcircumvent(?:ing)?\s+(?:applicable\s+)?(?:laws?|sanctions|regulatory\s+obligations?)\b|"
    r"\bcircumvention\s+of\s+(?:applicable\s+)?(?:laws?|sanctions|regulatory\s+obligations?)\b",
    re.IGNORECASE,
)
_STUB_AMBIGUOUS_NON_CIRCUMVENTION_PATTERN = re.compile(
    r"\backnowledge[s]?\s+non[-\s]?circumvention\b|"
    r"\bnon[-\s]?circumvention\b.{0,120}\b(?:principles|future|to\s+be\s+agreed|subject\s+to)\b",
    re.IGNORECASE,
)


def stub_ai_assessment_response(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic, key-free AI assessment used only under AI_ASSESSMENT_STUB_ENV.

    Passes every native clause (no change) and, for any dynamic prohibited clause
    (fallback.redline_action == "delete_paragraph"), fails it against the first
    paragraph carrying prohibited restriction text, proposing a delete_paragraph
    redline — exactly the assessment shape a real reviewer would return for a
    present prohibited restriction. Clauses without a prohibited paragraph pass.
    """
    clauses = packet.get("playbook", {}).get("clauses", []) if isinstance(packet, Mapping) else []
    paragraphs = packet.get("paragraphs", []) if isinstance(packet, Mapping) else []
    prohibited: list[Mapping[str, Any]] = []
    review: list[Mapping[str, Any]] = []
    for paragraph in paragraphs:
        paragraph_text = str(paragraph.get("text") or "")
        if _STUB_LAWFUL_CIRCUMVENTION_PATTERN.search(paragraph_text):
            continue
        if _STUB_FREEDOM_PRESERVING_PATTERN.search(paragraph_text):
            continue
        if _STUB_AMBIGUOUS_NON_CIRCUMVENTION_PATTERN.search(paragraph_text):
            review.append(paragraph)
            continue
        if _STUB_PROHIBITED_PATTERN.search(paragraph_text):
            prohibited.append(paragraph)
    assessments: list[dict[str, Any]] = []
    for clause in clauses:
        clause_id = str(clause.get("clause_id") or "")
        fallback = clause.get("fallback") if isinstance(clause.get("fallback"), Mapping) else {}
        is_prohibited_delete = str(fallback.get("redline_action") or "") == "delete_paragraph"
        if is_prohibited_delete and prohibited:
            assessments.append({
                "clause_id": clause_id,
                "decision": "fail",
                "issue_type": "present_but_wrong",
                "rationale": "Prohibited restriction present; remove the offending paragraph(s).",
                "evidence": [
                    {
                        "paragraph_id": str(p.get("id") or ""),
                        "quote": str(p.get("text") or ""),
                        "relevance": "States the prohibited restriction.",
                    }
                    for p in prohibited
                ],
                "proposed_redline": {"action": "delete_paragraph", "paragraph_id": str(prohibited[0].get("id") or "")},
                "confidence": 0.95,
                "blocks_send": False,
            })
        elif is_prohibited_delete and review:
            assessments.append({
                "clause_id": clause_id,
                "decision": "review",
                "issue_type": "unclear",
                "rationale": "Possible non-circumvention concept present, but operative scope is unclear.",
                "evidence": [
                    {
                        "paragraph_id": str(p.get("id") or ""),
                        "quote": str(p.get("text") or ""),
                        "relevance": "Mentions non-circumvention in ambiguous terms.",
                    }
                    for p in review
                ],
                "proposed_redline": {"action": "no_change"},
                "confidence": 0.8,
                "blocks_send": True,
            })
        else:
            assessments.append({
                "clause_id": clause_id,
                "decision": "pass",
                "issue_type": "none",
                "rationale": "Stub reviewer: no issue.",
                "evidence": [],
                "proposed_redline": {"action": "no_change"},
                "confidence": 0.95,
                "blocks_send": False,
            })
    return {"assessments": assessments}


def assess_nda_with_ai(
    source_text: str,
    *,
    paragraphs: Sequence[Paragraph] | None = None,
    reviewer: AIAssessmentReviewer | None = None,
    playbook: Mapping[str, Any] | None = None,
    checked_at: str | None = None,
) -> dict[str, Any]:
    settings = _ai_review_settings()
    configured_reviewer = reviewer
    if configured_reviewer is None:
        if not settings["enabled"]:
            raise AIAssessorError("AI-first assessment is disabled.")
        configured_reviewer = configured_ai_assessment_reviewer(settings)

    source = source_text or ""
    document_paragraphs = _review_paragraphs(source, paragraphs)
    review_playbook = deepcopy(playbook) if isinstance(playbook, Mapping) else load_playbook()
    validate_playbook(review_playbook)
    packet = build_ai_assessment_packet(
        source,
        playbook=review_playbook,
        paragraphs=document_paragraphs,
        provider=str(settings["provider"]),
        model=str(settings["model"]),
    )
    try:
        raw_response = configured_reviewer(packet)
    except Exception as error:
        raise AIAssessorError(f"AI-first assessment failed: {error}") from error

    raw_assessments = _validate_ai_assessment_response(
        raw_response,
        playbook=review_playbook,
        packet=packet,
    )
    used_provider = str(getattr(configured_reviewer, "last_success_provider", "") or settings["provider"])
    used_model = str(getattr(configured_reviewer, "last_success_model", "") or settings["model"])
    result = build_ai_first_review_result(
        source,
        raw_assessments,
        paragraphs=document_paragraphs,
        checked_at=checked_at,
        playbook=review_playbook,
    )
    document_info = packet.get("document", {}) if isinstance(packet.get("document"), Mapping) else {}
    truncation = _apply_truncation_guard(result, document_info)
    missing_clause_ids = list(result.get("ai_review", {}).get("missing_clause_ids", []))
    status = "partial" if (missing_clause_ids or truncation["truncated"]) else "completed"
    metadata = {
        "version": AI_ASSESSOR_VERSION,
        "status": status,
        "mode": AI_FIRST_ASSESSOR_MODE,
        "provider": used_provider,
        "model": used_model,
        "packet_version": AI_ASSESSMENT_PROMPT_VERSION,
        "assessment_contract_version": AI_ASSESSMENT_CONTRACT_VERSION,
        "record_count": len(raw_assessments),
        "missing_clause_ids": missing_clause_ids,
        "included_paragraph_count": int(document_info.get("included_paragraph_count") or 0),
        "omitted_paragraph_count": int(document_info.get("omitted_paragraph_count") or 0),
        "truncated": truncation["truncated"],
    }
    result["ai_first_review"] = {**dict(result.get("ai_first_review", {})), **metadata}
    result["ai_review"] = {**dict(result.get("ai_review", {})), **metadata}
    return result


def _apply_truncation_guard(
    result: dict[str, Any],
    document_info: Mapping[str, Any],
) -> dict[str, Any]:
    """Force a truncated document to manual review so omitted text can't false-clear.

    The AI only ever sees the paragraphs that fit the packet budget. When the
    document was truncated (paragraphs dropped, or a single oversized paragraph
    clipped), the unseen text was never assessed -- so a violation hiding past
    the budget would otherwise pass silently on a long document. We escalate the
    overall verdict to ``needs_review`` (never softening an existing fail) and
    surface a reviewer-facing notice naming how much went unreviewed.
    """
    omitted = int(document_info.get("omitted_paragraph_count") or 0)
    clipped = int(document_info.get("clipped_paragraph_count") or 0)
    truncated = bool(document_info.get("truncated")) or omitted > 0 or clipped > 0
    unreviewed = omitted + clipped
    summary = {
        "truncated": truncated,
        "omitted_paragraph_count": omitted,
        "clipped_paragraph_count": clipped,
        "unreviewed_paragraph_count": unreviewed,
        "included_paragraph_count": int(document_info.get("included_paragraph_count") or 0),
        "paragraph_count": int(document_info.get("paragraph_count") or 0),
        "context_budget": dict(document_info.get("context_budget") or {}),
    }
    if not truncated:
        summary["message"] = ""
        result["truncation"] = summary
        return summary

    notice = _truncation_notice(omitted, clipped)
    summary["message"] = notice
    summary["requires_manual_review"] = True
    result["truncation"] = summary
    _escalate_result_to_review(result, reason=notice)
    return summary


def _truncation_notice(omitted: int, clipped: int) -> str:
    parts: list[str] = []
    if omitted:
        parts.append(f"{omitted} paragraph{'s' if omitted != 1 else ''} omitted")
    if clipped:
        parts.append(f"{clipped} paragraph{'s' if clipped != 1 else ''} truncated")
    detail = " and ".join(parts) if parts else "part of the document"
    return f"Document truncated, {detail} unreviewed -> manual review required."


def _escalate_result_to_review(result: dict[str, Any], *, reason: str) -> None:
    """Lift a passing verdict to review without softening an existing fail.

    A ``check`` (deterministic fail) verdict already blocks the send and must
    stay a fail; truncation only ever needs to convert a clean ``pass`` into a
    ``review`` so the unseen text gets human eyes. Mirrors the review_state
    contract so the surfaced overall_status/review_state stay consistent.
    """
    review_state = result.get("review_state")
    review_state = dict(review_state) if isinstance(review_state, Mapping) else {}
    current = str(review_state.get("state") or "").strip().lower()
    if current == REVIEW_STATE_CHECK:
        # A failing document already requires human action; leave the stronger
        # signal in place but still record that truncation forced a manual look.
        result["truncation_blocks_send"] = True
        return

    review_state.update({
        "state": REVIEW_STATE_REVIEW,
        "overall_status": "needs_review",
        "label": "REVIEW",
        "tone": "review",
        "requires_attention": True,
        "requires_human_review": True,
        "blocks_send": True,
        "blocks_auto_send": True,
        "truncation_forced_review": True,
        "truncation_reason": reason,
    })
    result["review_state"] = review_state
    result["overall_status"] = "needs_review"
    result["truncation_blocks_send"] = True


def configured_ai_assessment_reviewer(settings: Mapping[str, Any] | None = None) -> AIAssessmentReviewer:
    # Test seam only: when AI_ASSESSMENT_STUB_ENV is exported, run the deterministic
    # key-free stub instead of any real provider. Off by default in production.
    if os.environ.get(AI_ASSESSMENT_STUB_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return InMemoryAssessmentReviewer(response=stub_ai_assessment_response)
    config = dict(settings or _ai_review_settings())
    provider = str(config.get("provider") or "openrouter").strip().lower()
    timeout_seconds = int(config.get("timeout_seconds") or DEFAULT_AI_TIMEOUT_SECONDS)
    model = str(config.get("model") or "").strip()
    if provider == "openrouter":
        return OpenRouterAIAssessmentReviewer(
            api_key=_configured_api_key(provider),
            model=model or DEFAULT_OPENROUTER_MODEL,
            timeout_seconds=timeout_seconds,
        )
    raise AIAssessorError(f"Unsupported AI provider: {provider}")


def openrouter_ai_assessment_request_body(packet: Mapping[str, Any], *, model: str) -> dict[str, Any]:
    prompt = build_ai_assessment_prompt(packet)
    return {
        "model": _sanitize_model_name(model or DEFAULT_OPENROUTER_MODEL),
        "messages": [
            {
                "role": "system",
                "content": prompt["system"],
            },
            {
                "role": "user",
                "content": prompt["user"],
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


def _validate_ai_assessment_response(
    response: object,
    *,
    playbook: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    envelope_errors = _response_envelope_errors(response)
    if envelope_errors:
        raise AIAssessorError("AI assessment response could not be validated: " + "; ".join(envelope_errors))
    assert isinstance(response, Mapping)
    raw_assessments = response.get("assessments")
    assert isinstance(raw_assessments, list)
    playbook_clauses_by_id = {
        str(clause.get("id") or ""): clause
        for clause in playbook.get("clauses", [])
        if isinstance(clause, Mapping) and str(clause.get("id") or "")
    }
    clause_ids = list(playbook_clauses_by_id)
    packet_paragraphs = [
        paragraph
        for paragraph in packet.get("paragraphs", [])
        if isinstance(paragraph, dict)
    ]
    try:
        validate_ai_clause_assessments(
            raw_assessments,
            valid_clause_ids=clause_ids,
            paragraphs=packet_paragraphs,
            playbook_clauses_by_id=playbook_clauses_by_id,
        )
    except AIAssessmentContractError as error:
        raise AIAssessorError(str(error)) from error
    return raw_assessments


def _response_envelope_errors(response: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(response, Mapping):
        return ["response must be an object"]
    allowed_keys = {"assessments"}
    for key in response:
        if str(key) not in allowed_keys:
            errors.append(f"unsupported response field {key}")
    raw_assessments = response.get("assessments")
    if not isinstance(raw_assessments, list):
        errors.append("assessments must be a list")
    return errors


def _review_paragraphs(source_text: str, paragraphs: Sequence[Paragraph] | None) -> list[Paragraph]:
    if paragraphs is None:
        return split_document_paragraphs(source_text)
    if source_text:
        return align_document_paragraphs(list(paragraphs), source_text)
    return [deepcopy(paragraph) for paragraph in paragraphs]


def _parse_provider_response_text(response_text: str, *, provider: str) -> dict[str, Any] | None:
    if not response_text:
        raise AIAssessorError(f"{provider} API returned no message content.")
    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise AIAssessorError(f"{provider} API returned non-JSON text.") from error
    return parsed if isinstance(parsed, dict) else None


def _openrouter_response_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, Mapping):
        return ""
    message = first.get("message")
    if not isinstance(message, Mapping):
        return ""
    return str(message.get("content") or "").strip()
