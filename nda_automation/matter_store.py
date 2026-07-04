from __future__ import annotations

import copy
from collections.abc import Callable
from contextlib import contextmanager, suppress
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import telemetry

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ["NDA_DATA_DIR"]).expanduser() if os.environ.get("NDA_DATA_DIR") else ROOT / "data"
MATTERS_PATH = DATA_DIR / "matters.json"
UPLOADS_DIR = DATA_DIR / "uploads"
# Persistent per-owner Gmail inbound drain cursor. A low-water-mark on Gmail's
# server-assigned ``internalDate`` (epoch ms) recording the oldest message the
# catch-up scan has reached. The inbound query date-bounds below this mark on the
# next poll so the already-drained newest prefix never re-surfaces -- this is what
# lets an arbitrary backlog drain WITHOUT the scan re-paging (and re-``get()``-ing)
# the imported prefix every poll, the defect that previously stalled the catch-up.
GMAIL_INBOUND_CURSORS_PATH = DATA_DIR / "gmail_inbound_cursors.json"
_MATTERS_LOCK = threading.RLock()
_GMAIL_CURSOR_LOCK = threading.RLock()
# --- list_matters read cache (perf #25) -------------------------------------
# ``list_matters`` is hit by the 15s board poll, the notifications poll, the
# corpus build and the assistant, and each call previously re-opened and
# re-parsed EVERY per-matter record file from disk. We cache the parsed full
# (unfiltered) matter list, keyed by a fingerprint of the records directory:
# the sorted tuple of every record file's (name, st_mtime_ns, st_size). Any
# create/update/delete rewrites a record file (atomic tmp+replace) or adds/
# removes one, which changes the fingerprint, so the very next call detects the
# change and re-parses. The cache is ONLY consulted/refreshed while the store
# lock is held (so it observes a consistent on-disk set), and only stores the
# UNFILTERED list -- owner scoping is always applied AFTER, per call, so the
# cache can never serve cross-tenant data. ``_CACHE_DISABLED`` lets tests that
# stress mtime-granularity force the uncached path.
_LIST_CACHE_LOCK = threading.RLock()
_list_cache_fingerprint: tuple[tuple[str, int, int], ...] | None = None
# The cached list is stored SERIALIZED, not as live dicts: a dict mapping each
# record FILENAME (``{cleaned_id}.json`` — exactly one cache entry per on-disk
# record file) to that record's own JSON blob. A hit joins the per-record blobs
# in sorted-filename order (the same order ``_matter_record_paths`` yields) and
# ``json.loads`` the result — fresh, fully-isolated dicts the caller may freely
# mutate (preserving the prior uncached contract) — and a JSON round-trip of this
# JSON-origin data is markedly cheaper than ``copy.deepcopy`` of the parsed list,
# so the cache is a real speed win and never hands out a shared mutable object.
#
# WHY PER-RECORD BLOBS AND NOT ONE WHOLE-LIST BLOB (the original shape): the G3
# write-through patch must update ONE matter after every store write, and with a
# single monolithic blob that meant ``json.loads`` + ``json.dumps`` of the ENTIRE
# store on EVERY write — O(total store bytes) per write, measured at ~330ms/write
# on a 47MB/1200-matter store, serialized under the store lock. Keyed per-record
# blobs make the write patch O(one record): serialize just the written matter and
# replace its single dict entry. Reads remain O(total bytes), as before.
#
# The name ``_list_cache_blob`` is retained from the monolithic-blob era (tests
# reach it by name to simulate a corrupted cache); any non-dict value is treated
# as malformed and fail-safe-dropped.
_list_cache_blob: dict[str, str] | None = None
_list_cache_dir: str | None = None
# Test seam: force list_matters onto the uncached path (e.g. mtime-granularity
# stress tests that deliberately defeat the fingerprint).
_CACHE_DISABLED = bool(os.environ.get("NDA_DISABLE_MATTER_LIST_CACHE"))
# Maximum seconds to wait when acquiring _MATTERS_LOCK or the on-disk flock
# before giving up and raising MatterStoreError.  30 s is well above any
# expected critical-section duration while keeping the export endpoint
# from hanging indefinitely under a stuck background thread.
_LOCK_TIMEOUT_SECONDS = 30
DEFAULT_MAX_STORED_MATTERS = 250
MATTER_RECORDS_DIRNAME = "matters"
PRUNED_ARCHIVE_DIRNAME = "pruned-matters"
MAX_SOURCE_FILENAME_LENGTH = 180
GMAIL_METADATA_FIELDS = (
    "gmail_account",
    "gmail_attachment_reasons",
    "gmail_attachment_id",
    "gmail_attachment_score",
    "gmail_attachment_selector",
    "gmail_attachment_selector_confidence",
    "gmail_attachment_selector_model",
    "gmail_attachment_selector_reason",
    "gmail_attachment_sha256",
    "gmail_detection_excerpt",
    "gmail_detection_sources",
    "gmail_detection_terms",
    # Provenance for e-sign platform notifications captured as likely executed
    # NDAs (the matched exclude-list entry, e.g. "docusign.net") -- lets an
    # operator filter these matters later (future Corpus/executed-NDA tagging).
    "gmail_esign_notification",
    "gmail_message_id",
    "gmail_part_id",
    "gmail_thread_id",
    "needs_triage",
    "reply_to",
    "triage_confidence",
    "triage_reason",
)
MATTER_UPDATE_FIELDS = {
    "awaiting_signature",
    "board_column",
    # Lazily-computed content fingerprint for the Corpus duplicate-document signal
    # (corpus_index caches it here on first build, scalar-compares it thereafter).
    "content_fingerprint",
    "docusign",
    "drive",
    # Outcome of the best-effort executed Drive auto-archive: {status: "ok"|"failed",
    # error, attempted_at}. Recorded so a FAILED archive (the signed copy never
    # reached Drive) is no longer silent — the matter card surfaces a non-blocking
    # "Drive archive failed" warning + Retry. Only written when an archive was
    # actually attempted (Drive connected); an unconnected matter carries no block.
    "drive_archive",
    "executed",
    "executed_at",
    "human_reviewed",
    # Inbound AI-review poison-pill guard (P1-2): how many times the background
    # AI review has FAILED for this matter, and when it last failed. The recovery
    # sweep stops re-enqueuing a matter once the count reaches the cap, so a
    # permanently-failing review (no deterministic fallback) cannot loop forever
    # burning paid assessor+verifier calls.
    "inbound_review_failures",
    "inbound_review_failed_at",
    # Async AI-review lifecycle (async review backend). The heavy AI review now
    # runs OFF the request thread on the storm-hardened inbound worker pool; these
    # three fields let the board + review polls report progress without the route
    # blocking on the ~145-245s pipeline. ``review_status`` is one of
    # idle/in_progress/completed/failed; ``review_started_at`` stamps when the async
    # job was enqueued (the 300s TTL "interrupted, retry" override is computed off
    # it on read); ``review_error`` carries a short reason when the last review failed.
    "review_status",
    "review_started_at",
    "review_error",
    "last_outbound_account",
    "last_outbound_at",
    "last_outbound_filename",
    "last_outbound_message_id",
    "last_outbound_subject",
    "last_outbound_thread_id",
    "last_outbound_to",
    "pdf_annotations",
    # DocuSign terminal-but-not-signed markers (the workflow deriver reads these to
    # surface a declined deal as "needs attention" and a voided one as re-sendable,
    # instead of stuck "awaiting signature" forever).
    "signature_declined",
    "signature_declined_at",
    "signature_voided",
    "signature_voided_at",
    "status",
    # Approach C (PDF→DOCX at ingest): the reconstructed working-DOCX body paragraphs,
    # re-keyed so each carries the reconstructed paragraph's source_index and drops the
    # source_part:"pdf" marker. Stored at ingest so the on-demand review produces
    # redlines that anchor by index into the working DOCX, exactly like native DOCX.
    "working_docx_paragraphs",
    # Approach C observability: the durable outcome of the PDF->working-DOCX retro
    # conversion (converted/timed_out/failed/empty_body/skipped) + a short reason, so a
    # converted PDF that ends with no working DOCX leaves a surfaced signal behind
    # instead of silently failing open. Set by retro_convert_pdf_matter[_guarded].
    "working_docx_status",
    "working_docx_status_reason",
}


class MatterStoreError(RuntimeError):
    """A persistence-layer failure.

    The raised message is intentionally diagnostic (it can name the
    ``<matter_id>.json`` record file, "not valid JSON", or the lock-timeout
    seconds) and is logged server-side. It must NEVER be shown verbatim to a
    user: route handlers translate it through :func:`friendly_matter_store_message`
    into generic, leak-free copy. ``lock_timeout`` distinguishes the transient
    "store is busy, retry" case from a genuine load failure.
    """

    def __init__(self, *args: Any, lock_timeout: bool = False) -> None:
        super().__init__(*args)
        self.lock_timeout = lock_timeout


# Generic, leak-free copy shown to users in place of a raw MatterStoreError
# message. The raw message stays server-side (in the exception / logs) for
# diagnosis; these strings carry no filename, JSON jargon, or lock seconds.
MATTER_STORE_BUSY_MESSAGE = "The system is busy. Please retry in a moment."
MATTER_STORE_UNAVAILABLE_MESSAGE = "We couldn't load this NDA right now. Please refresh and try again."


def friendly_matter_store_message(error: "MatterStoreError") -> str:
    """Map a :class:`MatterStoreError` to user-safe copy (never leaks internals)."""
    if getattr(error, "lock_timeout", False):
        return MATTER_STORE_BUSY_MESSAGE
    return MATTER_STORE_UNAVAILABLE_MESSAGE


def store_change_token() -> Any | None:
    """A cheap opaque change token over the record store, or None.

    A stat scan of the matter record files — the same (name, mtime_ns, size)
    per-file tuple the list cache keys on — so the token changes whenever ANY
    record changes on disk, for the cost of one ``stat`` per file (no reads, no
    parses, no lock). Read-only: never touches the cache itself. ``corpus_index``
    uses it to serve its app-state cache without listing matters when the store
    is untouched. ``None`` (on any error) means "unknown" and callers must fall
    back to a real read.
    """
    try:
        return ("records-dir-stat", _records_dir_fingerprint())
    except Exception:  # noqa: BLE001 -- the token is an optimisation only.
        return None


def list_matters(owner_user_id: str = "") -> list[dict[str, Any]]:
    with _locked_store():
        matters = [
            matter
            for matter in _load_matters_cached()
            if _matter_owner_matches(matter, owner_user_id)
        ]
    return sorted(matters, key=lambda matter: str(matter.get("created_at") or ""), reverse=True)


def export_matters_backup(owner_user_id: str = "") -> dict[str, Any]:
    with _locked_store():
        matters = [
            matter
            for matter in _load_matters()
            if _matter_owner_matches(matter, owner_user_id)
        ]
        documents = [_stored_document_manifest(matter) for matter in matters]
    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "matter_count": len(matters),
        "matters": matters,
        "documents": [document for document in documents if document is not None],
    }


def export_all_matters_backup() -> dict[str, Any]:
    """Owner-UNSCOPED backup: every matter regardless of owner, ownerless included.

    An explicit disaster-recovery/admin export path. It deliberately does NOT
    route through :func:`_matter_owner_matches` (whose empty-owner wildcard is
    the single-tenant convenience, not an export contract), so the fail-closed
    per-owner scoping stays untouched. Callers MUST admin-gate this: the dump
    contains every tenant's full extracted NDA text.
    """
    with _locked_store():
        matters = list(_load_matters())
        documents = [_stored_document_manifest(matter) for matter in matters]
    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "matter_count": len(matters),
        "matters": matters,
        "documents": [document for document in documents if document is not None],
    }


