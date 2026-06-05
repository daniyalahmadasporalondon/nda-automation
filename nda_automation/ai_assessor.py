from __future__ import annotations

import json
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
    AI_ASSESSMENT_RESPONSE_SCHEMA,
    build_ai_assessment_packet,
    build_ai_assessment_prompt,
)
from .ai_first_review import build_ai_first_review_result
from .ai_review import (
    DEFAULT_AI_TIMEOUT_SECONDS,
    DEFAULT_GEMINI_MODEL,
    GEMINI_ENDPOINT_TEMPLATE,
    _ai_review_settings,
    _configured_api_key,
    _gemini_response_text,
    _sanitize_model_name,
    _trusted_https_context,
)
from .checker import load_playbook, validate_playbook
from .gemini_schema import gemini_compatible_response_schema
from .review_document import Paragraph, align_document_paragraphs, split_document_paragraphs

AI_ASSESSOR_VERSION = 1
AI_FIRST_ASSESSOR_MODE = "ai_first_assessor"


class AIAssessorError(RuntimeError):
    pass


@runtime_checkable
class AIAssessmentReviewer(Protocol):
    def __call__(self, packet: dict[str, Any]) -> dict[str, Any] | None:
        ...


class GeminiAIAssessmentReviewer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        timeout_seconds: int = DEFAULT_AI_TIMEOUT_SECONDS,
    ) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise AIAssessorError("Gemini API key is not configured.")
        self.api_key = cleaned_key
        self.model = _sanitize_model_name(model or DEFAULT_GEMINI_MODEL)
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS))

    def __call__(self, packet: dict[str, Any]) -> dict[str, Any] | None:
        request = urllib.request.Request(
            GEMINI_ENDPOINT_TEMPLATE.format(model=self.model),
            data=json.dumps(gemini_ai_assessment_request_body(packet)).encode("utf-8"),
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
            raise AIAssessorError(f"Gemini API returned HTTP {error.code}: {message}") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise AIAssessorError(f"Gemini API request failed: {error}") from error
        return _parse_provider_response_text(_gemini_response_text(payload), provider="Gemini")


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
    result = build_ai_first_review_result(
        source,
        raw_assessments,
        paragraphs=document_paragraphs,
        checked_at=checked_at,
        playbook=review_playbook,
    )
    missing_clause_ids = list(result.get("ai_review", {}).get("missing_clause_ids", []))
    status = "partial" if missing_clause_ids else "completed"
    metadata = {
        "version": AI_ASSESSOR_VERSION,
        "status": status,
        "mode": AI_FIRST_ASSESSOR_MODE,
        "provider": str(settings["provider"]),
        "model": str(settings["model"]),
        "packet_version": AI_ASSESSMENT_PROMPT_VERSION,
        "assessment_contract_version": AI_ASSESSMENT_CONTRACT_VERSION,
        "record_count": len(raw_assessments),
        "missing_clause_ids": missing_clause_ids,
        "included_paragraph_count": packet["document"]["included_paragraph_count"],
        "omitted_paragraph_count": packet["document"]["omitted_paragraph_count"],
    }
    result["ai_first_review"] = {**dict(result.get("ai_first_review", {})), **metadata}
    result["ai_review"] = {**dict(result.get("ai_review", {})), **metadata}
    return result


def configured_ai_assessment_reviewer(settings: Mapping[str, Any] | None = None) -> AIAssessmentReviewer:
    config = dict(settings or _ai_review_settings())
    provider = str(config.get("provider") or "gemini").strip().lower()
    timeout_seconds = int(config.get("timeout_seconds") or DEFAULT_AI_TIMEOUT_SECONDS)
    model = str(config.get("model") or "").strip()
    if provider == "gemini":
        return GeminiAIAssessmentReviewer(
            api_key=_configured_api_key(provider),
            model=model or DEFAULT_GEMINI_MODEL,
            timeout_seconds=timeout_seconds,
        )
    raise AIAssessorError(f"Unsupported AI provider: {provider}")


def gemini_ai_assessment_request_body(packet: Mapping[str, Any]) -> dict[str, Any]:
    prompt = build_ai_assessment_prompt(packet)
    return {
        "systemInstruction": {"parts": [{"text": prompt["system"]}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt["user"]}],
        }],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": gemini_compatible_response_schema(AI_ASSESSMENT_RESPONSE_SCHEMA),
        },
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
    clause_ids = [
        str(clause.get("id") or "")
        for clause in playbook.get("clauses", [])
        if isinstance(clause, Mapping) and str(clause.get("id") or "")
    ]
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
