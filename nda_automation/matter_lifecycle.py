"""Small matter lifecycle operations used by route handlers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import approval, matter_store, matter_view, workflow
from .checker import (
    EvidenceProvenanceError,
    ParagraphAlignmentError,
    PlaybookTemplateError,
)
from .docx_text import DocxExtractionError, extract_docx_paragraphs
from .matter_repository import DiskMatterRepository, MatterRepository
from .review_document import STRUCTURAL_METADATA_KEYS, align_document_paragraphs
from .review_engine import ActiveReviewEngineError, review_nda_with_active_engine
from .review_staleness import review_result_is_stale
from .triage import triage_review_result

MatterLifecycleError = matter_store.MatterStoreError


@dataclass(frozen=True)
class MatterApprovalResult:
    matter: dict[str, Any] | None
    blocks: list[str]
    approved_at: str = ""
    approver: str = ""
    timeline_event: dict[str, Any] | None = None


@dataclass(frozen=True)
class MatterDeletionResult:
    matter: dict[str, Any] | None
    source_bytes: bytes | None = None
    source_filename: str = ""


def repository_for_handler(handler: object) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    if isinstance(repository, MatterRepository):
        return repository
    return DiskMatterRepository()


def list_matters(repository: MatterRepository, *, owner_user_id: str = "") -> list[dict[str, Any]]:
    return repository.list_matters(owner_user_id=owner_user_id)


def get_matter(
    repository: MatterRepository,
    matter_id: str,
    *,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    return repository.get_matter(matter_id, owner_user_id=owner_user_id)


def source_document_path(matter: dict[str, Any]) -> Path | None:
    return matter_store.source_document_path(matter)


def get_source_document_bytes(
    repository: MatterRepository,
    matter: dict[str, Any],
) -> bytes | None:
    return repository.get_source_document_bytes(matter)


def with_restored_paragraph_structure(
    repository: MatterRepository,
    matter: dict[str, Any],
) -> dict[str, Any]:
    merged = restored_review_result_paragraphs(repository, matter)
    if merged is None:
        return matter
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return matter
    return {**matter, "review_result": {**review_result, "paragraphs": merged}}


def restored_review_result_paragraphs(
    repository: MatterRepository,
    matter: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Re-attach source DOCX structure to legacy flat review paragraphs."""
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return None
    paragraphs = review_result.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        return None
    if any(
        isinstance(paragraph, dict) and (paragraph.get("numbering") or paragraph.get("structure_label"))
        for paragraph in paragraphs
    ):
        return None
    rich = original_docx_paragraphs(repository, matter)
    if rich is None:
        return None
    try:
        source_text = "\n\n".join(str(paragraph.get("text", "")) for paragraph in rich)
        aligned = align_document_paragraphs(rich, source_text)
    except (ParagraphAlignmentError, ValueError, OSError):
        return None
    if len(aligned) != len(paragraphs):
        return None
    merged: list[dict[str, Any]] = []
    for stored, fresh in zip(paragraphs, aligned):
        if not isinstance(stored, dict):
            return None
        if str(stored.get("text", "")).strip() != str(fresh.get("text", "")).strip():
            return None
        restored = dict(stored)
        for key in STRUCTURAL_METADATA_KEYS:
            if key in fresh and key not in restored:
                restored[key] = fresh[key]
        merged.append(restored)
    return merged


def original_docx_paragraphs(
    repository: MatterRepository,
    matter: dict[str, Any],
) -> list[dict[str, Any]] | None:
    source_path = source_document_path(matter)
    if source_path is None or source_path.suffix.casefold() != ".docx":
        return None
    source_bytes = get_source_document_bytes(repository, matter)
    if not source_bytes:
        return None
    try:
        rich = extract_docx_paragraphs(source_bytes)
    except (DocxExtractionError, ValueError, OSError):
        return None
    return rich or None


