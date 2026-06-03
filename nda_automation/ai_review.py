from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Dict, Iterable, List, Protocol, Tuple, runtime_checkable

from . import app_settings
from .checks.common import ClauseResult, Paragraph
from .redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from .review_state import (
    CLAUSE_DECISION_FAIL,
    CLAUSE_DECISION_PASS,
    CLAUSE_DECISION_REVIEW,
)

AI_REVIEW_VERSION = 1
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"
DEFAULT_ALIBABA_MODEL = "qwen3.7-plus-2026-05-26"
DEFAULT_AI_REVIEW_THRESHOLD = 0.75
DEFAULT_AI_TIMEOUT_SECONDS = 20
MAX_AI_CONTEXT_PARAGRAPHS = 40
MAX_AI_CONTEXT_CHARS = 20000
AI_REVIEW_ENV_ENABLED = "NDA_AI_REVIEW_ENABLED"
AI_REVIEW_ENV_PROVIDER = "NDA_AI_PROVIDER"
AI_REVIEW_ENV_MODEL = "NDA_AI_MODEL"
AI_REVIEW_ENV_TIMEOUT = "NDA_AI_TIMEOUT_SECONDS"
AI_REVIEW_ENV_THRESHOLD = "NDA_AI_REVIEW_THRESHOLD"
AI_REVIEW_ENV_CLAUSES = "NDA_AI_REVIEW_CLAUSES"
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
ALIBABA_API_KEY_ENV = "ALIBABA_API_KEY"
DASHSCOPE_API_KEY_ENV = "DASHSCOPE_API_KEY"
GEMINI_ENDPOINT_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENROUTER_CHAT_COMPLETIONS_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
ALIBABA_CHAT_COMPLETIONS_ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
AI_REVIEW_CLAUSE_IDS = {
    "mutuality",
    "confidential_information",
    "governing_law",
    "term_and_survival",
    "non_circumvention",
}
@runtime_checkable
class AIReviewer(Protocol):
    """Public seam for AI semantic reviewers.

    A reviewer maps a review packet (from build_ai_review_packet) to a verdict
    dict matching AI_REVIEW_SCHEMA, or None when it has nothing usable to say.
    The provider adapters (Gemini / OpenRouter / Alibaba), the prod resolver
    (_configured_reviewer), and InMemoryReviewer all implement this interface;
    tests inject a reviewer through the reviewer=/ai_reviewer= parameter to cross
    the real seam instead of mocking app_settings. Plain functions with the same
    signature also satisfy it.
    """

    def __call__(self, packet: Dict[str, object]) -> Dict[str, object] | None:
        ...


# Back-compat alias for the historical callable type name.
AIReviewFn = AIReviewer

AI_REVIEW_SCHEMA: Dict[str, object] = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["pass", "fail", "review"],
            "description": "The semantic decision for this clause.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Confidence in the semantic decision from 0 to 1.",
        },
        "reason": {
            "type": "string",
            "description": "Concise legal reasoning tied to the cited text.",
        },
        "cited_spans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "paragraph_id": {"type": "string"},
                    "quote": {"type": "string"},
                    "relevance": {"type": "string"},
                },
                "required": ["paragraph_id", "quote", "relevance"],
                "additionalProperties": False,
            },
            "description": "Exact source quotes supporting the decision.",
        },
        "issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Issue labels found by the model, if any.",
        },
        "suggested_fix": {
            "type": "string",
            "description": "Suggested fix when decision is fail or review.",
        },
    },
    "required": ["decision", "confidence", "reason", "cited_spans", "issues", "suggested_fix"],
    "additionalProperties": False,
}


class AIReviewError(RuntimeError):
    pass


class GeminiAIReviewer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        timeout_seconds: int = DEFAULT_AI_TIMEOUT_SECONDS,
    ) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise AIReviewError("Gemini API key is not configured.")
        self.api_key = cleaned_key
        self.model = _sanitize_model_name(model or DEFAULT_GEMINI_MODEL)
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS))

    def __call__(self, packet: Dict[str, object]) -> Dict[str, object] | None:
        request = urllib.request.Request(
            GEMINI_ENDPOINT_TEMPLATE.format(model=self.model),
            data=json.dumps(_gemini_request_body(packet)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=_trusted_https_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:500]
            raise AIReviewError(f"Gemini API returned HTTP {error.code}: {message}") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise AIReviewError(f"Gemini API request failed: {error}") from error

        response_text = _gemini_response_text(payload)
        if not response_text:
            raise AIReviewError("Gemini API returned no text candidate.")
        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise AIReviewError("Gemini API returned non-JSON text.") from error
        return parsed if isinstance(parsed, dict) else None


class OpenRouterAIReviewer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_OPENROUTER_MODEL,
        timeout_seconds: int = DEFAULT_AI_TIMEOUT_SECONDS,
    ) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise AIReviewError("OpenRouter API key is not configured.")
        self.api_key = cleaned_key
        self.model = str(model or DEFAULT_OPENROUTER_MODEL).strip() or DEFAULT_OPENROUTER_MODEL
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS))

    def __call__(self, packet: Dict[str, object]) -> Dict[str, object] | None:
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
            data=json.dumps(_openrouter_request_body(packet, self.model)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "nda-automation",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=_trusted_https_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:500]
            raise AIReviewError(f"OpenRouter API returned HTTP {error.code}: {message}") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise AIReviewError(f"OpenRouter API request failed: {error}") from error

        response_text = _openrouter_response_text(payload)
        if not response_text:
            raise AIReviewError("OpenRouter API returned no message content.")
        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise AIReviewError("OpenRouter API returned non-JSON text.") from error
        return parsed if isinstance(parsed, dict) else None


