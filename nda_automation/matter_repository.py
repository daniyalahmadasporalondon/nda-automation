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
    _clean_owner_user_id,
    _find_gmail_duplicate_unlocked,
    _gmail_duplicate_removal_ids,
    _intake_metadata,
    _matter_owner_matches,
    _prune_stored_matters,
    _safe_filename,
)


@runtime_checkable
class MatterRepository(Protocol):
    """Persistence-agnostic operations on matters and their source documents."""

    def list_matters(self, owner_user_id: str = "") -> list[dict[str, Any]]: ...

    def get_matter(self, matter_id: str, owner_user_id: str = "") -> dict[str, Any] | None: ...

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
        owner_user_id: str = "",
    ) -> dict[str, Any]: ...

    def update_matter_stage(self, matter_id: str, board_column: str, owner_user_id: str = "") -> dict[str, Any] | None: ...

    def update_matter_fields(self, matter_id: str, fields: dict[str, Any], owner_user_id: str = "") -> dict[str, Any] | None: ...

    def update_redline_draft(
        self, matter_id: str, redline_draft: dict[str, Any] | None, owner_user_id: str = ""
    ) -> dict[str, Any] | None: ...

    def update_matter_review(
        self, matter_id: str, review_result: dict[str, Any], triage: dict[str, Any], owner_user_id: str = ""
    ) -> dict[str, Any] | None: ...

    def set_clause_reviewer_decision(
        self,
        matter_id: str,
        clause_id: str,
        reviewer_decision: dict[str, Any] | None,
        owner_user_id: str = "",
    ) -> dict[str, Any] | None: ...

    def record_matter_approval(
        self,
        matter_id: str,
        *,
        approver: str,
        approved_at: str,
        timeline_event: dict[str, Any],
        owner_user_id: str = "",
    ) -> dict[str, Any] | None: ...

    def append_timeline_event(
        self, matter_id: str, timeline_event: dict[str, Any], owner_user_id: str = ""
    ) -> dict[str, Any] | None: ...

    def set_workflow_error(
        self, matter_id: str, workflow_error: dict[str, Any] | None, owner_user_id: str = ""
    ) -> dict[str, Any] | None: ...

    def update_matter_ai_first_review(
        self, matter_id: str, ai_first_review_result: dict[str, Any], metadata: dict[str, Any], owner_user_id: str = ""
    ) -> dict[str, Any] | None: ...

    def update_matter_artifacts(
        self,
        matter_id: str,
        artifacts: list[dict[str, Any]],
        current_artifact_id: str = "",
        owner_user_id: str = "",
    ) -> dict[str, Any] | None: ...

    def put_artifact_document(self, stored_filename: str, document_bytes: bytes) -> str: ...

    def get_artifact_document(self, stored_filename: str) -> bytes | None: ...

    def delete_matter(self, matter_id: str, owner_user_id: str = "") -> dict[str, Any] | None: ...

    def reset_demo_repository(self, owner_user_id: str = "") -> int: ...

    def deduplicate_gmail_matters(self, owner_user_id: str = "") -> int: ...

    def find_gmail_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        attachment_filename: str = "",
        attachment_sha256: str = "",
        part_id: str = "",
        owner_user_id: str = "",
    ) -> dict[str, Any] | None: ...

    def export_matters_backup(self, owner_user_id: str = "") -> dict[str, Any]: ...


