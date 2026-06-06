from __future__ import annotations

import os
from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import app_settings
from .ai_assessor import AIAssessorError, assess_nda_with_ai
from .checker import review_nda
from .review_document import Paragraph
from . import telemetry
from .routes import playbook as playbook_routes

ACTIVE_REVIEW_ENGINE_ENV = "NDA_ACTIVE_REVIEW_ENGINE"
REVIEW_ENGINE_DETERMINISTIC = "deterministic"
REVIEW_ENGINE_AI_FIRST = "ai_first"
DEFAULT_ACTIVE_REVIEW_ENGINE = REVIEW_ENGINE_AI_FIRST
REVIEW_ENGINE_SOURCE_DEFAULT = "default"
REVIEW_ENGINE_SOURCE_ENVIRONMENT = "environment"
REVIEW_ENGINE_SOURCE_RUNTIME_SETTINGS = "runtime_settings"


class ActiveReviewEngineError(RuntimeError):
    pass


ReviewEngineFn = Callable[..., dict[str, Any]]
PlaybookRuntimeFn = Callable[[], dict[str, Any]]


def active_review_engine() -> str:
    return _active_review_engine_config()["value"]


def active_review_engine_status() -> dict[str, Any]:
    engine_config = _active_review_engine_config()
    stored_settings = app_settings.review_runtime_settings()
    return {
        "active_engine": engine_config["value"],
        "engine_source": engine_config["source"],
        "engine_source_key": engine_config["source_key"],
        "stored_active_engine": stored_settings.get("active_review_engine"),
        "environment_active_engine": _normalized_active_review_engine(os.environ.get(ACTIVE_REVIEW_ENGINE_ENV, "")),
        "supported_engines": [REVIEW_ENGINE_DETERMINISTIC, REVIEW_ENGINE_AI_FIRST],
    }


def _offline_deterministic_review(text: str, *, paragraphs: Sequence[Paragraph] | None = None) -> dict[str, Any]:
    # The deterministic engine is the OFFLINE oracle. review_nda defaults to
    # verify=True, which runs an AI verifier over the findings — i.e. live network
    # calls. That (not the checks themselves) is what made the "deterministic"
    # review take ~20s. Pin ai_enabled=False (no AI overlay) + verify=False (no AI
    # verifier) so the deterministic engine is genuinely offline and fast, as its
    # name promises.
    return review_nda(text, paragraphs=paragraphs, verify=False, ai_enabled=False)


def review_nda_with_active_engine(
    text: str,
    *,
    paragraphs: Sequence[Paragraph] | None = None,
    deterministic_review_func: ReviewEngineFn = _offline_deterministic_review,
    ai_first_review_func: ReviewEngineFn = assess_nda_with_ai,
    playbook_runtime_func: PlaybookRuntimeFn = playbook_routes.ensure_active_playbook_runtime,
    force_engine: str | None = None,
) -> dict[str, Any]:
    # force_engine lets a caller pin the engine regardless of the active config —
    # used by outbound NDA generation to run the fast deterministic review at
    # creation and defer the AI review to on-demand (Refresh Review). The inbound
    # review paths leave it unset, so the AI-first fail-closed policy is unchanged.
    engine_config = _active_review_engine_config()
    selected_engine = force_engine or engine_config["value"]
    engine_source = "forced" if force_engine else engine_config["source"]
    if selected_engine != REVIEW_ENGINE_AI_FIRST:
        telemetry.increment("active_review_deterministic_completed")
        result = deterministic_review_func(text, paragraphs=paragraphs)
        playbook_runtime = _review_playbook_runtime(playbook_runtime_func)
        return _with_active_engine_metadata(
            result,
            selected_engine=REVIEW_ENGINE_DETERMINISTIC,
            executed_engine=REVIEW_ENGINE_DETERMINISTIC,
            status="completed",
            engine_source=engine_source,
            playbook_runtime=playbook_runtime,
        )

    telemetry.increment("active_review_ai_first_attempted")
    try:
        result = ai_first_review_func(text, paragraphs=paragraphs)
    except AIAssessorError as error:
        telemetry.increment("active_review_ai_first_failed")
        telemetry.increment("active_review_ai_first_fail_closed")
        raise ActiveReviewEngineError(f"AI-first review failed: {error}") from error

    telemetry.increment("active_review_ai_first_completed")
    ai_first_status = _ai_first_status(result)
    if ai_first_status == "partial":
        telemetry.increment("active_review_ai_first_partial")
    playbook_runtime = _review_playbook_runtime(playbook_runtime_func)
    return _with_active_engine_metadata(
        result,
        selected_engine=REVIEW_ENGINE_AI_FIRST,
        executed_engine=REVIEW_ENGINE_AI_FIRST,
        status=ai_first_status or "completed",
        engine_source=engine_config["source"],
        playbook_runtime=playbook_runtime,
    )


