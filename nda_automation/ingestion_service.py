from __future__ import annotations

import logging
import os
import threading
from typing import Any

from .checker import ParagraphAlignmentError
from .document_limits import ensure_document_size
from .docx_text import DocxExtractionError, detect_docx_tracked_changes, extract_docx_paragraphs
from .matter_lifecycle import BackgroundRunner, RepositoryMatterLifecycle, run_in_daemon_thread
from .matter_repository import DiskMatterRepository, MatterRepository
from .pdf_text import PdfExtractionError, extract_pdf_document
from .review_engine import PlaybookRuntimeFn, ReviewEngineFn, review_nda_with_active_engine
from .review_result_contract import (
    attach_document_source,
    extracted_text_from_paragraphs,
    review_result_paragraphs,
)
from .triage import triage_review_result


LOGGER = logging.getLogger(__name__)

SUPPORTED_DOCUMENT_EXTENSIONS = {".docx", ".pdf"}

# --------------------------------------------------------------------------- #
# Inbound auto-review (async + serialized)
# --------------------------------------------------------------------------- #
# Inbound Gmail NDAs import FAST: create_matter_from_document runs only the
# offline deterministic first-pass (defer_ai_review=True) so the single poll
# thread never blocks on the slow Opus/Pro AI review. To restore the core
# feature (inbound NDAs auto-reviewed by AI) WITHOUT the storm that motivated
# bb62b8f, the full active-engine review (assessor + verifier) is scheduled to
# run AFTER import, OFF the poll thread, and SERIALIZED behind a process-wide
# semaphore so AT MOST N inbound reviews run at once (default 1). A batch of new
# NDAs therefore reviews sequentially in the background -- never N-at-once on the
# worker, never blocking generation/requests, and never re-storming the inbox.
INBOUND_REVIEW_CONCURRENCY_ENV = "NDA_INBOUND_REVIEW_CONCURRENCY"
_DEFAULT_INBOUND_REVIEW_CONCURRENCY = 1


def inbound_review_concurrency() -> int:
    """How many inbound auto-reviews may run concurrently (env-configurable).

    Defaults to 1 -- strict serialization, the structural anti-storm guarantee.
    A larger value (e.g. ``NDA_INBOUND_REVIEW_CONCURRENCY=2``) is allowed if a
    bigger worker can absorb it; anything below 1 (or unparseable) clamps to 1 so
    the semaphore is always a valid bound.
    """

    raw = os.environ.get(INBOUND_REVIEW_CONCURRENCY_ENV, "").strip()
    if not raw:
        return _DEFAULT_INBOUND_REVIEW_CONCURRENCY
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_INBOUND_REVIEW_CONCURRENCY
    return max(1, value)


# One process-wide semaphore gates EVERY inbound async review. It is created once
# at import for the configured limit; tests that need a different bound inject a
# runner or override the semaphore. The BoundedSemaphore is the single structural
# fact that prevents an N-at-once storm when a poll imports a batch of new NDAs.
_INBOUND_REVIEW_SEMAPHORE = threading.BoundedSemaphore(inbound_review_concurrency())


def _matter_already_ai_reviewed(matter: dict[str, Any]) -> bool:
    """True when the matter already carries a full AI (ai_first) review result.

    The inbound first-pass stamps ``active_review_engine.executed_engine =
    "deterministic"``; a completed async review overwrites ``review_result`` with
    the ai_first engine output (``executed_engine = "ai_first"``). Checking the
    executed engine makes the async review idempotent: a matter already reviewed
    by the AI (in a prior poll, by on-demand Refresh, or before a worker restart
    finished a different matter) is never re-reviewed.
    """

    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return False
    engine = review_result.get("active_review_engine")
    if not isinstance(engine, dict):
        return False
    return str(engine.get("executed_engine") or "") == "ai_first"


def _perform_inbound_ai_review(
    matter_id: str,
    *,
    repository: MatterRepository,
    owner_user_id: str,
    review_engine_func: ReviewEngineFn,
) -> None:
    """Run the full active-engine review for one inbound matter and persist it.

    Acquires the process-wide serialization semaphore for the WHOLE review so at
    most ``inbound_review_concurrency()`` inbound reviews execute at once. Re-reads
    the matter fresh from durable storage inside the critical section so a matter
    already reviewed (idempotency) is skipped, and so a worker that restarted
    mid-batch resumes only the not-yet-reviewed matters. Fail-soft: any error is
    logged and swallowed -- a failed background review must never crash the worker
    or wedge the poll; the matter keeps its deterministic first-pass and stays
    reviewable on-demand.
    """

    from . import telemetry

    with _INBOUND_REVIEW_SEMAPHORE:
        try:
            matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
            if not isinstance(matter, dict):
                return
            if _matter_already_ai_reviewed(matter):
                telemetry.increment("inbound_ai_review_skipped_already_reviewed")
                return
            extracted_text = str(matter.get("extracted_text") or "")
            if not extracted_text.strip():
                return
            paragraphs = review_result_paragraphs(matter.get("review_result"))
            review_result = review_engine_func(extracted_text, paragraphs=paragraphs)
        except ParagraphAlignmentError:
            # Stored paragraph offsets did not align; retry text-only so a tracked
            # -changes / reconstructed source still gets its AI review.
            try:
                review_result = review_engine_func(extracted_text)
            except Exception:
                telemetry.increment("inbound_ai_review_failed")
                LOGGER.warning("Inbound AI review failed for matter %s", matter_id, exc_info=True)
                return
        except Exception:
            telemetry.increment("inbound_ai_review_failed")
            LOGGER.warning("Inbound AI review failed for matter %s", matter_id, exc_info=True)
            return

        try:
            updated = repository.update_matter_review(
                matter_id,
                review_result,
                triage_review_result(review_result),
                owner_user_id=owner_user_id,
            )
        except Exception:
            telemetry.increment("inbound_ai_review_failed")
            LOGGER.warning("Inbound AI review persist failed for matter %s", matter_id, exc_info=True)
            return
        if updated is None:
            return
        telemetry.increment("inbound_ai_review_completed")