def refresh_stale_matter_review(
    repository: MatterRepository,
    matter: dict[str, Any],
) -> dict[str, Any]:
    if not review_result_is_stale(matter.get("review_result")):
        return matter
    extracted_text = str(matter.get("extracted_text") or "")
    if not extracted_text.strip():
        return matter
    paragraphs = original_docx_paragraphs(repository, matter)
    try:
        review_result = review_nda_with_active_engine(extracted_text, paragraphs=paragraphs)
    except ParagraphAlignmentError:
        if paragraphs is None:
            return matter
        try:
            review_result = review_nda_with_active_engine(extracted_text)
        except (
            ActiveReviewEngineError,
            EvidenceProvenanceError,
            ParagraphAlignmentError,
            PlaybookTemplateError,
            ValueError,
        ):
            return matter
    except (ActiveReviewEngineError, EvidenceProvenanceError, PlaybookTemplateError, ValueError):
        return matter
    triage = triage_review_result(review_result)
    updated_matter = repository.update_matter_review(
        str(matter.get("id") or ""),
        review_result,
        triage,
        owner_user_id=str(matter.get("owner_user_id") or ""),
    )
    if updated_matter is not None:
        return updated_matter
    refreshed_matter = {
        **matter,
        "review_result": review_result,
        **triage,
        "human_reviewed": False,
    }
    refreshed_matter.pop("redline_draft", None)
    return refreshed_matter