class AlibabaAIReviewer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_ALIBABA_MODEL,
        timeout_seconds: int = DEFAULT_AI_TIMEOUT_SECONDS,
    ) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise AIReviewError("Alibaba API key is not configured.")
        self.api_key = cleaned_key
        self.model = str(model or DEFAULT_ALIBABA_MODEL).strip() or DEFAULT_ALIBABA_MODEL
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS))

    def __call__(self, packet: Dict[str, object]) -> Dict[str, object] | None:
        request = urllib.request.Request(
            ALIBABA_CHAT_COMPLETIONS_ENDPOINT,
            data=json.dumps(_alibaba_request_body(packet, self.model)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=_trusted_https_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:500]
            raise AIReviewError(f"Alibaba API returned HTTP {error.code}: {message}") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise AIReviewError(f"Alibaba API request failed: {error}") from error

        response_text = _chat_completion_response_text(payload)
        if not response_text:
            raise AIReviewError("Alibaba API returned no message content.")
        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise AIReviewError("Alibaba API returned non-JSON text.") from error
        return parsed if isinstance(parsed, dict) else None


class InMemoryReviewer:
    """In-memory ``AIReviewer`` adapter for tests.

    Crosses the real reviewer seam -- a built packet goes in, a verdict comes out
    -- so the request shaping (the packet) and the verdict-to-decision path (the
    arbiter) are exercised by the real pipeline, without a network call or
    mocking app_settings. Inject it through ``review_nda(..., ai_reviewer=...)``
    or ``apply_ai_review(..., reviewer=...)``.

    - ``responses``: clause id -> verdict dict, or a callable ``packet -> dict``.
    - ``default``: verdict (dict or callable) for clauses without a specific entry.
    - ``error``: when set, raised on every call to exercise the AI-error path.
    - ``packets``: every packet received, recorded for request-shape assertions.
    """

    def __init__(
        self,
        *,
        responses: Dict[str, object] | None = None,
        default: object | None = None,
        error: Exception | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.error = error
        self.packets: List[Dict[str, object]] = []

    def __call__(self, packet: Dict[str, object]) -> Dict[str, object] | None:
        self.packets.append(deepcopy(packet))
        if self.error is not None:
            raise self.error
        clause = packet.get("clause") if isinstance(packet.get("clause"), dict) else {}
        clause_id = str(clause.get("id") or "")
        response = self.responses.get(clause_id, self.default)
        if callable(response):
            return response(packet)
        return deepcopy(response) if isinstance(response, dict) else response


def apply_ai_review(
    *,
    clause_results: List[ClauseResult],
    clauses_by_id: Dict[str, Dict[str, object]],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object],
    reviewer: AIReviewFn | None = None,
    target_clause_ids: Iterable[str] | None = None,
) -> Tuple[List[ClauseResult], Dict[str, object]]:
    settings = _ai_review_settings()
    if reviewer is None and not settings["enabled"]:
        return clause_results, _summary(status="disabled", provider=settings["provider"], model=settings["model"])

    configured_reviewer = reviewer
    if configured_reviewer is None:
        try:
            configured_reviewer = _configured_reviewer(settings)
        except AIReviewError as error:
            return clause_results, _summary(
                status="configuration_error",
                provider=settings["provider"],
                model=settings["model"],
                error=str(error),
            )

    updated_results = [deepcopy(result) for result in clause_results]
    records: List[Dict[str, object]] = []
    if target_clause_ids is None:
        targeted_clause_ids = _targeted_clause_ids(settings)
    else:
        targeted_clause_ids = {
            str(clause_id).strip()
            for clause_id in target_clause_ids
            if str(clause_id).strip() in AI_REVIEW_CLAUSE_IDS
        }
    threshold = _confidence_threshold(settings)
    for clause in updated_results:
        clause_id = str(clause.get("id") or "")
        if clause_id not in targeted_clause_ids:
            continue
        playbook_clause = clauses_by_id.get(clause_id)
        if not isinstance(playbook_clause, dict):
            continue
        packet = build_ai_review_packet(
            clause=clause,
            playbook_clause=playbook_clause,
            paragraphs=paragraphs,
            review_context=review_context,
            provider=str(settings["provider"]),
            model=str(settings["model"]),
        )
        record = _evaluate_clause_with_ai(
            clause=clause,
            packet=packet,
            paragraphs=paragraphs,
            reviewer=configured_reviewer,
            threshold=threshold,
        )
        records.append(record)

    return updated_results, {
        "version": AI_REVIEW_VERSION,
        "status": "completed",
        "provider": str(settings["provider"]),
        "model": str(settings["model"]),
        "confidence_threshold": threshold,
        "record_count": len(records),
        "records": records,
    }


def validate_ai_draft_fix(
    *,
    clause: ClauseResult,
    playbook_clause: Dict[str, object],
    redline_edit: Dict[str, object],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object],
    reviewer: AIReviewFn | None = None,
) -> Dict[str, object]:
    settings = _ai_review_settings()
    if reviewer is None and not settings["enabled"]:
        return _summary(status="disabled", provider=settings["provider"], model=settings["model"])

    configured_reviewer = reviewer
    if configured_reviewer is None:
        try:
            configured_reviewer = _configured_reviewer(settings)
        except AIReviewError as error:
            return _summary(
                status="configuration_error",
                provider=settings["provider"],
                model=settings["model"],
                error=str(error),
            )

    threshold = _confidence_threshold(settings)
    validation_paragraphs = _draft_validation_paragraphs(redline_edit)
    validation_paragraphs.extend(_context_paragraphs(clause, paragraphs, review_context))
    validation_paragraphs = _fit_context_budget(_dedupe_paragraphs(validation_paragraphs))
    packet = build_ai_draft_fix_packet(
        clause=clause,
        playbook_clause=playbook_clause,
        redline_edit=redline_edit,
        validation_paragraphs=validation_paragraphs,
        provider=str(settings["provider"]),
        model=str(settings["model"]),
    )
    analysis = _evaluate_draft_fix_with_ai(
        packet=packet,
        validation_paragraphs=validation_paragraphs,
        reviewer=configured_reviewer,
        threshold=threshold,
    )
    record = {
        "clause_id": str(clause.get("id") or ""),
        "redline_id": str(redline_edit.get("id") or ""),
        "status": str(analysis.get("status") or ""),
        "ai_decision": str(analysis.get("ai_decision") or ""),
        "ai_confidence": analysis.get("ai_confidence"),
        "ai_reason": str(analysis.get("ai_reason") or ""),
        "reason": str(analysis.get("reason") or ""),
    }
    return {
        "version": AI_REVIEW_VERSION,
        "status": "completed",
        "mode": "draft_fix_validation",
        "provider": str(settings["provider"]),
        "model": str(settings["model"]),
        "confidence_threshold": threshold,
        "record_count": 1,
        "target_clause_id": str(clause.get("id") or ""),
        "redline_id": str(redline_edit.get("id") or ""),
        "validation": analysis,
        "records": [record],
    }


