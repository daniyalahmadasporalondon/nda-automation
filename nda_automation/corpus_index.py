"""Corpus index — a read-only filing-cabinet view of a user's whole NDA corpus.

The Corpus tab groups a user's NDAs **Counterparty -> Contract (matter) ->
lifecycle artifacts**. There is no search, no AI, and no write/send/delete action
here; this module only *reads* and *reconciles* two sources of truth:

* **App-state (rich/fast):** ``repository.list_matters`` + :mod:`artifact_registry`
  + :mod:`workflow` — the authoritative, tenant-filtered live state.
* **Drive (durable/complete):** the app-owned ``NDAs`` tree, crawled read-only via
  the four :mod:`drive_integration` listing helpers, with each matter folder's
  ``metadata/matter_summary.json`` as the reconciliation record.

The two are merged by ``matter_id`` so a matter that survives only in Drive (after
a ``/tmp`` wipe wiped app-state) still appears — that is the whole point of the
Drive pass. The app-state pass is the tenant filter and runs every request; only
the (heavier) Drive listing is cached, per-owner, behind a short TTL.

drive.file scope keeps this safe across tenants: the Drive token is the signed-in
user's own, and ``drive.file`` only exposes folders THIS app created for THIS user,
so the Drive crawl can never surface another tenant's documents.

This module is a pure leaf (like ``matter_summary`` / ``workflow``): it takes a
repository + ids + an optional injected ``drive_service`` and ``clock``, so it is
fully testable without HTTP or a live Drive.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import (
    app_settings,
    artifact_registry,
    drive_integration,
    governing_law_view,
    review_state,
    workflow,
)

# How long a per-owner Drive listing stays warm before a fresh crawl. The
# app-state pass is always cheap and runs every request; only the Drive crawl is
# cached so the tab does not hammer Drive on every load.
CORPUS_DRIVE_CACHE_TTL_SECONDS = 300

# drive.reason codes the frontend keys off when reconciled=false.
REASON_NOT_CONNECTED = "not_connected"
REASON_DRIVE_ERROR = "drive_error"
REASON_RATE_LIMITED = "rate_limited"
REASON_DRIVE_SKIPPED = "drive_skipped"

# Reconciliation provenance for a matter.
SOURCE_APP = "app"
SOURCE_DRIVE = "drive"
SOURCE_BOTH = "both"

# role -> lifecycle stage label for an artifact. Mirrors artifact_registry.stage_for
# but is kept simple/stable for display (the FE shows it verbatim). An outbound
# artifact (role "sent") reads as "sent".
_ROLE_STAGE_LABELS = {
    artifact_registry.ROLE_ORIGINAL: "received",
    artifact_registry.ROLE_REDLINE: "ai_redline",
    artifact_registry.ROLE_REVIEWED: "legal_review",
    artifact_registry.ROLE_GENERATED: "draft",
    artifact_registry.ROLE_COUNTER: "counter",
    artifact_registry.ROLE_SENT: "sent",
    artifact_registry.ROLE_SIGNED: "signed",
}

# The schema version stamped on a matter_summary.json ``facets`` block. corpus_index
# keys ``facets_available`` off the PRESENCE of this block, so a legacy summary
# written before the facets enrichment degrades gracefully (see _drive_facets).
FACETS_SCHEMA_VERSION = 1

# Workflow statuses that resolve the ``signed`` facet. Anything else (intake /
# review / approval, pre-send) resolves to ``None`` -> "unknown", so a signed
# filter never silently includes or excludes a pre-send matter. Mirrors the FE
# ``matterSigned`` polarity exactly.
_SIGNED_TRUE_STATUSES: frozenset[str] = frozenset({workflow.STATUS_FULLY_SIGNED})
_SIGNED_FALSE_STATUSES: frozenset[str] = frozenset(
    {
        workflow.STATUS_SENT_AWAITING_COUNTERPARTY,
        workflow.STATUS_COUNTER_RECEIVED,
        workflow.STATUS_SENDING,
    }
)


def _empty_facets(*, available: bool) -> dict[str, Any]:
    """The all-empty facet block. With ``available=False`` it is the legacy/degraded
    shape: every facet at its empty/null value so a facet filter never positively
    matches (the graceful-degradation linchpin)."""
    return {
        "governing_law": "",
        "signed": None,
        "has_clauses": [],
        "term_years": None,
        # The workflow enums (phase/status) the existing status/phase search
        # dimensions filter on, surfaced here so the FE adapter can reconstruct a
        # workflow_state over a corpus matter (which otherwise only carries the
        # board_column + phase_label display strings). "" -> won't match any enum.
        "phase": "",
        "status": "",
        "facets_available": available,
    }


def _signed_from_status(status: str) -> bool | None:
    token = str(status or "").strip().lower()
    if token in _SIGNED_TRUE_STATUSES:
        return True
    if token in _SIGNED_FALSE_STATUSES:
        return False
    return None


def _flatten_clause_ids(clause_ids: dict[str, Any]) -> list[str]:
    """Union of the pass/review/check clause-id buckets, de-duplicated + ordered.

    Caveat (carry from memory): ``non_solicitation``/``non_compete`` are dynamic
    clauses only the AI-first engine emits -- a deterministically-reviewed matter
    never lists them, so ``has_clause`` for those resolves only on AI-reviewed
    matters. Not fixed here; the matcher comment flags it.
    """
    seen: dict[str, None] = {}
    if isinstance(clause_ids, dict):
        for bucket in ("pass", "review", "check"):
            ids = clause_ids.get(bucket)
            if isinstance(ids, list):
                for clause_id in ids:
                    token = str(clause_id or "").strip()
                    if token:
                        seen.setdefault(token, None)
    return list(seen)


def _app_clause_ids(matter: dict[str, Any]) -> list[str]:
    """The matter's clause-id buckets, preferring the stored ``review_state`` and
    re-deriving from ``review_result`` clauses only when the stored block is absent."""
    stored = matter.get("review_state")
    if isinstance(stored, dict) and isinstance(stored.get("clause_ids"), dict):
        return _flatten_clause_ids(stored["clause_ids"])
    review_result = matter.get("review_result")
    if isinstance(review_result, dict):
        try:
            derived = review_state.review_state_from_result(review_result)
        except Exception:  # noqa: BLE001 -- an odd review shape never breaks the index.
            return []
        if isinstance(derived, dict) and isinstance(derived.get("clause_ids"), dict):
            return _flatten_clause_ids(derived["clause_ids"])
    return []


def _app_term_years(matter: dict[str, Any]) -> float | None:
    """Best-effort term in years from the stored ``term_and_survival`` clause result.

    Reads the clean ``term_years`` scalar the checker persists; absent -> None (the
    term facet degrades to "unknown" rather than guessing). Never raises.
    """
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return None
    clauses = review_result.get("clauses")
    if not isinstance(clauses, list):
        return None
    for clause in clauses:
        if not isinstance(clause, dict) or str(clause.get("id") or "") != "term_and_survival":
            continue
        value = clause.get("term_years")
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _app_facets(matter: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Derive the rich-facet block for an app-state matter (live, from review data).

    Any single derivation failure degrades to the empty value for that facet rather
    than breaking the corpus index; ``facets_available`` stays true because the
    matter IS in app-state (it just may carry sparse review data).
    """
    facets = _empty_facets(available=True)
    try:
        facets["governing_law"] = governing_law_view.derive_governing_law(matter)
    except Exception:  # noqa: BLE001
        facets["governing_law"] = ""
    try:
        facets["signed"] = _signed_from_status(str(state.get("status") or ""))
    except Exception:  # noqa: BLE001
        facets["signed"] = None
    try:
        facets["has_clauses"] = _app_clause_ids(matter)
    except Exception:  # noqa: BLE001
        facets["has_clauses"] = []
    try:
        facets["term_years"] = _app_term_years(matter)
    except Exception:  # noqa: BLE001
        facets["term_years"] = None
    facets["phase"] = str(state.get("phase") or "")
    facets["status"] = str(state.get("status") or "")
    return facets