class DiskMatterRepository:
    """Production adapter over the disk-backed ``matter_store`` module."""

    def list_matters(self, owner_user_id: str = "") -> list[dict[str, Any]]:
        return matter_store.list_matters(owner_user_id=owner_user_id)

    def get_matter(self, matter_id: str, owner_user_id: str = "") -> dict[str, Any] | None:
        return matter_store.get_matter(matter_id, owner_user_id=owner_user_id)

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
        owner_user_id: str = "",
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
            owner_user_id=owner_user_id,
        )

    def update_matter_stage(self, matter_id: str, board_column: str, owner_user_id: str = "") -> dict[str, Any] | None:
        return matter_store.update_matter_stage(matter_id, board_column, owner_user_id=owner_user_id)

    def update_matter_fields(self, matter_id: str, fields: dict[str, Any], owner_user_id: str = "") -> dict[str, Any] | None:
        return matter_store.update_matter_fields(matter_id, fields, owner_user_id=owner_user_id)

    def update_redline_draft(
        self, matter_id: str, redline_draft: dict[str, Any] | None, owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        return matter_store.update_redline_draft(matter_id, redline_draft, owner_user_id=owner_user_id)

    def update_matter_review(
        self, matter_id: str, review_result: dict[str, Any], triage: dict[str, Any], owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        return matter_store.update_matter_review(matter_id, review_result, triage, owner_user_id=owner_user_id)

    def set_clause_reviewer_decision(
        self,
        matter_id: str,
        clause_id: str,
        reviewer_decision: dict[str, Any] | None,
        owner_user_id: str = "",
    ) -> dict[str, Any] | None:
        return matter_store.set_clause_reviewer_decision(
            matter_id, clause_id, reviewer_decision, owner_user_id=owner_user_id
        )

    def record_matter_approval(
        self,
        matter_id: str,
        *,
        approver: str,
        approved_at: str,
        timeline_event: dict[str, Any],
        owner_user_id: str = "",
    ) -> dict[str, Any] | None:
        return matter_store.record_matter_approval(
            matter_id,
            approver=approver,
            approved_at=approved_at,
            timeline_event=timeline_event,
            owner_user_id=owner_user_id,
        )

    def append_timeline_event(
        self, matter_id: str, timeline_event: dict[str, Any], owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        return matter_store.append_timeline_event(matter_id, timeline_event, owner_user_id=owner_user_id)

    def set_workflow_error(
        self, matter_id: str, workflow_error: dict[str, Any] | None, owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        return matter_store.set_workflow_error(matter_id, workflow_error, owner_user_id=owner_user_id)

    def update_matter_ai_first_review(
        self, matter_id: str, ai_first_review_result: dict[str, Any], metadata: dict[str, Any], owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        return matter_store.update_matter_ai_first_review(matter_id, ai_first_review_result, metadata, owner_user_id=owner_user_id)

    def update_matter_artifacts(
        self,
        matter_id: str,
        artifacts: list[dict[str, Any]],
        current_artifact_id: str = "",
        owner_user_id: str = "",
    ) -> dict[str, Any] | None:
        return matter_store.update_matter_artifacts(
            matter_id, artifacts, current_artifact_id, owner_user_id=owner_user_id
        )

    def put_artifact_document(self, stored_filename: str, document_bytes: bytes) -> str:
        return matter_store.put_artifact_document(stored_filename, document_bytes)

    def get_artifact_document(self, stored_filename: str) -> bytes | None:
        return matter_store.get_artifact_document(stored_filename)

    def delete_matter(self, matter_id: str, owner_user_id: str = "") -> dict[str, Any] | None:
        return matter_store.delete_matter(matter_id, owner_user_id=owner_user_id)

    def reset_demo_repository(self, owner_user_id: str = "") -> int:
        return matter_store.reset_demo_repository(owner_user_id=owner_user_id)

    def deduplicate_gmail_matters(self, owner_user_id: str = "") -> int:
        return matter_store.deduplicate_gmail_matters(owner_user_id=owner_user_id)

    def find_gmail_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        attachment_filename: str = "",
        attachment_sha256: str = "",
        part_id: str = "",
        owner_user_id: str = "",
    ) -> dict[str, Any] | None:
        return matter_store.find_gmail_attachment(
            message_id,
            attachment_id,
            attachment_filename=attachment_filename,
            attachment_sha256=attachment_sha256,
            part_id=part_id,
            owner_user_id=owner_user_id,
        )

    def export_matters_backup(self, owner_user_id: str = "") -> dict[str, Any]:
        return matter_store.export_matters_backup(owner_user_id=owner_user_id)


class InMemoryMatterRepository:
    """Dict-backed adapter for tests. No DATA_DIR, no files, isolated per instance."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._matters: list[dict[str, Any]] = []
        self._documents: dict[str, bytes] = {}

    # --- reads ---------------------------------------------------------
    def list_matters(self, owner_user_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            matters = [
                copy.deepcopy(matter)
                for matter in self._matters
                if _matter_owner_matches(matter, owner_user_id)
            ]
        return sorted(matters, key=lambda matter: str(matter.get("created_at") or ""), reverse=True)

    def get_matter(self, matter_id: str, owner_user_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            for matter in self._matters:
                if matter.get("id") == matter_id and _matter_owner_matches(matter, owner_user_id):
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

        with self._lock:
            if dedupe_gmail:
                existing_matter = _find_gmail_duplicate_unlocked(
                    self._matters,
                    metadata,
                    owner_user_id=owner_user_id,
                )
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
    def update_matter_stage(self, matter_id: str, board_column: str, owner_user_id: str = "") -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
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

    def update_matter_fields(self, matter_id: str, fields: dict[str, Any], owner_user_id: str = "") -> dict[str, Any] | None:
        cleaned_fields = {
            key: value for key, value in fields.items() if key in matter_store.MATTER_UPDATE_FIELDS
        }
        if not cleaned_fields:
            return self.get_matter(matter_id, owner_user_id=owner_user_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
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
        self, matter_id: str, redline_draft: dict[str, Any] | None, owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
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
        self, matter_id: str, review_result: dict[str, Any], triage: dict[str, Any], owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
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

    def set_clause_reviewer_decision(
        self,
        matter_id: str,
        clause_id: str,
        reviewer_decision: dict[str, Any] | None,
        owner_user_id: str = "",
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
                    continue
                decisions = dict(matter.get("reviewer_decisions") or {})
                if reviewer_decision is None:
                    decisions.pop(clause_id, None)
                else:
                    decisions[clause_id] = copy.deepcopy(reviewer_decision)
                updated_matter = {
                    **matter,
                    "reviewer_decisions": decisions,
                    "updated_at": now,
                }
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def record_matter_approval(
        self,
        matter_id: str,
        *,
        approver: str,
        approved_at: str,
        timeline_event: dict[str, Any],
        owner_user_id: str = "",
    ) -> dict[str, Any] | None:
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
                    continue
                timeline = list(matter.get("matter_timeline") or [])
                timeline.append(copy.deepcopy(timeline_event))
                updated_matter = {
                    **matter,
                    "status": "approved",
                    "approver": approver,
                    "approved_at": approved_at,
                    "matter_timeline": timeline,
                    "updated_at": approved_at,
                }
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def append_timeline_event(
        self, matter_id: str, timeline_event: dict[str, Any], owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        if not isinstance(timeline_event, dict):
            return self.get_matter(matter_id, owner_user_id=owner_user_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
                    continue
                timeline = list(matter.get("matter_timeline") or [])
                timeline.append(copy.deepcopy(timeline_event))
                updated_matter = {
                    **matter,
                    "matter_timeline": timeline,
                    "updated_at": now,
                }
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def set_workflow_error(
        self, matter_id: str, workflow_error: dict[str, Any] | None, owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
                    continue
                updated_matter = {
                    **matter,
                    "updated_at": now,
                }
                if workflow_error is None:
                    updated_matter.pop("workflow_error", None)
                else:
                    updated_matter["workflow_error"] = copy.deepcopy(workflow_error)
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def update_matter_ai_first_review(
        self, matter_id: str, ai_first_review_result: dict[str, Any], metadata: dict[str, Any], owner_user_id: str = ""
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
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

    def update_matter_artifacts(
        self,
        matter_id: str,
        artifacts: list[dict[str, Any]],
        current_artifact_id: str = "",
        owner_user_id: str = "",
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for index, matter in enumerate(self._matters):
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id):
                    continue
                updated_matter = {
                    **matter,
                    "artifacts": copy.deepcopy(list(artifacts)),
                    "current_artifact_id": str(current_artifact_id or ""),
                    "updated_at": now,
                }
                self._matters[index] = updated_matter
                return copy.deepcopy(updated_matter)
        return None

    def put_artifact_document(self, stored_filename: str, document_bytes: bytes) -> str:
        safe_name = str(stored_filename or "") or f"artifact_{uuid.uuid4().hex[:12]}.docx"
        with self._lock:
            self._documents[safe_name] = document_bytes
        return safe_name

    def get_artifact_document(self, stored_filename: str) -> bytes | None:
        safe_name = str(stored_filename or "")
        if not safe_name:
            return None
        with self._lock:
            return self._documents.get(safe_name)

    # --- delete / reset / dedupe --------------------------------------
    def delete_matter(self, matter_id: str, owner_user_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            deleted_matter = next(
                (
                    matter
                    for matter in self._matters
                    if matter.get("id") == matter_id and _matter_owner_matches(matter, owner_user_id)
                ),
                None,
            )
            if deleted_matter is None:
                return None
            self._matters = [
                matter
                for matter in self._matters
                if matter.get("id") != matter_id or not _matter_owner_matches(matter, owner_user_id)
            ]
            self._documents.pop(str(deleted_matter.get("stored_filename") or ""), None)
            return copy.deepcopy(deleted_matter)

    def reset_demo_repository(self, owner_user_id: str = "") -> int:
        with self._lock:
            removed = [
                matter
                for matter in self._matters
                if _matter_owner_matches(matter, owner_user_id)
            ]
            kept = [
                matter
                for matter in self._matters
                if not _matter_owner_matches(matter, owner_user_id)
            ]
            self._matters = kept
            for matter in removed:
                self._documents.pop(str(matter.get("stored_filename") or ""), None)
            count = len(removed)
        return count

    def deduplicate_gmail_matters(self, owner_user_id: str = "") -> int:
        with self._lock:
            matters = self._matters
            removal_ids = _gmail_duplicate_removal_ids(matters, owner_user_id=owner_user_id)

            removed: list[dict[str, Any]] = []
            kept: list[dict[str, Any]] = []
            for matter in matters:
                if id(matter) in removal_ids:
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
        owner_user_id: str = "",
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
                    "owner_user_id": _clean_owner_user_id(owner_user_id),
                },
                owner_user_id=owner_user_id,
            )
        return copy.deepcopy(match) if match is not None else None

    def export_matters_backup(self, owner_user_id: str = "") -> dict[str, Any]:
        with self._lock:
            matters = [
                copy.deepcopy(matter)
                for matter in self._matters
                if _matter_owner_matches(matter, owner_user_id)
            ]
            documents = []
            for matter in matters:
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
