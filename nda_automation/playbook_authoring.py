"""Playbook authoring workflow.

This module owns the user-visible Playbook authoring grammar: loading the
published workspace, validating drafts, saving/discarding drafts, publishing, and
restoring history snapshots. The lower-level runtime module still owns durable
files, locks, hashes, and sidecar persistence.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .checker import PLAYBOOK_PATH, PlaybookTemplateError, validate_playbook
from .playbook_rules import (
    PlaybookRulesError,
    derived_policy_fields,
    validate_playbook_rules,
)

try:  # pragma: no cover - exercised only when the lint module is present
    from .playbook_lint import lint_playbook
except ImportError:  # pragma: no cover - lint module may not be wired yet
    lint_playbook = None

try:  # pragma: no cover - exercised only when the semantic-lint module is present
    from .playbook_semantic_lint import (
        semantic_lint_enabled,
        semantic_lint_playbook,
    )
except ImportError:  # pragma: no cover - semantic-lint module may not be wired yet
    semantic_lint_enabled = None
    semantic_lint_playbook = None
from .playbook_runtime import (
    PLAYBOOK_RUNTIME_VERSION,
    PlaybookDraftConflict,
    _DRAFT_RUNTIME_KEYS,
    _active_runtime_from_playbook,
    _actor_from_payload,
    _draft_base_conflict,
    _draft_discard_history_entry,
    _draft_history_entry,
    _draft_payload_from_playbook,
    _draft_runtime_fields,
    _expected_active_conflict,
    _history_entry,
    _publish_candidate_from_payload,
    _publish_history_entry,
    _remove_file_durably,
    _runtime_fields_for_draft,
    draft_path_for,
    ensure_active_runtime_for_playbook,
    locked_playbook,
    public_playbook_draft,
    public_playbook_draft_payload,
    public_playbook_history,
    public_playbook_runtime,
    read_playbook_draft,
    read_playbook_from_path,
    read_playbook_history,
    read_playbook_runtime,
    write_active_playbook_bundle_atomically,
    write_playbook_draft,
    write_playbook_history,
    write_playbook_runtime,
)

LOGGER = logging.getLogger(__name__)


class PlaybookAuthoringError(RuntimeError):
    """Structured authoring failure for route adapters."""

    def __init__(self, payload: dict[str, Any], *, status: int = 400) -> None:
        super().__init__(str(payload.get("error") or "Playbook authoring failed."))
        self.payload = payload
        self.status = status


def _reset_playbook_derived_caches() -> None:
    """Invalidate module caches derived from the active Playbook after a mutation.

    ``governing_law_view`` (dashboard search-intent / corpus-facet option maps),
    ``law_forum_check`` (approved-law/forum JURISDICTIONS buckets) and
    ``dashboard_search_intent`` (the ``has_clause`` clause-id allowlist) cache data
    derived from the active Playbook at first use / import. Without this call,
    publish/save/restore leaves those modules serving the OLD option/clause set
    until the process restarts. This is IN-PROCESS invalidation only -- a future
    multi-process deploy needs hash-keyed staleness detection instead.

    CRITICAL: this must be called AFTER ``locked_playbook`` is released (i.e. outside
    the ``with locked_playbook(...)`` block). ``law_forum_check.reset_buckets()``
    rebuilds EAGERLY: it re-reads the Playbook through
    ``ensure_active_playbook_bundle`` -> ``locked_playbook``, and a second flock
    acquisition on the same file from a new fd in the same process deadlocks.

    Called from a ``finally`` on every mutation path: the playbook-file write is the
    commit point, and a LATER step (parent-dir fsync, transaction unlink, draft-file
    removal) can raise OSError after the active playbook already changed on disk --
    a success-only reset would strand stale caches behind a 500. A reset after a
    pure validation rejection is a harmless extra rebuild.

    Each reset is individually guarded: cache invalidation must NEVER turn a
    successful publish into a 500.
    """
    try:
        from . import governing_law_view  # noqa: PLC0415 -- avoid import cycle at load.

        governing_law_view.reset_caches()
    except Exception:  # noqa: BLE001 -- invalidation must never fail the mutation.
        LOGGER.warning(
            "governing_law_view.reset_caches() failed after a Playbook mutation; "
            "the process may serve stale governing-law options until restart.",
            exc_info=True,
        )
    try:
        from . import law_forum_check  # noqa: PLC0415 -- avoid import cycle at load.

        law_forum_check.reset_buckets()
    except Exception:  # noqa: BLE001 -- invalidation must never fail the mutation.
        LOGGER.warning(
            "law_forum_check.reset_buckets() failed after a Playbook mutation; "
            "the process may serve stale law/forum buckets until restart.",
            exc_info=True,
        )
    try:
        from . import dashboard_search_intent  # noqa: PLC0415 -- avoid import cycle at load.

        dashboard_search_intent.reset_clause_id_cache()
    except Exception:  # noqa: BLE001 -- invalidation must never fail the mutation.
        LOGGER.warning(
            "dashboard_search_intent.reset_clause_id_cache() failed after a Playbook "
            "mutation; the process may serve a stale clause-id allowlist until restart.",
            exc_info=True,
        )


_LINT_FAILURE_PREFIX = "Playbook is self-contradictory and cannot be published: "


def _format_lint_violation(violation: Any) -> str:
    """Render a single lint violation as a stable, human-readable line."""
    clause_id = str(getattr(violation, "clause_id", "") or "").strip()
    message = str(getattr(violation, "message", "") or "").strip() or "Playbook lint violation."
    return f"Playbook clause {clause_id} {message}" if clause_id else message


def _violation_severity(violation: Any) -> str:
    """A lint violation's severity, defaulting to the blocking ``"error"``."""
    return str(getattr(violation, "severity", "") or "error").strip().lower() or "error"


