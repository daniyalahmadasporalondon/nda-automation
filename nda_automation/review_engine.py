from __future__ import annotations

import os
import inspect
from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import app_settings
from . import checker as checker_module
from .ai_assessor import AIAssessorError, assess_nda_with_ai
from .checker import compute_unmatched_sections, review_nda
from .review_document import Paragraph
from . import telemetry
from .playbook_runtime import (
    ActivePlaybookBundle,
    active_playbook_bundle_from_runtime,
    ensure_active_runtime_for_playbook,
)

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
PlaybookRuntimeFn = Callable[[], dict[str, Any] | ActivePlaybookBundle]


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
        "supported_engines": [REVIEW_ENGINE_AI_FIRST],
    }


def _offline_deterministic_review(
    text: str,
    *,
    paragraphs: Sequence[Paragraph] | None = None,
    playbook: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # The deterministic engine is the OFFLINE oracle. review_nda defaults to
    # verify=True, which runs an AI verifier over the findings — i.e. live network
    # calls. That (not the checks themselves) is what made the "deterministic"
    # review take ~20s. Pin ai_enabled=False (no AI overlay) + verify=False (no AI
    # verifier) so the deterministic engine is genuinely offline and fast, as its
    # name promises.
    return review_nda(text, paragraphs=paragraphs, playbook=playbook, verify=False, ai_enabled=False)


def ensure_active_review_playbook_bundle() -> ActivePlaybookBundle:
    playbook = checker_module.load_playbook()
    checker_module.validate_playbook(playbook)
    runtime = ensure_active_runtime_for_playbook(
        playbook,
        playbook_path=checker_module.PLAYBOOK_PATH,
    )
    return ActivePlaybookBundle(playbook=playbook, runtime=runtime)


def review_nda_with_active_engine(
    text: str,
    *,
    paragraphs: Sequence[Paragraph] | None = None,
    deterministic_review_func: ReviewEngineFn = _offline_deterministic_review,
    ai_first_review_func: ReviewEngineFn = assess_nda_with_ai,
    playbook_runtime_func: PlaybookRuntimeFn = ensure_active_review_playbook_bundle,
    force_engine: str | None = None,
) -> dict[str, Any]:
    # force_engine lets a caller pin the engine regardless of the active config —
    # used by outbound NDA generation to run the fast deterministic review at
    # creation and defer the AI review to on-demand (Refresh Review). The inbound
    # review paths leave it unset, so the AI-first fail-closed policy is unchanged.
    engine_config = _active_review_engine_config()
    selected_engine = force_engine or engine_config["value"]
    engine_source = "forced" if force_engine else engine_config["source"]
    playbook_bundle = _review_playbook_bundle(playbook_runtime_func)
    if selected_engine != REVIEW_ENGINE_AI_FIRST:
        telemetry.increment("active_review_deterministic_completed")
        result = _call_review_engine(
            deterministic_review_func,
            text,
            paragraphs=paragraphs,
            playbook=playbook_bundle.playbook,
        )
        return _with_active_engine_metadata(
            result,
            selected_engine=REVIEW_ENGINE_DETERMINISTIC,
            executed_engine=REVIEW_ENGINE_DETERMINISTIC,
            status="completed",
            engine_source=engine_source,
            playbook_runtime=playbook_bundle.runtime,
        )

    telemetry.increment("active_review_ai_first_attempted")
    try:
        result = _call_review_engine(
            ai_first_review_func,
            text,
            paragraphs=paragraphs,
            playbook=playbook_bundle.playbook,
        )
    except AIAssessorError as error:
        telemetry.increment("active_review_ai_first_failed")
        telemetry.increment("active_review_ai_first_fail_closed")
        raise ActiveReviewEngineError(f"AI-first review failed: {error}") from error

    telemetry.increment("active_review_ai_first_completed")
    ai_first_status = _ai_first_status(result)
    if ai_first_status == "partial":
        telemetry.increment("active_review_ai_first_partial")
    return _with_active_engine_metadata(
        result,
        selected_engine=REVIEW_ENGINE_AI_FIRST,
        executed_engine=REVIEW_ENGINE_AI_FIRST,
        status=ai_first_status or "completed",
        engine_source=engine_config["source"],
        playbook_runtime=playbook_bundle.runtime,
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
    # Coverage metadata is engine-agnostic. The deterministic engine already adds
    # unmatched_sections, but the AI-first engine result doesn't carry it -- derive
    # it here from the (engine-independent) contract structure + clause matches so
    # both review paths surface uncovered document sections.
    if "unmatched_sections" not in updated:
        updated["unmatched_sections"] = compute_unmatched_sections(
            updated.get("contract_structure"), updated.get("clauses")
        )
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


def _review_playbook_bundle(playbook_runtime_func: PlaybookRuntimeFn) -> ActivePlaybookBundle:
    candidate = playbook_runtime_func()
    if isinstance(candidate, ActivePlaybookBundle):
        runtime = candidate.runtime
        playbook = candidate.playbook
    else:
        runtime = candidate
        playbook = {}
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
    if not isinstance(playbook, dict):
        playbook = {}
    return active_playbook_bundle_from_runtime(runtime, playbook=playbook)


def _call_review_engine(
    review_func: ReviewEngineFn,
    text: str,
    *,
    paragraphs: Sequence[Paragraph] | None,
    playbook: Mapping[str, Any],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"paragraphs": paragraphs}
    if playbook and _accepts_keyword(review_func, "playbook"):
        kwargs["playbook"] = playbook
    return review_func(text, **kwargs)


def _accepts_keyword(func: ReviewEngineFn, keyword: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return keyword in signature.parameters


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
    if stored_value == REVIEW_ENGINE_AI_FIRST:
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
    if configured in {"ai", "ai_first", "ai_first_review"}:
        return REVIEW_ENGINE_AI_FIRST
    return ""