def get_matter(matter_id: str, owner_user_id: str = "") -> dict[str, Any] | None:
    with _locked_store():
        if not MATTERS_PATH.is_file():
            matter = _load_matter_record_by_id(matter_id)
            if matter is not None and _matter_owner_matches(matter, owner_user_id):
                return matter
            return None
        for matter in _load_matters():
            if matter.get("id") == matter_id and _matter_owner_matches(matter, owner_user_id):
                return matter
    return None


def source_document_path(matter: dict[str, Any]) -> Path | None:
    """Resolve a matter's stored source document to a safe path under UPLOADS_DIR.

    Encapsulates the on-disk layout (UPLOADS_DIR + the path-traversal guard) so
    callers never build the path themselves. Returns None when there is no stored
    document, the resolved path escapes UPLOADS_DIR, or the file is missing.
    """
    stored_filename = str(matter.get("stored_filename") or "")
    if not stored_filename:
        return None
    source_path = (UPLOADS_DIR / stored_filename).resolve()
    if source_path.parent != UPLOADS_DIR.resolve() or not source_path.is_file():
        return None
    return source_path


def get_source_document_bytes(matter: dict[str, Any]) -> bytes | None:
    source_path = source_document_path(matter)
    if source_path is None:
        return None
    try:
        return source_path.read_bytes()
    except OSError:
        return None