def build_ai_review_packet(
    *,
    clause: ClauseResult,
    playbook_clause: Dict[str, object],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object],
    provider: str,
    model: str,
) -> Dict[str, object]:
    context_paragraphs = _context_paragraphs(clause, paragraphs, review_context)
    return {
        "version": AI_REVIEW_VERSION,
        "provider": provider,
        "model": model,
        "task": "semantic_clause_crosscheck",
        "clause": {
            "id": str(clause.get("id") or ""),
            "name": str(clause.get("name") or playbook_clause.get("name") or ""),
            "type": str(playbook_clause.get("type") or ""),
            "requirement": str(clause.get("requirement") or playbook_clause.get("requirement") or ""),
            "preferred_standard": str(playbook_clause.get("preferred_standard") or ""),
            "check_trigger": str(playbook_clause.get("check_trigger") or ""),
            "rationale": str(playbook_clause.get("rationale") or ""),
            "evidence_guidance": str(playbook_clause.get("evidence_guidance") or ""),
        },
        "structure_context": clause.get("structure_context") if isinstance(clause.get("structure_context"), dict) else {},
        "paragraphs": [
            {
                "id": str(paragraph.get("id") or ""),
                "index": paragraph.get("index"),
                "text": str(paragraph.get("text") or ""),
            }
            for paragraph in context_paragraphs
        ],
        "instructions": [
            "You are an independent semantic reviewer. No prior automated or deterministic decision is supplied; judge the clause solely from the playbook requirement, structure context, and paragraphs below.",
            "Decide whether the clause satisfies the playbook requirement using only the supplied paragraphs.",
            "Return pass only when cited paragraphs affirmatively satisfy the requirement.",
            "Return fail when cited paragraphs show a prohibited or deficient clause.",
            "Return review when evidence is ambiguous, incomplete, conflicting, or depends on unavailable text.",
            "Every pass or fail decision must cite exact source quote text from the supplied paragraph ids.",
        ],
    }


