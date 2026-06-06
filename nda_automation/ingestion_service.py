from __future__ import annotations

from typing import Any

from . import artifact_service, workflow
from .artifact_registry import ArtifactRegistryError
from .checker import ParagraphAlignmentError
from .document_limits import ensure_document_size
from .docx_text import DocxExtractionError, extract_docx_paragraphs
from .matter_repository import DiskMatterRepository, MatterRepository
from .pdf_text import PdfExtractionError, extract_pdf_document
from .review_engine import review_nda_with_active_engine
from .triage import triage_review_result


SUPPORTED_DOCUMENT_EXTENSIONS = {".docx", ".pdf"}


def create_matter_from_document(
    *,
    filename: str,
    document_bytes: bytes,
    source_type: str = "gmail_demo",
    board_column: str = "gmail_demo",
    intake_metadata: dict[str, Any] | None = None,
    dedupe_gmail: bool = False,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
    defer_ai_review: bool = False,
) -> dict[str, Any]:
    repository = repository or DiskMatterRepository()
    ensure_document_size(document_bytes)
    document_type, extracted_paragraphs, extraction_quality = extract_document(filename, document_bytes)
    extracted_text = "\n\n".join(str(paragraph["text"]) for paragraph in extracted_paragraphs)
    # Outbound NDA generation defers the slow AI review: it runs the fast
    # deterministic review at creation (so the matter is valid + sendable
    # immediately) and leaves the AI review for on-demand (Refresh Review). Inbound
    # intake (gmail / manual upload) leaves this off and keeps the active engine.
    review_result = review_nda_with_active_engine(
        extracted_text,
        paragraphs=extracted_paragraphs,
        force_engine="deterministic" if defer_ai_review else None,
    )
    review_result["source"] = {
        "filename": filename,
        "type": document_type,
        "extracted_characters": len(extracted_text),
        "extracted_paragraphs": len(extracted_paragraphs),
    }
    if extraction_quality:
        review_result["source"]["extraction_quality"] = extraction_quality
        _append_extraction_warnings(review_result, extraction_quality)
    review_result["extracted_text"] = extracted_text
    matter = repository.create_matter(
        source_filename=filename,
        document_bytes=document_bytes,
        extracted_text=extracted_text,
        review_result=review_result,
        triage=triage_review_result(review_result),
        source_type=source_type,
        board_column=board_column,
        intake_metadata=intake_metadata,
        dedupe_gmail=dedupe_gmail,
        owner_user_id=owner_user_id,
    )
    # Two best-effort, dedupe-aware post-create hooks. Artifact registration
    # runs first so the original artifact exists before the timeline (which may
    # later reference the current artifact); both are fail-soft so neither can
    # break an otherwise-successful intake.
    _register_original_artifact(matter, repository=repository, owner_user_id=owner_user_id)
    _record_intake_timeline(repository, matter, owner_user_id=owner_user_id)
    return matter


def _register_original_artifact(
    matter: dict[str, Any],
    *,
    repository: MatterRepository,
    owner_user_id: str = "",
) -> None:
    """Auto-register the matter's source document as its original artifact.

    Runs for every NEWLY created matter so the artifact layer is real from
    intake, not only after a one-time backfill. Skipped when ``create_matter``
    returned an existing gmail duplicate (the original is already registered, or
    will be by backfill) so we never double-register. Fail-soft: the matter is
    the source of truth, so a registry hiccup must never break intake — backfill
    can re-register later (it is idempotent).
    """
    if matter.get("_existing_gmail_duplicate"):
        return
    try:
        artifact_service.backfill_matter(matter, repository=repository, owner_user_id=owner_user_id)
    except (ArtifactRegistryError, OSError, KeyError, ValueError):
        # Never let artifact registration fail an otherwise-successful intake.
        pass


def _record_intake_timeline(
    repository: MatterRepository,
    matter: dict[str, Any],
    *,
    owner_user_id: str = "",
) -> None:
    """Stamp the Intake->Review transition on the timeline backbone.

    Intake review runs synchronously inside create, so a freshly created matter
    already carries its review_result. We append a ``created`` event and the
    review outcome (``review_completed`` when auto-cleared, ``flagged_for_human``
    when the review blocks send) so the lifecycle log starts from arrival. A
    de-duped gmail re-import returns the existing matter unchanged -- skip it so
    we don't double-log. Timeline stamping is best-effort: it must never fail the
    intake (the matter is already persisted), so errors are swallowed.
    """
    if not isinstance(matter, dict) or matter.get("_existing_gmail_duplicate"):
        return
    matter_id = str(matter.get("id") or "")
    if not matter_id:
        return
    state = workflow.workflow_state(matter)
    try:
        repository.append_timeline_event(
            matter_id,
            workflow.build_timeline_event(
                workflow.EVENT_CREATED,
                phase=workflow.PHASE_INTAKE,
                status=workflow.STATUS_EXTRACTED,
                actor="system",
                detail=str(matter.get("source_type") or ""),
            ),
            owner_user_id=owner_user_id,
        )
        if state["status"] == workflow.STATUS_AWAITING_HUMAN:
            event_type = workflow.EVENT_FLAGGED_FOR_HUMAN
        else:
            event_type = workflow.EVENT_REVIEW_COMPLETED
        repository.append_timeline_event(
            matter_id,
            workflow.build_timeline_event(
                event_type,
                phase=state["phase"],
                status=state["status"],
                actor="system",
            ),
            owner_user_id=owner_user_id,
        )
    except Exception:
        # Never let timeline stamping break intake; the matter is already saved.
        return


def extract_document_paragraphs(filename: str, document_bytes: bytes) -> tuple[str, list[dict[str, Any]]]:
    document_type, paragraphs, _quality = extract_document(filename, document_bytes)
    return document_type, paragraphs


def extract_document(filename: str, document_bytes: bytes) -> tuple[str, list[dict[str, Any]], dict[str, object] | None]:
    lower_filename = filename.lower()
    if lower_filename.endswith(".docx"):
        return "docx", extract_docx_paragraphs(document_bytes), None
    if lower_filename.endswith(".pdf"):
        extraction = extract_pdf_document(document_bytes)
        return "pdf", extraction.paragraphs, extraction.quality
    raise ValueError("Upload a .docx Word document or text-based PDF.")


def is_supported_document_filename(filename: object) -> bool:
    if not isinstance(filename, str):
        return False
    return any(filename.lower().endswith(extension) for extension in SUPPORTED_DOCUMENT_EXTENSIONS)


def _append_extraction_warnings(review_result: dict[str, Any], extraction_quality: dict[str, object]) -> None:
    warnings = extraction_quality.get("warnings")
    if not isinstance(warnings, list) or not warnings:
        return
    review_warnings = review_result.setdefault("review_warnings", [])
    if isinstance(review_warnings, list):
        review_warnings.extend(warnings)


__all__ = [
    "DocxExtractionError",
    "ParagraphAlignmentError",
    "PdfExtractionError",
    "create_matter_from_document",
    "extract_document",
    "extract_document_paragraphs",
    "is_supported_document_filename",
]