def schedule_inbound_ai_review(
    matter: dict[str, Any] | None,
    *,
    repository: MatterRepository | None = None,
    owner_user_id: str = "",
    runner: BackgroundRunner = run_in_daemon_thread,
    review_engine_func: ReviewEngineFn | None = None,
) -> bool:
    """Schedule the full ai_first review of a just-imported inbound matter.

    Runs OFF the caller's (poll) thread via ``runner`` and SERIALIZED behind the
    module semaphore. Returns True when work was scheduled, False when there is
    nothing to do (no matter id, a gmail duplicate, or already AI-reviewed). The
    scheduling itself is best-effort: a runner that raises is logged and swallowed
    so a background-review failure can never break the import that triggered it.
    """

    if not isinstance(matter, dict) or matter.get("_existing_gmail_duplicate"):
        return False
    matter_id = str(matter.get("id") or "")
    if not matter_id:
        return False
    if _matter_already_ai_reviewed(matter):
        return False

    repo = repository or DiskMatterRepository()
    engine = review_engine_func or review_nda_with_active_engine

    def _work() -> None:
        _perform_inbound_ai_review(
            matter_id,
            repository=repo,
            owner_user_id=str(owner_user_id or ""),
            review_engine_func=engine,
        )

    try:
        runner(_work)
    except Exception:
        from . import telemetry

        telemetry.increment("inbound_ai_review_schedule_failed")
        LOGGER.warning("Failed to schedule inbound AI review for matter %s", matter_id, exc_info=True)
        return False
    return True


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
    drive_sync_runner: BackgroundRunner = run_in_daemon_thread,
    playbook_runtime_func: PlaybookRuntimeFn | None = None,
) -> dict[str, Any]:
    repository = repository or DiskMatterRepository()
    ensure_document_size(document_bytes)
    document_type, extracted_paragraphs, extraction_quality = extract_document(filename, document_bytes)
    extracted_text = extracted_text_from_paragraphs(extracted_paragraphs)
    # Outbound NDA generation defers the slow AI review: it runs the fast
    # deterministic review at creation (so the matter is valid + sendable
    # immediately) and leaves the AI review for on-demand (Refresh Review). Inbound
    # intake (gmail / manual upload) leaves this off and keeps the active engine.
    review_result = review_nda_with_active_engine(
        extracted_text,
        paragraphs=extracted_paragraphs,
        force_engine="deterministic" if defer_ai_review else None,
        **({"playbook_runtime_func": playbook_runtime_func} if playbook_runtime_func is not None else {}),
    )
    attach_document_source(
        review_result,
        filename=filename,
        document_type=document_type,
        extracted_paragraphs=extracted_paragraphs,
        extracted_text=extracted_text,
        extraction_quality=extraction_quality,
    )
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
    RepositoryMatterLifecycle(repository).complete_intake(
        matter,
        owner_user_id=owner_user_id,
        drive_sync_runner=drive_sync_runner,
    )
    return matter


def extract_document_paragraphs(filename: str, document_bytes: bytes) -> tuple[str, list[dict[str, Any]]]:
    document_type, paragraphs, _quality = extract_document(filename, document_bytes)
    return document_type, paragraphs


def extract_document(filename: str, document_bytes: bytes) -> tuple[str, list[dict[str, Any]], dict[str, object] | None]:
    lower_filename = filename.lower()
    if lower_filename.endswith(".docx"):
        paragraphs = extract_docx_paragraphs(document_bytes)
        # Surface unresolved tracked changes as an extraction-quality warning so
        # the review never silently acts on a synthesized redline state. The flat
        # text now reflects the in-force baseline (see docx_text), but the matter
        # must still be flagged + gated for human resolution of the redlines.
        tracked_changes = detect_docx_tracked_changes(document_bytes)
        return "docx", paragraphs, tracked_changes
    if lower_filename.endswith(".pdf"):
        extraction = extract_pdf_document(document_bytes)
        return "pdf", extraction.paragraphs, extraction.quality
    raise ValueError("Upload a .docx Word document or text-based PDF.")


def is_supported_document_filename(filename: object) -> bool:
    if not isinstance(filename, str):
        return False
    return any(filename.lower().endswith(extension) for extension in SUPPORTED_DOCUMENT_EXTENSIONS)


__all__ = [
    "DocxExtractionError",
    "ParagraphAlignmentError",
    "PdfExtractionError",
    "create_matter_from_document",
    "extract_document",
    "extract_document_paragraphs",
    "inbound_review_concurrency",
    "is_supported_document_filename",
    "schedule_inbound_ai_review",
]
