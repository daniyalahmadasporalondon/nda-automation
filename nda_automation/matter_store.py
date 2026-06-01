from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
MAX_SOURCE_FILENAME_LENGTH = 180
GMAIL_METADATA_FIELDS = (
    "gmail_account",
    "gmail_attachment_id",
    "gmail_attachment_sha256",
    "gmail_message_id",
    "gmail_part_id",
    "gmail_thread_id",
    "reply_to",
)
MATTER_UPDATE_FIELDS = {
    "board_column",
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


def list_matters() -> list[dict[str, Any]]:
    with _locked_store():
        matters = _load_matters()
    return sorted(matters, key=lambda matter: str(matter.get("created_at") or ""), reverse=True)


def export_matters_backup() -> dict[str, Any]:
    with _locked_store():
        matters = _load_matters()
        documents = [_stored_document_manifest(matter) for matter in matters]
    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "matter_count": len(matters),
        "matters": matters,
        "documents": [document for document in documents if document is not None],
    }


def get_matter(matter_id: str) -> dict[str, Any] | None:
    with _locked_store():
        for matter in _load_matters():
            if matter.get("id") == matter_id:
                return matter
    return None


def get_source_document_bytes(matter: dict[str, Any]) -> bytes | None:
    stored_filename = str(matter.get("stored_filename") or "")
    if not stored_filename:
        return None
    with _locked_store():
        source_path = (UPLOADS_DIR / stored_filename).resolve()
        if source_path.parent != UPLOADS_DIR.resolve() or not source_path.is_file():
            return None
        return source_path.read_bytes()


def find_gmail_attachment(
    message_id: str,
    attachment_id: str,
    *,
    attachment_filename: str = "",
    attachment_sha256: str = "",
    part_id: str = "",
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
            },
        )


def update_matter_stage(matter_id: str, board_column: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        matters = _load_matters()
        for index, matter in enumerate(matters):
            if matter.get("id") != matter_id:
                continue
            updated_matter = {
                **matter,
                "board_column": board_column,
                "status": "closed" if board_column == "signed_closed" else "active",
                "updated_at": now,
            }
            matters[index] = updated_matter
            _save_matters(matters)
            return updated_matter
    return None


def update_matter_fields(matter_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    cleaned_fields = {key: value for key, value in fields.items() if key in MATTER_UPDATE_FIELDS}
    if not cleaned_fields:
        return get_matter(matter_id)

    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        matters = _load_matters()
        for index, matter in enumerate(matters):
            if matter.get("id") != matter_id:
                continue
            updated_matter = {
                **matter,
                **cleaned_fields,
                "updated_at": now,
            }
            if "board_column" in cleaned_fields and "status" not in cleaned_fields:
                updated_matter["status"] = "closed" if cleaned_fields["board_column"] == "signed_closed" else "active"
            matters[index] = updated_matter
            _save_matters(matters)
            return updated_matter
    return None


def update_redline_draft(matter_id: str, redline_draft: dict[str, Any] | None) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _locked_store():
        matters = _load_matters()
        for index, matter in enumerate(matters):
            if matter.get("id") != matter_id:
                continue
            updated_matter = {
                **matter,
                "updated_at": now,
            }
            if redline_draft is None:
                updated_matter.pop("redline_draft", None)
            else:
                updated_matter["redline_draft"] = redline_draft
            matters[index] = updated_matter
            _save_matters(matters)
            return updated_matter
    return None


def reset_demo_repository() -> int:
    with _locked_store():
        matters = _load_matters()
        _save_matters([])
        for matter in matters:
            _delete_stored_document(matter)
    return len(matters)


def delete_matter(matter_id: str) -> dict[str, Any] | None:
    with _locked_store():
        matters = _load_matters()
        deleted_matter = next((matter for matter in matters if matter.get("id") == matter_id), None)
        if deleted_matter is None:
            return None
        kept_matters = [matter for matter in matters if matter.get("id") != matter_id]
        _save_matters(kept_matters)
        _delete_stored_document(deleted_matter)
        return deleted_matter


def deduplicate_gmail_matters() -> int:
    with _locked_store():
        matters = _load_matters()
        duplicate_groups: list[list[dict[str, Any]]] = []
        for matter in matters:
            if not _gmail_attachment_keys_for_metadata(matter):
                continue
            matching_groups = [
                group
                for group in duplicate_groups
                if any(_gmail_attachments_match(matter, existing_matter) for existing_matter in group)
            ]
            if not matching_groups:
                duplicate_groups.append([matter])
                continue
            primary_group = matching_groups[0]
            primary_group.append(matter)
            for extra_group in matching_groups[1:]:
                primary_group.extend(extra_group)
                duplicate_groups.remove(extra_group)

        duplicate_member_ids = {
            id(matter)
            for group in duplicate_groups
            if len(group) > 1
            for matter in group
        }
        winner_ids = {
            id(max(group, key=_matter_duplicate_rank))
            for group in duplicate_groups
            if len(group) > 1
        }

        removed: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for matter in matters:
            if id(matter) in duplicate_member_ids and id(matter) not in winner_ids:
                removed.append(matter)
                continue
            kept.append(matter)

        if not removed:
            return 0
        _save_matters(kept)
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
) -> dict[str, Any]:
    matter_id = f"matter_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    source_filename = _clean_source_filename(source_filename)
    safe_source_name = _safe_filename(source_filename)
    stored_filename = f"{matter_id}-{safe_source_name}"
    metadata = _intake_metadata(source_filename, now, intake_metadata)
    if dedupe_gmail and not metadata.get("gmail_attachment_sha256"):
        metadata["gmail_attachment_sha256"] = hashlib.sha256(document_bytes).hexdigest()

    with _locked_store():
        matters = _load_matters()
        if dedupe_gmail:
            existing_matter = _find_gmail_duplicate_unlocked(matters, metadata)
            if existing_matter is not None:
                return {**existing_matter, "_existing_gmail_duplicate": True}

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
        matters.append(matter)
        matters, pruned_matters = _prune_stored_matters(matters, protected_matter_id=matter_id)
        try:
            _save_matters(matters)
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = MATTERS_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(matters, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary_path.replace(MATTERS_PATH)
    _fsync_directory(DATA_DIR)


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
        if matter.get("id") != protected_matter_id
    ]
    removable.sort(key=lambda item: _matter_retention_sort_key(item[1]))
    remove_count = len(matters) - retention_limit
    removed_indexes = {index for index, _matter in removable[:remove_count]}
    kept = [matter for index, matter in enumerate(matters) if index not in removed_indexes]
    pruned = [matter for _index, matter in removable[:remove_count]]
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
        "redline_ready": 2,
        "signed_closed": 3,
    }.get(str(matter.get("board_column") or ""), 0)
    return (board_rank, str(matter.get("updated_at") or matter.get("created_at") or ""))


def _find_gmail_duplicate_unlocked(
    matters: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    if not _gmail_attachment_keys_for_metadata(metadata):
        return None
    for matter in matters:
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
