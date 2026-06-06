from __future__ import annotations

import copy
from contextlib import contextmanager, suppress
import hashlib
import json
import os
import re
import threading
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
_MATTERS_LOCK = threading.RLock()
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
    "gmail_message_id",
    "gmail_part_id",
    "gmail_thread_id",
    "reply_to",
)
MATTER_UPDATE_FIELDS = {
    "board_column",
    "human_reviewed",
    "last_outbound_account",
    "last_outbound_at",
    "last_outbound_filename",
    "last_outbound_message_id",
    "last_outbound_subject",
    "last_outbound_thread_id",
    "last_outbound_to",
    "status",
}


class MatterStoreError(RuntimeError):
    pass


def list_matters(owner_user_id: str = "") -> list[dict[str, Any]]:
    with _locked_store():
        matters = [
            matter
            for matter in _load_matters()
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
    return None


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
    return None


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
    return None


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
    return None


def set_clause_reviewer_decision(
    matter_id: str,
    clause_id: str,
    reviewer_decision: dict[str, Any] | None,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    """Persist (or clear when None) a single clause's reviewer_decision.

    Decisions live in a matter-level ``reviewer_decisions`` map keyed by clause
    id; the review_result payload is never mutated here.
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
        _save_matter_record(updated_matter)
        return updated_matter
    return None


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
            "matter_timeline": timeline,
            "updated_at": approved_at,
        }
        _save_matter_record(updated_matter)
        return updated_matter
    return None


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
    return None


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
    return None


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
        matters = _load_matters()
        removed = [
            matter
            for matter in matters
            if _matter_owner_matches(matter, owner_user_id)
        ]
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
        no_hash_ids = [
            object_id
            for object_id in object_ids
            if not str(matters_by_id[object_id].get("gmail_attachment_sha256") or "")
        ]
        if no_hash_ids:
            for object_id in object_ids[1:]:
                union(object_ids[0], object_id)
            continue
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
        matters = _load_matters()
        removal_ids = _gmail_duplicate_removal_ids(matters, owner_user_id=owner_user_id)

        removed: list[dict[str, Any]] = []
        for matter in matters:
            if id(matter) in removal_ids:
                removed.append(matter)

        if not removed:
            return 0
        for matter in removed:
            _delete_matter_record(matter)
    for matter in removed:
        _delete_stored_document(matter)
    return len(removed)


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
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    stored_path = UPLOADS_DIR / stored_filename
    stored_path.write_bytes(document_bytes)

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
    pruned_matters: list[dict[str, Any]] = []
    duplicate_matter: dict[str, Any] | None = None
    saved_new_record = False
    try:
        with _locked_store():
            _ensure_matter_records_from_legacy()
            matters = _load_matters()
            if dedupe_gmail:
                existing_matter = _find_gmail_duplicate_unlocked(matters, metadata, owner_user_id=owner_user_id)
                if existing_matter is not None:
                    duplicate_matter = {**existing_matter, "_existing_gmail_duplicate": True}
                else:
                    matters.append(matter)
                    matters, pruned_matters = _apply_retention_pruning(matters, protected_matter_id=matter_id)
                    _save_matter_record(matter)
                    saved_new_record = True
                    for pruned_matter in pruned_matters:
                        _delete_matter_record(pruned_matter)
            else:
                matters.append(matter)
                matters, pruned_matters = _apply_retention_pruning(matters, protected_matter_id=matter_id)
                _save_matter_record(matter)
                saved_new_record = True
                for pruned_matter in pruned_matters:
                    _delete_matter_record(pruned_matter)
    except Exception:
        stored_path.unlink(missing_ok=True)
        if saved_new_record:
            with suppress(MatterStoreError, OSError):
                _delete_matter_record(matter_id)
        raise
    if duplicate_matter is not None:
        stored_path.unlink(missing_ok=True)
        return duplicate_matter
    for pruned_matter in pruned_matters:
        _delete_stored_document(pruned_matter)
    return matter


@contextmanager
def _locked_store():
    with _MATTERS_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (DATA_DIR / "matters.lock").open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_matters() -> list[dict[str, Any]]:
    if MATTERS_PATH.is_file():
        return _load_legacy_matters()
    return _load_matter_records()


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


def _delete_matter_record(matter: dict[str, Any] | str) -> None:
    matter_id = _clean_matter_record_id(matter.get("id") if isinstance(matter, dict) else matter)
    if not matter_id:
        raise MatterStoreError("Matter record must include an id.")
    record_path = _matter_records_dir() / f"{matter_id}.json"
    try:
        record_path.unlink(missing_ok=True)
        _fsync_directory(record_path.parent)
    except OSError as exc:
        raise MatterStoreError("Matter record could not be deleted.") from exc


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
    temporary_path = path.with_name(f"{path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary_path.replace(path)
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
    retention_limit = _stored_matter_limit()
    if retention_limit <= 0 or len(matters) <= retention_limit:
        return matters, []

    removable = [
        (index, matter)
        for index, matter in enumerate(matters)
        if matter.get("id") != protected_matter_id and not _matter_is_active(matter)
    ]
    if not removable:
        telemetry.increment("matter_retention_over_cap_without_prune")
        return matters, []
    removable.sort(key=lambda item: _matter_retention_sort_key(item[1]))
    remove_count = min(len(matters) - retention_limit, len(removable))
    if remove_count <= 0:
        return matters, []
    removed_indexes = {index for index, _matter in removable[:remove_count]}
    kept = [matter for index, matter in enumerate(matters) if index not in removed_indexes]
    pruned = [matter for _index, matter in removable[:remove_count]]
    return kept, pruned


def _apply_retention_pruning(
    matters: list[dict[str, Any]],
    *,
    protected_matter_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept, pruned = _prune_stored_matters(matters, protected_matter_id=protected_matter_id)
    if not pruned:
        return kept, []
    if not _archive_pruned_matters(pruned):
        return matters, []
    return kept, pruned


def _stored_matter_limit() -> int:
    raw_limit = os.environ.get("NDA_MATTER_RETENTION_LIMIT", str(DEFAULT_MAX_STORED_MATTERS))
    try:
        return max(0, int(raw_limit))
    except ValueError:
        return DEFAULT_MAX_STORED_MATTERS


def _matter_retention_sort_key(matter: dict[str, Any]) -> tuple[int, str]:
    is_closed = 0 if matter.get("status") == "closed" or matter.get("board_column") == "signed_closed" else 1
    return (is_closed, str(matter.get("updated_at") or matter.get("created_at") or ""))


def _matter_is_active(matter: dict[str, Any]) -> bool:
    return matter.get("status") != "closed" and matter.get("board_column") != "signed_closed"


def _archive_pruned_matters(pruned_matters: list[dict[str, Any]]) -> bool:
    # Retention pruning deletes stored documents. Archive each source document and
    # full matter record before saving the pruned store so an archive failure
    # keeps the matter live.
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
    temporary_path = path.with_name(f"{path.name}.tmp")
    with temporary_path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary_path.replace(path)
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
    left_sha256 = str(left.get("gmail_attachment_sha256") or "")
    right_sha256 = str(right.get("gmail_attachment_sha256") or "")
    return not (left_sha256 and right_sha256 and left_sha256 != right_sha256)


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


def _clean_metadata_value(value: object, max_length: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_length]