def build_ai_draft_fix_packet(
    *,
    clause: ClauseResult,
    playbook_clause: Dict[str, object],
    redline_edit: Dict[str, object],
    validation_paragraphs: List[Paragraph],
    provider: str,
    model: str,
) -> Dict[str, object]:
    action = str(redline_edit.get("action") or "")
    proposed_text = _draft_proposed_text(redline_edit)
    return {
        "version": AI_REVIEW_VERSION,
        "provider": provider,
        "model": model,
        "task": "draft_fix_validation",
        "clause": {
            "id": str(clause.get("id") or ""),
            "name": str(clause.get("name") or playbook_clause.get("name") or ""),
            "type": str(playbook_clause.get("type") or ""),
            "requirement": str(clause.get("requirement") or playbook_clause.get("requirement") or ""),
            "preferred_standard": str(playbook_clause.get("preferred_standard") or ""),
            "check_trigger": str(playbook_clause.get("check_trigger") or ""),
            "rationale": str(playbook_clause.get("rationale") or ""),
            "evidence_guidance": str(playbook_clause.get("evidence_guidance") or ""),
        },
        "current_issue": {
            "decision": _deterministic_decision(clause),
            "status": str(clause.get("status") or ""),
            "reason": str(clause.get("reason") or clause.get("finding") or ""),
            "issue_type": str(clause.get("issue_type") or ""),
            "what_to_fix": str(clause.get("what_to_fix") or ""),
        },
        "proposed_draft": {
            "redline_id": str(redline_edit.get("id") or ""),
            "action": action,
            "action_label": str(redline_edit.get("action_label") or ""),
            "original_text": str(redline_edit.get("original_text") or ""),
            "anchor_text": str(redline_edit.get("anchor_text") or ""),
            "replacement_text": str(redline_edit.get("replacement_text") or ""),
            "insert_text": str(redline_edit.get("insert_text") or ""),
            "proposed_text": proposed_text,
        },
        "paragraphs": [
            {
                "id": str(paragraph.get("id") or ""),
                "index": paragraph.get("index"),
                "text": str(paragraph.get("text") or ""),
            }
            for paragraph in validation_paragraphs
        ],
        "instructions": [
            "Decide whether the proposed draft fix would satisfy the playbook requirement if applied.",
            "Return pass only when the proposed draft text, or deletion action, resolves the current issue.",
            "Return fail when the proposed draft remains deficient, unclear, or introduces prohibited language.",
            "Return review when the fix cannot be validated from the supplied draft and source context.",
            "Cite exact quote text from supplied paragraph ids; for deletion fixes, cite the deleted source text that the draft removes.",
        ],
    }


def ai_review_status() -> Dict[str, object]:
    settings = _ai_review_settings()
    stored = app_settings.ai_settings()
    provider = str(settings["provider"])
    api_key_source = _api_key_source(provider)
    return {
        "version": AI_REVIEW_VERSION,
        "enabled": bool(settings["enabled"]),
        "stored_enabled": stored.get("enabled"),
        "environment_enabled": _env_enabled(AI_REVIEW_ENV_ENABLED),
        "provider": str(settings["provider"]),
        "model": str(settings["model"]),
        "confidence_threshold": _confidence_threshold(settings),
        "api_key_configured": bool(_configured_api_key(provider)),
        "api_key_source": api_key_source,
        "target_clause_ids": sorted(_targeted_clause_ids(settings)),
    }


def _evaluate_clause_with_ai(
    *,
    clause: ClauseResult,
    packet: Dict[str, object],
    paragraphs: List[Paragraph],
    reviewer: AIReviewFn,
    threshold: float,
) -> Dict[str, object]:
    deterministic_decision = _deterministic_decision(clause)
    try:
        raw_response = reviewer(packet)
    except Exception as error:
        analysis = _ai_analysis(
            status="error",
            deterministic_decision=deterministic_decision,
            reason=str(error),
            threshold=threshold,
        )
        clause["ai_review_analysis"] = analysis
        return _record_from_analysis(clause, analysis)

    validation = _validate_ai_response(raw_response, paragraphs)
    if not validation["valid"]:
        analysis = _ai_analysis(
            status="invalid",
            deterministic_decision=deterministic_decision,
            ai_response=validation["response"],
            reason="AI response could not be validated: " + "; ".join(validation["errors"]),
            validation_errors=validation["errors"],
            threshold=threshold,
        )
        _attach_ai_analysis(clause, analysis, "ai_citation_validation_failed")
        return _record_from_analysis(clause, analysis)

    ai_response = validation["response"]
    ai_decision = str(ai_response["decision"])
    confidence = float(ai_response["confidence"])
    if confidence < threshold:
        analysis = _ai_analysis(
            status="low_confidence",
            deterministic_decision=deterministic_decision,
            ai_response=ai_response,
            reason=f"AI confidence {confidence:.2f} is below the review threshold of {threshold:.2f}.",
            threshold=threshold,
        )
        _attach_ai_analysis(clause, analysis, "ai_confidence_below_threshold")
        return _record_from_analysis(clause, analysis)

    if ai_decision != deterministic_decision:
        analysis = _ai_analysis(
            status="disagreement",
            deterministic_decision=deterministic_decision,
            ai_response=ai_response,
            reason=(
                "AI semantic review disagreed with the deterministic checker; "
                "human review is required before clearing or redlining this clause."
            ),
            threshold=threshold,
        )
        _attach_ai_analysis(clause, analysis, "ai_semantic_disagreement")
        return _record_from_analysis(clause, analysis)

    analysis = _ai_analysis(
        status="confirmed",
        deterministic_decision=deterministic_decision,
        ai_response=ai_response,
        reason=str(ai_response.get("reason") or "AI semantic review confirmed the deterministic decision."),
        threshold=threshold,
    )
    clause["ai_review_analysis"] = analysis
    return _record_from_analysis(clause, analysis)