def find_gmail_attachment(
    message_id: str,
    attachment_id: str,
    *,
    attachment_filename: str = "",
    attachment_sha256: str = "",
    part_id: str = "",
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    if not message_id:
        return None
    with _locked_store():
        return _find_gmail_duplicate_unlocked(
            _load_matters(),
            {
                "attachment_filename": attachment_filename,
                "gmail_attachment_id": attachment_id,
                "gmail_attachment_sha256": attachment_sha256,
                "gmail_message_id": message_id,
                "gmail_part_id": part_id,
                "owner_user_id": _clean_owner_user_id(owner_user_id),
            },
            owner_user_id=owner_user_id,
        )


def gmail_inbound_cursor(owner_user_id: str = "") -> int:
    """The persisted per-owner inbound drain cursor (oldest reached internalDate, ms).

    ``0`` means "no cursor yet" — the catch-up has not paged below the inbox head,
    so the inbound query must NOT date-bound and scans newest-first as before. The
    value is Gmail's server-assigned ``internalDate`` (epoch milliseconds), which is
    monotonic and tamper-proof (unlike the ``Date`` header), so a ``before:`` bound
    derived from it reliably skips the already-drained newest prefix.
    """
    key = _clean_owner_user_id(owner_user_id)
    with _GMAIL_CURSOR_LOCK:
        cursors = _load_gmail_inbound_cursors()
    try:
        return max(0, int(cursors.get(key, 0)))
    except (TypeError, ValueError):
        return 0


def advance_gmail_inbound_cursor(owner_user_id: str, internal_date_ms: int) -> int:
    """Lower the per-owner drain cursor toward ``internal_date_ms`` (monotonic down).

    The cursor only ever moves to an OLDER message (a smaller internalDate) so a
    burst of newly-arrived mail above the frontier can never push it back up and
    re-expose an already-drained region. A non-positive ``internal_date_ms`` is
    ignored (we never learned a real date for the batch). Returns the cursor in
    force after the call.
    """
    key = _clean_owner_user_id(owner_user_id)
    try:
        candidate = int(internal_date_ms)
    except (TypeError, ValueError):
        candidate = 0
    if candidate <= 0:
        return gmail_inbound_cursor(owner_user_id)
    with _GMAIL_CURSOR_LOCK:
        cursors = _load_gmail_inbound_cursors()
        existing = 0
        try:
            existing = int(cursors.get(key, 0))
        except (TypeError, ValueError):
            existing = 0
        # First cursor for this owner, or a strictly-older frontier: persist it.
        if existing <= 0 or candidate < existing:
            cursors[key] = candidate
            _write_json_atomic(GMAIL_INBOUND_CURSORS_PATH, cursors)
            return candidate
        return existing


def reset_gmail_inbound_cursor(owner_user_id: str = "") -> None:
    """Drop the per-owner drain cursor (e.g. after a full backlog drain or reset)."""
    key = _clean_owner_user_id(owner_user_id)
    with _GMAIL_CURSOR_LOCK:
        cursors = _load_gmail_inbound_cursors()
        if key in cursors:
            del cursors[key]
            _write_json_atomic(GMAIL_INBOUND_CURSORS_PATH, cursors)


def _load_gmail_inbound_cursors() -> dict[str, int]:
    if not GMAIL_INBOUND_CURSORS_PATH.is_file():
        return {}
    try:
        with GMAIL_INBOUND_CURSORS_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    cursors: dict[str, int] = {}
    for owner_key, value in payload.items():
        try:
            cursors[str(owner_key)] = int(value)
        except (TypeError, ValueError):
            continue
    return cursors


def update_matter_stage(matter_id: str, board_column: str, owner_user_id: str = "") -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        updated_matter = {
            **matter,
            "board_column": board_column,
            "status": "closed" if board_column == "signed_closed" else "active",
            "updated_at": now,
        }
        _save_matter_record(updated_matter)
        return updated_matter


def update_matter_fields(matter_id: str, fields: dict[str, Any], owner_user_id: str = "") -> dict[str, Any] | None:
    cleaned_fields = {key: value for key, value in fields.items() if key in MATTER_UPDATE_FIELDS}
    if not cleaned_fields:
        return get_matter(matter_id, owner_user_id=owner_user_id)

    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        updated_matter = {
            **matter,
            **cleaned_fields,
            "updated_at": now,
        }
        if "board_column" in cleaned_fields and "status" not in cleaned_fields:
            updated_matter["status"] = "closed" if cleaned_fields["board_column"] == "signed_closed" else "active"
        _save_matter_record(updated_matter)
        return updated_matter


def update_redline_draft(
    matter_id: str,
    redline_draft: dict[str, Any] | None,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        updated_matter = {
            **matter,
            "updated_at": now,
        }
        if redline_draft is None:
            updated_matter.pop("redline_draft", None)
        else:
            updated_matter["redline_draft"] = redline_draft
        _save_matter_record(updated_matter)
        return updated_matter


def update_matter_review(
    matter_id: str,
    review_result: dict[str, Any],
    triage: dict[str, Any],
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        updated_matter = {
            **matter,
            "review_result": review_result,
            **triage,
            # A fresh review supersedes any prior human sign-off.
            "human_reviewed": False,
            "updated_at": now,
        }
        updated_matter.pop("redline_draft", None)
        _save_matter_record(updated_matter)
        return updated_matter


def refresh_matter_review(
    matter_id: str,
    review_result: dict[str, Any],
    triage: dict[str, Any],
    *,
    expected_updated_at: str = "",
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Store a refreshed review WITHOUT clobbering a human edit that raced the AI.

    ``refresh_review`` reads the matter, runs a multi-second AI pass, then writes
    the result. A mark-reviewed (``human_reviewed=True``) or a saved
    ``redline_draft`` landing during that window would be silently reverted by the
    unconditional reset/pop in ``update_matter_review``.

    This writer guards against that. ``expected_updated_at`` is the matter's
    ``updated_at`` as captured at refresh START (before the AI ran). Under the
    store lock we re-read the matter:

    * If its ``updated_at`` still equals ``expected_updated_at`` — NOTHING else
      wrote during the window — we apply the normal refresh semantics: reset
      ``human_reviewed`` (a fresh review supersedes a prior sign-off) and drop the
      now-stale ``redline_draft``.
    * If ``updated_at`` has MOVED — a human (or any) write landed during the
      window — we still store the fresh ``review_result`` + ``triage`` (the refresh
      must not be lost), but we PRESERVE the current ``human_reviewed`` flag and the
      current ``redline_draft`` exactly as they now stand, so the concurrent human
      edit survives.

    An empty ``expected_updated_at`` falls back to the unconditional
    ``update_matter_review`` semantics (reset + pop), so existing callers are
    unaffected.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        current_updated_at = str(matter.get("updated_at") or "")
        raced = bool(expected_updated_at) and current_updated_at != str(expected_updated_at)
        updated_matter = {
            **matter,
            "review_result": review_result,
            **triage,
            "updated_at": now,
        }
        if raced:
            # A write landed during the AI window. Preserve the human edit: keep the
            # matter's CURRENT human_reviewed flag and redline_draft (re-read above)
            # rather than resetting/popping them. ``triage`` may carry a
            # ``human_reviewed`` key; the explicit re-assignment below makes the
            # preservation authoritative regardless of triage's contents.
            updated_matter["human_reviewed"] = matter.get("human_reviewed", False)
            if isinstance(matter.get("redline_draft"), dict):
                updated_matter["redline_draft"] = matter["redline_draft"]
            else:
                updated_matter.pop("redline_draft", None)
        else:
            # Uncontended refresh: a fresh review supersedes any prior human sign-off
            # and the prior redline draft is now stale.
            updated_matter["human_reviewed"] = False
            updated_matter.pop("redline_draft", None)
        _save_matter_record(updated_matter)
        return updated_matter


def update_matter_counterparty(
    matter_id: str,
    counterparty: dict[str, Any],
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Persist a human override of the matter's counterparty (locked R-M-W).

    Writes the coerced block to ``matter["intake_metadata"]["counterparty"]`` -- the
    durable nested storage location (the whole matter dict is json-persisted with no
    field whitelist, so the nested dict round-trips faithfully). This is a NARROW,
    shape-controlled writer: it never routes through ``MATTER_UPDATE_FIELDS`` (which
    excludes ``intake_metadata`` and would silently drop the write), so the only way
    to mutate the counterparty is through this validated path.

    OWNER-SCOPED: returns ``None`` when the matter is missing or not owned by
    ``owner_user_id`` (no cross-tenant writes). The incoming dict is COERCED to the
    canonical ``{name, confidence, verified, first_party, second_party, source}``
    shape before storage, so a malformed override cannot poison the field; an empty
    name can never be ``verified``.
    """
    coerced = _coerce_counterparty_block(counterparty)
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        intake_metadata = matter.get("intake_metadata")
        intake_metadata = dict(intake_metadata) if isinstance(intake_metadata, dict) else {}
        intake_metadata["counterparty"] = coerced
        updated_matter = {
            **matter,
            "intake_metadata": intake_metadata,
            "updated_at": now,
        }
        _save_matter_record(updated_matter)
        return updated_matter


def _coerce_counterparty_block(counterparty: object) -> dict[str, Any]:
    """Coerce an incoming counterparty override to the canonical stored shape.

    Reuses ``review_result_contract._normalize_counterparty_block`` (the single
    source of truth for the block shape) when importable; falls back to a minimal
    inline guard otherwise. Either way the result is the canonical dict, and an
    empty name is never ``verified``.
    """
    try:
        from .review_result_contract import _normalize_counterparty_block

        return _normalize_counterparty_block(counterparty)
    except Exception:  # noqa: BLE001 -- never let a shape-helper import break the write.
        # ``unreviewed`` is the honest default for a block no AI extraction produced;
        # an explicit source on the override (e.g. ``human``/``ai_review_preamble``) is
        # preserved as-is. Mirrors review_result_contract._normalize_counterparty_block.
        block = {
            "name": "",
            "confidence": 0.0,
            "verified": False,
            "first_party": "",
            "second_party": "",
            "source": "unreviewed",
        }
        if not isinstance(counterparty, dict):
            return block
        name = str(counterparty.get("name") or "").strip()
        block["name"] = name
        block["first_party"] = str(counterparty.get("first_party") or "").strip()
        block["second_party"] = str(counterparty.get("second_party") or "").strip()
        block["source"] = str(counterparty.get("source") or "unreviewed").strip() or "unreviewed"
        try:
            confidence = float(counterparty.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence != confidence or confidence in (float("inf"), float("-inf")):
            confidence = 0.0
        block["confidence"] = max(0.0, min(1.0, confidence))
        block["verified"] = bool(counterparty.get("verified")) and bool(name)
        return block


def _matter_was_cleared_for_signature(matter: dict[str, Any]) -> bool:
    """True when a matter is (or was) cleared to send for signature.

    A read-side mirror of ``docusign_workflow.matter_cleared_for_signature``'s
    APPROVAL signals -- kept inline so the store never imports the workflow layer.
    Any of these means the matter had been signed off and a reviewed artifact may
    have been minted against a NOW-superseded decision set:

    * ``status == "approved"`` (the canonical approve transition), OR
    * an ``approved_at`` stamp, OR
    * the board's ``human_reviewed`` flag.

    (The workflow's fourth signal -- a clean auto-review -- is intentionally NOT
    reset here: it reflects the review engine's own verdict, not a human sign-off
    that a later decision edit would contradict.)
    """
    if str(matter.get("status") or "").strip().lower() == "approved":
        return True
    if matter.get("approved_at"):
        return True
    return bool(matter.get("human_reviewed"))


def set_clause_reviewer_decision(
    matter_id: str,
    clause_id: str,
    reviewer_decision: dict[str, Any] | None,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Persist (or clear when None) a single clause's reviewer_decision.

    Decisions live in a matter-level ``reviewer_decisions`` map keyed by clause
    id; the review_result payload is never mutated here.

    RE-APPROVAL GATE (P1): a reviewer decision changed AFTER the matter was cleared
    for signature (approved / marked human-reviewed) must never leave the matter in
    the cleared state -- the reviewed artifact minted at approval baked in the OLD
    decisions, so sending now would sign a document reflecting a decision the
    reviewer has since REVERSED or CHANGED (e.g. accept c1 -> approve -> reject c1;
    the stale reviewed copy still carries c1's redline). We reset the matter OUT of
    the cleared state (back to ``in_review``, clearing ``approved_at`` / ``approver``
    / ``human_reviewed``) so it MUST be re-approved -- and re-approval re-mints the
    correct reviewed artifact (or none, if the decisions now yield no edits, in which
    case the original is signed). This runs on the SAME store-locked write as the
    decision itself, so the un-clear can't be lost-updated by a concurrent send.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        decisions = dict(matter.get("reviewer_decisions") or {})
        if reviewer_decision is None:
            decisions.pop(clause_id, None)
        else:
            decisions[clause_id] = reviewer_decision
        updated_matter = {
            **matter,
            "reviewer_decisions": decisions,
            "updated_at": now,
        }
        if _matter_was_cleared_for_signature(matter):
            # Un-clear: force re-approval so the reviewed artifact is re-minted (or
            # dropped) against the CURRENT decisions. Do not touch review_result /
            # reviewer_decisions beyond the write above.
            updated_matter["status"] = "in_review"
            updated_matter["approved_at"] = None
            updated_matter["approver"] = None
            updated_matter["human_reviewed"] = False
            timeline = list(updated_matter.get("matter_timeline") or [])
            timeline.append({
                "type": "approval_reset",
                "at": now,
                "detail": (
                    "Approval reset: a reviewer decision changed after approval; "
                    "re-approve to refresh the document sent for signature."
                ),
            })
            updated_matter["matter_timeline"] = capped_timeline(timeline)
        _save_matter_record(updated_matter)
        return updated_matter


def record_matter_approval(
    matter_id: str,
    *,
    approver: str,
    approved_at: str,
    timeline_event: dict[str, Any],
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Stamp a matter as approved and append an immutable timeline event."""
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        timeline = list(matter.get("matter_timeline") or [])
        timeline.append(timeline_event)
        updated_matter = {
            **matter,
            "status": "approved",
            "approver": approver,
            "approved_at": approved_at,
            "matter_timeline": capped_timeline(timeline),
            "updated_at": approved_at,
        }
        _save_matter_record(updated_matter)
        return updated_matter


# Cap on the stored ``matter_timeline`` length. The timeline used to be append-only
# UNCAPPED, so a matter accumulating machine-written events (poll cycles, repeated
# transitions) grew without bound — a 5000-event matter added ~950KB to every store
# read/write of that record and to every detail response. Every timeline writer now
# caps through :func:`capped_timeline`: the NEWEST events are kept and the oldest are
# dropped behind a single truncation-marker event carrying the running
# ``dropped_count`` — so the log stays honest about what it no longer holds.
TIMELINE_MAX_EVENTS = 500
TIMELINE_TRUNCATED_EVENT_TYPE = "timeline_truncated"


def capped_timeline(timeline: list[Any]) -> list[Any]:
    """Cap a timeline to ``TIMELINE_MAX_EVENTS``, newest-kept, with a marker event.

    Under the cap the list is returned unchanged (the common case: no copy, no
    marker). Over the cap the OLDEST events are dropped and replaced by ONE leading
    ``timeline_truncated`` marker whose ``dropped_count`` accumulates across
    successive truncations (an existing leading marker is folded into the new one,
    never stacked). The result is exactly ``TIMELINE_MAX_EVENTS`` entries: the
    marker + the newest ``TIMELINE_MAX_EVENTS - 1`` events. Kept events are never
    rewritten — the append-only contract now reads "append-only up to the cap;
    beyond it the oldest history is dropped and counted".
    """
    if len(timeline) <= TIMELINE_MAX_EVENTS:
        return timeline
    already_dropped = 0
    prior_marker_actor = ""
    body = timeline
    head = timeline[0] if timeline else None
    if isinstance(head, dict) and str(head.get("type") or "") == TIMELINE_TRUNCATED_EVENT_TYPE:
        try:
            already_dropped = max(0, int(head.get("dropped_count") or 0))
        except (TypeError, ValueError):
            already_dropped = 0
        prior_marker_actor = str(head.get("actor") or "")
        body = timeline[1:]
    keep = TIMELINE_MAX_EVENTS - 1
    dropped_now = max(0, len(body) - keep)
    total_dropped = already_dropped + dropped_now
    # FAIL-CLOSED actor propagation: the bulk-archive pristine gate
    # (routes/admin._only_system_timeline_events) treats any non-"system" event as
    # evidence of human-adjacent activity. Dropping such an event behind a
    # system-actor marker would LAUNDER that evidence, so the marker inherits a
    # non-system actor whenever any dropped event (or a prior marker) carried one —
    # the truncated log then still fails the pristine gate, exactly like the full
    # log would have.
    marker_actor = "system"
    if prior_marker_actor and prior_marker_actor != "system":
        marker_actor = prior_marker_actor
    else:
        for event in body[:dropped_now]:
            actor = str(event.get("actor") or "") if isinstance(event, dict) else ""
            if actor != "system":
                marker_actor = actor or "unknown"
                break
    marker = {
        "type": TIMELINE_TRUNCATED_EVENT_TYPE,
        "at": datetime.now(timezone.utc).isoformat(),
        "actor": marker_actor,
        "dropped_count": total_dropped,
        "detail": (
            f"Timeline capped at {TIMELINE_MAX_EVENTS} events; "
            f"{total_dropped} oldest event(s) dropped."
        ),
    }
    return [marker, *body[dropped_now:]]


def append_timeline_event(
    matter_id: str,
    timeline_event: dict[str, Any],
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Append one immutable event to a matter's ``matter_timeline`` (append-only).

    The canonical writer for the workflow timeline backbone: every lifecycle
    transition (created, extracted, review_completed, sent, ...) appends through
    here so the log is built one way. Prior events are never rewritten; once the
    log exceeds ``TIMELINE_MAX_EVENTS`` the oldest are dropped behind the
    ``capped_timeline`` truncation marker. Runs under the store lock so a
    concurrent transition can't lose-update the list.
    """
    if not isinstance(timeline_event, dict):
        return get_matter(matter_id, owner_user_id=owner_user_id)
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        timeline = list(matter.get("matter_timeline") or [])
        timeline.append(timeline_event)
        updated_matter = {
            **matter,
            "matter_timeline": capped_timeline(timeline),
            "updated_at": now,
        }
        _save_matter_record(updated_matter)
        return updated_matter


def set_matter_workflow_error(
    matter_id: str,
    workflow_error: dict[str, Any] | None,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Record (or clear when None) the ``workflow_error`` failure marker.

    The single new persisted bit of the workflow layer, written ONLY on failure
    paths (render/AI/send errored). It's what the pure-derive ``workflow.py`` reads
    to surface ``needs_attention`` -- a failure isn't otherwise recoverable from a
    half-written matter. Its own dedicated writer (never folded into an existing
    happy-path writer) so a normal review/artifact/send write can't clobber it and
    it can't clobber them.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        updated_matter = {
            **matter,
            "updated_at": now,
        }
        if workflow_error is None:
            updated_matter.pop("workflow_error", None)
        else:
            updated_matter["workflow_error"] = workflow_error
        _save_matter_record(updated_matter)
        return updated_matter


def migrate_ownerless_matter_ownership(
    *,
    user_email_to_id: dict[str, str],
    admin_user_id: str = "",
) -> dict[str, Any]:
    """Assign an owner to every ownerless matter (idempotent, one-time backfill).

    Ownerless matters (``owner_user_id`` empty/missing — legacy imports, global
    Gmail shared-sync before per-user ownership) are invisible to authenticated
    users after the fail-closed access fix. This backfills a real owner so the
    data is reachable again:

    * PRIMARY — map the matter's ``gmail_account`` (the connected mailbox email)
      to the user who owns that mailbox, via ``user_email_to_id`` (case-folded
      ``email -> user_id`` built by the caller from ``user_store``).
    * FALLBACK — ``admin_user_id`` (the sole/admin user) when no mapping resolves.

    NEVER assigns a wildcard. Only ownerless matters are touched; already-owned
    matters are left exactly as-is, so re-running is a no-op. Returns a summary
    ``{scanned, already_owned, assigned_by_gmail, assigned_to_admin, skipped_unresolved}``.
    """
    email_to_id = {
        _clean_email_key(email): _clean_owner_user_id(user_id)
        for email, user_id in (user_email_to_id or {}).items()
        if _clean_email_key(email) and _clean_owner_user_id(user_id)
    }
    admin_user_id = _clean_owner_user_id(admin_user_id)
    now = datetime.now(timezone.utc).isoformat()

    summary = {
        "scanned": 0,
        "already_owned": 0,
        "assigned_by_gmail": 0,
        "assigned_to_admin": 0,
        "skipped_unresolved": 0,
    }
    with _locked_store():
        _ensure_matter_records_from_legacy()
        for matter in _load_matters():
            summary["scanned"] += 1
            if _clean_owner_user_id(matter.get("owner_user_id")):
                summary["already_owned"] += 1
                continue
            resolved_user_id = email_to_id.get(_clean_email_key(matter.get("gmail_account")))
            assignment = "assigned_by_gmail"
            if not resolved_user_id:
                resolved_user_id = admin_user_id
                assignment = "assigned_to_admin"
            if not resolved_user_id:
                # No gmail mapping and no admin fallback configured: leave the
                # matter ownerless rather than guess. Still single-tenant-visible.
                summary["skipped_unresolved"] += 1
                continue
            updated_matter = {
                **matter,
                "owner_user_id": resolved_user_id,
                "updated_at": now,
            }
            _save_matter_record(updated_matter)
            summary[assignment] += 1
    return summary


def update_matter_ai_first_review(
    matter_id: str,
    ai_first_review_result: dict[str, Any],
    metadata: dict[str, Any],
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        updated_matter = {
            **matter,
            "ai_first_review_result": ai_first_review_result,
            "ai_first_review_metadata": {
                **metadata,
                "stored_at": now,
            },
            "updated_at": now,
        }
        _save_matter_record(updated_matter)
        return updated_matter


def update_matter_artifacts(
    matter_id: str,
    artifacts: list[dict[str, Any]],
    current_artifact_id: str = "",
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Persist a matter's artifact registry (the ``artifacts`` list + pointer).

    Additive: only the registry fields are touched; the rest of the matter
    record is left exactly as-is.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        updated_matter = {
            **matter,
            "artifacts": list(artifacts),
            "current_artifact_id": str(current_artifact_id or ""),
            "updated_at": now,
        }
        _save_matter_record(updated_matter)
        return updated_matter


def mutate_matter_artifacts(
    matter_id: str,
    mutate: Callable[[list[dict[str, Any]], str], tuple[list[dict[str, Any]], str]],
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Atomically read-modify-write a matter's artifact registry under ONE lock.

    The fix for the artifact lost-update race: callers that previously did
    ``get_matter()`` (lock 1) -> compute ``existing + [new]`` in Python ->
    ``update_matter_artifacts(whole_list)`` (lock 2) opened a window where a
    concurrent writer's list overwrote the first writer's (orphaned bytes, broken
    lineage). Here the load, the caller-supplied ``mutate`` transform, and the
    save all happen inside a single ``_locked_store()`` critical section, so two
    concurrent registrations serialise and neither is lost.

    ``mutate`` receives the matter's CURRENT ``(artifacts_list, current_artifact_id)``
    as freshly read under the lock and must return the new ``(artifacts_list,
    current_artifact_id)``. It runs while the global store lock is held, so it must
    be fast and pure (no I/O, no further store calls — those would deadlock on the
    flock or amplify the critical section). Returns the updated matter, or ``None``
    when the matter is missing / not owned by ``owner_user_id``.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matter = _load_matter_record_by_id(matter_id)
        if matter is None or not _matter_owner_matches(matter, owner_user_id):
            return None
        current_artifacts = matter.get("artifacts")
        current_artifacts = list(current_artifacts) if isinstance(current_artifacts, list) else []
        current_pointer = str(matter.get("current_artifact_id") or "")
        new_artifacts, new_pointer = mutate(current_artifacts, current_pointer)
        updated_matter = {
            **matter,
            "artifacts": list(new_artifacts),
            "current_artifact_id": str(new_pointer or ""),
            "updated_at": now,
        }
        _save_matter_record(updated_matter)
        return updated_matter


def put_artifact_document(stored_filename: str, document_bytes: bytes) -> str:
    """Store an artifact's bytes under UPLOADS_DIR and return its storage key.

    The key is the (sanitised) ``stored_filename`` callers later pass to
    ``get_artifact_document`` / ``source_document_path``. Bytes for the original
    NDA are NOT re-stored — the registry reuses the matter's existing
    ``stored_filename`` — so this is only used for generated/derived artifacts.
    """
    safe_name = _safe_artifact_stored_filename(stored_filename)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(UPLOADS_DIR / safe_name, document_bytes)
    return safe_name


def get_artifact_document(stored_filename: str) -> bytes | None:
    """Read an artifact's bytes by storage key (None when missing/escaping)."""
    safe_name = str(stored_filename or "")
    if not safe_name:
        return None
    source_path = (UPLOADS_DIR / safe_name).resolve()
    if source_path.parent != UPLOADS_DIR.resolve() or not source_path.is_file():
        return None
    try:
        return source_path.read_bytes()
    except OSError:
        return None


def _safe_artifact_stored_filename(filename: str) -> str:
    basename = Path(str(filename or "")).name
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip("-._")
    if not safe_name:
        safe_name = f"artifact-{uuid.uuid4().hex[:12]}.docx"
    return safe_name[:MAX_SOURCE_FILENAME_LENGTH] or f"artifact-{uuid.uuid4().hex[:12]}.docx"


def reset_demo_repository(owner_user_id: str = "") -> int:
    with _locked_store():
        _ensure_matter_records_from_legacy()
        # Owner-scoped read: only owner-matching matters are ever removed, so the
        # locked critical section need not parse the whole cross-tenant store.
        removed = _load_matters_for_owner(owner_user_id)
    # Defer the fsync-heavy record unlinks + dir-fsyncs until after the lock is
    # released, mirroring _delete_stored_document and the create_matter pruning.
    for matter in removed:
        _delete_matter_record(matter)
    for matter in removed:
        _delete_stored_document(matter)
    return len(removed)


def delete_matter(matter_id: str, owner_user_id: str = "") -> dict[str, Any] | None:
    with _locked_store():
        _ensure_matter_records_from_legacy()
        deleted_matter = _load_matter_record_by_id(matter_id)
        if deleted_matter is not None and not _matter_owner_matches(deleted_matter, owner_user_id):
            deleted_matter = None
        if deleted_matter is None:
            return None
        _delete_matter_record(deleted_matter)
    _delete_stored_document(deleted_matter)
    return deleted_matter


def _gmail_duplicate_removal_ids(matters: list[dict[str, Any]], owner_user_id: str = "") -> set[int]:
    parent: dict[int, int] = {}
    matters_by_id: dict[int, dict[str, Any]] = {}
    keyed_matters: list[tuple[dict[str, Any], tuple[tuple[str, str], ...]]] = []

    def root(object_id: int) -> int:
        while parent[object_id] != object_id:
            parent[object_id] = parent[parent[object_id]]
            object_id = parent[object_id]
        return object_id

    def union(left_id: int, right_id: int) -> None:
        left_root = root(left_id)
        right_root = root(right_id)
        if left_root != right_root:
            parent[right_root] = left_root

    for matter in matters:
        if not _matter_owner_matches(matter, owner_user_id):
            continue
        keys = _gmail_attachment_keys_for_metadata(matter)
        if not keys:
            continue
        object_id = id(matter)
        parent[object_id] = object_id
        matters_by_id[object_id] = matter
        keyed_matters.append((matter, keys))

    non_filename_index: dict[tuple[str, str], int] = {}
    filename_index: dict[tuple[str, str], list[int]] = {}
    for matter, keys in keyed_matters:
        object_id = id(matter)
        for key in keys:
            if key[1].startswith("filename:"):
                filename_index.setdefault(key, []).append(object_id)
                continue
            existing_id = non_filename_index.get(key)
            if existing_id is None:
                non_filename_index[key] = object_id
            else:
                union(existing_id, object_id)

    for object_ids in filename_index.values():
        # A shared filename is NOT a content identity: two genuinely different
        # documents can carry the same name. Only merge matters in a same-filename
        # group when their stored bytes hash equal. A matter with no content hash
        # cannot be confirmed as a duplicate by filename alone, so it is left on its
        # own (merging it away would be real data loss).
        hash_index: dict[str, list[int]] = {}
        for object_id in object_ids:
            attachment_sha256 = str(matters_by_id[object_id].get("gmail_attachment_sha256") or "")
            if attachment_sha256:
                hash_index.setdefault(attachment_sha256, []).append(object_id)
        for matching_hash_ids in hash_index.values():
            for object_id in matching_hash_ids[1:]:
                union(matching_hash_ids[0], object_id)

    duplicate_groups: dict[int, list[dict[str, Any]]] = {}
    for object_id, matter in matters_by_id.items():
        duplicate_groups.setdefault(root(object_id), []).append(matter)

    removal_ids: set[int] = set()
    for group in duplicate_groups.values():
        if len(group) <= 1:
            continue
        winner_id = id(max(group, key=_matter_duplicate_rank))
        removal_ids.update(id(matter) for matter in group if id(matter) != winner_id)
    return removal_ids


def deduplicate_gmail_matters(owner_user_id: str = "") -> int:
    with _locked_store():
        _ensure_matter_records_from_legacy()
        # Scope the locked read to the target owner: the union-find dedupe only
        # ever considers owner-matching matters, so loading the whole cross-tenant
        # store under the lock is pure read amplification. Narrowing here keeps the
        # exclusive critical section O(owner matters), not O(total matters).
        matters = _load_matters_for_owner(owner_user_id)
        removal_ids = _gmail_duplicate_removal_ids(matters, owner_user_id=owner_user_id)

        removed: list[dict[str, Any]] = []
        for matter in matters:
            if id(matter) in removal_ids:
                removed.append(matter)

        if not removed:
            return 0
    # Defer the fsync-heavy record unlinks + dir-fsyncs until AFTER the lock is
    # released (mirroring _delete_stored_document below), so a concurrent
    # list_matters / create_matter is not blocked for the whole delete loop.
    for matter in removed:
        _delete_matter_record(matter)
    for matter in removed:
        _delete_stored_document(matter)
    return len(removed)


def _bulk_archive_sort_key(matter: dict[str, Any]) -> tuple[str, str]:
    """Deterministic selection order for bulk archive: oldest first, id tiebreak.

    A stable order is what makes the dry-run selection hash reproducible across
    the dry-run call and the execute call (same store state => same selection =>
    same hash), so the confirm handshake compares like with like.
    """
    return (str(matter.get("created_at") or ""), str(matter.get("id") or ""))


def bulk_archive_gmail_matters(
    owner_user_id: str,
    predicate: Callable[[dict[str, Any]], bool],
    limit: int = 200,
    *,
    confirmed_matter_ids: "set[str] | frozenset[str] | None" = None,
) -> dict[str, Any]:
    """Archive-then-delete the owner's matters that pass ``predicate`` (capped).

    The admin bulk-archive primitive for auto-imported Gmail noise. Safety
    contract:

    * OWNER IS MANDATORY: an empty ``owner_user_id`` would make every matter in
      a single-tenant store eligible, so it is rejected outright — this routine
      must never run store-wide.
    * The predicate is RE-EVALUATED per matter under ``_locked_store()`` — the
      caller's dry-run snapshot is never trusted, so a matter that gained human
      work (review, decision, move, ...) between dry-run and execute is skipped.
    * ``confirmed_matter_ids`` (when given) is the other half of that handshake:
      only matters in ``predicate ∩ confirmed_matter_ids`` are deleted. The
      predicate can only SHRINK the confirmed set, never widen it — a matter
      that newly qualifies between the caller's confirm-hash check and this
      lock (e.g. a fresh import inside a future-dated window) was never
      reviewed by the operator, is skipped here, and simply surfaces in the
      next dry-run.
    * Archive-before-delete (the retention-pruning invariant, via
      ``_archive_pruned_matters``): if archiving to ``pruned-matters/`` fails,
      NOTHING is deleted and the report says ``archive_failed``.
    * Record unlinks happen under the lock (mirroring ``delete_matter``); the
      fsync-heavy stored-document unlinks and the best-effort render-cache purge
      are deferred until after the lock is released. CRASH NOTE: a crash after
      the under-lock record unlink but before the deferred post-lock document
      unlink can orphan a file under uploads/ — the matter record is gone but
      its source bytes remain on disk (and are ALSO preserved in the
      ``pruned-matters/`` archive written beforehand, so nothing is lost; the
      orphan is just unreclaimed space an operator may sweep).

    Returns ``{"archived": bool, "archive_failed": bool, "deleted_matters": [...]}``
    where ``deleted_matters`` are the full matter dicts that were removed (the
    caller derives ids / gmail message ids / audit fields from them).
    """
    owner_user_id = _clean_owner_user_id(owner_user_id)
    if not owner_user_id:
        raise MatterStoreError("Bulk archive requires an explicit owner_user_id.")
    limit = max(0, int(limit))
    report: dict[str, Any] = {"archived": False, "archive_failed": False, "deleted_matters": []}
    if not limit:
        return report
    selected: list[dict[str, Any]] = []
    with _locked_store():
        _ensure_matter_records_from_legacy()
        matters = _load_matters_for_owner(owner_user_id)
        matters.sort(key=_bulk_archive_sort_key)
        for matter in matters:
            if len(selected) >= limit:
                break
            # Delete only the CONFIRMED ∩ predicate set: the confirmed-id gate
            # keeps anything the operator never reviewed out of the batch, and
            # the under-lock predicate RE-EVALUATION (never trusting the dry-run
            # snapshot) drops anything that gained human work since.
            if (
                confirmed_matter_ids is not None
                and str(matter.get("id") or "") not in confirmed_matter_ids
            ):
                continue
            if predicate(matter):
                selected.append(matter)
        if not selected:
            return report
        if not _archive_pruned_matters(selected, context="bulk_archive"):
            report["archive_failed"] = True
            return report
        for matter in selected:
            _delete_matter_record(matter)
    # --- deferred, post-lock cleanup (mirrors delete_matter / retention) ---
    # Render bookkeeping first, while the stored source bytes still exist: the
    # per-user render-cache key is content-derived, so the purge needs the bytes
    # (replicates repository_board_workflow.delete_card). Lazy import because
    # document_rendering imports matter_store at module level.
    from . import document_rendering  # noqa: PLC0415 - avoid an import cycle.

    for matter in selected:
        matter_id = str(matter.get("id") or "")
        try:
            source_bytes = get_source_document_bytes(matter)
            document_rendering.matter_render_coordinator().forget(matter_id)
            if source_bytes is not None:
                document_rendering.purge_render_cache_for_source(
                    source_bytes,
                    owner_user_id=owner_user_id,
                    source_filename=str(matter.get("source_filename") or ""),
                )
        except Exception:  # noqa: BLE001 - render cleanup is best-effort, never blocks the batch.
            pass
    for matter in selected:
        _delete_stored_document(matter)
    report["archived"] = True
    report["deleted_matters"] = selected
    return report


def create_matter(
    *,
    source_filename: str,
    document_bytes: bytes,
    extracted_text: str,
    review_result: dict[str, Any],
    triage: dict[str, Any],
    source_type: str = "gmail_demo",
    board_column: str = "gmail_demo",
    intake_metadata: dict[str, Any] | None = None,
    dedupe_gmail: bool = False,
    owner_user_id: str = "",
) -> dict[str, Any]:
    matter_id = f"matter_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    source_filename = _clean_source_filename(source_filename)
    safe_source_name = _safe_filename(source_filename)
    stored_filename = f"{matter_id}-{safe_source_name}"
    metadata = _intake_metadata(source_filename, now, intake_metadata)
    owner_user_id = _clean_owner_user_id(owner_user_id or metadata.get("owner_user_id"))
    if owner_user_id:
        metadata["owner_user_id"] = owner_user_id
    if dedupe_gmail and not metadata.get("gmail_attachment_sha256"):
        metadata["gmail_attachment_sha256"] = hashlib.sha256(document_bytes).hexdigest()

    # The dedupe check + record write happen together under a single _locked_store()
    # below (the authoritative critical section). An earlier, separate pre-check used
    # to run here outside that lock; it re-read the whole store (read amplification)
    # and opened a TOCTOU window between the check and the write, so a concurrent
    # gmail-sync and HTTP create could both pass it and persist the same attachment
    # twice. We instead always stage the bytes and let the locked check below reject
    # a duplicate (unlinking the staged bytes), keeping dedupe/write a lost-update-free
    # atomic step.
    stored_path = UPLOADS_DIR / stored_filename
    # Stage the source bytes with the same tmp+fsync+replace+dir-fsync durability as
    # every other byte payload in this module (artifacts, pruning archive). A bare
    # write_bytes here could leave a TRUNCATED source doc on a crash/OOM mid-write
    # that the later-written matter record would point at as valid/present.
    # _write_bytes_atomic mkdir's the parent dir, so no separate mkdir is needed.
    _write_bytes_atomic(stored_path, document_bytes)

    matter: dict[str, Any] = {
        "id": matter_id,
        "created_at": now,
        "updated_at": now,
        "source_type": source_type,
        "source_filename": source_filename,
        "stored_filename": stored_filename,
        "document_title": Path(source_filename).stem or "Untitled NDA",
        "status": "active",
        "board_column": board_column,
        **metadata,
        "extracted_text": extracted_text,
        "review_result": review_result,
        **triage,
    }
    # SHARED CONTRACT: persist the AI-extracted counterparty at the durable nested
    # location matter["intake_metadata"]["counterparty"] (other teammates read it via
    # an accessor keyed to exactly this path). It is set directly on the matter dict
    # -- NOT routed through the intake_metadata PARAMETER -- because _intake_metadata()
    # flattens that parameter into top-level STRING fields and drops nested dicts. The
    # whole matter dict is json.dump/json.load round-tripped with no field whitelist,
    # so a nested dict written here persists + reloads faithfully.
    _attach_intake_counterparty(matter, review_result)
    pruned_matters: list[dict[str, Any]] = []
    duplicate_matter: dict[str, Any] | None = None
    saved_new_record = False
    try:
        # The exclusive critical section is deliberately kept O(new record): under
        # the lock we only load matters, run the dedupe check, append, compute the
        # prune SET in memory (no fsync), write the single new record, and let the
        # write-through cache invalidation run. The fsync-heavy retention work --
        # archiving pruned matters (byte copies + fsync'd archive JSON) and
        # unlinking their records (per-record unlink + dir fsync) -- is deferred to
        # AFTER the lock is released, exactly like _delete_stored_document below.
        # This stops inbound imports from serializing behind O(N) archive/delete
        # work while a concurrent list_matters waits on the same lock.
        with _locked_store():
            _ensure_matter_records_from_legacy()
            matters = _load_matters()
            if dedupe_gmail:
                existing_matter = _find_gmail_duplicate_unlocked(matters, metadata, owner_user_id=owner_user_id)
                if existing_matter is not None:
                    duplicate_matter = {**existing_matter, "_existing_gmail_duplicate": True}
            if duplicate_matter is None:
                matters.append(matter)
                _kept, pruned_matters = _prune_stored_matters(matters, protected_matter_id=matter_id)
                _save_matter_record(matter)
                saved_new_record = True
    except Exception:
        stored_path.unlink(missing_ok=True)
        if saved_new_record:
            with suppress(MatterStoreError, OSError):
                _delete_matter_record(matter_id)
        raise
    if duplicate_matter is not None:
        stored_path.unlink(missing_ok=True)
        return duplicate_matter
    # --- deferred, post-lock retention work ---
    # Archive-before-delete invariant is preserved: if _archive_pruned_matters
    # fails, we do NOT delete anything -- the would-be-pruned matters stay live
    # (their records and source documents are untouched) and only the new matter
    # is persisted. Only after a successful archive do we unlink the pruned
    # records and their source documents.
    if pruned_matters and _archive_pruned_matters(pruned_matters):
        for pruned_matter in pruned_matters:
            with suppress(MatterStoreError, OSError):
                _delete_matter_record(pruned_matter)
        for pruned_matter in pruned_matters:
            _delete_stored_document(pruned_matter)
    return matter


@contextmanager
def _locked_store():
    # --- in-process lock (threading.RLock) with timeout ---
    # RLock.acquire(timeout=N) still succeeds immediately when the *same* thread
    # already holds the lock (re-entrancy is preserved), so nested _locked_store()
    # calls from the same thread are unaffected.
    if not _MATTERS_LOCK.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
        raise MatterStoreError(
            "Matter store could not be locked within the timeout "
            f"({_LOCK_TIMEOUT_SECONDS}s). A long-running operation may be "
            "holding the lock. Please retry.",
            lock_timeout=True,
        )
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (DATA_DIR / "matters.lock").open("a+", encoding="utf-8") as lock_file:
            # --- cross-process flock with bounded retry ---
            # Use LOCK_EX|LOCK_NB so we never sleep inside a kernel call; instead
            # we poll in a tight loop (10 ms intervals) and give up after
            # _LOCK_TIMEOUT_SECONDS, matching the in-process timeout above.
            if fcntl is not None:
                _deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        if time.monotonic() >= _deadline:
                            raise MatterStoreError(
                                "Matter store file lock could not be acquired within the "
                                f"timeout ({_LOCK_TIMEOUT_SECONDS}s). Another process may "
                                "be holding the lock. Please retry."
                            ) from exc
                        time.sleep(0.01)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        _MATTERS_LOCK.release()


def _load_matters() -> list[dict[str, Any]]:
    if MATTERS_PATH.is_file():
        return _load_legacy_matters()
    return _load_matter_records()


def _load_matters_for_owner(owner_user_id: str = "") -> list[dict[str, Any]]:
    """Load only the matters owned by ``owner_user_id``.

    Matter record filenames are ``{matter_id}.json`` and carry no owner, so the
    owner cannot be derived from the path without opening the file; we therefore
    parse each record and keep only owner-matching ones. This still bounds the
    IN-MEMORY working set (and any downstream union-find / scan) to the target
    owner's matters instead of the whole cross-tenant store. An empty
    ``owner_user_id`` (single-tenant / no-auth) returns every matter, matching
    ``_matter_owner_matches`` semantics.
    """
    return [
        matter
        for matter in _load_matters()
        if _matter_owner_matches(matter, owner_user_id)
    ]


def _load_legacy_matters() -> list[dict[str, Any]]:
    if not MATTERS_PATH.is_file():
        return []
    try:
        with MATTERS_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise MatterStoreError("Matter store could not be read.") from exc
    except json.JSONDecodeError as exc:
        raise MatterStoreError("Matter store is not valid JSON.") from exc
    if not isinstance(payload, list):
        raise MatterStoreError("Matter store must contain a JSON list.")
    return payload


def _save_matters(matters: list[dict[str, Any]]) -> None:
    _write_json_atomic(MATTERS_PATH, matters)


def _matter_records_dir() -> Path:
    return MATTERS_PATH.parent / MATTER_RECORDS_DIRNAME


def _matter_record_paths() -> list[Path]:
    records_dir = _matter_records_dir()
    if not records_dir.is_dir():
        return []
    return sorted(path for path in records_dir.glob("*.json") if path.is_file())


def _load_matter_records() -> list[dict[str, Any]]:
    matters: list[dict[str, Any]] = []
    for record_path in _matter_record_paths():
        matters.append(_load_matter_record_path(record_path))
    return matters


def _records_dir_fingerprint() -> tuple[tuple[str, int, int], ...]:
    """A cheap, change-sensitive signature of the matter-records directory.

    The sorted tuple of ``(filename, st_mtime_ns, st_size)`` over every record
    file. Stat-ing is far cheaper than open+json.parse of every record, and any
    create/update/delete changes a file's mtime/size or the set of files, so the
    fingerprint moves whenever the parsed result would. ``st_mtime_ns`` gives
    nanosecond resolution (where the filesystem supports it), and ``st_size`` is
    an independent second axis, so a same-instant rewrite that happened to keep an
    identical mtime would still be caught whenever the byte length differs. The
    cache is additionally write-through-invalidated on every store write, so a
    same-process write is never even at the mercy of stat granularity.
    """
    records_dir = _matter_records_dir()
    if not records_dir.is_dir():
        return ()
    entries: list[tuple[str, int, int]] = []
    for record_path in records_dir.glob("*.json"):
        try:
            stat = record_path.stat()
        except OSError:
            # A file vanishing mid-scan (concurrent prune) just means our snapshot
            # is already out of date; record a sentinel so the fingerprint differs
            # from any complete read and the cache is not trusted this round.
            entries.append((record_path.name, -1, -1))
            continue
        if not record_path.is_file():
            continue
        entries.append((record_path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(entries))


def _invalidate_list_cache() -> None:
    """Drop the cached list. Called on every store write (write-through)."""
    global _list_cache_fingerprint, _list_cache_blob, _list_cache_dir
    with _LIST_CACHE_LOCK:
        _list_cache_fingerprint = None
        _list_cache_blob = None
        _list_cache_dir = None


def _record_fingerprint_entry(record_path: Path) -> tuple[str, int, int] | None:
    """The single ``(name, st_mtime_ns, st_size)`` fingerprint entry for one record.

    Mirrors exactly the per-file tuple ``_records_dir_fingerprint`` builds, so an
    entry produced here is byte-for-byte substitutable into the cached fingerprint.
    Returns ``None`` when the file cannot be stat-ed (vanished / permission), which
    the callers treat as "uncertain" and fall back to a full invalidation.
    """
    try:
        stat = record_path.stat()
    except OSError:
        return None
    return (record_path.name, stat.st_mtime_ns, stat.st_size)


def _coherent_cache_update_after_write(matter: dict[str, Any]) -> None:
    """Patch the ONE just-written matter into the cached list in place (G3).

    ``_write_matter_record`` previously always dropped the whole cached
    ``list_matters`` blob, so the very next ``list_matters`` (the 15s board poll,
    notifications poll, corpus, assistant) suffered a full cache miss and re-opened
    + re-parsed EVERY record file. Because we just wrote this record under the store
    lock we know its exact new value, so we can update the cache entry for THIS
    matter alone and advance the fingerprint entry for THIS file alone, leaving the
    next ``list_matters`` a cache HIT (zero record re-parses) instead of an O(N)
    reload.

    Coherence is preserved by construction:

    * EXTERNAL-CHANGE DETECTION IS UNTOUCHED. Only this file's fingerprint entry is
      refreshed to its post-write stat; every other file keeps its prior stat entry.
      ``_records_dir_fingerprint`` re-stats ALL files on the next read, so if another
      process wrote a DIFFERENT record its stat won't match our carried-over entry ->
      fingerprint mismatch -> full reload -> the external change is seen. The
      incremental path only ever makes OUR OWN write a hit; it never suppresses
      anyone else's write.
    * FAIL-SAFE. On ANY uncertainty -- no live cache to patch, a directory mismatch,
      the cached blob or fingerprint being an unexpected shape, the file's post-write
      stat being unreadable, or any exception -- we fall back to a full
      ``_invalidate_list_cache``. A wrong cache is data corruption; a dropped cache
      is only a slower next read, so we always prefer the drop.
    * O(ONE RECORD) PER WRITE. The cache is a dict of per-record blobs keyed by the
      record FILENAME, so the patch serializes just this matter and replaces its one
      dict entry. It never round-trips the whole store (the original monolithic-blob
      shape cost O(total store bytes) per write -- ~330ms at 47MB/1200 matters --
      serialized under the store lock, on EVERY review save / Gmail intake / timeline
      append). Keying the blob entry and the fingerprint entry on the SAME cleaned
      filename also keeps create/update/delete symmetric by construction: a stored
      raw ``id`` that differs from its cleaned filename form (the e88baaad ghost)
      still maps to exactly the entry the write stored, because both paths derive
      the key through ``_clean_matter_record_id``.

    MUST be called under ``_locked_store()`` (as ``_write_matter_record`` is), so the
    on-disk file and the fingerprint we snapshot describe the same committed write.
    """
    global _list_cache_fingerprint, _list_cache_blob, _list_cache_dir
    try:
        matter_id = _clean_matter_record_id(matter.get("id"))
        if not matter_id:
            _invalidate_list_cache()
            return
        # The legacy monolithic file is never cached (see _load_matters_cached); if
        # it is present we are in the migration window -- just drop the cache.
        if MATTERS_PATH.is_file():
            _invalidate_list_cache()
            return
        record_path = _matter_records_dir() / f"{matter_id}.json"
        new_entry = _record_fingerprint_entry(record_path)
        if new_entry is None:
            _invalidate_list_cache()
            return
        current_dir = str(_matter_records_dir())
        # Serialize THIS matter only -- the stored blob is an immutable string, so
        # the cache can never alias a live matter dict the caller mutates afterwards.
        record_blob = json.dumps(matter)
        with _LIST_CACHE_LOCK:
            if (
                _list_cache_blob is None
                or _list_cache_fingerprint is None
                or _list_cache_dir != current_dir
            ):
                # No live, same-dir cache to patch: nothing to keep warm. Leave the
                # cache dropped so the next read parses fresh.
                _list_cache_fingerprint = None
                _list_cache_blob = None
                _list_cache_dir = None
                return
            if not isinstance(_list_cache_blob, dict):
                # Unexpected cache shape -- do not trust it; drop the cache.
                _list_cache_fingerprint = None
                _list_cache_blob = None
                _list_cache_dir = None
                return
            # Rebuild the fingerprint: swap in THIS file's new entry (or add it for a
            # create), carrying every other file's prior entry unchanged.
            fingerprint_map = {
                entry[0]: entry
                for entry in _list_cache_fingerprint
                if isinstance(entry, tuple) and len(entry) == 3
            }
            if len(fingerprint_map) != len(_list_cache_fingerprint):
                # Unexpected fingerprint shape -- do not trust it; drop the cache.
                _list_cache_fingerprint = None
                _list_cache_blob = None
                _list_cache_dir = None
                return
            fingerprint_map[new_entry[0]] = new_entry
            # Replace (update) or add (create) the ONE entry for this record file.
            # Dict insertion order is not load-bearing: reads assemble the list in
            # sorted-filename order, matching _matter_record_paths.
            _list_cache_blob[record_path.name] = record_blob
            _list_cache_fingerprint = tuple(sorted(fingerprint_map.values()))
            _list_cache_dir = current_dir
    except Exception:  # noqa: BLE001 -- correctness beats the optimization; drop the cache.
        _invalidate_list_cache()


def _coherent_cache_update_after_delete(raw_id: object) -> None:
    """Drop the ONE just-deleted matter from the cached list in place (G3).

    The delete counterpart to ``_coherent_cache_update_after_write``: we removed
    this record file, so we drop its entry from both the cached list and the cached
    fingerprint, leaving every other file's fingerprint entry in place. External-
    change detection is preserved for the same reason as the write path, and every
    uncertainty (the file still existing, an unexpected cache/fingerprint shape, any
    exception) falls back to a full invalidation.

    The cache entry and the fingerprint entry are BOTH keyed on the CLEANED record
    filename -- exactly the key ``_coherent_cache_update_after_write`` stores under --
    so create/update/delete are symmetric by construction, even when a stored ``id``
    differs from its cleaned/filename form (the e88baaad ghost): both paths derive
    the same key from the raw id via ``_clean_matter_record_id``. ``raw_id`` is the
    caller's stored id (pre-cleaning), matching what ``_delete_matter_record`` holds.

    Holds ``_LIST_CACHE_LOCK`` for the blob read-modify-write; it does NOT require
    ``_locked_store()``. The blob mutation is serialized by ``_LIST_CACHE_LOCK``, and
    coherence against a concurrent (out-of-store-lock) delete is enforced on the READ
    side by ``_load_matters_cached``: it re-stats every file via
    ``_records_dir_fingerprint`` and only serves the blob when that fingerprint still
    matches, so any concurrent change is caught as a mismatch and forces a reload.
    """
    global _list_cache_fingerprint, _list_cache_blob, _list_cache_dir
    try:
        cleaned_id = _clean_matter_record_id(raw_id)
        if not cleaned_id:
            _invalidate_list_cache()
            return
        if MATTERS_PATH.is_file():
            _invalidate_list_cache()
            return
        record_path = _matter_records_dir() / f"{cleaned_id}.json"
        record_name = record_path.name
        # The record must be gone for the incremental drop to be sound. If it still
        # exists (unlink was a no-op / raced), fall back so we never claim a delete
        # the disk did not make.
        if record_path.exists():
            _invalidate_list_cache()
            return
        current_dir = str(_matter_records_dir())
        with _LIST_CACHE_LOCK:
            if (
                _list_cache_blob is None
                or _list_cache_fingerprint is None
                or _list_cache_dir != current_dir
            ):
                _list_cache_fingerprint = None
                _list_cache_blob = None
                _list_cache_dir = None
                return
            if not isinstance(_list_cache_blob, dict):
                _list_cache_fingerprint = None
                _list_cache_blob = None
                _list_cache_dir = None
                return
            # O(one record): drop the single per-record blob entry and this file's
            # fingerprint entry; every other entry is untouched.
            _list_cache_blob.pop(record_name, None)
            fingerprint = tuple(
                entry
                for entry in _list_cache_fingerprint
                if not (isinstance(entry, tuple) and len(entry) == 3 and entry[0] == record_name)
            )
            _list_cache_fingerprint = fingerprint
            _list_cache_dir = current_dir
    except Exception:  # noqa: BLE001 -- correctness beats the optimization; drop the cache.
        _invalidate_list_cache()


def _load_matters_cached() -> list[dict[str, Any]]:
    """``_load_matters`` with an mtime/size-fingerprinted in-memory cache.

    MUST be called under ``_locked_store()`` so the fingerprint and the parse
    observe a consistent on-disk set (no writer mutating files mid-read). Only
    used by ``list_matters`` — the read-amplified hot path. The legacy
    ``matters.json`` path (transient, pre-migration) is never cached: we fall back
    to the uncached ``_load_matters`` so the rare migration window is always fresh.

    Returns FRESH, fully-isolated dicts on every call (a cache hit deserializes the
    cached JSON blob), so a caller mutating a returned matter can never corrupt the
    cache — exactly like the prior uncached path, which returned freshly-parsed
    dicts.
    """
    global _list_cache_fingerprint, _list_cache_blob, _list_cache_dir
    if MATTERS_PATH.is_file():
        # Pre-migration legacy file present: do not cache, always read fresh.
        return _load_matters()
    if _CACHE_DISABLED:
        return _load_matters()

    current_dir = str(_matter_records_dir())
    fingerprint = _records_dir_fingerprint()
    with _LIST_CACHE_LOCK:
        if (
            isinstance(_list_cache_blob, dict)
            and _list_cache_dir == current_dir
            and _list_cache_fingerprint == fingerprint
        ):
            # Snapshot the per-record blob strings (immutable) in sorted-filename
            # order -- the same order _matter_record_paths yields -- under the lock;
            # the O(total bytes) join+parse happens outside it.
            hit_blobs = [_list_cache_blob[name] for name in sorted(_list_cache_blob)]
        else:
            hit_blobs = None
    if hit_blobs is not None:
        if not hit_blobs:
            return []
        return json.loads("[" + ",".join(hit_blobs) + "]")
    # Miss (or stale): parse fresh, then re-snapshot the fingerprint AFTER the
    # parse so a write that races our parse is reflected by a fingerprint mismatch
    # on the next call rather than being cached against a pre-write fingerprint.
    record_paths = _matter_record_paths()
    parsed = [_load_matter_record_path(record_path) for record_path in record_paths]
    fingerprint_after = _records_dir_fingerprint()
    if fingerprint_after == fingerprint:
        # Stable across the parse: safe to cache against this fingerprint. Serialize
        # per record (keyed by filename) so later writes patch one entry in O(1
        # record); the total dumps cost equals the old whole-list dumps.
        blobs = {
            record_path.name: json.dumps(record)
            for record_path, record in zip(record_paths, parsed)
        }
        with _LIST_CACHE_LOCK:
            _list_cache_blob = blobs
            _list_cache_fingerprint = fingerprint_after
            _list_cache_dir = current_dir
    else:
        # The directory changed while we parsed; don't cache a possibly-torn read.
        # The next call will re-parse against the settled fingerprint.
        with _LIST_CACHE_LOCK:
            _list_cache_fingerprint = None
            _list_cache_blob = None
            _list_cache_dir = None
    return parsed


def _load_matter_record_by_id(matter_id: str) -> dict[str, Any] | None:
    cleaned_id = _clean_matter_record_id(matter_id)
    if not cleaned_id:
        return None
    record_path = _matter_records_dir() / f"{cleaned_id}.json"
    if not record_path.is_file():
        return None
    return _load_matter_record_path(record_path)


def _load_matter_record_path(record_path: Path) -> dict[str, Any]:
    try:
        with record_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise MatterStoreError(f"Matter record could not be read: {record_path.name}.") from exc
    except json.JSONDecodeError as exc:
        raise MatterStoreError(f"Matter record is not valid JSON: {record_path.name}.") from exc
    if not isinstance(payload, dict):
        raise MatterStoreError(f"Matter record must contain a JSON object: {record_path.name}.")
    return payload


def _save_matter_record(matter: dict[str, Any]) -> None:
    _ensure_matter_records_from_legacy()
    _write_matter_record(matter)


def _write_matter_record(matter: dict[str, Any]) -> None:
    matter_id = _clean_matter_record_id(matter.get("id"))
    if not matter_id:
        raise MatterStoreError("Matter record must include an id.")
    _write_json_atomic(_matter_records_dir() / f"{matter_id}.json", matter)
    # Write-through cache maintenance: a record file changed. We just wrote this
    # exact record under the store lock, so instead of dropping the whole cached
    # list (which forces the next list_matters to re-parse EVERY record) we patch
    # the single changed entry in place and advance only this file's fingerprint
    # (G3). This preserves external-change detection -- every other file keeps its
    # prior stat entry, so a concurrent write to a different record is still caught
    # on the next read -- and falls back to a full invalidation on any uncertainty.
    # Runs while the store lock is held by the caller.
    _coherent_cache_update_after_write(matter)


def _delete_matter_record(matter: dict[str, Any] | str) -> None:
    # Pass the RAW stored id through to the cache prune: the cache entry (like the
    # record FILE) is keyed by the CLEANED record filename, and the prune derives
    # that same key from the raw id via _clean_matter_record_id -- exactly as the
    # write path does -- so write and delete stay symmetric by construction.
    raw_id = matter.get("id") if isinstance(matter, dict) else matter
    matter_id = _clean_matter_record_id(raw_id)
    if not matter_id:
        raise MatterStoreError("Matter record must include an id.")
    record_path = _matter_records_dir() / f"{matter_id}.json"
    try:
        record_path.unlink(missing_ok=True)
        _fsync_directory(record_path.parent)
    except OSError as exc:
        raise MatterStoreError("Matter record could not be deleted.") from exc
    # Write-through cache maintenance: a record file was removed. We drop this one
    # matter (and its fingerprint entry) from the cached list in place (G3) rather
    # than blowing the whole cache away, keeping the next list_matters a hit. Falls
    # back to a full invalidation on any uncertainty. Both the cached per-record
    # blob entry and the fingerprint entry are keyed on the CLEANED record filename
    # (derived from the raw id via _clean_matter_record_id, exactly as the write
    # path derives its key), so write and delete prune the same entry by construction.
    _coherent_cache_update_after_delete(raw_id)


def _ensure_matter_records_from_legacy() -> None:
    if not MATTERS_PATH.is_file():
        _matter_records_dir().mkdir(parents=True, exist_ok=True)
        return

    legacy_matters = _load_legacy_matters()
    _matter_records_dir().mkdir(parents=True, exist_ok=True)
    for matter in legacy_matters:
        if isinstance(matter, dict):
            _write_matter_record(matter)

    archive_path = _legacy_matters_archive_path()
    try:
        MATTERS_PATH.replace(archive_path)
        _fsync_directory(MATTERS_PATH.parent)
    except OSError as exc:
        raise MatterStoreError("Legacy matter store could not be archived after migration.") from exc


def _legacy_matters_archive_path() -> Path:
    base_path = MATTERS_PATH.with_name(f"{MATTERS_PATH.name}.legacy")
    if not base_path.exists():
        return base_path
    return MATTERS_PATH.with_name(f"{MATTERS_PATH.name}.legacy-{uuid.uuid4().hex[:8]}")


def _clean_matter_record_id(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip())[:160].strip("-")


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A PER-CALL-UNIQUE temp name. A FIXED ``.tmp`` suffix is unsafe when two
    # writers target the SAME path concurrently (e.g. artifact byte-staging, which
    # runs OUTSIDE the store lock): both open the same tmp file, the first
    # ``replace`` moves it onto ``path``, and the second ``replace`` then raises
    # FileNotFoundError on the now-vanished tmp. The uuid keeps each writer's tmp
    # private; the final ``replace`` onto ``path`` is still atomic.
    temporary_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = getattr(os, "O_RDONLY", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _prune_stored_matters(
    matters: list[dict[str, Any]],
    *,
    protected_matter_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute the retention prune set, scoped to the protected matter's OWNER.

    Retention is enforced PER OWNER: ``NDA_MATTER_RETENTION_LIMIT`` (default
    ``DEFAULT_MAX_STORED_MATTERS``) is a per-owner cap. A prolific tenant's
    imports therefore only ever prune that tenant's OWN oldest closed matters —
    never the globally-oldest closed matter of a quiet, unrelated tenant. Only
    the partition that owns ``protected_matter_id`` is examined; every other
    owner's matters are returned untouched in ``kept``.

    A separate, much larger global hard ceiling (``NDA_MATTER_GLOBAL_LIMIT``,
    disabled by default) can be layered on top as a distinct safety cap for the
    whole store; it never substitutes for the owner-scoped routine and only ever
    prunes closed/inactive matters that are not the protected one.

    This routine is purely in-memory (no archive, no fsync): the caller archives
    the returned ``pruned`` set and deletes their records AFTER releasing the
    store lock, so the exclusive critical section stays O(new record).
    """
    retention_limit = _stored_matter_limit()

    protected_owner = _prune_partition_owner(matters, protected_matter_id)
    partition_indexes = [
        index
        for index, matter in enumerate(matters)
        if _clean_owner_user_id(matter.get("owner_user_id")) == protected_owner
    ]

    removed_indexes: set[int] = set()
    _select_prune_indexes(
        matters,
        candidate_indexes=partition_indexes,
        limit=retention_limit,
        protected_matter_id=protected_matter_id,
        removed_indexes=removed_indexes,
        over_cap_telemetry="matter_retention_over_cap_without_prune",
    )

    global_ceiling = _global_matter_hard_ceiling()
    if global_ceiling > 0 and (len(matters) - len(removed_indexes)) > global_ceiling:
        remaining_indexes = [
            index for index in range(len(matters)) if index not in removed_indexes
        ]
        _select_prune_indexes(
            matters,
            candidate_indexes=remaining_indexes,
            limit=global_ceiling,
            protected_matter_id=protected_matter_id,
            removed_indexes=removed_indexes,
            over_cap_telemetry="matter_global_ceiling_over_cap_without_prune",
            live_total=len(matters) - len(removed_indexes),
        )

    if not removed_indexes:
        return matters, []
    kept = [matter for index, matter in enumerate(matters) if index not in removed_indexes]
    pruned = [matters[index] for index in sorted(removed_indexes)]
    return kept, pruned


def _prune_partition_owner(matters: list[dict[str, Any]], protected_matter_id: str) -> str:
    """The owner key of the retention partition to prune within.

    The protected matter's owner defines the partition. Fall back to the empty
    (ownerless / single-tenant) partition when the protected matter is not found,
    so a legacy/global import prunes only other ownerless matters.
    """
    for matter in matters:
        if matter.get("id") == protected_matter_id:
            return _clean_owner_user_id(matter.get("owner_user_id"))
    return ""


def _select_prune_indexes(
    matters: list[dict[str, Any]],
    *,
    candidate_indexes: list[int],
    limit: int,
    protected_matter_id: str,
    removed_indexes: set[int],
    over_cap_telemetry: str,
    live_total: int | None = None,
) -> None:
    """Mark the oldest removable candidates for pruning until ``limit`` is met.

    ``candidate_indexes`` is the set of positions (into ``matters``) eligible for
    this pass. ``live_total`` overrides the count compared against ``limit`` (used
    by the global ceiling pass, where the live total spans all owners); by default
    it is the number of candidates in this partition. Only CLOSED/inactive matters
    that are not ``protected_matter_id`` and not already removed are removable.
    Mutates ``removed_indexes`` in place.
    """
    if limit <= 0:
        return
    total = live_total if live_total is not None else len(candidate_indexes)
    if total <= limit:
        return

    removable = [
        (index, matters[index])
        for index in candidate_indexes
        if index not in removed_indexes
        and matters[index].get("id") != protected_matter_id
        and not _matter_is_active(matters[index])
    ]
    if not removable:
        telemetry.increment(over_cap_telemetry)
        return
    removable.sort(key=lambda item: _matter_retention_sort_key(item[1]))
    remove_count = min(total - limit, len(removable))
    if remove_count <= 0:
        return
    for index, _matter in removable[:remove_count]:
        removed_indexes.add(index)


def _stored_matter_limit() -> int:
    raw_limit = os.environ.get("NDA_MATTER_RETENTION_LIMIT", str(DEFAULT_MAX_STORED_MATTERS))
    try:
        return max(0, int(raw_limit))
    except ValueError:
        return DEFAULT_MAX_STORED_MATTERS


def _global_matter_hard_ceiling() -> int:
    """Optional store-wide hard cap across ALL owners (0 = disabled, the default).

    This is a distinct safety valve from the per-owner retention limit; it is not
    the routine retention path. When unset it never fires, so ordinary pruning is
    entirely owner-scoped.
    """
    raw_limit = os.environ.get("NDA_MATTER_GLOBAL_LIMIT", "0")
    try:
        return max(0, int(raw_limit))
    except ValueError:
        return 0


def _matter_retention_sort_key(matter: dict[str, Any]) -> tuple[int, str]:
    is_closed = 0 if matter.get("status") == "closed" or matter.get("board_column") == "signed_closed" else 1
    return (is_closed, str(matter.get("updated_at") or matter.get("created_at") or ""))


def _matter_is_active(matter: dict[str, Any]) -> bool:
    return matter.get("status") != "closed" and matter.get("board_column") != "signed_closed"


def _archive_pruned_matters(pruned_matters: list[dict[str, Any]], *, context: str = "retention") -> bool:
    # Retention pruning deletes stored documents. Archive each source document and
    # full matter record before saving the pruned store so an archive failure
    # keeps the matter live.
    #
    # ``context`` selects the success telemetry/logging only (the archive
    # mechanics are identical): the default "retention" keeps the existing
    # counters and the active-matter DATA-LOSS warning; "bulk_archive" (the
    # admin bulk-archive endpoint, which deletes deliberately-selected ACTIVE
    # gmail-noise matters) counts under its own counter so it never pollutes the
    # "active_matters_pruned" retention red-flag signal.
    if not pruned_matters:
        return True
    archive_dir = DATA_DIR / PRUNED_ARCHIVE_DIRNAME
    archived_records = 0
    archived_sources = 0
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for matter in pruned_matters:
            matter_id = str(matter.get("id") or "")
            cleaned_matter_id = _clean_matter_record_id(matter_id)
            if not cleaned_matter_id:
                continue
            archived_matter = copy.deepcopy(matter)
            source_archive = _archive_pruned_source_document(matter, archive_dir)
            if source_archive is not None:
                archived_matter["archived_source_document"] = source_archive
                if source_archive.get("present"):
                    archived_sources += 1
            archive_path = archive_dir / f"{cleaned_matter_id}.json"
            _write_json_atomic(archive_path, archived_matter)
            archived_records += 1
    except (MatterStoreError, OSError) as error:
        telemetry.increment("matter_prune_archive_failures")
        print(f"Could not archive pruned matters before deletion: {error.__class__.__name__}")
        return False
    if context == "bulk_archive":
        telemetry.increment("bulk_archive_matters_archived", len(pruned_matters))
        if archived_sources:
            telemetry.increment("matter_sources_archived", archived_sources)
        # Log counts only, never matter titles, to avoid leaking NDA content.
        print(
            f"Bulk archive: archived {archived_records} matter record(s) and "
            f"{archived_sources} source document(s) to {archive_dir.name}/."
        )
        return True
    telemetry.increment("matters_pruned", len(pruned_matters))
    if archived_sources:
        telemetry.increment("matter_sources_archived", archived_sources)
    active_count = sum(1 for matter in pruned_matters if _matter_is_active(matter))
    if active_count:
        telemetry.increment("active_matters_pruned", active_count)
        # Log counts only, never matter titles, to avoid leaking NDA content.
        print(
            f"Retention limit reached: pruned {len(pruned_matters)} matter(s) including "
            f"{active_count} active NDA(s); archived {archived_records} record(s) and "
            f"{archived_sources} source document(s) to {archive_dir.name}/."
        )
    return True


def _archive_pruned_source_document(matter: dict[str, Any], archive_dir: Path) -> dict[str, Any] | None:
    stored_filename = str(matter.get("stored_filename") or "")
    if not stored_filename:
        return None
    source_archive: dict[str, Any] = {
        "stored_filename": stored_filename,
        "present": False,
    }
    source_path = source_document_path(matter)
    if source_path is None:
        return source_archive

    archive_relative_path = Path("uploads") / source_path.name
    archive_path = archive_dir / archive_relative_path
    document_bytes = source_path.read_bytes()
    _write_bytes_atomic(archive_path, document_bytes)
    source_archive.update({
        "present": True,
        "archive_path": archive_relative_path.as_posix(),
        "size_bytes": len(document_bytes),
        "sha256": hashlib.sha256(document_bytes).hexdigest(),
    })
    return source_archive


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-call-unique temp name — see _write_json_atomic. Artifact byte-staging
    # (put_artifact_document) runs outside the store lock, so two concurrent
    # same-role registrations can target the same path; a fixed ``.tmp`` would let
    # one writer's ``replace`` vanish the other's tmp mid-flight (FileNotFoundError).
    temporary_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _gmail_attachment_keys_for_metadata(metadata: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    message_id = str(metadata.get("gmail_message_id") or "")
    if not message_id:
        return ()
    keys: list[tuple[str, str]] = []
    attachment_sha256 = str(metadata.get("gmail_attachment_sha256") or "")
    if attachment_sha256:
        keys.append((message_id, f"sha256:{attachment_sha256}"))
    part_id = str(metadata.get("gmail_part_id") or "")
    if part_id:
        keys.append((message_id, f"part:{part_id}"))
    attachment_id = str(metadata.get("gmail_attachment_id") or "")
    if attachment_id:
        keys.append((message_id, f"attachment:{attachment_id}"))
    filename_key = _gmail_attachment_filename_key(
        str(metadata.get("attachment_filename") or metadata.get("source_filename") or "")
    )
    if filename_key:
        keys.append((message_id, f"filename:{filename_key}"))
    return tuple(keys)


def _matter_duplicate_rank(matter: dict[str, Any]) -> tuple[int, str]:
    board_rank = {
        "gmail_demo": 0,
        "in_review": 1,
        "reviewed": 2,
        "redline_ready": 2,
        "sent": 3,
        "signed_closed": 4,
    }.get(str(matter.get("board_column") or ""), 0)
    return (board_rank, str(matter.get("updated_at") or matter.get("created_at") or ""))


def _gmail_attachment_key_index(matters: list[dict[str, Any]]) -> dict[tuple[str, str], list[int]]:
    """Map each gmail-attachment key to the positions of the matters that carry it.

    Built once per store load so a dedupe lookup is O(candidate keys) instead of an
    O(N) scan over every matter for every create/dedupe — the read/compare cost that
    otherwise grows with the store on each gmail import.
    """
    index: dict[tuple[str, str], list[int]] = {}
    for position, matter in enumerate(matters):
        for key in _gmail_attachment_keys_for_metadata(matter):
            index.setdefault(key, []).append(position)
    return index


def _find_gmail_duplicate_unlocked(
    matters: list[dict[str, Any]],
    metadata: dict[str, Any],
    owner_user_id: str = "",
    *,
    key_index: dict[tuple[str, str], list[int]] | None = None,
) -> dict[str, Any] | None:
    metadata_keys = _gmail_attachment_keys_for_metadata(metadata)
    if not metadata_keys:
        return None
    owner_user_id = _clean_owner_user_id(owner_user_id or metadata.get("owner_user_id"))
    if key_index is None:
        key_index = _gmail_attachment_key_index(matters)
    # Gather only the matters that share at least one key, then return the earliest
    # in store order (preserving the prior first-match-wins behaviour) that also
    # satisfies owner + sha256-on-filename matching.
    candidate_positions = sorted({
        position
        for key in metadata_keys
        for position in key_index.get(key, ())
    })
    for position in candidate_positions:
        matter = matters[position]
        if not _matter_owner_matches(matter, owner_user_id):
            continue
        if _gmail_attachments_match(metadata, matter):
            return matter
    return None


def _gmail_attachments_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    shared_keys = set(_gmail_attachment_keys_for_metadata(left)) & set(_gmail_attachment_keys_for_metadata(right))
    if not shared_keys:
        return False
    if any(not key[1].startswith("filename:") for key in shared_keys):
        return True
    # The only thing the two attachments share is a filename. A filename is NOT a
    # content identity: two genuinely different documents can carry the same name
    # (two counterparties both send "NDA.docx", or a name reused for a new draft).
    # Treat them as the same attachment ONLY when their stored bytes hash equal.
    # If either side is missing a content hash we cannot confirm they are the same
    # document, so we must keep both rather than merge one away (data loss).
    left_sha256 = str(left.get("gmail_attachment_sha256") or "")
    right_sha256 = str(right.get("gmail_attachment_sha256") or "")
    return bool(left_sha256) and left_sha256 == right_sha256


def _gmail_attachment_filename_key(filename: str) -> str:
    return _clean_source_filename(filename).casefold() if filename else ""


def _matter_owner_matches(matter: dict[str, Any], owner_user_id: str = "") -> bool:
    # Access scoping is fail-closed for authenticated requests. An empty
    # owner_user_id is the single-tenant / auth-disabled path: there is no
    # caller identity to scope against, so every matter is in scope (this is
    # how local/no-auth deployments work). A NON-empty owner_user_id means an
    # authenticated multi-tenant request, and it must only match matters owned
    # by exactly that user. A matter with NO owner (legacy import, Gmail
    # shared-sync before ownership assignment, etc.) is NOT a wildcard — it must
    # never be served to an arbitrary authenticated user, or one tenant could
    # read/edit/delete/export another's data.
    owner_user_id = _clean_owner_user_id(owner_user_id)
    if not owner_user_id:
        return True
    matter_owner_user_id = _clean_owner_user_id(matter.get("owner_user_id"))
    return matter_owner_user_id == owner_user_id


def _clean_owner_user_id(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.@:-]+", "-", str(value or "").strip())[:160].strip("-")


def _clean_email_key(value: object) -> str:
    """Case-folded, whitespace-stripped email for ownership-backfill matching."""
    return str(value or "").strip().casefold()


def _stored_document_manifest(matter: dict[str, Any]) -> dict[str, Any] | None:
    stored_filename = str(matter.get("stored_filename") or "")
    if not stored_filename:
        return None
    manifest: dict[str, Any] = {
        "matter_id": str(matter.get("id") or ""),
        "source_filename": str(matter.get("source_filename") or ""),
        "stored_filename": stored_filename,
        "present": False,
    }
    source_path = (UPLOADS_DIR / stored_filename).resolve()
    if source_path.parent != UPLOADS_DIR.resolve() or not source_path.is_file():
        return manifest
    stat = source_path.stat()
    manifest.update({
        "present": True,
        "size_bytes": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    })
    return manifest


def _delete_stored_document(matter: dict[str, Any]) -> None:
    stored_filename = str(matter.get("stored_filename") or "")
    if not stored_filename:
        return
    try:
        source_path = (UPLOADS_DIR / stored_filename).resolve()
        if source_path.parent == UPLOADS_DIR.resolve():
            source_path.unlink(missing_ok=True)
    except OSError:
        return


def _safe_filename(filename: str) -> str:
    basename = Path(filename).name or "nda.docx"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip("-._")
    if not safe_name.lower().endswith((".docx", ".pdf")):
        safe_name = f"{safe_name or 'nda'}.docx"
    if len(safe_name) > MAX_SOURCE_FILENAME_LENGTH:
        stem = Path(safe_name).stem[: MAX_SOURCE_FILENAME_LENGTH - 5].rstrip("-._") or "nda"
        suffix = Path(safe_name).suffix if Path(safe_name).suffix.lower() in {".docx", ".pdf"} else ".docx"
        safe_name = f"{stem}{suffix}"
    return safe_name or "nda.docx"


def _clean_source_filename(filename: str) -> str:
    clean_name = " ".join(str(filename or "").split())
    if len(clean_name) <= MAX_SOURCE_FILENAME_LENGTH:
        return clean_name or "nda.docx"
    suffix = Path(clean_name).suffix
    suffix = suffix if suffix.lower() in {".docx", ".pdf"} else ".docx"
    stem_limit = max(1, MAX_SOURCE_FILENAME_LENGTH - len(suffix))
    return f"{Path(clean_name).stem[:stem_limit].rstrip(' ._-') or 'nda'}{suffix}"


def _intake_metadata(source_filename: str, received_at: str, metadata: dict[str, Any] | None) -> dict[str, str]:
    metadata = metadata or {}
    subject = _clean_metadata_value(metadata.get("subject")) or Path(source_filename).stem or "Untitled NDA"
    sender = _clean_metadata_value(metadata.get("sender")) or "Manual upload"
    snippet = _clean_metadata_value(metadata.get("message_snippet")) or f"Manual upload of {Path(source_filename).name or 'NDA document'}."
    attachment_filename = _clean_metadata_value(metadata.get("attachment_filename")) or source_filename
    metadata_received_at = _clean_metadata_value(metadata.get("received_at"))
    intake = {
        "sender": sender,
        "subject": subject,
        "received_at": metadata_received_at or received_at,
        "message_snippet": snippet,
        "attachment_filename": attachment_filename,
    }
    for field in GMAIL_METADATA_FIELDS:
        value = _clean_metadata_value(metadata.get(field))
        if value:
            intake[field] = value
    return intake


def _attach_intake_counterparty(matter: dict[str, Any], review_result: object) -> None:
    """Persist the AI-extracted counterparty at matter["intake_metadata"]["counterparty"].

    SHARED CONTRACT: this exact nested location is what other teammates' accessor
    reads. The counterparty block is produced by the AI-first review and rides on
    review_result["counterparty"]; copy it onto the matter's intake_metadata so it is
    durable (the matter dict is json-persisted whole) and reloads in the same shape.

    Best-effort + fail-open: when the review result carries no counterparty block the
    intake_metadata is left untouched (no empty key churn), and this never raises.
    """
    if not isinstance(review_result, dict):
        return
    counterparty = review_result.get("counterparty")
    if not isinstance(counterparty, dict):
        return
    intake_metadata = matter.get("intake_metadata")
    if not isinstance(intake_metadata, dict):
        intake_metadata = {}
        matter["intake_metadata"] = intake_metadata
    intake_metadata["counterparty"] = copy.deepcopy(counterparty)


def _clean_metadata_value(value: object, max_length: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_length]