def lint_violations_for(playbook: Any, *, severities: set[str] | None = None) -> list[str]:
    """Return formatted consistency-lint violation messages for a candidate playbook.

    Resolves ``lint_playbook`` at call time so tests (and the integrator) can
    monkeypatch :data:`nda_automation.playbook_authoring.lint_playbook` and so the
    integration degrades gracefully when the lint module is not yet wired.

    ``severities`` filters which violation severities are returned. ``None`` returns
    all (the draft-validation surface, which shows warnings AND errors); the publish
    hard-gate passes ``{"error"}`` so advisory ``"warning"`` violations (e.g. a law
    alias collision) surface in the UI without wedging a legitimate publish.
    """
    lint = lint_playbook
    if lint is None:
        return []
    try:
        violations = lint(playbook)
    except Exception as exc:  # noqa: BLE001 - lint machinery itself broke
        # FAIL CLOSED. If the lint machinery itself errors we must NOT silently
        # return a no-op (the old behaviour) -- that turns the hard structural
        # publish gate off and lets a self-contradictory playbook go live. Surface
        # the failure loudly AND block, so a broken lint engine cannot be the path
        # to publishing an unvalidated rulebook.
        LOGGER.error(
            "Playbook consistency lint machinery raised; blocking publish (fail-closed).",
            exc_info=True,
        )
        return [
            f"playbook consistency lint could not run ({type(exc).__name__}: {exc}); "
            "blocking publish until the lint machinery is healthy"
        ]
    selected = [
        violation
        for violation in (violations or [])
        if severities is None or _violation_severity(violation) in severities
    ]
    return [_format_lint_violation(violation) for violation in selected]


def _enforce_playbook_lint(playbook: Any) -> None:
    """Reject a playbook that fails the consistency lint.

    Raises :class:`PlaybookTemplateError` (the existing publish-failure type) with a
    message that enumerates every violation, so it surfaces through the same handlers
    and response shape as any other ``validate_playbook`` failure.

    Only ``"error"``-severity violations BLOCK: advisory ``"warning"`` violations
    (e.g. a law alias collision) are surfaced in draft validation but must not wedge
    a legitimate publish, matching the project's "don't false-positive-block authoring"
    stance.
    """
    messages = lint_violations_for(playbook, severities={"error"})
    if messages:
        raise PlaybookTemplateError(_LINT_FAILURE_PREFIX + "; ".join(messages))


