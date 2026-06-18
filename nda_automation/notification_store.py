"""File-backed, locked, capped failure-event log surfaced through the toast UI.

This is the durable spine behind the in-app FAILURE toasts. Integration code
(Drive archive, Gmail readiness, DocuSign reconnect, AI-key invalid, ...) calls
``emit_event(...)`` when an operator-actionable failure happens; the frontend
notifications controller polls ``GET /api/notifications`` and pops ONE toast per
new active event through the existing toast machinery (no new bell/badge/center).

Design mirrors ``matter_store``:

* A LOCAL ``threading.RLock`` (``_NOTIFICATIONS_LOCK``) plus an OWN on-disk
  ``notifications.lock`` flock -- deliberately NOT reusing matter_store's lock, so
  an emit on a failure path can never contend with / be blocked by the matter
  store's critical section.
* The atomic tmp+replace+fsync write pattern via ``durable_io.fsync_directory``
  so a crash mid-write never leaves a torn ``notifications.json``.

CRUCIAL CONTRACT: ``emit_event`` NEVER raises into its caller. It is invoked from
already-failing code paths; a notification write that itself errored must not turn
a recoverable failure into an unhandled exception. The whole body is wrapped in a
log-and-swallow guard. Every other reader/mutator may raise
``NotificationStoreError`` (they're called from request handlers that have an
exception ladder), but ``emit_event`` is the one swallow-only entry point.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import durable_io

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = (
    Path(os.environ["NDA_DATA_DIR"]).expanduser()
    if os.environ.get("NDA_DATA_DIR")
    else ROOT / "data"
)
NOTIFICATIONS_PATH = DATA_DIR / "notifications.json"

# LOCAL lock — intentionally distinct from matter_store's lock (see module docstring).
_NOTIFICATIONS_LOCK = threading.RLock()
_LOCK_TIMEOUT_SECONDS = 30

# Hard cap on stored events. When exceeded we shed NON-active events first
# (resolved/dismissed, oldest-first) so the actionable active set is preserved as
# long as possible; only once no shed-able rows remain do we drop oldest active.
MAX_EVENTS = 500

_VALID_SOURCES = frozenset({"ai", "drive", "gmail", "docusign", "system"})
_VALID_SEVERITIES = frozenset({"error", "warning", "info"})
_VALID_STATUSES = frozenset({"active", "resolved", "dismissed"})


class NotificationStoreError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# dedupe_key builder helpers
#
# A dedupe_key identifies a recurring failure CONDITION (not a single
# occurrence), so the same condition firing repeatedly bumps a count instead of
# spamming a new row + a new toast each poll. Integration code should build keys
# through these helpers so the keyspace stays consistent across the later
# emit-wiring pass.
# --------------------------------------------------------------------------- #
def drive_archive_key(matter_id: str) -> str:
    """A failed executed-Drive auto-archive for one matter."""
    return f"drive_archive:{str(matter_id or '').strip() or 'unknown'}"


def docusign_reconnect_key(owner: str) -> str:
    """DocuSign needs reconnecting for an owner (token expired/revoked)."""
    return f"docusign_reconnect:{str(owner or '').strip() or 'global'}"


def ai_key_invalid_key() -> str:
    """The configured AI/OpenRouter API key is missing or rejected (global)."""
    return "ai_key_invalid"


def gmail_not_ready_key(owner: str, reason: str) -> str:
    """Gmail inbound is not ready for an owner, qualified by a reason code."""
    owner_part = str(owner or "").strip() or "global"
    reason_part = str(reason or "").strip() or "unknown"
    return f"gmail_not_ready:{owner_part}:{reason_part}"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def emit_event(
    *,
    source: str,
    severity: str,
    title: str,
    detail: str = "",
    matter_id: str | None = None,
    dedupe_key: str,
    status: str = "active",
) -> dict[str, Any] | None:
    """Record (or dedupe-bump) a failure event. NEVER raises into the caller.

    If an ACTIVE event already exists for ``dedupe_key``, no new row is added:
    its ``count``/``last_seen_at``/``updated_at`` are bumped (and ``detail`` is
    refreshed) so the frontend, which toasts once per NEW active id, does not
    re-toast. Otherwise a fresh event row is appended.

    Returns the stored event dict on success, or ``None`` if anything went wrong
    (the failure is logged, never propagated) — the caller is already on a
    failure path and must not be derailed by a notification-write error.
    """
    try:
        clean_source = _coerce_choice(source, _VALID_SOURCES, "system")
        clean_severity = _coerce_choice(severity, _VALID_SEVERITIES, "error")
        clean_status = _coerce_choice(status, _VALID_STATUSES, "active")
        clean_key = str(dedupe_key or "").strip()
        if not clean_key:
            # A dedupe_key is mandatory; without it we cannot collapse repeats.
            # Fall back to a unique key so the event is still recorded once.
            clean_key = f"adhoc:{uuid.uuid4().hex}"
        clean_title = str(title or "").strip() or "Integration failure"
        clean_detail = str(detail or "")
        clean_matter_id = str(matter_id).strip() if matter_id else None
        now = _now()

        with _locked_store():
            events = _load_events()
            existing = None
            if clean_status == "active":
                for event in events:
                    if (
                        event.get("dedupe_key") == clean_key
                        and event.get("status") == "active"
                    ):
                        existing = event
                        break
            if existing is not None:
                existing["count"] = int(existing.get("count") or 1) + 1
                existing["last_seen_at"] = now
                existing["updated_at"] = now
                if clean_detail:
                    existing["detail"] = clean_detail
                # Title/severity refresh keeps the surfaced toast accurate if the
                # condition's phrasing shifted, without minting a new row.
                existing["title"] = clean_title
                existing["severity"] = clean_severity
                stored = dict(existing)
            else:
                event = {
                    "id": f"ntf_{uuid.uuid4().hex[:12]}",
                    "created_at": now,
                    "updated_at": now,
                    "source": clean_source,
                    "severity": clean_severity,
                    "title": clean_title,
                    "detail": clean_detail,
                    "matter_id": clean_matter_id,
                    "dedupe_key": clean_key,
                    "status": clean_status,
                    "count": 1,
                    "last_seen_at": now,
                    "resolved_at": now if clean_status == "resolved" else None,
                }
                events.append(event)
                stored = dict(event)
            events = _apply_cap(events)
            _save_events(events)
            return stored
    except Exception as error:  # noqa: BLE001 -- emit_event must NEVER raise into a caller.
        # Logged, never propagated: the caller is on a failure path already.
        print(f"notification_store.emit_event swallowed error: {error.__class__.__name__}")
        return None


def resolve_event(dedupe_key: str) -> dict[str, Any] | None:
    """Mark the active event for ``dedupe_key`` resolved (drops it from unread).

    Returns the resolved event, or ``None`` when there was no active event for
    the key. Idempotent: a second call is a no-op returning ``None``.
    """
    clean_key = str(dedupe_key or "").strip()
    if not clean_key:
        return None
    now = _now()
    with _locked_store():
        events = _load_events()
        resolved = None
        for event in events:
            if event.get("dedupe_key") == clean_key and event.get("status") == "active":
                event["status"] = "resolved"
                event["resolved_at"] = now
                event["updated_at"] = now
                resolved = dict(event)
                break
        if resolved is None:
            return None
        _save_events(events)
        return resolved


def dismiss(event_id: str) -> dict[str, Any] | None:
    """Mark a single event dismissed by id (user clicked it away).

    Returns the dismissed event, or ``None`` when no such event exists.
    """
    clean_id = str(event_id or "").strip()
    if not clean_id:
        return None
    now = _now()
    with _locked_store():
        events = _load_events()
        dismissed = None
        for event in events:
            if event.get("id") == clean_id:
                event["status"] = "dismissed"
                event["updated_at"] = now
                dismissed = dict(event)
                break
        if dismissed is None:
            return None
        _save_events(events)
        return dismissed


def list_events(
    *,
    status: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return events newest-first (by ``created_at``), optionally filtered.

    * ``status`` — when given, only events with that status.
    * ``since`` — when given, only events whose ``created_at`` is strictly
      greater (ISO-8601 lexical compare, which is chronological for UTC isoformat).
    * ``limit`` — max rows returned (clamped to [0, MAX_EVENTS]).
    """
    clean_status = str(status).strip() if status else None
    clean_since = str(since).strip() if since else None
    try:
        clamped_limit = max(0, min(int(limit), MAX_EVENTS))
    except (TypeError, ValueError):
        clamped_limit = 100
    with _locked_store():
        events = _load_events()
    filtered = [
        event
        for event in events
        if (clean_status is None or event.get("status") == clean_status)
        and (clean_since is None or str(event.get("created_at") or "") > clean_since)
    ]
    filtered.sort(key=lambda event: str(event.get("created_at") or ""), reverse=True)
    return [dict(event) for event in filtered[:clamped_limit]]