def _drive_facets(summary: dict[str, Any]) -> dict[str, Any]:
    """Read the durable ``facets`` block from a matter_summary.json (see §2).

    A legacy summary written before the facets enrichment has no ``facets`` block;
    that matter degrades to ``facets_available=False`` so a facet filter skips it
    (text/counterparty/date search still works). The values are read defensively so
    a hand-edited durable summary can never break the index.
    """
    raw = summary.get("facets") if isinstance(summary, dict) else None
    if not isinstance(raw, dict) or raw.get("schema_version") is None:
        return _empty_facets(available=False)
    signed = raw.get("signed")
    has_clauses = raw.get("has_clauses")
    term_years = raw.get("term_years")
    # phase/status come from the durable workflow_state block, not the facets block
    # (drive_integration writes them there); read defensively so a hand-edited summary
    # can never break the index.
    workflow_state = summary.get("workflow_state") if isinstance(summary.get("workflow_state"), dict) else {}
    return {
        "governing_law": str(raw.get("governing_law") or ""),
        "signed": signed if isinstance(signed, bool) else None,
        "has_clauses": [str(c).strip() for c in has_clauses if str(c or "").strip()]
        if isinstance(has_clauses, list)
        else [],
        "term_years": float(term_years)
        if isinstance(term_years, (int, float)) and not isinstance(term_years, bool) and term_years > 0
        else None,
        "phase": str((workflow_state or {}).get("phase") or ""),
        "status": str((workflow_state or {}).get("status") or ""),
        "facets_available": True,
    }