def _format_semantic_lint_violation(violation: Any) -> dict[str, Any]:
    """Render a Layer-2 semantic violation as a structured advisory warning record.

    Same envelope shape as :func:`_structured_playbook_error` (location/clause/field/
    message) so the UI can render warnings and errors uniformly, but with
    ``severity == "warning"`` and the model's self-reported ``confidence`` attached.
    """
    clause_id = str(getattr(violation, "clause_id", "") or "").strip()
    message = str(getattr(violation, "message", "") or "").strip() or "Playbook semantic-lint warning."
    check_id = str(getattr(violation, "check_id", "") or "").strip()
    confidence = getattr(violation, "confidence", None)
    return {
        "location": clause_id,
        "clause": clause_id or None,
        "field": None,
        "message": message,
        "severity": "warning",
        "check_id": check_id,
        "confidence": confidence,
    }


def semantic_lint_warnings_for(playbook: Any) -> list[dict[str, Any]]:
    """Return advisory Layer-2 semantic-lint warnings for a candidate playbook.

    ADVISORY ONLY: an AI lint must never hard-block publishing (false positives +
    model flakiness), so these are surfaced as a separate ``warnings`` list in the
    draft-validation path -- never in the blocking ``errors`` list and never in the
    publish hard-gate.

    Gated by :func:`semantic_lint_enabled` (DEFAULT OFF) and fully FAIL-OPEN: if the
    module is not wired, the flag is off, or the pass raises, this returns ``[]``.
    Resolved at call time so tests can monkeypatch the module-level symbols.
    """
    lint = semantic_lint_playbook
    enabled = semantic_lint_enabled
    if lint is None or enabled is None:
        return []
    try:
        if not enabled():
            return []
        violations = lint(playbook)
    except Exception:  # noqa: BLE001 - an advisory AI lint must never block; fail open to a no-op
        LOGGER.warning("Playbook semantic lint raised; skipping the advisory pass.", exc_info=True)
        return []
    return [_format_semantic_lint_violation(violation) for violation in (violations or [])]


#: The semantic-lint check id that marks a POISONED standard (prose telling the
#: reviewer to ignore the playbook / mark everything pass). Mirrors
#: ``playbook_semantic_lint.POISON_CHECK_ID``; surfaced prominently at publish.
_POISON_CHECK_ID = "poison_instruction"


def _log_prominent_semantic_warnings(warnings: list[dict[str, Any]]) -> None:
    """Log Layer-2 semantic warnings at publish, ELEVATING poison findings.

    A poison standard (a clause that instructs the downstream reviewer to ignore the
    playbook / mark everything pass) is the most dangerous semantic finding. It does
    NOT hard-block publish -- an AI judgement must not wedge authoring -- but it is
    logged at ERROR so it is impossible to miss, while ordinary advisories log at a
    quieter level.
    """
    for warning in warnings or []:
        message = str(warning.get("message") or "").strip()
        clause = str(warning.get("clause") or "").strip()
        if str(warning.get("check_id") or "").strip() == _POISON_CHECK_ID:
            LOGGER.error(
                "PLAYBOOK POISON SUGGESTED at publish (clause %s): %s",
                clause or "?",
                message,
            )
        else:
            LOGGER.info(
                "Playbook semantic-lint advisory at publish (clause %s): %s",
                clause or "?",
                message,
            )