def _evaluate_draft_fix_with_ai(
    *,
    packet: Dict[str, object],
    validation_paragraphs: List[Paragraph],
    reviewer: AIReviewFn,
    threshold: float,
) -> Dict[str, object]:
    expected_decision = CLAUSE_DECISION_PASS
    try:
        raw_response = reviewer(packet)
    except Exception as error:
        return _ai_analysis(
            status="error",
            deterministic_decision=expected_decision,
            reason=str(error),
            threshold=threshold,
        )

    validation = _validate_ai_response(raw_response, validation_paragraphs)
    if not validation["valid"]:
        return _ai_analysis(
            status="invalid",
            deterministic_decision=expected_decision,
            ai_response=validation["response"],
            reason="AI draft validation response could not be validated: " + "; ".join(validation["errors"]),
            validation_errors=validation["errors"],
            threshold=threshold,
        )

    ai_response = validation["response"]
    ai_decision = str(ai_response["decision"])
    confidence = float(ai_response["confidence"])
    if confidence < threshold:
        return _ai_analysis(
            status="low_confidence",
            deterministic_decision=expected_decision,
            ai_response=ai_response,
            reason=f"AI confidence {confidence:.2f} is below the draft-validation threshold of {threshold:.2f}.",
            threshold=threshold,
        )

    if ai_decision != CLAUSE_DECISION_PASS:
        return _ai_analysis(
            status="needs_revision",
            deterministic_decision=expected_decision,
            ai_response=ai_response,
            reason="AI draft validation did not clear the proposed fix; revise or human-review the draft language.",
            threshold=threshold,
        )

    return _ai_analysis(
        status="validated",
        deterministic_decision=expected_decision,
        ai_response=ai_response,
        reason=str(ai_response.get("reason") or "AI draft validation confirmed the proposed fix."),
        threshold=threshold,
    )


def _attach_ai_analysis(clause: ClauseResult, analysis: Dict[str, object], reason_code: str) -> None:
    # AI is an escalate-only overlay. Record the verdict (with its reason code)
    # for the DecisionArbiter and the audit trace, but never mutate the clause's
    # deterministic decision here -- the arbiter owns precedence and applies the
    # fail-floor (AI may escalate a pass to review, never soften a fail/review).
    # In particular do NOT write clause["semantic_confidence"]: that field feeds
    # the deterministic confidence rule and must not carry the AI's confidence.
    analysis["reason_code"] = reason_code
    clause["ai_review_analysis"] = analysis


def _validate_ai_response(response: object, paragraphs: List[Paragraph]) -> Dict[str, object]:
    errors: List[str] = []
    cleaned: Dict[str, object] = {}
    if not isinstance(response, dict):
        return {"valid": False, "errors": ["response is not an object"], "response": cleaned}

    decision = str(response.get("decision") or "").strip().lower()
    if decision not in {CLAUSE_DECISION_PASS, CLAUSE_DECISION_FAIL, CLAUSE_DECISION_REVIEW}:
        errors.append("decision must be pass, fail, or review")
    cleaned["decision"] = decision

    try:
        confidence = float(response.get("confidence"))
    except (TypeError, ValueError):
        confidence = -1.0
    if confidence < 0 or confidence > 1:
        errors.append("confidence must be between 0 and 1")
    cleaned["confidence"] = confidence

    reason = str(response.get("reason") or "").strip()
    if not reason:
        errors.append("reason is required")
    cleaned["reason"] = reason

    paragraph_by_id = {str(paragraph.get("id") or ""): str(paragraph.get("text") or "") for paragraph in paragraphs}
    cleaned_spans: List[Dict[str, object]] = []
    raw_spans = response.get("cited_spans")
    if not isinstance(raw_spans, list):
        errors.append("cited_spans must be a list")
        raw_spans = []
    for span in raw_spans[:8]:
        if not isinstance(span, dict):
            errors.append("cited span must be an object")
            continue
        paragraph_id = str(span.get("paragraph_id") or "").strip()
        quote = str(span.get("quote") or "").strip()
        if paragraph_id not in paragraph_by_id:
            errors.append(f"cited paragraph id does not exist: {paragraph_id}")
            continue
        if not quote:
            errors.append(f"cited quote is empty for paragraph {paragraph_id}")
            continue
        if not _quote_appears_in_text(quote, paragraph_by_id[paragraph_id]):
            errors.append(f"cited quote does not appear in paragraph {paragraph_id}")
            continue
        cleaned_spans.append({
            "paragraph_id": paragraph_id,
            "quote": quote,
            "relevance": str(span.get("relevance") or "").strip(),
        })
    if decision in {CLAUSE_DECISION_PASS, CLAUSE_DECISION_FAIL} and not cleaned_spans:
        errors.append("pass/fail decisions require at least one valid cited span")
    cleaned["cited_spans"] = cleaned_spans

    issues = response.get("issues")
    cleaned["issues"] = [str(issue).strip() for issue in issues if str(issue).strip()] if isinstance(issues, list) else []
    cleaned["suggested_fix"] = str(response.get("suggested_fix") or "").strip()
    return {"valid": not errors, "errors": errors, "response": cleaned}


