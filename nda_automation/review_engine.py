from __future__ import annotations

import os
from copy import deepcopy
from collections.abc import Callable, Sequence
from typing import Any

from . import app_settings
from .ai_assessor import AIAssessorError, assess_nda_with_ai
from .checker import review_nda
from .review_document import Paragraph
from . import telemetry

ACTIVE_REVIEW_ENGINE_ENV = "NDA_ACTIVE_REVIEW_ENGINE"
AI_FIRST_FALLBACK_MODE_ENV = "NDA_AI_FIRST_FALLBACK_MODE"
REVIEW_ENGINE_DETERMINISTIC = "deterministic"
REVIEW_ENGINE_AI_FIRST = "ai_first"
FALLBACK_MODE_DETERMINISTIC = "deterministic"
FALLBACK_MODE_FAIL_CLOSED = "fail_closed"
DEFAULT_ACTIVE_REVIEW_ENGINE = REVIEW_ENGINE_AI_FIRST
DEFAULT_AI_FIRST_FALLBACK_MODE = FALLBACK_MODE_FAIL_CLOSED
REVIEW_ENGINE_SOURCE_DEFAULT = "default"
REVIEW_ENGINE_SOURCE_ENVIRONMENT = "environment"
REVIEW_ENGINE_SOURCE_RUNTIME_SETTINGS = "runtime_settings"


class ActiveReviewEngineError(RuntimeError):
    pass


ReviewEngineFn = Callable[..., dict[str, Any]]


def active_review_engine() -> str:
    return _active_review_engine_config()["value"]


def ai_first_fallback_mode() -> str:
    return _ai_first_fallback_mode_config()["value"]


def active_review_engine_status() -> dict[str, Any]:
    engine_config = _active_review_engine_config()
    fallback_config = _ai_first_fallback_mode_config()
    stored_settings = app_settings.review_runtime_settings()
    return {
        "active_engine": engine_config["value"],
        "engine_source": engine_config["source"],
        "engine_source_key": engine_config["source_key"],
        "ai_first_fallback_mode": fallback_config["value"],
        "fallback_source": fallback_config["source"],
        "fallback_source_key": fallback_config["source_key"],
        "stored_active_engine": stored_settings.get("active_review_engine"),
        "stored_ai_first_fallback_mode": stored_settings.get("ai_first_fallback_mode"),
        "environment_active_engine": _normalized_active_review_engine(os.environ.get(ACTIVE_REVIEW_ENGINE_ENV, "")),
        "environment_ai_first_fallback_mode": _normalized_ai_first_fallback_mode(os.environ.get(AI_FIRST_FALLBACK_MODE_ENV, "")),
        "supported_engines": [REVIEW_ENGINE_DETERMINISTIC, REVIEW_ENGINE_AI_FIRST],
        "supported_ai_first_fallback_modes": [FALLBACK_MODE_DETERMINISTIC, FALLBACK_MODE_FAIL_CLOSED],
    }


def review_nda_with_active_engine(
    text: str,
    *,
    paragraphs: Sequence[Paragraph] | None = None,
    deterministic_review_func: ReviewEngineFn = review_nda,
    ai_first_review_func: ReviewEngineFn = assess_nda_with_ai,
) -> dict[str, Any]:
    engine_config = _active_review_engine_config()
    fallback_config = _ai_first_fallback_mode_config()
    selected_engine = engine_config["value"]
    fallback_mode = fallback_config["value"]
    if selected_engine != REVIEW_ENGINE_AI_FIRST:
        telemetry.increment("active_review_deterministic_completed")
        result = deterministic_review_func(text, paragraphs=paragraphs)
        return _with_active_engine_metadata(
            result,
            selected_engine=REVIEW_ENGINE_DETERMINISTIC,
            executed_engine=REVIEW_ENGINE_DETERMINISTIC,
            status="completed",
            fallback_mode=fallback_mode,
            engine_source=engine_config["source"],
            fallback_source=fallback_config["source"],
        )

    telemetry.increment("active_review_ai_first_attempted")
    try:
        result = ai_first_review_func(text, paragraphs=paragraphs)
    except AIAssessorError as error:
        telemetry.increment("active_review_ai_first_failed")
        if fallback_mode == FALLBACK_MODE_FAIL_CLOSED:
            telemetry.increment("active_review_ai_first_fail_closed")
            raise ActiveReviewEngineError(f"AI-first review failed: {error}") from error
        telemetry.increment("active_review_ai_first_fallback_deterministic")
        result = deterministic_review_func(text, paragraphs=paragraphs)
        result = _append_review_warning(
            result,
            "AI-first review failed; deterministic fallback was used.",
        )
        result["ai_first_review"] = {
            "status": "failed",
            "mode": REVIEW_ENGINE_AI_FIRST,
            "fallback_used": True,
            "error_type": error.__class__.__name__,
        }
        return _with_active_engine_metadata(
            result,
            selected_engine=REVIEW_ENGINE_AI_FIRST,
            executed_engine=REVIEW_ENGINE_DETERMINISTIC,
            status="fallback",
            fallback_mode=fallback_mode,
            engine_source=engine_config["source"],
            fallback_source=fallback_config["source"],
            fallback_used=True,
            error=error,
        )

    telemetry.increment("active_review_ai_first_completed")
    ai_first_status = _ai_first_status(result)
    if ai_first_status == "partial":
        telemetry.increment("active_review_ai_first_partial")
    return _with_active_engine_metadata(
        result,
        selected_engine=REVIEW_ENGINE_AI_FIRST,
        executed_engine=REVIEW_ENGINE_AI_FIRST,
        status=ai_first_status or "completed",
        fallback_mode=fallback_mode,
        engine_source=engine_config["source"],
        fallback_source=fallback_config["source"],
    )