def record_clause_decision(
    repository: MatterRepository,
    matter_id: str,
    clause_id: str,
    reviewer_decision: dict[str, Any],
    *,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    return repository.set_clause_reviewer_decision(
        matter_id,
        clause_id,
        reviewer_decision,
        owner_user_id=owner_user_id,
    )


def update_matter_stage(
    repository: MatterRepository,
    matter_id: str,
    board_column: str,
    *,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    return repository.update_matter_stage(matter_id, board_column, owner_user_id=owner_user_id)


def set_matter_human_reviewed(
    repository: MatterRepository,
    matter_id: str,
    reviewed: bool,
    *,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    return repository.update_matter_fields(matter_id, {"human_reviewed": reviewed}, owner_user_id=owner_user_id)


def update_matter_ai_first_review(
    repository: MatterRepository,
    matter_id: str,
    ai_first_review_result: dict[str, Any],
    metadata: dict[str, Any],
    *,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    return repository.update_matter_ai_first_review(
        matter_id,
        ai_first_review_result,
        metadata,
        owner_user_id=owner_user_id,
    )


def update_redline_draft(
    repository: MatterRepository,
    matter_id: str,
    draft: dict[str, Any] | None,
    *,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    return repository.update_redline_draft(matter_id, draft, owner_user_id=owner_user_id)


def update_matter_drive_sync(
    repository: MatterRepository,
    matter_id: str,
    drive_block: dict[str, Any],
    *,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    return repository.update_matter_fields(
        matter_id,
        {"drive": drive_block},
        owner_user_id=owner_user_id,
    )


def create_sent_document_matter(
    repository: MatterRepository,
    *,
    filename: str,
    document_bytes: bytes,
    recipient: str,
    subject: str,
    sent: dict[str, Any],
    owner_user_id: str = "",
) -> dict[str, Any]:
    matter = repository.create_matter(
        source_filename=filename,
        document_bytes=document_bytes,
        extracted_text="",
        review_result={},
        triage=_send_document_triage(),
        source_type="send_document",
        board_column="sent",
        intake_metadata=_send_document_metadata(filename, recipient, subject),
        owner_user_id=owner_user_id,
    )
    updated_matter = stamp_matter_sent(
        repository,
        str(matter.get("id") or ""),
        sent,
        filename=filename,
        append_timeline=False,
        owner_user_id=owner_user_id,
    )
    return updated_matter or matter


def stamp_matter_sent(
    repository: MatterRepository,
    matter_id: str,
    sent: dict[str, Any],
    *,
    filename: str,
    append_timeline: bool = True,
    owner_user_id: str = "",
) -> dict[str, Any] | None:
    updated_matter = repository.update_matter_fields(
        matter_id,
        {
            "board_column": "sent",
            "last_outbound_account": sent.get("outbound_account", ""),
            "last_outbound_at": sent.get("sent_at", ""),
            "last_outbound_filename": filename,
            "last_outbound_message_id": sent.get("message_id", ""),
            "last_outbound_subject": sent.get("subject", ""),
            "last_outbound_thread_id": sent.get("thread_id", ""),
            "last_outbound_to": sent.get("to", ""),
            "status": "active",
        },
        owner_user_id=owner_user_id,
    )
    if updated_matter is not None and append_timeline:
        append_sent_timeline_event(repository, updated_matter, sent, owner_user_id=owner_user_id)
    return updated_matter


def append_sent_timeline_event(
    repository: MatterRepository,
    matter: dict[str, Any],
    sent: dict[str, Any],
    *,
    owner_user_id: str = "",
) -> None:
    matter_id = str(matter.get("id") or "")
    if not matter_id:
        return
    try:
        repository.append_timeline_event(
            matter_id,
            workflow.build_timeline_event(
                workflow.EVENT_SENT,
                phase=workflow.PHASE_SENT,
                status=workflow.STATUS_SENT_AWAITING_COUNTERPARTY,
                actor=str(sent.get("outbound_account") or "system"),
                detail=str(sent.get("to") or ""),
            ),
            owner_user_id=owner_user_id,
        )
    except Exception:
        return


def matter_blocks_redline_send(matter: dict[str, Any]) -> bool:
    return matter_view.matter_needs_human_review(matter) and not matter.get("human_reviewed")


def delete_matter(
    repository: MatterRepository,
    matter_id: str,
    *,
    owner_user_id: str = "",
) -> MatterDeletionResult:
    pre_delete = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    source_bytes = repository.get_source_document_bytes(pre_delete) if pre_delete else None
    source_filename = str(pre_delete.get("source_filename") or "") if pre_delete else ""
    matter = repository.delete_matter(matter_id, owner_user_id=owner_user_id)
    return MatterDeletionResult(matter=matter, source_bytes=source_bytes, source_filename=source_filename)


def reset_demo_repository(repository: MatterRepository, *, owner_user_id: str = "") -> int:
    return repository.reset_demo_repository(owner_user_id=owner_user_id)


def deduplicate_gmail_matters(repository: MatterRepository, *, owner_user_id: str = "") -> int:
    return repository.deduplicate_gmail_matters(owner_user_id=owner_user_id)


def approve_matter(
    repository: MatterRepository,
    matter_id: str,
    *,
    actor: str,
    owner_user_id: str = "",
) -> MatterApprovalResult:
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        return MatterApprovalResult(matter=None, blocks=[])

    blocks = approval.approval_blocks(matter)
    if blocks:
        return MatterApprovalResult(matter=matter, blocks=blocks)

    approved_at = datetime.now(timezone.utc).isoformat()
    timeline_event = approval.approval_timeline_event(actor=actor)
    updated_matter = repository.record_matter_approval(
        matter_id,
        approver=actor,
        approved_at=approved_at,
        timeline_event=timeline_event,
        owner_user_id=owner_user_id,
    )
    return MatterApprovalResult(
        matter=updated_matter,
        blocks=[],
        approved_at=approved_at,
        approver=actor,
        timeline_event=timeline_event,
    )


def _send_document_metadata(filename: str, recipient: str, subject: str) -> dict[str, str]:
    return {
        "sender": recipient,
        "reply_to": recipient,
        "subject": subject,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "message_snippet": f"Sent {Path(str(filename or '')).name or 'document'} to {recipient}.",
        "attachment_filename": filename,
    }


def _send_document_triage() -> dict[str, Any]:
    return {
        "triage_status": "sent",
        "next_action": "Document sent",
        "issue_count": 0,
        "requirements_passed": 0,
        "requirements_needs_review": 0,
        "requirements_failed": 0,
    }