def _draft_validation_paragraphs(redline_edit: Dict[str, object]) -> List[Paragraph]:
    redline_id = str(redline_edit.get("id") or "draft").strip() or "draft"
    action = str(redline_edit.get("action") or "")
    original_text = str(redline_edit.get("original_text") or "").strip()
    anchor_text = str(redline_edit.get("anchor_text") or "").strip()
    proposed_text = _draft_proposed_text(redline_edit)
    paragraphs: List[Paragraph] = []
    if action == REDLINE_DELETE_PARAGRAPH:
        text = original_text or anchor_text
        if text:
            paragraphs.append({
                "id": f"draft-action-{redline_id}",
                "index": 0,
                "text": f"Draft action deletes this source paragraph: {text}",
            })
        return paragraphs

    if proposed_text:
        paragraphs.append({
            "id": f"draft-proposed-{redline_id}",
            "index": 0,
            "text": proposed_text,
        })
    if original_text:
        paragraphs.append({
            "id": f"draft-original-{redline_id}",
            "index": 0,
            "text": original_text,
        })
    elif anchor_text:
        paragraphs.append({
            "id": f"draft-anchor-{redline_id}",
            "index": 0,
            "text": anchor_text,
        })
    return paragraphs


def _draft_proposed_text(redline_edit: Dict[str, object]) -> str:
    action = str(redline_edit.get("action") or "")
    if action == REDLINE_INSERT_AFTER_PARAGRAPH:
        return str(redline_edit.get("insert_text") or redline_edit.get("replacement_text") or "").strip()
    if action == REDLINE_REPLACE_PARAGRAPH:
        return str(redline_edit.get("replacement_text") or "").strip()
    return str(redline_edit.get("replacement_text") or redline_edit.get("insert_text") or "").strip()


def _ai_analysis(
    *,
    status: str,
    deterministic_decision: str,
    reason: str,
    threshold: float,
    ai_response: object | None = None,
    validation_errors: List[str] | None = None,
) -> Dict[str, object]:
    response = ai_response if isinstance(ai_response, dict) else {}
    ai_decision = str(response.get("decision") or "") if response else ""
    confidence = _ai_response_confidence(response)
    return {
        "version": AI_REVIEW_VERSION,
        "status": status,
        "deterministic_decision": deterministic_decision,
        "ai_decision": ai_decision,
        "ai_confidence": confidence,
        "ai_reason": str(response.get("reason") or "") if response else "",
        "confidence_threshold": threshold,
        "disagreement": bool(ai_decision and ai_decision != deterministic_decision),
        "reason": reason,
        "cited_spans": response.get("cited_spans", []) if response else [],
        "issues": response.get("issues", []) if response else [],
        "suggested_fix": str(response.get("suggested_fix") or "") if response else "",
        "validation_errors": validation_errors or [],
    }


def _record_from_analysis(clause: ClauseResult, analysis: Dict[str, object]) -> Dict[str, object]:
    return {
        "clause_id": str(clause.get("id") or ""),
        "status": str(analysis.get("status") or ""),
        "deterministic_decision": str(analysis.get("deterministic_decision") or ""),
        "ai_decision": str(analysis.get("ai_decision") or ""),
        "ai_confidence": analysis.get("ai_confidence"),
        "ai_reason": str(analysis.get("ai_reason") or ""),
        "disagreement": bool(analysis.get("disagreement")),
        "reason": str(analysis.get("reason") or ""),
    }


def _configured_reviewer(settings: Dict[str, object]) -> AIReviewFn:
    provider = str(settings["provider"]).strip().lower()
    if provider == "gemini":
        return GeminiAIReviewer(
            api_key=_configured_api_key(provider),
            model=str(settings["model"]),
            timeout_seconds=int(settings["timeout_seconds"]),
        )
    if provider == "openrouter":
        return OpenRouterAIReviewer(
            api_key=_configured_api_key(provider),
            model=str(settings["model"]),
            timeout_seconds=int(settings["timeout_seconds"]),
        )
    if provider == "alibaba":
        return AlibabaAIReviewer(
            api_key=_configured_api_key(provider),
            model=str(settings["model"]),
            timeout_seconds=int(settings["timeout_seconds"]),
        )
    raise AIReviewError(f"Unsupported AI provider: {provider}")


def provider_for_api_key(api_key: str) -> str:
    cleaned_key = str(api_key or "").strip()
    if _looks_like_openrouter_key(cleaned_key):
        return "openrouter"
    if _looks_like_alibaba_key(cleaned_key):
        return "alibaba"
    return "gemini"


def default_model_for_provider(provider: str) -> str:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider == "openrouter":
        return DEFAULT_OPENROUTER_MODEL
    if normalized_provider == "alibaba":
        return DEFAULT_ALIBABA_MODEL
    return DEFAULT_GEMINI_MODEL


def _configured_api_key(provider: str) -> str:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider == "openrouter":
        return os.environ.get(OPENROUTER_API_KEY_ENV, "").strip() or _stored_key_for_provider("openrouter")
    if normalized_provider == "alibaba":
        return (
            os.environ.get(ALIBABA_API_KEY_ENV, "").strip()
            or os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip()
            or _stored_key_for_provider("alibaba")
        )
    return os.environ.get(GEMINI_API_KEY_ENV, "").strip() or _stored_key_for_provider("gemini")


def _api_key_source(provider: str) -> str:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider == "openrouter" and os.environ.get(OPENROUTER_API_KEY_ENV, "").strip():
        return "environment"
    if normalized_provider == "alibaba" and (
        os.environ.get(ALIBABA_API_KEY_ENV, "").strip() or os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip()
    ):
        return "environment"
    if normalized_provider == "gemini" and os.environ.get(GEMINI_API_KEY_ENV, "").strip():
        return "environment"
    if _stored_key_for_provider(normalized_provider):
        return "local_settings"
    return ""