def load_playbook_workspace(
    *,
    playbook_path=PLAYBOOK_PATH,
    replace_file=os.replace,
    include_playbook: bool = True,
) -> dict[str, Any]:
    try:
        with locked_playbook(playbook_path):
            playbook = read_playbook_from_path(playbook_path)
            validate_playbook(playbook)
            runtime = ensure_active_runtime_for_playbook(
                playbook,
                playbook_path=playbook_path,
                replace_file=replace_file,
                source="bootstrap",
            )
            draft = read_playbook_draft(playbook_path=playbook_path)
            history = read_playbook_history(playbook_path=playbook_path)
    except (OSError, json.JSONDecodeError) as error:
        raise PlaybookAuthoringError({"error": "Playbook could not be loaded."}, status=500) from error
    except PlaybookTemplateError as error:
        raise PlaybookAuthoringError({"error": str(error)}, status=400) from error

    return _workspace_payload(playbook, runtime, draft, history, include_playbook=include_playbook)


def validate_playbook_draft(payload: dict[str, Any]) -> dict[str, Any]:
    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        raise PlaybookAuthoringError({"error": "Playbook draft payload must include a playbook object."}, status=400)

    errors = collect_playbook_validation_errors(playbook)
    # Layer-2 semantic lint is ADVISORY: its findings ride in a SEPARATE "warnings"
    # list and never affect ``valid`` or the publish gate. Default-off + fail-open.
    warnings = semantic_lint_warnings_for(playbook)
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def save_playbook_draft(
    payload: dict[str, Any],
    *,
    playbook_path=PLAYBOOK_PATH,
    replace_file=os.replace,
) -> dict[str, Any]:
    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        raise PlaybookAuthoringError({"error": "Playbook draft payload must include a playbook object."}, status=400)

    try:
        with locked_playbook(playbook_path):
            active_playbook = read_playbook_from_path(playbook_path)
            validate_playbook(active_playbook)
            validate_playbook(playbook)
            runtime = ensure_active_runtime_for_playbook(
                active_playbook,
                playbook_path=playbook_path,
                replace_file=replace_file,
                source="bootstrap",
            )
            conflict = _expected_active_conflict(payload, runtime)
            if conflict:
                raise PlaybookAuthoringError(conflict, status=409)
            draft = _draft_payload_from_playbook(playbook, active_playbook, runtime, payload)
            write_playbook_draft(draft, playbook_path=playbook_path, replace_file=replace_file)
            runtime = {
                **runtime,
                **_runtime_fields_for_draft(draft),
            }
            write_playbook_runtime(runtime, playbook_path=playbook_path, replace_file=replace_file)
            history = read_playbook_history(playbook_path=playbook_path)
            history.insert(0, _draft_history_entry(draft, playbook, active_playbook))
            write_playbook_history(history, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookAuthoringError:
        raise
    except PlaybookTemplateError as error:
        raise PlaybookAuthoringError({"error": str(error)}, status=400) from error
    except OSError as error:
        raise PlaybookAuthoringError({"error": "Playbook draft could not be saved."}, status=500) from error

    return {
        "active": _active_payload(active_playbook, runtime),
        "draft": public_playbook_draft_payload(runtime, draft),
        "history": public_playbook_history(history),
        "saved_draft_at": draft["updated_at"],
    }


def discard_playbook_draft(
    payload: dict[str, Any],
    *,
    playbook_path=PLAYBOOK_PATH,
    replace_file=os.replace,
) -> dict[str, Any]:
    try:
        with locked_playbook(playbook_path):
            active_playbook = read_playbook_from_path(playbook_path)
            validate_playbook(active_playbook)
            runtime = ensure_active_runtime_for_playbook(
                active_playbook,
                playbook_path=playbook_path,
                replace_file=replace_file,
                source="bootstrap",
            )
            draft = read_playbook_draft(playbook_path=playbook_path)
            draft_id = str((draft or {}).get("draft_id") or runtime.get("draft_id") or "")
            if not draft_id:
                raise PlaybookAuthoringError({"error": "No Playbook draft exists."}, status=404)
            requested_draft_id = str(payload.get("draft_id") or "").strip()
            if requested_draft_id and requested_draft_id != draft_id:
                raise PlaybookAuthoringError({
                    "error": "The Playbook draft changed while this request was open.",
                    "code": "playbook_draft_conflict",
                    "draft": public_playbook_draft_payload(runtime, draft),
                }, status=409)

            _remove_file_durably(draft_path_for(playbook_path))
            runtime = {key: value for key, value in runtime.items() if key not in _DRAFT_RUNTIME_KEYS}
            write_playbook_runtime(runtime, playbook_path=playbook_path, replace_file=replace_file)
            history = read_playbook_history(playbook_path=playbook_path)
            history.insert(0, _draft_discard_history_entry(active_playbook, draft, payload))
            write_playbook_history(history, playbook_path=playbook_path, replace_file=replace_file)
    except PlaybookAuthoringError:
        raise
    except PlaybookTemplateError as error:
        raise PlaybookAuthoringError({"error": str(error)}, status=400) from error
    except OSError as error:
        raise PlaybookAuthoringError({"error": "Playbook draft could not be discarded."}, status=500) from error

    return {
        "active": _active_payload(active_playbook, runtime),
        "draft": None,
        "history": public_playbook_history(history),
        "discarded_draft_at": datetime.now(timezone.utc).isoformat(),
    }


def publish_playbook(
    payload: dict[str, Any],
    *,
    playbook_path=PLAYBOOK_PATH,
    replace_file=os.replace,
) -> dict[str, Any]:
    try:
        with locked_playbook(playbook_path):
            active_playbook = read_playbook_from_path(playbook_path)
            validate_playbook(active_playbook)
            runtime = ensure_active_runtime_for_playbook(
                active_playbook,
                playbook_path=playbook_path,
                replace_file=replace_file,
                source="bootstrap",
            )
            conflict = _expected_active_conflict(payload, runtime)
            if conflict:
                raise PlaybookAuthoringError(conflict, status=409)

            draft = read_playbook_draft(playbook_path=playbook_path)
            publish_playbook, source_draft = _publish_candidate_from_payload(payload, draft)
            if publish_playbook is None:
                raise PlaybookAuthoringError(
                    {"error": "Provide a Playbook draft id or playbook object to publish."},
                    status=400,
                )
            conflict = _draft_base_conflict(source_draft, runtime)
            if conflict:
                raise PlaybookAuthoringError(conflict, status=409)
            validate_playbook(publish_playbook)
            _enforce_playbook_lint(publish_playbook)
            # Layer-2 AI semantic lint at PUBLISH (default-off, fail-open). ADVISORY:
            # it never blocks publish (false positives + model flakiness must not wedge
            # a legitimate publish), but it RUNS here and its findings -- especially a
            # POISON standard that tells the reviewer to ignore the playbook -- are
            # surfaced prominently in the response and logged loudly.
            semantic_warnings = semantic_lint_warnings_for(publish_playbook)
            _log_prominent_semantic_warnings(semantic_warnings)

            runtime = _active_runtime_from_playbook(
                publish_playbook,
                actor=_actor_from_payload(payload),
                source="publish",
            )
            history = read_playbook_history(playbook_path=playbook_path)
            history.insert(0, _publish_history_entry(
                publish_playbook,
                active_playbook,
                runtime,
                payload,
                source_draft,
            ))
            write_active_playbook_bundle_atomically(
                publish_playbook,
                runtime,
                history,
                playbook_path=playbook_path,
                replace_file=replace_file,
            )
            if source_draft is not None:
                _remove_file_durably(draft_path_for(playbook_path))
    except PlaybookAuthoringError:
        raise
    except PlaybookTemplateError as error:
        raise PlaybookAuthoringError({"error": str(error)}, status=400) from error
    except PlaybookDraftConflict as error:
        raise PlaybookAuthoringError(error.payload, status=error.status) from error
    except OSError as error:
        raise PlaybookAuthoringError({"error": "Playbook draft could not be published."}, status=500) from error
    finally:
        # AFTER the lock is released (the with-block has exited); runs on failure
        # paths too -- see the helper's docstring for both constraints.
        _reset_playbook_derived_caches()

    return {
        "playbook": publish_playbook,
        "active": _active_payload(publish_playbook, runtime),
        "draft": None,
        "history": public_playbook_history(history),
        "published_at": runtime["published_at"],
        # Advisory Layer-2 warnings surfaced PROMINENTLY at publish. Empty when the
        # semantic lint is disabled (default) or finds nothing. Poison findings are
        # tagged so the UI can elevate them above ordinary advisories.
        "semantic_warnings": semantic_warnings,
    }


def save_active_playbook(
    payload: dict[str, Any],
    *,
    playbook_path=PLAYBOOK_PATH,
    replace_file=os.replace,
) -> dict[str, Any]:
    playbook = payload.get("playbook")
    if not isinstance(playbook, dict):
        raise PlaybookAuthoringError({"error": "Playbook payload must include a playbook object."}, status=400)

    try:
        with locked_playbook(playbook_path):
            validate_playbook(playbook)
            # Same Layer-1 consistency-lint hard-gate publish enforces: a
            # validate-passing but lint-FAILING playbook must never reach the live
            # rules via save. Runs before any disk write, so rejection is a no-op,
            # and reuses the fail-open helper (a lint bug logs + treats as clean).
            _enforce_playbook_lint(playbook)
            previous_playbook = read_playbook_from_path(playbook_path) if playbook_path.exists() else None
            history = read_playbook_history(playbook_path=playbook_path)
            if previous_playbook and not history:
                history.append(_history_entry(
                    previous_playbook,
                    action="baseline",
                    actor="system",
                    summary="Initial playbook snapshot before version history.",
                ))
            existing_runtime = read_playbook_runtime(playbook_path=playbook_path)
            runtime = {
                "version": PLAYBOOK_RUNTIME_VERSION,
                **_active_runtime_from_playbook(
                    playbook,
                    actor=_actor_from_payload(payload),
                    source="save",
                ),
                **_draft_runtime_fields(existing_runtime),
            }
            history.insert(0, _history_entry(
                playbook,
                actor=_actor_from_payload(payload),
                action="save",
                previous_playbook=previous_playbook,
            ))
            write_active_playbook_bundle_atomically(
                playbook,
                runtime,
                history,
                playbook_path=playbook_path,
                replace_file=replace_file,
            )
    except PlaybookTemplateError as error:
        raise PlaybookAuthoringError({"error": str(error)}, status=400) from error
    except OSError as error:
        raise PlaybookAuthoringError({"error": "Playbook could not be saved."}, status=500) from error
    finally:
        # AFTER the lock is released (the with-block has exited); runs on failure
        # paths too -- see the helper's docstring for both constraints.
        _reset_playbook_derived_caches()

    return {
        "playbook": playbook,
        "active": _active_payload(playbook, runtime),
        "draft": public_playbook_draft(runtime),
        "history": public_playbook_history(history),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }


def restore_playbook_history_entry(
    payload: dict[str, Any],
    *,
    playbook_path=PLAYBOOK_PATH,
    replace_file=os.replace,
) -> dict[str, Any]:
    history_id = str(payload.get("history_id") or "").strip()
    if not history_id:
        raise PlaybookAuthoringError({"error": "Provide a playbook history id to restore."}, status=400)

    try:
        with locked_playbook(playbook_path):
            history = read_playbook_history(playbook_path=playbook_path)
            source_entry = next((entry for entry in history if str(entry.get("id") or "") == history_id), None)
            if source_entry is None:
                raise PlaybookAuthoringError({"error": "Playbook history entry was not found."}, status=404)
            snapshot = source_entry.get("snapshot")
            if not isinstance(snapshot, dict):
                raise PlaybookAuthoringError(
                    {"error": "Playbook history entry does not include a restorable snapshot."},
                    status=409,
                )
            validate_playbook(snapshot)
            previous_playbook = read_playbook_from_path(playbook_path) if playbook_path.exists() else None
            restored_playbook = json.loads(json.dumps(snapshot))
            # Same Layer-1 consistency-lint hard-gate publish enforces: a historical
            # snapshot that validates but is lint-FAILING must never be restored into
            # the live rules. Runs before any disk write, so rejection is a no-op, and
            # reuses the fail-open helper (a lint bug logs + treats as clean).
            _enforce_playbook_lint(restored_playbook)
            existing_runtime = read_playbook_runtime(playbook_path=playbook_path)
            runtime = {
                "version": PLAYBOOK_RUNTIME_VERSION,
                **_active_runtime_from_playbook(
                    restored_playbook,
                    actor=_actor_from_payload(payload),
                    source="restore",
                ),
                **_draft_runtime_fields(existing_runtime),
            }
            history.insert(0, _history_entry(
                restored_playbook,
                actor=_actor_from_payload(payload),
                action="restore",
                previous_playbook=previous_playbook,
                restored_from_id=history_id,
                summary=f"Restored playbook version from {str(source_entry.get('recorded_at') or 'history')}.",
            ))
            write_active_playbook_bundle_atomically(
                restored_playbook,
                runtime,
                history,
                playbook_path=playbook_path,
                replace_file=replace_file,
            )
    except PlaybookAuthoringError:
        raise
    except PlaybookTemplateError as error:
        raise PlaybookAuthoringError({"error": str(error)}, status=400) from error
    except OSError as error:
        raise PlaybookAuthoringError({"error": "Playbook could not be restored."}, status=500) from error
    finally:
        # AFTER the lock is released (the with-block has exited); runs on failure
        # paths too -- see the helper's docstring for both constraints.
        _reset_playbook_derived_caches()

    return {
        "playbook": restored_playbook,
        "active": _active_payload(restored_playbook, runtime),
        "draft": public_playbook_draft(runtime),
        "history": public_playbook_history(history),
        "restored_at": datetime.now(timezone.utc).isoformat(),
    }


_PLAYBOOK_RULES_FAILURE_PREFIX = "Playbook rules validation failed: "
_TOP_LEVEL_PLAYBOOK_FIELDS = ("name", "version", "clauses")

# Field paths the validators name in their messages, longest-first so that
# "rules.approved_options" wins over "rules" and "approved_options".
_KNOWN_PLAYBOOK_FIELD_PATHS = (
    "rules.evidence_requirements.minimum_evidence_for_pass",
    "rules.evidence_requirements.minimum_evidence_for_fail",
    "rules.evidence_requirements.quote_required",
    "rules.evidence_requirements.guidance",
    "rules.evidence_requirements",
    "rules.redline_guidance.default_action",
    "rules.redline_guidance",
    "rules.approved_options",
    "rules.pass_conditions",
    "rules.fail_conditions",
    "rules.review_triggers",
    "rules.acceptable_position",
    "rules.clause_type",
    "rules.version",
    "rules",
    "approved_laws",
    "preferred_law",
    "law_phrases",
    "max_term_years",
    "indefinite_terms",
    "longer_survival_carve_out_terms",
    "search_terms",
    "taxonomy_groups",
    "semantic_signals",
    "redline_template",
    "preferred_position",
    "check_trigger",
    "requirement",
    "name",
    "type",
    "id",
)


def collect_playbook_validation_errors(playbook: Any) -> list[dict[str, Any]]:
    """Return every validation error for a candidate Playbook as structured records."""
    if not isinstance(playbook, dict):
        return [_structured_playbook_error("Playbook payload must include a playbook object.")]

    messages: list[str] = []
    seen: set[str] = set()

    def _add(raw_message: str) -> None:
        message = str(raw_message or "").strip()
        if not message or message in seen:
            return
        seen.add(message)
        messages.append(message)

    try:
        validate_playbook(playbook)
    except PlaybookTemplateError as error:
        for part in _split_template_error(str(error)):
            _add(part)

    try:
        validate_playbook_rules(playbook)
    except PlaybookRulesError as error:
        for rule_error in error.errors:
            _add(rule_error)

    for lint_message in lint_violations_for(playbook):
        _add(lint_message)

    return [_structured_playbook_error(message) for message in messages]


def _workspace_payload(
    playbook: dict[str, Any],
    runtime: dict[str, Any],
    draft: dict[str, Any] | None,
    history: list[dict[str, Any]],
    *,
    include_playbook: bool,
) -> dict[str, Any]:
    payload = {
        "active": _active_payload(playbook, runtime),
        "draft": public_playbook_draft_payload(runtime, draft),
        "history": public_playbook_history(history),
        # Programmatic map of (clause_id -> [server-derived field names]) so the editor
        # FE can grey out + explain the inputs (governing_law / term_and_survival
        # preferred_position + check_trigger) that the server re-derives and would
        # otherwise silently discard an admin edit to. Derivation behaviour is unchanged;
        # this only makes it DISCOVERABLE.
        "derived_fields": _derived_fields_map(playbook),
    }
    if include_playbook:
        payload = {"playbook": playbook, **payload}
    return payload


def _derived_fields_map(playbook: dict[str, Any]) -> dict[str, list[str]]:
    """Map each clause id to its SERVER-DERIVED (read-only) field names.

    Reads :func:`derived_policy_fields` so it stays the single source of truth with the
    packet-build re-derivation. Only clauses that actually carry derived fields appear,
    keyed by clause id, so the FE can look up ``derived_fields[clause_id]`` directly.
    """
    clauses = playbook.get("clauses", [])
    out: dict[str, list[str]] = {}
    if isinstance(clauses, list):
        for clause in clauses:
            if not isinstance(clause, Mapping):
                continue
            derived = derived_policy_fields(clause)
            if derived:
                clause_id = str(clause.get("id") or "")
                if clause_id:
                    out[clause_id] = list(derived)
    return out


def _active_payload(playbook: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "playbook": playbook,
        "metadata": public_playbook_runtime(runtime),
    }


def _split_template_error(message: str) -> list[str]:
    text = str(message or "").strip()
    if not text:
        return []
    if text.startswith(_PLAYBOOK_RULES_FAILURE_PREFIX):
        remainder = text[len(_PLAYBOOK_RULES_FAILURE_PREFIX):]
        parts = [part.strip() for part in remainder.split(";")]
        return [part for part in parts if part]
    return [text]


def _structured_playbook_error(message: str) -> dict[str, Any]:
    clause_id, field = _locate_playbook_error(message)
    location = clause_id or ""
    if clause_id and field:
        location = f"{clause_id}.{field}"
    elif not clause_id and field:
        location = field
    return {
        "location": location,
        "clause": clause_id,
        "field": field,
        "message": message,
        "severity": "error",
    }


def _locate_playbook_error(message: str) -> tuple[str | None, str | None]:
    text = str(message or "").strip()
    clause_id: str | None = None
    match = re.match(r"^Playbook clause ([A-Za-z0-9_]+)\b", text)
    if match:
        clause_id = match.group(1)
    elif re.match(r"^Playbook clauses\[", text):
        clause_id = None
    else:
        for top_field in _TOP_LEVEL_PLAYBOOK_FIELDS:
            if re.match(rf"^Playbook {top_field}\b", text):
                return None, top_field

    field = _extract_field_path(text)
    return clause_id, field


def _extract_field_path(message: str) -> str | None:
    best: tuple[int, int, str] | None = None
    for field_path in _KNOWN_PLAYBOOK_FIELD_PATHS:
        match = re.search(rf"\b{re.escape(field_path)}\b", message)
        if match is None:
            continue
        candidate = (match.start(), -len(field_path), field_path)
        if best is None or candidate < best:
            best = candidate
    return best[2] if best else None
