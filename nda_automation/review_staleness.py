from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .checker import REVIEW_ENGINE_VERSION
from .routes import playbook as playbook_routes

STALE_REVIEW_EXPORT_MESSAGE = (
    "Review is stale. Refresh the review before exporting or sending a redline."
)

_PLAYBOOK_RUNTIME_KEYS = (
    "active_version_id",
    "active_hash",
    "playbook_name",
    "playbook_version",
    "published_at",
    "published_by",
)

CurrentRuntimeFn = Callable[[], dict[str, Any]]


def review_result_is_stale(review_result: object) -> bool:
    return bool(review_result_staleness(review_result)["stale"])


def review_result_stale_reasons(
    review_result: object,
    *,
    current_runtime: Mapping[str, Any] | None = None,
    current_runtime_error: Exception | None = None,
) -> list[str]:
    reasons: list[str] = []
    if not isinstance(review_result, dict):
        return ["missing_review_result"]

    if review_result.get("review_engine_version") != REVIEW_ENGINE_VERSION:
        reasons.append("review_engine_version_changed")

    clauses = review_result.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        reasons.append("missing_clauses")
    elif any(
        not isinstance(clause, dict)
        or not isinstance(clause.get("structure_context"), dict)
        or not isinstance(clause.get("review_state"), dict)
        for clause in clauses
    ):
        reasons.append("missing_clause_review_metadata")

    if not isinstance(review_result.get("review_state"), dict):
        reasons.append("missing_review_state")

    review_runtime = review_result.get("playbook_runtime")
    if not isinstance(review_runtime, dict):
        reasons.append("missing_playbook_runtime")
        return reasons

    missing_runtime_keys = [
        key for key in _PLAYBOOK_RUNTIME_KEYS if not str(review_runtime.get(key) or "").strip()
    ]
    if missing_runtime_keys:
        reasons.append("incomplete_playbook_runtime")

    if current_runtime_error is not None:
        reasons.append("playbook_runtime_unavailable")
        return reasons
    if not isinstance(current_runtime, Mapping):
        reasons.append("playbook_runtime_unavailable")
        return reasons

    current_hash = str(current_runtime.get("active_hash") or "").strip()
    review_hash = str(review_runtime.get("active_hash") or "").strip()
    if not current_hash:
        reasons.append("playbook_runtime_unavailable")
    elif review_hash and review_hash != current_hash:
        reasons.append("playbook_changed")
    return reasons


def review_result_staleness(
    review_result: object,
    *,
    current_runtime_func: CurrentRuntimeFn = playbook_routes.ensure_active_playbook_runtime,
) -> dict[str, Any]:
    current_runtime: dict[str, Any] | None = None
    current_runtime_error: Exception | None = None
    try:
        current_runtime = current_runtime_func()
    except Exception as error:  # Fail closed when the active playbook cannot be loaded.
        current_runtime_error = error

    reasons = review_result_stale_reasons(
        review_result,
        current_runtime=current_runtime,
        current_runtime_error=current_runtime_error,
    )
    summary: dict[str, Any] = {
        "stale": bool(reasons),
        "stale_reasons": reasons,
        "current_review_engine_version": REVIEW_ENGINE_VERSION,
        "current_playbook": playbook_routes.public_playbook_runtime(current_runtime),
        "review_playbook": review_playbook_runtime_metadata(review_result),
    }
    if reasons:
        summary["message"] = stale_review_message(reasons)
    return summary


def review_playbook_runtime_metadata(review_result: object) -> dict[str, Any]:
    if not isinstance(review_result, dict):
        return {}
    runtime = review_result.get("playbook_runtime")
    if not isinstance(runtime, dict):
        return {}
    keys = (*_PLAYBOOK_RUNTIME_KEYS, "source", "active_source")
    return {key: runtime.get(key) for key in keys if key in runtime}


def stale_review_message(reasons: list[str]) -> str:
    if "playbook_changed" in reasons:
        return "The active Playbook has changed since this review was generated. Refresh the review before exporting or sending a redline."
    if "review_engine_version_changed" in reasons:
        return "The review engine has changed since this review was generated. Refresh the review before exporting or sending a redline."
    if "playbook_runtime_unavailable" in reasons:
        return "The active Playbook runtime could not be verified. Refresh the review before exporting or sending a redline."
    return STALE_REVIEW_EXPORT_MESSAGE