def _with_active_engine_metadata(
    result: dict[str, Any],
    *,
    selected_engine: str,
    executed_engine: str,
    status: str,
    engine_source: str,
    playbook_runtime: Mapping[str, Any],
    error: Exception | None = None,
) -> dict[str, Any]:
    updated = deepcopy(result)
    metadata: dict[str, Any] = {
        "selected_engine": selected_engine,
        "executed_engine": executed_engine,
        "engine": executed_engine,
        "source": engine_source,
        "status": status,
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
    updated["playbook_runtime"] = _public_review_playbook_runtime(playbook_runtime)
    updated["playbook_version"] = _review_playbook_version(playbook_runtime)
    return updated


def _review_playbook_runtime(playbook_runtime_func: PlaybookRuntimeFn) -> dict[str, Any]:
    runtime = playbook_runtime_func()
    if not isinstance(runtime, dict):
        raise ActiveReviewEngineError("Active Playbook runtime metadata could not be loaded.")
    required_keys = [
        "active_version_id",
        "active_hash",
        "playbook_name",
        "playbook_version",
        "published_at",
        "published_by",
    ]
    missing_keys = [key for key in required_keys if key not in runtime]
    if missing_keys:
        raise ActiveReviewEngineError(
            "Active Playbook runtime metadata is incomplete: " + ", ".join(missing_keys)
        )
    return runtime


def _public_review_playbook_runtime(runtime: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "active_version_id": str(runtime.get("active_version_id") or ""),
        "active_hash": str(runtime.get("active_hash") or ""),
        "playbook_name": str(runtime.get("playbook_name") or ""),
        "playbook_version": str(runtime.get("playbook_version") or ""),
        "published_at": str(runtime.get("published_at") or ""),
        "published_by": str(runtime.get("published_by") or ""),
        "source": "active",
        "active_source": str(runtime.get("source") or ""),
    }


def _review_playbook_version(runtime: Mapping[str, Any]) -> dict[str, str]:
    """Compact, stable provenance stamp recorded on every review result.

    ``hash`` is the same stable content hash carried by
    ``playbook_runtime.active_hash`` (``playbook_snapshot_hash`` over the active
    published Playbook): no timestamps or ordering, so it is identical across
    re-reads and changes only when the published Playbook changes. The approval
    gate compares this ``hash`` to the current published hash to detect staleness,
    so the field name/shape is a shared contract — keep ``id``/``hash``/``label``.
    """
    return {
        "id": str(runtime.get("active_version_id") or ""),
        "hash": str(runtime.get("active_hash") or ""),
        "label": _playbook_version_label(runtime),
    }


def _playbook_version_label(runtime: Mapping[str, Any]) -> str:
    name = str(runtime.get("playbook_name") or "").strip()
    version = str(runtime.get("playbook_version") or "").strip()
    if name and version:
        return f"{name} v{version}"
    return name or (f"v{version}" if version else "")


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


def _normalized_active_review_engine(value: object) -> str:
    configured = str(value or "").strip().lower().replace("-", "_")
    if configured in {"deterministic", "rules"}:
        return REVIEW_ENGINE_DETERMINISTIC
    if configured in {"ai", "ai_first", "ai_first_review"}:
        return REVIEW_ENGINE_AI_FIRST
    return ""