def _stored_key_for_provider(provider: str) -> str:
    stored_key = app_settings.stored_ai_api_key()
    if not stored_key:
        return ""
    stored_provider = provider_for_api_key(stored_key)
    return stored_key if stored_provider == str(provider).strip().lower() else ""


def _ai_review_settings() -> Dict[str, object]:
    stored = app_settings.ai_settings()
    stored_enabled = stored.get("enabled")
    env_enabled = _env_enabled(AI_REVIEW_ENV_ENABLED)
    provider = _configured_provider(stored)
    return {
        "enabled": stored_enabled if isinstance(stored_enabled, bool) else env_enabled,
        "provider": provider,
        "model": _configured_model(provider, stored),
        "timeout_seconds": _env_int(AI_REVIEW_ENV_TIMEOUT, DEFAULT_AI_TIMEOUT_SECONDS),
        "confidence_threshold": _env_float(AI_REVIEW_ENV_THRESHOLD, DEFAULT_AI_REVIEW_THRESHOLD),
        "clause_ids": os.environ.get(AI_REVIEW_ENV_CLAUSES, ""),
    }


def _configured_provider(stored: Dict[str, object]) -> str:
    env_provider = os.environ.get(AI_REVIEW_ENV_PROVIDER, "").strip().lower()
    if env_provider in {"gemini", "openrouter", "alibaba"}:
        return env_provider
    stored_provider = str(stored.get("provider") or "").strip().lower()
    if stored_provider in {"gemini", "openrouter", "alibaba"}:
        return stored_provider
    if os.environ.get(ALIBABA_API_KEY_ENV, "").strip() or os.environ.get(DASHSCOPE_API_KEY_ENV, "").strip():
        return "alibaba"
    if os.environ.get(OPENROUTER_API_KEY_ENV, "").strip():
        return "openrouter"
    if _looks_like_alibaba_key(app_settings.stored_ai_api_key()):
        return "alibaba"
    if _looks_like_openrouter_key(app_settings.stored_ai_api_key()):
        return "openrouter"
    return "gemini"


def _configured_model(provider: str, stored: Dict[str, object]) -> str:
    env_model = os.environ.get(AI_REVIEW_ENV_MODEL, "").strip()
    if env_model:
        return env_model
    stored_model = str(stored.get("model") or "").strip()
    if stored_model:
        return stored_model
    return default_model_for_provider(provider)


def _looks_like_openrouter_key(api_key: str) -> bool:
    return str(api_key or "").strip().startswith("sk-or-")


def _looks_like_alibaba_key(api_key: str) -> bool:
    cleaned = str(api_key or "").strip()
    return cleaned.startswith("sk-ws-") or (cleaned.startswith("sk-") and not cleaned.startswith("sk-or-"))


def _summary(
    *,
    status: str,
    provider: str,
    model: str,
    error: str = "",
) -> Dict[str, object]:
    summary = {
        "version": AI_REVIEW_VERSION,
        "status": status,
        "provider": provider,
        "model": model,
        "record_count": 0,
        "records": [],
    }
    if error:
        summary["error"] = error
    return summary


def _gemini_request_body(packet: Dict[str, object]) -> Dict[str, object]:
    prompt = json.dumps(packet, ensure_ascii=False, indent=2)
    return {
        "systemInstruction": {
            "parts": [{
                "text": (
                    "You are a legal QA semantic reviewer for NDA hard-clause checks. "
                    "Use only supplied paragraph text. Do not invent document terms. "
                    "Return only schema-valid JSON."
                )
            }]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}],
        }],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": AI_REVIEW_SCHEMA,
        },
    }


def _openrouter_request_body(packet: Dict[str, object], model: str) -> Dict[str, object]:
    prompt = json.dumps(packet, ensure_ascii=False, indent=2)
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a legal QA semantic reviewer for NDA hard-clause checks. "
                    "Use only supplied paragraph text. Do not invent document terms. "
                    "Return only schema-valid JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "nda_clause_semantic_review",
                "strict": True,
                "schema": AI_REVIEW_SCHEMA,
            },
        },
    }


def _alibaba_request_body(packet: Dict[str, object], model: str) -> Dict[str, object]:
    prompt = json.dumps(packet, ensure_ascii=False, indent=2)
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a legal QA semantic reviewer for NDA hard-clause checks. "
                    "Use only supplied paragraph text. Do not invent document terms. "
                    "Return only JSON matching this schema: "
                    + json.dumps(AI_REVIEW_SCHEMA, ensure_ascii=False)
                ),
            },
            {"role": "user", "content": "Return schema-valid JSON for this review packet:\n" + prompt},
        ],
        "temperature": 0,
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }


def _openrouter_response_text(payload: Dict[str, object]) -> str:
    return _chat_completion_response_text(payload)


def _chat_completion_response_text(payload: Dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {None, "text"}
        ]
        return "".join(parts).strip()
    return ""


def _trusted_https_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return ssl.create_default_context(cafile=certifi.where())
    except (OSError, ssl.SSLError):
        return None


