from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Callable, Dict, Iterable, List, Tuple

from . import app_settings
from .checks.common import ClauseResult, Paragraph
from .review_state import (
    CLAUSE_DECISION_FAIL,
    CLAUSE_DECISION_PASS,
    CLAUSE_DECISION_REVIEW,
)

AI_REVIEW_VERSION = 1
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
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
GEMINI_ENDPOINT_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
AI_REVIEW_CLAUSE_IDS = {
    "mutuality",
    "confidential_information",
    "governing_law",
    "term_and_survival",
    "non_circumvention",
}
AIReviewFn = Callable[[Dict[str, object]], Dict[str, object] | None]

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
                "required": ["paragraph_id", "quote"],
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
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
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


def apply_ai_review(
    *,
    clause_results: List[ClauseResult],
    clauses_by_id: Dict[str, Dict[str, object]],
    paragraphs: List[Paragraph],
    review_context: Dict[str, object],
    reviewer: AIReviewFn | None = None,
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
    targeted_clause_ids = _targeted_clause_ids(settings)
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
    deterministic_decision = _deterministic_decision(clause)
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
        "deterministic_result": {
            "decision": deterministic_decision,
            "status": str(clause.get("status") or ""),
            "passes": bool(clause.get("passes")),
            "needs_review": bool(clause.get("needs_review")),
            "reason": str(clause.get("reason") or clause.get("finding") or ""),
            "issue_type": str(clause.get("issue_type") or ""),
            "what_to_fix": str(clause.get("what_to_fix") or ""),
            "matched_paragraph_ids": [
                str(paragraph_id)
                for paragraph_id in clause.get("matched_paragraph_ids", [])
                if str(paragraph_id)
            ] if isinstance(clause.get("matched_paragraph_ids"), list) else [],
        },
        "structure_context": clause.get("structure_context") if isinstance(clause.get("structure_context"), dict) else {},
        "analysis_objects": _clause_analysis_objects(clause),
        "paragraphs": [
            {
                "id": str(paragraph.get("id") or ""),
                "index": paragraph.get("index"),
                "text": str(paragraph.get("text") or ""),
            }
            for paragraph in context_paragraphs
        ],
        "instructions": [
            "Decide whether the clause satisfies the playbook requirement using only the supplied paragraphs.",
            "Return pass only when cited paragraphs affirmatively satisfy the requirement.",
            "Return fail when cited paragraphs show a prohibited or deficient clause.",
            "Return review when evidence is ambiguous, incomplete, conflicting, or depends on unavailable text.",
            "Every pass or fail decision must cite exact source quote text from the supplied paragraph ids.",
        ],
    }


def ai_review_status() -> Dict[str, object]:
    settings = _ai_review_settings()
    stored = app_settings.ai_settings()
    api_key_source = _gemini_api_key_source()
    return {
        "version": AI_REVIEW_VERSION,
        "enabled": bool(settings["enabled"]),
        "stored_enabled": stored.get("enabled"),
        "environment_enabled": _env_enabled(AI_REVIEW_ENV_ENABLED),
        "provider": str(settings["provider"]),
        "model": str(settings["model"]),
        "confidence_threshold": _confidence_threshold(settings),
        "api_key_configured": bool(_gemini_api_key()),
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
        _attach_ai_analysis(clause, analysis, "ai_review_unavailable")
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


def _attach_ai_analysis(clause: ClauseResult, analysis: Dict[str, object], reason_code: str) -> None:
    clause["ai_review_analysis"] = analysis
    clause["decision"] = CLAUSE_DECISION_REVIEW
    clause["needs_review"] = True
    clause["review_reason"] = str(analysis.get("reason") or "AI semantic review requires human review.")
    clause["decision_reason"] = str(clause["review_reason"])
    clause["reason_code"] = reason_code
    clause["reason_codes"] = [reason_code]
    confidence = analysis.get("ai_confidence")
    if confidence is not None:
        clause["semantic_confidence"] = confidence


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
        "disagreement": bool(analysis.get("disagreement")),
        "reason": str(analysis.get("reason") or ""),
    }


def _configured_reviewer(settings: Dict[str, object]) -> AIReviewFn:
    provider = str(settings["provider"]).strip().lower()
    if provider != "gemini":
        raise AIReviewError(f"Unsupported AI provider: {provider}")
    return GeminiAIReviewer(
        api_key=_gemini_api_key(),
        model=str(settings["model"]),
        timeout_seconds=int(settings["timeout_seconds"]),
    )


def _gemini_api_key() -> str:
    return os.environ.get(GEMINI_API_KEY_ENV, "").strip() or app_settings.stored_ai_api_key()


def _gemini_api_key_source() -> str:
    if os.environ.get(GEMINI_API_KEY_ENV, "").strip():
        return "environment"
    if app_settings.stored_ai_api_key():
        return "local_settings"
    return ""


def _ai_review_settings() -> Dict[str, object]:
    stored = app_settings.ai_settings()
    stored_enabled = stored.get("enabled")
    env_enabled = _env_enabled(AI_REVIEW_ENV_ENABLED)
    return {
        "enabled": stored_enabled if isinstance(stored_enabled, bool) else env_enabled,
        "provider": os.environ.get(AI_REVIEW_ENV_PROVIDER, "gemini").strip().lower() or "gemini",
        "model": os.environ.get(AI_REVIEW_ENV_MODEL, DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL,
        "timeout_seconds": _env_int(AI_REVIEW_ENV_TIMEOUT, DEFAULT_AI_TIMEOUT_SECONDS),
        "confidence_threshold": _env_float(AI_REVIEW_ENV_THRESHOLD, DEFAULT_AI_REVIEW_THRESHOLD),
        "clause_ids": os.environ.get(AI_REVIEW_ENV_CLAUSES, ""),
    }


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


def _clause_analysis_objects(clause: ClauseResult) -> Dict[str, object]:
    return {
        str(key): value
        for key, value in clause.items()
        if key.endswith("_analysis") and isinstance(value, dict)
    }


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
    cleaned = str(model or DEFAULT_GEMINI_MODEL).strip()
    return cleaned.removeprefix("models/")