def _with_active_engine_metadata(
    result: dict[str, Any],
    *,
    selected_engine: str,
    executed_engine: str,
    status: str,
    fallback_mode: str,
    engine_source: str,
    fallback_source: str,
    fallback_used: bool = False,
    error: Exception | None = None,
) -> dict[str, Any]:
    updated = deepcopy(result)
    metadata: dict[str, Any] = {
        "selected_engine": selected_engine,
        "executed_engine": executed_engine,
        "engine": executed_engine,
        "source": engine_source,
        "fallback_source": fallback_source,
        "status": status,
        "fallback_mode": fallback_mode,
        "fallback_used": fallback_used,
    }
    if error is not None:
        metadata["error_type"] = error.__class__.__name__
    ai_first_metadata = updated.get("ai_first_review")
    if isinstance(ai_first_metadata, dict):
        for key in ("provider", "model", "record_count", "assessment_contract_version", "packet_version"):
            if key in ai_first_metadata:
                metadata[key] = ai_first_metadata[key]
        metadata["ai_first_status"] = str(ai_first_metadata.get("status") or "")
        missing_clause_ids = ai_first_metadata.get("missing_clause_ids")
        if isinstance(missing_clause_ids, list):
            metadata["missing_clause_ids"] = [str(clause_id) for clause_id in missing_clause_ids]
    updated["active_review_engine"] = metadata
    return updated


def _append_review_warning(result: dict[str, Any], warning: str) -> dict[str, Any]:
    updated = deepcopy(result)
    warnings = updated.get("review_warnings")
    if not isinstance(warnings, list):
        warnings = []
    warnings.append(warning)
    updated["review_warnings"] = warnings
    return updated


def _ai_first_status(result: dict[str, Any]) -> str:
    ai_first = result.get("ai_first_review")
    if isinstance(ai_first, dict):
        return str(ai_first.get("status") or "").strip()
    return ""


def _active_review_engine_config() -> dict[str, str]:
    environment_value = _normalized_active_review_engine(os.environ.get(ACTIVE_REVIEW_ENGINE_ENV, ""))
    if environment_value:
        return {
            "value": environment_value,
            "source": REVIEW_ENGINE_SOURCE_ENVIRONMENT,
            "source_key": ACTIVE_REVIEW_ENGINE_ENV,
        }
    stored_value = app_settings.review_runtime_settings().get("active_review_engine")
    if stored_value in {REVIEW_ENGINE_DETERMINISTIC, REVIEW_ENGINE_AI_FIRST}:
        return {
            "value": str(stored_value),
            "source": REVIEW_ENGINE_SOURCE_RUNTIME_SETTINGS,
            "source_key": "review_runtime.active_review_engine",
        }
    return {
        "value": DEFAULT_ACTIVE_REVIEW_ENGINE,
        "source": REVIEW_ENGINE_SOURCE_DEFAULT,
        "source_key": "",
    }


def _ai_first_fallback_mode_config() -> dict[str, str]:
    environment_value = _normalized_ai_first_fallback_mode(os.environ.get(AI_FIRST_FALLBACK_MODE_ENV, ""))
    if environment_value:
        return {
            "value": environment_value,
            "source": REVIEW_ENGINE_SOURCE_ENVIRONMENT,
            "source_key": AI_FIRST_FALLBACK_MODE_ENV,
        }
    stored_value = app_settings.review_runtime_settings().get("ai_first_fallback_mode")
    if stored_value in {FALLBACK_MODE_DETERMINISTIC, FALLBACK_MODE_FAIL_CLOSED}:
        return {
            "value": str(stored_value),
            "source": REVIEW_ENGINE_SOURCE_RUNTIME_SETTINGS,
            "source_key": "review_runtime.ai_first_fallback_mode",
        }
    return {
        "value": DEFAULT_AI_FIRST_FALLBACK_MODE,
        "source": REVIEW_ENGINE_SOURCE_DEFAULT,
        "source_key": "",
    }


def _normalized_active_review_engine(value: object) -> str:
    configured = str(value or "").strip().lower().replace("-", "_")
    if configured in {"deterministic", "rules"}:
        return REVIEW_ENGINE_DETERMINISTIC
    if configured in {"ai", "ai_first", "ai_first_review"}:
        return REVIEW_ENGINE_AI_FIRST
    return ""


def _normalized_ai_first_fallback_mode(value: object) -> str:
    configured = str(value or "").strip().lower().replace("-", "_")
    if configured in {"deterministic", "rules"}:
        return FALLBACK_MODE_DETERMINISTIC
    if configured in {"fail_closed", "error", "none"}:
        return FALLBACK_MODE_FAIL_CLOSED
    return ""