# --- per-owner Drive-listing cache ----------------------------------------
_CACHE_LOCK = threading.Lock()
# owner_user_id -> {"built_at": float (monotonic-ish epoch), "built_at_iso": str,
#                   "drive": {...}, "drive_matters": {matter_id: {...}},
#                   "drive_orphans": [ {...} ]}
_DRIVE_CACHE: dict[str, dict[str, Any]] = {}


def invalidate_cache(owner_user_id: str = "") -> None:
    """Drop the cached Drive listing for one owner, or the whole cache when empty."""
    with _CACHE_LOCK:
        if owner_user_id:
            _DRIVE_CACHE.pop(owner_user_id, None)
        else:
            _DRIVE_CACHE.clear()


def _now_epoch(clock: Optional[Callable[[], float]]) -> float:
    if clock is not None:
        return float(clock())
    return datetime.now(timezone.utc).timestamp()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- public entrypoint -----------------------------------------------------
def build_corpus(
    repository,
    owner_user_id: str,
    drive_owner_user_id: str,
    *,
    drive_service: Any | None = None,
    force_refresh: bool = False,
    clock: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Build the corpus index payload for one owner (see module docstring).

    Reads app-state every call (the authoritative tenant filter); the Drive crawl
    is cached per owner under a short TTL. ``force_refresh`` bypasses the cache.
    ``drive_service``/``clock`` are injectable for tests. A Drive hiccup never
    raises out of here — it degrades to an app-state-only corpus with
    ``drive.reconciled=false`` and a ``drive.reason`` code.
    """
    # 1. App-state pass — always runs; it is the tenant filter and the rich source.
    app_matters = _build_app_state_matters(repository, owner_user_id)

    # 2. Drive pass — cached per owner; only when Drive is connected.
    drive_result = _drive_pass(
        drive_owner_user_id,
        drive_service=drive_service,
        force_refresh=force_refresh,
        clock=clock,
    )

    # 3. Merge by matter_id + 4. group/sort.
    return _assemble(app_matters, drive_result)


def flatten_corpus(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a ``build_corpus`` payload's ``groups[].matters[]`` into a flat list.

    The single place the grouped corpus payload is unfolded into the flat matter
    list the search matcher / analytical counts consume, so the FE adapter and the
    assistant share one contract. Tolerant of a malformed payload (returns []).
    """
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return []
    flat: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        matters = group.get("matters")
        if isinstance(matters, list):
            flat.extend(matter for matter in matters if isinstance(matter, dict))
    return flat


# --- app-state pass --------------------------------------------------------
def _build_app_state_matters(repository, owner_user_id: str) -> dict[str, dict[str, Any]]:
    """Map matter_id -> a partially-built CorpusMatter from app-state."""
    matters: dict[str, dict[str, Any]] = {}
    for matter in repository.list_matters(owner_user_id=owner_user_id):
        matter_id = str(matter.get("id") or "")
        if not matter_id:
            continue
        counterparty = artifact_registry.derive_counterparty(matter)
        state = workflow.workflow_state(matter)
        artifacts = artifact_registry.matter_artifacts(matter)
        drive_block = matter.get("drive") if isinstance(matter.get("drive"), dict) else {}
        synced_url = str(drive_block.get("matter_folder_url") or "")

        matters[matter_id] = {
            "matter_id": matter_id,
            "counterparty": counterparty,
            "title": _app_title(matter),
            "created_at": str(matter.get("created_at") or ""),
            # Workflow axis = the Repository board column (FE renders it via
            # RepositoryModel.boardColumnLabel). The dead 6-phase phase_label is
            # NOT surfaced; "On file" is a SOURCE state, not a workflow status.
            "status": str(state.get("board_column") or ""),
            "source": SOURCE_APP,
            "in_app": True,
            "open_matter_url": _open_matter_url(matter_id),
            "open_in_drive_url": synced_url,
            "duplicate": False,
            "duplicate_folder_urls": [],
            "facets": _app_facets(matter, state),
            "artifacts": [
                _app_artifact(matter_id, sequence, artifact)
                for sequence, artifact in enumerate(artifacts, start=1)
            ],
        }
    return matters


def _app_title(matter: dict[str, Any]) -> str:
    for key in ("document_title", "subject"):
        value = str(matter.get(key) or "").strip()
        if value:
            return value
    return "NDA"


def _app_artifact(matter_id: str, sequence: int, artifact: artifact_registry.Artifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "sequence": sequence,
        "role": artifact.role,
        "actor": artifact.actor,
        "version": artifact.version,
        "filename": artifact.name,
        "stage_label": _stage_label(artifact.role),
        "created_at": artifact.created_at,
        "drive_file_url": "",
        "download_url": _artifact_download_url(matter_id, artifact.id),
    }


def _stage_label(role: str) -> str:
    return _ROLE_STAGE_LABELS.get(str(role or "").strip().casefold(), str(role or "") or "doc")


def _safe_int(value: object, default: int) -> int:
    """Coerce a Drive summary integer field, falling back to ``default``.

    ``matter_summary.json`` is durable, hand-editable Drive data, so a record may
    carry a non-numeric ``sequence``/``version`` (e.g. ``"v2"``). Mirrors
    :func:`artifact_registry._coerce_version`: a bad value must never raise out of
    the Drive pass — it degrades to a sane default instead.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --- Drive pass ------------------------------------------------------------
def _drive_pass(
    drive_owner_user_id: str,
    *,
    drive_service: Any | None,
    force_refresh: bool,
    clock: Optional[Callable[[], float]],
) -> dict[str, Any]:
    """Return ``{drive, drive_matters, drive_orphans}`` for the merge step.

    ``drive`` is the response ``drive`` block. ``drive_matters`` maps a Drive
    ``matter_id`` to its reconciliation record + folder bookkeeping;
    ``drive_orphans`` are summary-less folders (degraded entries). On any Drive
    error/disconnection the maps are empty and ``drive.reconciled=false``.
    """
    connected = drive_service is not None or drive_integration.drive_connected(drive_owner_user_id)
    if not connected:
        return {
            "drive": _drive_block(connected=False, reconciled=False, reason=REASON_NOT_CONNECTED),
            "drive_matters": {},
            "drive_orphans": [],
        }

    # Serve from a warm cache unless a refresh is forced.
    if not force_refresh:
        cached = _cached_drive(drive_owner_user_id, clock)
        if cached is not None:
            return cached

    try:
        crawl = _crawl_drive(drive_owner_user_id, drive_service=drive_service)
    except drive_integration.DriveRateLimitError:
        return {
            "drive": _drive_block(connected=True, reconciled=False, reason=REASON_RATE_LIMITED),
            "drive_matters": {},
            "drive_orphans": [],
        }
    except drive_integration.DriveNotConnectedError:
        return {
            "drive": _drive_block(connected=False, reconciled=False, reason=REASON_NOT_CONNECTED),
            "drive_matters": {},
            "drive_orphans": [],
        }
    except drive_integration.DriveIntegrationError:
        return {
            "drive": _drive_block(connected=True, reconciled=False, reason=REASON_DRIVE_ERROR),
            "drive_matters": {},
            "drive_orphans": [],
        }

    built_at_iso = _now_iso()
    result = {
        "drive": _drive_block(
            connected=True,
            reconciled=True,
            reason="",
            built_at=built_at_iso,
            from_cache=False,
            stale=False,
        ),
        "drive_matters": crawl["drive_matters"],
        "drive_orphans": crawl["drive_orphans"],
    }
    _store_drive_cache(drive_owner_user_id, result, built_at_iso, clock)
    return result


def _cached_drive(
    drive_owner_user_id: str,
    clock: Optional[Callable[[], float]],
) -> dict[str, Any] | None:
    now = _now_epoch(clock)
    with _CACHE_LOCK:
        entry = _DRIVE_CACHE.get(drive_owner_user_id)
        if entry is None:
            return None
        if (now - float(entry.get("built_at", 0.0))) > CORPUS_DRIVE_CACHE_TTL_SECONDS:
            return None
        # Return a copy so callers cannot mutate the cached entry in place.
        return {
            "drive": {**entry["drive"], "from_cache": True},
            "drive_matters": entry["drive_matters"],
            "drive_orphans": entry["drive_orphans"],
        }


def _store_drive_cache(
    drive_owner_user_id: str,
    result: dict[str, Any],
    built_at_iso: str,
    clock: Optional[Callable[[], float]],
) -> None:
    with _CACHE_LOCK:
        _DRIVE_CACHE[drive_owner_user_id] = {
            "built_at": _now_epoch(clock),
            "drive": dict(result["drive"]),
            "drive_matters": result["drive_matters"],
            "drive_orphans": result["drive_orphans"],
        }


def _crawl_drive(drive_owner_user_id: str, *, drive_service: Any | None) -> dict[str, Any]:
    """Read-only crawl of the app-owned ``NDAs`` tree -> reconciliation records.

    Layout: ``{root_parent}/NDAs/{counterparty}/{matter}/metadata/matter_summary.json``.
    Each matter folder's summary is the reconciliation record. Folders without a
    parseable summary become degraded "orphan" entries naming the folder.
    """
    settings = app_settings.drive_settings()
    parent_id = str(settings.get("folder_id") or "")
    root_id = drive_integration.find_folder(
        name=drive_integration.DEFAULT_ROOT_FOLDER_NAME,
        parent_id=parent_id,
        owner_user_id=drive_owner_user_id,
        service=drive_service,
    )
    drive_matters: dict[str, dict[str, Any]] = {}
    drive_orphans: list[dict[str, Any]] = []
    if not root_id:
        return {"drive_matters": drive_matters, "drive_orphans": drive_orphans}

    counterparty_folders = drive_integration.list_child_folders(
        parent_id=root_id, owner_user_id=drive_owner_user_id, service=drive_service
    )
    for cp_folder in counterparty_folders:
        cp_name = str(cp_folder.get("name") or "")
        cp_id = str(cp_folder.get("id") or "")
        if not cp_id:
            continue
        matter_folders = drive_integration.list_child_folders(
            parent_id=cp_id, owner_user_id=drive_owner_user_id, service=drive_service
        )
        for matter_folder in matter_folders:
            folder_id = str(matter_folder.get("id") or "")
            folder_name = str(matter_folder.get("name") or "")
            if not folder_id:
                continue
            folder_url = drive_integration.folder_web_url(folder_id)
            summary = _read_matter_summary(
                folder_id, drive_owner_user_id, drive_service=drive_service
            )
            if summary is None:
                drive_orphans.append(
                    {
                        "counterparty": cp_name,
                        "folder_name": folder_name,
                        "folder_id": folder_id,
                        "folder_url": folder_url,
                    }
                )
                continue
            matter_id = str(summary.get("matter_id") or "")
            record = {
                "summary": summary,
                "counterparty": cp_name,
                "folder_id": folder_id,
                "folder_url": folder_url,
                "folder_name": folder_name,
            }
            if not matter_id:
                # A summary without a matter_id cannot be a join key; treat the
                # folder as an orphan so it still surfaces (named by the folder).
                drive_orphans.append(
                    {
                        "counterparty": cp_name,
                        "folder_name": folder_name,
                        "folder_id": folder_id,
                        "folder_url": folder_url,
                        "summary": summary,
                    }
                )
                continue
            existing = drive_matters.get(matter_id)
            if existing is None:
                record["duplicate_folder_urls"] = []
                drive_matters[matter_id] = record
            else:
                # Same matter_id in a second Drive folder => duplicate. Keep the
                # first as the canonical folder; list the rest.
                existing["duplicate_folder_urls"].append(folder_url)
    return {"drive_matters": drive_matters, "drive_orphans": drive_orphans}


def _read_matter_summary(
    matter_folder_id: str,
    drive_owner_user_id: str,
    *,
    drive_service: Any | None,
) -> dict[str, Any] | None:
    metadata_id = drive_integration.find_folder(
        name=drive_integration.METADATA_FOLDER_NAME,
        parent_id=matter_folder_id,
        owner_user_id=drive_owner_user_id,
        service=drive_service,
    )
    if not metadata_id:
        return None
    summary_file_id = drive_integration.find_child_file(
        name=drive_integration.MATTER_SUMMARY_FILENAME,
        parent_id=metadata_id,
        owner_user_id=drive_owner_user_id,
        service=drive_service,
    )
    if not summary_file_id:
        return None
    raw = drive_integration.download_file_bytes(
        file_id=summary_file_id, owner_user_id=drive_owner_user_id, service=drive_service
    )
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# --- merge + group ---------------------------------------------------------
def _assemble(
    app_matters: dict[str, dict[str, Any]],
    drive_result: dict[str, Any],
) -> dict[str, Any]:
    drive_matters: dict[str, dict[str, Any]] = drive_result["drive_matters"]
    drive_orphans: list[dict[str, Any]] = drive_result["drive_orphans"]

    merged: list[dict[str, Any]] = []

    # App-state matters, enriched with Drive when the matter_id matches.
    for matter_id, matter in app_matters.items():
        drive_record = drive_matters.get(matter_id)
        if drive_record is not None:
            _merge_drive_into_app(matter, drive_record)
        merged.append(matter)

    # Drive-only matters: a summary matter_id not present in app-state.
    for matter_id, drive_record in drive_matters.items():
        if matter_id in app_matters:
            continue
        merged.append(_drive_only_matter(matter_id, drive_record))

    # Summary-less folders: degraded single entries naming the folder.
    for orphan in drive_orphans:
        merged.append(_orphan_matter(orphan))

    return _group_and_wrap(merged, drive_result["drive"])


def _merge_drive_into_app(matter: dict[str, Any], drive_record: dict[str, Any]) -> None:
    """In-both: prefer app-state fields, fill gaps from the summary, add Drive links."""
    matter["source"] = SOURCE_BOTH
    matter["open_in_drive_url"] = drive_record["folder_url"]
    summary = drive_record.get("summary") or {}

    if not matter.get("created_at"):
        matter["created_at"] = str(summary.get("created_at") or "")
    if not matter.get("counterparty"):
        matter["counterparty"] = str(summary.get("counterparty") or "")

    duplicate_urls = list(drive_record.get("duplicate_folder_urls") or [])
    if duplicate_urls:
        matter["duplicate"] = True
        matter["duplicate_folder_urls"] = duplicate_urls

    # Backfill drive_file_url onto app artifacts by artifact_id where the summary
    # carries a Drive URL (a download still goes through the app-state route).
    drive_urls = _summary_artifact_urls(summary)
    if drive_urls:
        for artifact in matter["artifacts"]:
            url = drive_urls.get(artifact["artifact_id"])
            if url:
                artifact["drive_file_url"] = url


def _drive_only_matter(matter_id: str, drive_record: dict[str, Any]) -> dict[str, Any]:
    summary = drive_record.get("summary") or {}
    workflow_state = summary.get("workflow_state") if isinstance(summary.get("workflow_state"), dict) else {}
    duplicate_urls = list(drive_record.get("duplicate_folder_urls") or [])
    counterparty = str(summary.get("counterparty") or "") or str(drive_record.get("counterparty") or "")
    return {
        "matter_id": str(summary.get("matter_id") or ""),
        "counterparty": counterparty or artifact_registry.COUNTERPARTY_UNKNOWN,
        "title": _drive_only_title(summary, drive_record),
        "created_at": str(summary.get("created_at") or ""),
        # Drive-only: board_column from the summary if present, else "" so the
        # status chip renders "—". "On file" lives on the SOURCE badge only.
        "status": str((workflow_state or {}).get("board_column") or ""),
        "source": SOURCE_DRIVE,
        "in_app": False,
        "open_matter_url": "",
        "open_in_drive_url": drive_record["folder_url"],
        "duplicate": bool(duplicate_urls),
        "duplicate_folder_urls": duplicate_urls,
        "facets": _drive_facets(summary),
        "artifacts": _drive_only_artifacts(summary),
    }


def _drive_only_title(summary: dict[str, Any], drive_record: dict[str, Any]) -> str:
    for key in ("document_title", "subject"):
        value = str(summary.get(key) or "").strip()
        if value:
            return value
    folder_name = str(drive_record.get("folder_name") or "").strip()
    return folder_name or "NDA"


def _drive_only_artifacts(summary: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    raw = summary.get("artifacts")
    if not isinstance(raw, list):
        return artifacts
    for record in raw:
        if not isinstance(record, dict):
            continue
        role = str(record.get("role") or "")
        artifacts.append(
            {
                "artifact_id": str(record.get("artifact_id") or ""),
                "sequence": _safe_int(record.get("sequence") or 0, 0),
                "role": role,
                "actor": str(record.get("actor") or ""),
                "version": _safe_int(record.get("version") or 1, 1),
                "filename": str(record.get("filename") or ""),
                "stage_label": _stage_label(role),
                "created_at": str(record.get("created_at") or ""),
                "drive_file_url": str(record.get("drive_file_url") or ""),
                # Drive-only artifacts have no app-state bytes to download.
                "download_url": "",
            }
        )
    return artifacts


def _orphan_matter(orphan: dict[str, Any]) -> dict[str, Any]:
    folder_name = str(orphan.get("folder_name") or "").strip() or "NDA"
    summary = orphan.get("summary") if isinstance(orphan.get("summary"), dict) else {}
    return {
        "matter_id": str(summary.get("matter_id") or ""),
        "counterparty": str(orphan.get("counterparty") or "") or artifact_registry.COUNTERPARTY_UNKNOWN,
        "title": folder_name,
        "created_at": str(summary.get("created_at") or ""),
        # Summary-less orphan: no workflow status; chip renders "—".
        "status": "",
        "source": SOURCE_DRIVE,
        "in_app": False,
        "open_matter_url": "",
        "open_in_drive_url": str(orphan.get("folder_url") or ""),
        "duplicate": False,
        "duplicate_folder_urls": [],
        "facets": _drive_facets(summary),
        "artifacts": _drive_only_artifacts(summary),
    }


def _summary_artifact_urls(summary: dict[str, Any]) -> dict[str, str]:
    urls: dict[str, str] = {}
    raw = summary.get("artifacts")
    if not isinstance(raw, list):
        return urls
    for record in raw:
        if not isinstance(record, dict):
            continue
        artifact_id = str(record.get("artifact_id") or "")
        url = str(record.get("drive_file_url") or "")
        if artifact_id and url:
            urls[artifact_id] = url
    return urls


def _group_and_wrap(matters: list[dict[str, Any]], drive_block: dict[str, Any]) -> dict[str, Any]:
    groups_by_cp: dict[str, list[dict[str, Any]]] = {}
    for matter in matters:
        counterparty = str(matter.get("counterparty") or "") or artifact_registry.COUNTERPARTY_UNKNOWN
        matter["counterparty"] = counterparty
        matter["artifact_count"] = len(matter["artifacts"])
        groups_by_cp.setdefault(counterparty, []).append(matter)

    groups: list[dict[str, Any]] = []
    for counterparty in sorted(groups_by_cp, key=lambda name: name.casefold()):
        cp_matters = sorted(
            groups_by_cp[counterparty],
            key=lambda matter: str(matter.get("created_at") or ""),
            reverse=True,
        )
        groups.append(
            {
                "counterparty": counterparty,
                "matter_count": len(cp_matters),
                "matters": cp_matters,
            }
        )

    matter_count = sum(group["matter_count"] for group in groups)
    return {
        "groups": groups,
        "matter_count": matter_count,
        "counterparty_count": len(groups),
        "drive": drive_block,
    }


# --- url builders ----------------------------------------------------------
def _open_matter_url(matter_id: str) -> str:
    return f"/?tab=corpus&matter={matter_id}" if matter_id else ""


def _artifact_download_url(matter_id: str, artifact_id: str) -> str:
    if not matter_id or not artifact_id:
        return ""
    return f"/api/corpus/artifacts/{matter_id}/{artifact_id}"


def _drive_block(
    *,
    connected: bool,
    reconciled: bool,
    reason: str,
    built_at: str = "",
    from_cache: bool = False,
    stale: bool = False,
) -> dict[str, Any]:
    return {
        "connected": connected,
        "reconciled": reconciled,
        "reason": reason,
        "built_at": built_at,
        "from_cache": from_cache,
        "stale": stale,
    }
