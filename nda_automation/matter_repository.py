"""The matter-persistence seam.

Callers depend on the ``MatterRepository`` operations — never on ``DATA_DIR``,
``UPLOADS_DIR`` or the ``matters.json`` layout. Two adapters implement it:

* ``DiskMatterRepository`` — the production adapter; delegates to the disk-backed
  ``matter_store`` module (kept exactly as-is).
* ``InMemoryMatterRepository`` — a dict-backed adapter for tests, fully isolated
  per instance, so matter-dependent code is testable without a tempdir. It
  reuses ``matter_store``'s pure helpers so matter shapes, sorting, source bytes
  and gmail de-duplication match the disk store.
"""
from __future__ import annotations

import copy
import hashlib
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import matter_store
from .matter_store import (
    _clean_source_filename,
    _find_gmail_duplicate_unlocked,
    _gmail_attachments_match,
    _gmail_attachment_keys_for_metadata,
    _intake_metadata,
    _matter_duplicate_rank,
    _prune_stored_matters,
    _safe_filename,
)


@runtime_checkable
class MatterRepository(Protocol):
    """Persistence-agnostic operations on matters and their source documents."""

    def list_matters(self) -> list[dict[str, Any]]: ...

    def get_matter(self, matter_id: str) -> dict[str, Any] | None: ...

    def get_source_document_bytes(self, matter: dict[str, Any]) -> bytes | None: ...

    def create_matter(
        self,
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
    ) -> dict[str, Any]: ...

    def update_matter_stage(self, matter_id: str, board_column: str) -> dict[str, Any] | None: ...

    def update_matter_fields(self, matter_id: str, fields: dict[str, Any]) -> dict[str, Any] | None: ...

    def update_redline_draft(
        self, matter_id: str, redline_draft: dict[str, Any] | None
    ) -> dict[str, Any] | None: ...

    def update_matter_review(
        self, matter_id: str, review_result: dict[str, Any], triage: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def update_matter_ai_first_review(
        self, matter_id: str, ai_first_review_result: dict[str, Any], metadata: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def update_matter_review_comparison(
        self, matter_id: str, review_comparison: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def delete_matter(self, matter_id: str) -> dict[str, Any] | None: ...

    def reset_demo_repository(self) -> int: ...

    def deduplicate_gmail_matters(self) -> int: ...

    def find_gmail_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        attachment_filename: str = "",
        attachment_sha256: str = "",
        part_id: str = "",
    ) -> dict[str, Any] | None: ...

    def export_matters_backup(self) -> dict[str, Any]: ...


class DiskMatterRepository:
    """Production adapter over the disk-backed ``matter_store`` module."""

    def list_matters(self) -> list[dict[str, Any]]:
        return matter_store.list_matters()

    def get_matter(self, matter_id: str) -> dict[str, Any] | None:
        return matter_store.get_matter(matter_id)

    def get_source_document_bytes(self, matter: dict[str, Any]) -> bytes | None:
        return matter_store.get_source_document_bytes(matter)

    def create_matter(
        self,
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
        return matter_store.create_matter(
            source_filename=source_filename,
            document_bytes=document_bytes,
            extracted_text=extracted_text,
            review_result=review_result,
            triage=triage,
            source_type=source_type,
            board_column=board_column,
            intake_metadata=intake_metadata,
            dedupe_gmail=dedupe_gmail,
        )

    def update_matter_stage(self, matter_id: str, board_column: str) -> dict[str, Any] | None:
        return matter_store.update_matter_stage(matter_id, board_column)

    def update_matter_fields(self, matter_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        return matter_store.update_matter_fields(matter_id, fields)

    def update_redline_draft(
        self, matter_id: str, redline_draft: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        return matter_store.update_redline_draft(matter_id, redline_draft)

    def update_matter_review(
        self, matter_id: str, review_result: dict[str, Any], triage: dict[str, Any]
    ) -> dict[str, Any] | None:
        return matter_store.update_matter_review(matter_id, review_result, triage)

    def update_matter_ai_first_review(
        self, matter_id: str, ai_first_review_result: dict[str, Any], metadata: dict[str, Any]
    ) -> dict[str, Any] | None:
        return matter_store.update_matter_ai_first_review(matter_id, ai_first_review_result, metadata)

    def update_matter_review_comparison(
        self, matter_id: str, review_comparison: dict[str, Any]
    ) -> dict[str, Any] | None:
        return matter_store.update_matter_review_comparison(matter_id, review_comparison)

    def delete_matter(self, matter_id: str) -> dict[str, Any] | None:
        return matter_store.delete_matter(matter_id)

    def reset_demo_repository(self) -> int:
        return matter_store.reset_demo_repository()

    def deduplicate_gmail_matters(self) -> int:
        return matter_store.deduplicate_gmail_matters()

    def find_gmail_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        attachment_filename: str = "",
        attachment_sha256: str = "",
        part_id: str = "",
    ) -> dict[str, Any] | None:
        return matter_store.find_gmail_attachment(
            message_id,
            attachment_id,
            attachment_filename=attachment_filename,
            attachment_sha256=attachment_sha256,
            part_id=part_id,
        )

    def export_matters_backup(self) -> dict[str, Any]:
        return matter_store.export_matters_backup()


class InMemoryMatterRepository:
    """Dict-backed adapter for tests. No DATA_DIR, no files, isolated per instance."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._matters: list[dict[str, Any]] = []
        self._documents: dict[str, bytes] = {}

    # --- reads ---------------------------------------------------------
    def list_matters(self) -> list[dict[str, Any]]:
        with self._lock:
            matters = copy.deepcopy(self._matters)
        return sorted(matters, key=lambda matter: str(matter.get("created_at") or ""), reverse=True)

    def get_matter(self, matter_id: str) -> dict[str, Any] | None:
        with self._lock:
            for matter in self._matters:
                if matter.get("id") == matter_id:
                    return copy.deepcopy(matter)
        return None

    def get_source_document_bytes(self, matter: dict[str, Any]) -> bytes | None:
        stored_filename = str(matter.get("stored_filename") or "")
        if not stored_filename:
            return None
        with self._lock:
            return self._documents.get(stored_filename)

    # --- create --------------------------------------------------------
    def create_matter(
        self,
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

        with self._lock:
            if dedupe_gmail:
                existing_matter = _find_gmail_duplicate_unlocked(self._matters, metadata)
                if existing_matter is not None:
                    return {**copy.deepcopy(existing_matter), "_existing_gmail_duplicate": True}

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
            self._documents[stored_filename] = document_bytes
            self._matters.append(matter)
            kept_matters, pruned_matters = _prune_stored_matters(
                self._matters, protected_matter_id=matter_id
            )
            self._matters = kept_matters
            for pruned_matter in pruned_matters:
                self._documents.pop(str(pruned_matter.get("stored_filename") or ""), None)
            return copy.deepcopy(matter)

    # --- updates -------------------------------------------------------
    def update_matter_stage(self, matter_id: str, board_column: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id:
                    continue
                updated_matter = {
                    **matter,
                    "board_column": board_column,
                    "status": "closed" if board_column == "signed_closed" else "active",
                    "updated_at": now,
                }
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def update_matter_fields(self, matter_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        cleaned_fields = {
            key: value for key, value in fields.items() if key in matter_store.MATTER_UPDATE_FIELDS
        }
        if not cleaned_fields:
            return self.get_matter(matter_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id:
                    continue
                updated_matter = {**matter, **cleaned_fields, "updated_at": now}
                if "board_column" in cleaned_fields and "status" not in cleaned_fields:
                    updated_matter["status"] = (
                        "closed" if cleaned_fields["board_column"] == "signed_closed" else "active"
                    )
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def update_redline_draft(
        self, matter_id: str, redline_draft: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id:
                    continue
                updated_matter = {**matter, "updated_at": now}
                if redline_draft is None:
                    updated_matter.pop("redline_draft", None)
                else:
                    updated_matter["redline_draft"] = redline_draft
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def update_matter_review(
        self, matter_id: str, review_result: dict[str, Any], triage: dict[str, Any]
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id:
                    continue
                updated_matter = {
                    **matter,
                    "review_result": review_result,
                    **triage,
                    "human_reviewed": False,
                    "updated_at": now,
                }
                updated_matter.pop("redline_draft", None)
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def update_matter_ai_first_review(
        self, matter_id: str, ai_first_review_result: dict[str, Any], metadata: dict[str, Any]
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id:
                    continue
                updated_matter = {
                    **matter,
                    "ai_first_review_result": copy.deepcopy(ai_first_review_result),
                    "ai_first_review_metadata": {
                        **copy.deepcopy(metadata),
                        "stored_at": now,
                    },
                    "updated_at": now,
                }
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def update_matter_review_comparison(
        self, matter_id: str, review_comparison: dict[str, Any]
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id:
                    continue
                updated_matter = {
                    **matter,
                    "review_comparison": {
                        **copy.deepcopy(review_comparison),
                        "stored_at": now,
                    },
                    "updated_at": now,
                }
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    # --- delete / reset / dedupe --------------------------------------
    def delete_matter(self, matter_id: str) -> dict[str, Any] | None:
        with self._lock:
            deleted_matter = next(
                (matter for matter in self._matters if matter.get("id") == matter_id), None
            )
            if deleted_matter is None:
                return None
            self._matters = [matter for matter in self._matters if matter.get("id") != matter_id]
            self._documents.pop(str(deleted_matter.get("stored_filename") or ""), None)
            return copy.deepcopy(deleted_matter)

    def reset_demo_repository(self) -> int:
        with self._lock:
            count = len(self._matters)
            self._matters = []
            self._documents = {}
        return count

    def deduplicate_gmail_matters(self) -> int:
        with self._lock:
            matters = self._matters
            duplicate_groups: list[list[dict[str, Any]]] = []
            for matter in matters:
                if not _gmail_attachment_keys_for_metadata(matter):
                    continue
                matching_groups = [
                    group
                    for group in duplicate_groups
                    if any(_gmail_attachments_match(matter, existing) for existing in group)
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
                id(matter) for group in duplicate_groups if len(group) > 1 for matter in group
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
            self._matters = kept
            for matter in removed:
                self._documents.pop(str(matter.get("stored_filename") or ""), None)
            return len(removed)

    def find_gmail_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        attachment_filename: str = "",
        attachment_sha256: str = "",
        part_id: str = "",
    ) -> dict[str, Any] | None:
        if not message_id:
            return None
        with self._lock:
            match = _find_gmail_duplicate_unlocked(
                self._matters,
                {
                    "attachment_filename": attachment_filename,
                    "gmail_attachment_id": attachment_id,
                    "gmail_attachment_sha256": attachment_sha256,
                    "gmail_message_id": message_id,
                    "gmail_part_id": part_id,
                },
            )
        return copy.deepcopy(match) if match is not None else None

    def export_matters_backup(self) -> dict[str, Any]:
        with self._lock:
            matters = copy.deepcopy(self._matters)
            documents = []
            for matter in self._matters:
                stored_filename = str(matter.get("stored_filename") or "")
                if not stored_filename:
                    continue
                data = self._documents.get(stored_filename)
                manifest: dict[str, Any] = {
                    "matter_id": str(matter.get("id") or ""),
                    "source_filename": str(matter.get("source_filename") or ""),
                    "stored_filename": stored_filename,
                    "present": data is not None,
                }
                if data is not None:
                    manifest["size_bytes"] = len(data)
                documents.append(manifest)
        return {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "matter_count": len(matters),
            "matters": matters,
            "documents": documents,
        }