def unread_count() -> int:
    """Count of ACTIVE events (the toast/badge unread signal)."""
    with _locked_store():
        events = _load_events()
    return sum(1 for event in events if event.get("status") == "active")


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_choice(value: object, allowed: frozenset[str], fallback: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in allowed else fallback


@contextmanager
def _locked_store():
    """LOCAL RLock + own ``notifications.lock`` flock (mirrors matter_store).

    Deliberately a SEPARATE lock from matter_store's, so a notification write on
    a failure path cannot contend with the matter store's critical section.
    """
    if not _NOTIFICATIONS_LOCK.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
        raise NotificationStoreError(
            "Notification store could not be locked within the timeout "
            f"({_LOCK_TIMEOUT_SECONDS}s). Please retry."
        )
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (DATA_DIR / "notifications.lock").open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        if time.monotonic() >= deadline:
                            raise NotificationStoreError(
                                "Notification store file lock could not be acquired "
                                f"within the timeout ({_LOCK_TIMEOUT_SECONDS}s). Please retry."
                            ) from exc
                        time.sleep(0.01)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        _NOTIFICATIONS_LOCK.release()


def _load_events() -> list[dict[str, Any]]:
    """Load the event list (MUST be called under ``_locked_store()``).

    A missing file is an empty log. A corrupt/non-list file raises
    ``NotificationStoreError`` for request-handler callers; ``emit_event``
    swallows it via its own guard.
    """
    if not NOTIFICATIONS_PATH.is_file():
        return []
    try:
        with NOTIFICATIONS_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise NotificationStoreError("Notification store could not be read.") from exc
    except json.JSONDecodeError as exc:
        raise NotificationStoreError("Notification store is not valid JSON.") from exc
    if not isinstance(payload, list):
        raise NotificationStoreError("Notification store must contain a JSON list.")
    return [event for event in payload if isinstance(event, dict)]


def _save_events(events: list[dict[str, Any]]) -> None:
    """Atomically persist the event list (tmp+replace+fsync). Under the lock."""
    NOTIFICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = NOTIFICATIONS_PATH.with_name(
        f"{NOTIFICATIONS_PATH.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(events, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(NOTIFICATIONS_PATH)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        raise NotificationStoreError("Notification store could not be written.") from exc
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    durable_io.fsync_directory(NOTIFICATIONS_PATH.parent)


def _apply_cap(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce ``MAX_EVENTS``, shedding NON-active events first (oldest-first).

    Active events are the actionable set; we evict resolved/dismissed rows before
    ever touching an active one. Only if the log is still over-cap after shedding
    every non-active row do we drop the oldest active events.
    """
    if len(events) <= MAX_EVENTS:
        return events
    overflow = len(events) - MAX_EVENTS

    def sort_key(event: dict[str, Any]) -> str:
        return str(event.get("created_at") or "")

    non_active = sorted(
        (event for event in events if event.get("status") != "active"),
        key=sort_key,
    )
    shed_ids: set[int] = set()
    for event in non_active:
        if overflow <= 0:
            break
        shed_ids.add(id(event))
        overflow -= 1
    if overflow > 0:
        # No more non-active rows to shed — drop oldest ACTIVE as a last resort.
        active = sorted(
            (event for event in events if event.get("status") == "active"),
            key=sort_key,
        )
        for event in active:
            if overflow <= 0:
                break
            shed_ids.add(id(event))
            overflow -= 1
    return [event for event in events if id(event) not in shed_ids]