def _gemini_response_text(payload: Dict[str, object]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    first = candidates[0]
    if not isinstance(first, dict):
        return ""
    content = first.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    text_parts = [str(part.get("text") or "") for part in parts if isinstance(part, dict)]
    return "".join(text_parts).strip()


def _context_paragraphs(
    clause: ClauseResult,
    paragraphs: List[Paragraph],
    review_context: Dict[str, object],
) -> List[Paragraph]:
    paragraph_by_id = {str(paragraph.get("id") or ""): paragraph for paragraph in paragraphs}
    selected_ids: List[str] = []
    matched_paragraph_ids = clause.get("matched_paragraph_ids", [])
    if isinstance(matched_paragraph_ids, list):
        selected_ids.extend(str(paragraph_id) for paragraph_id in matched_paragraph_ids if str(paragraph_id))

    structure_context = clause.get("structure_context")
    if isinstance(structure_context, dict):
        sections = structure_context.get("sections", [])
        if isinstance(sections, list):
            for section in sections:
                if not isinstance(section, dict):
                    continue
                section_paragraph_ids = section.get("paragraph_ids", [])
                if isinstance(section_paragraph_ids, list):
                    selected_ids.extend(
                        str(paragraph_id)
                        for paragraph_id in section_paragraph_ids
                        if str(paragraph_id)
                    )
        selected_ids.extend(_paragraph_ids_for_concepts(structure_context.get("concepts", []), review_context))

    selected = _dedupe_paragraphs([paragraph_by_id[paragraph_id] for paragraph_id in selected_ids if paragraph_id in paragraph_by_id])
    if len(selected) < 8:
        selected = _dedupe_paragraphs([*selected, *paragraphs])
    return _fit_context_budget(selected)


def _paragraph_ids_for_concepts(concepts: object, review_context: Dict[str, object]) -> List[str]:
    if not isinstance(concepts, list):
        return []
    concept_set = {str(concept) for concept in concepts if str(concept)}
    classifier = review_context.get("concept_classifier") if isinstance(review_context, dict) else None
    if not concept_set or not isinstance(classifier, dict):
        return []
    concepts_by_paragraph = classifier.get("concepts_by_paragraph_id")
    if not isinstance(concepts_by_paragraph, dict):
        return []
    paragraph_ids: List[str] = []
    for paragraph_id, paragraph_concepts in concepts_by_paragraph.items():
        if not isinstance(paragraph_concepts, list):
            continue
        if concept_set.intersection(str(concept) for concept in paragraph_concepts):
            paragraph_ids.append(str(paragraph_id))
    return paragraph_ids


def _fit_context_budget(paragraphs: List[Paragraph]) -> List[Paragraph]:
    fitted: List[Paragraph] = []
    char_count = 0
    for paragraph in paragraphs[:MAX_AI_CONTEXT_PARAGRAPHS]:
        text = str(paragraph.get("text") or "")
        if char_count + len(text) > MAX_AI_CONTEXT_CHARS and fitted:
            break
        fitted.append(paragraph)
        char_count += len(text)
    return fitted


def _dedupe_paragraphs(paragraphs: Iterable[Paragraph]) -> List[Paragraph]:
    deduped: List[Paragraph] = []
    seen = set()
    for paragraph in paragraphs:
        paragraph_id = str(paragraph.get("id") or "")
        key = paragraph_id or (paragraph.get("start"), paragraph.get("end"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(paragraph)
    return deduped


def _deterministic_decision(clause: ClauseResult) -> str:
    explicit = str(clause.get("decision") or "").strip().lower()
    if explicit in {CLAUSE_DECISION_PASS, CLAUSE_DECISION_FAIL, CLAUSE_DECISION_REVIEW}:
        return explicit
    if clause.get("needs_review"):
        return CLAUSE_DECISION_REVIEW
    if not clause.get("passes"):
        return CLAUSE_DECISION_FAIL
    return CLAUSE_DECISION_PASS


def _ai_response_confidence(response: object) -> float | None:
    if not isinstance(response, dict) or response.get("confidence") is None:
        return None
    try:
        return float(response["confidence"])
    except (TypeError, ValueError):
        return None


def _quote_appears_in_text(quote: str, text: str) -> bool:
    normalized_quote = _normalize_quote_text(quote)
    normalized_text = _normalize_quote_text(text)
    return bool(normalized_quote and normalized_quote in normalized_text)


def _normalize_quote_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _targeted_clause_ids(settings: Dict[str, object]) -> set[str]:
    raw_clause_ids = str(settings.get("clause_ids") or "").strip()
    if not raw_clause_ids:
        return set(AI_REVIEW_CLAUSE_IDS)
    return {
        value.strip()
        for value in raw_clause_ids.split(",")
        if value.strip() in AI_REVIEW_CLAUSE_IDS
    }


def _confidence_threshold(settings: Dict[str, object]) -> float:
    raw_threshold = settings.get("confidence_threshold")
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError):
        threshold = DEFAULT_AI_REVIEW_THRESHOLD
    return min(1.0, max(0.0, threshold))


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, fallback: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return fallback


def _env_float(name: str, fallback: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return fallback


def _sanitize_model_name(model: str) -> str:
    cleaned = str(model or DEFAULT_GEMINI_MODEL).strip().removeprefix("models/")
    # The model name is interpolated into the Gemini endpoint URL path, so
    # restrict it to a safe allowlist to prevent path/query injection if a
    # set-model route is ever added.
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", cleaned)
    return cleaned or DEFAULT_GEMINI_MODEL
