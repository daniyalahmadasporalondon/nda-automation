from __future__ import annotations

import base64
import binascii
from pathlib import Path

from .. import document_rendering, gmail_integration, matter_render_job, matter_store, matter_summary, matter_view, telemetry
from ..ai_assessor import AIAssessorError
from ..checker import EvidenceProvenanceError, ParagraphAlignmentError, PlaybookTemplateError
from ..document_limits import DocumentSizeError, DOCUMENT_TOO_LARGE_MESSAGE, ensure_document_size
from ..docx_text import DocxExtractionError, extract_docx_paragraphs
from ..http_auth import _env_flag_enabled
from ..ingestion_service import create_matter_from_document, is_supported_document_filename
from ..matter_lifecycle import (
    MatterNotFoundError,
    MatterReviewUnavailableError,
    RedlineDraftError,
    RepositoryMatterLifecycle,
    ai_first_review_store_metadata as lifecycle_ai_first_review_store_metadata,
    clean_bool_map as lifecycle_clean_bool_map,
    clean_dict_list as lifecycle_clean_dict_list,
    clean_redline_draft as lifecycle_clean_redline_draft,
    clean_text_map as lifecycle_clean_text_map,
)
from ..matter_repository import DiskMatterRepository
from ..pdf_text import PdfExtractionError
from ..review_document import STRUCTURAL_METADATA_KEYS, align_document_paragraphs
from ..review_engine import ActiveReviewEngineError, review_nda_with_active_engine
from ..review_staleness import review_result_is_stale, review_result_staleness
from .common import parse_matter_id, request_owner_user_id

AI_FIRST_REVIEW_FEATURE_FLAG = "NDA_AI_FIRST_REVIEW_ENABLED"
HTTP_MATTER_SOURCE_COLUMNS = {"manual_upload": "in_review"}
MATTER_BOARD_COLUMNS = {"gmail_demo", "in_review", "reviewed", "sent"}
MANUAL_UPLOAD_BOARD_COLUMNS = {"gmail_demo", "in_review", "reviewed", "sent"}
def handle_matter_list(handler, *, send_body: bool = True) -> None:
    try:
        handler._send_json(
            {"matters": matter_view.public_matters(matter_store.list_matters(request_owner_user_id(handler)))},
            send_body=send_body,
        )
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)


def handle_matter_review(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/review")
    matter = _matter_for_review_response(handler, matter_id, send_body=send_body)
    if matter is None:
        return
    handler._send_json(_matter_review_payload(matter, matter_id), send_body=send_body)


def handle_matter_review_refresh(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/review-refresh")
    matter = _matter_for_review_response(handler, matter_id, send_body=True)
    if matter is None:
        return
    refresh = RepositoryMatterLifecycle(DiskMatterRepository()).refresh_review(
        matter,
        review_engine_func=review_nda_with_active_engine,
        review_staleness_func=review_result_is_stale,
    )
    handler._send_json(_matter_review_payload(
        refresh.matter,
        matter_id,
        was_stale=refresh.was_stale,
        had_redline_draft=refresh.had_redline_draft,
        refresh_attempted=refresh.refresh_attempted,
    ))


def _matter_for_review_response(handler, matter_id: str | None, *, send_body: bool) -> dict | None:
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return None
    try:
        matter = matter_store.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return None
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return None
    return _with_restored_paragraph_structure(matter)


def _restored_review_result_paragraphs(matter: dict) -> list[dict] | None:
    """Best-effort: re-attach structural metadata (numbering, indentation, runs,
    font size, style) to a matter's stored review paragraphs by re-extracting its
    original .docx.

    Matters reviewed before the extractor captured contract structure stored only
    flat text paragraphs, so the review render shows no clause/sub-clause numbers.
    Re-extracting the original .docx and merging its structural fields onto the
    stored paragraphs restores that fidelity on open -- without re-running the AI
    review or disturbing clause<->paragraph references (ids/text are unchanged).

    Returns merged paragraphs, or None when restoration does not apply or cannot be
    done safely (no original .docx, not a .docx, extraction fails, or the
    re-extracted paragraphs do not line up 1:1 with the stored text).
    """
    review_result = matter.get("review_result")
    if not isinstance(review_result, dict):
        return None
    paragraphs = review_result.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        return None
    # Already structured -- nothing to restore (and avoids re-extracting every open).
    if any(
        isinstance(paragraph, dict) and (paragraph.get("numbering") or paragraph.get("structure_label"))
        for paragraph in paragraphs
    ):
        return None
    source_path = matter_store.source_document_path(matter)
    if source_path is None or source_path.suffix.casefold() != ".docx":
        return None
    source_bytes = matter_store.get_source_document_bytes(matter)
    if not source_bytes:
        return None
    try:
        rich = extract_docx_paragraphs(source_bytes)
        source_text = "\n\n".join(str(paragraph.get("text", "")) for paragraph in rich)
        aligned = align_document_paragraphs(rich, source_text)
    except (DocxExtractionError, ParagraphAlignmentError, ValueError, OSError):
        return None
    if len(aligned) != len(paragraphs):
        return None
    merged: list[dict] = []
    for stored, fresh in zip(paragraphs, aligned):
        if not isinstance(stored, dict):
            return None
        # Bail entirely on any text divergence: a partial/misaligned merge would
        # mislabel paragraphs, which is worse than showing none.
        if str(stored.get("text", "")).strip() != str(fresh.get("text", "")).strip():
            return None
        restored = dict(stored)
        for key in STRUCTURAL_METADATA_KEYS:
            if key in fresh and key not in restored:
                restored[key] = fresh[key]
        merged.append(restored)
    return merged


def _with_restored_paragraph_structure(matter: dict) -> dict:
    merged = _restored_review_result_paragraphs(matter)
    if merged is None:
        return matter
    review_result = matter.get("review_result")
    return {**matter, "review_result": {**review_result, "paragraphs": merged}}


def _original_docx_paragraphs(matter: dict) -> list[dict] | None:
    """Rich paragraphs re-extracted from the matter's original .docx, or None.

    Used to restore contract structure on a review refresh. Returns None unless the
    matter's original document is a .docx that extracts to at least one paragraph;
    the caller aligns these against the stored extracted text.
    """
    source_path = matter_store.source_document_path(matter)
    if source_path is None or source_path.suffix.casefold() != ".docx":
        return None
    source_bytes = matter_store.get_source_document_bytes(matter)
    if not source_bytes:
        return None
    try:
        rich = extract_docx_paragraphs(source_bytes)
    except (DocxExtractionError, ValueError, OSError):
        return None
    return rich or None


def _matter_review_payload(
    matter: dict,
    matter_id: str | None,
    *,
    was_stale: bool | None = None,
    had_redline_draft: bool = False,
    refresh_attempted: bool = False,
) -> dict:
    staleness = review_result_staleness(matter.get("review_result"))
    if was_stale is None:
        was_stale = bool(staleness["stale"])
    is_stale = bool(staleness["stale"])
    refreshed = bool(refresh_attempted and was_stale and not is_stale)
    redline_draft_cleared = bool(
        refreshed
        and had_redline_draft
        and not isinstance(matter.get("redline_draft"), dict)
    )
    payload = matter_view.review_matter(matter)
    payload["review_refresh"] = {
        "stale": is_stale,
        "refresh_method": "POST",
        "refresh_url": f"/api/matters/{matter_id}/review-refresh",
        "refreshed": refreshed,
        "redline_draft_cleared": redline_draft_cleared,
        "stale_reasons": staleness["stale_reasons"],
        "current_playbook": staleness["current_playbook"],
        "review_playbook": staleness["review_playbook"],
        "current_review_engine_version": staleness["current_review_engine_version"],
    }
    if is_stale and staleness.get("message"):
        payload["review_refresh"]["stale_message"] = staleness["message"]
    if redline_draft_cleared:
        payload["review_refresh"]["message"] = "Saved redline draft was cleared because the review was re-analyzed."
    return payload


def handle_matter_detail(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path)
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    try:
        matter = matter_store.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)}, send_body=send_body)


def refresh_stale_matter_review(matter: dict) -> dict:
    return RepositoryMatterLifecycle(DiskMatterRepository()).refresh_review(
        matter,
        review_engine_func=review_nda_with_active_engine,
        review_staleness_func=review_result_is_stale,
    ).matter


def handle_matter_source(handler, path: str, *, send_body: bool = True) -> None:
    """Stream a matter's stored original .docx/.pdf for faithful rendering."""
    matter_id = parse_matter_id(path, suffix="/source")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    try:
        matter = matter_store.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    source_path = matter_store.source_document_path(matter)
    if source_path is None:
        handler._send_json({"error": "No source document for this matter."}, status=404, send_body=send_body)
        return
    ext = source_path.suffix.lower()
    if ext == ".docx":
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif ext == ".pdf":
        mime = "application/pdf"
    else:
        mime = "application/octet-stream"
    handler._send_file(source_path, content_type=mime, send_body=send_body)


def handle_matter_render_status(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/render-status")
    try:
        payload = matter_render_job.render_status_payload(
            matter_id,
            owner_user_id=request_owner_user_id(handler),
        )
    except matter_render_job.MatterRenderJobError as error:
        _send_render_job_error(handler, error, send_body=send_body)
        return
    handler._send_json(payload, send_body=send_body)


def handle_matter_render_pdf(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/render-pdf")
    try:
        result = matter_render_job.render_pdf_file(matter_id, owner_user_id=request_owner_user_id(handler))
    except matter_render_job.MatterRenderJobError as error:
        _send_render_job_error(handler, error, send_body=send_body)
        return
    handler._send_file(result.path, content_type=result.content_type, send_body=send_body)


def handle_matter_render_page(handler, path: str, *, send_body: bool = True) -> None:
    parsed = matter_render_job.parse_matter_render_page_path(path)
    if parsed is None:
        handler._send_json({"error": "Page image not found."}, status=404, send_body=send_body)
        return
    matter_id, page_number = parsed
    try:
        result = matter_render_job.render_page_image_file(
            matter_id,
            page_number,
            owner_user_id=request_owner_user_id(handler),
        )
    except matter_render_job.MatterRenderJobError as error:
        _send_render_job_error(handler, error, send_body=send_body)
        return
    handler._send_file(result.path, content_type=result.content_type, send_body=send_body)


def _send_render_job_error(handler, error: matter_render_job.MatterRenderJobError, *, send_body: bool) -> None:
    if error.headers:
        handler._send_json(
            error.payload,
            status=error.status,
            headers=error.headers,
            send_body=send_body,
        )
        return
    handler._send_json(error.payload, status=error.status, send_body=send_body)


def handle_matter_upload(handler, *, create_matter_from_document_func=create_matter_from_document) -> None:
    telemetry.increment("matter_upload_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    filename = payload.get("filename", "")
    content_base64 = payload.get("content_base64", "")
    source_type = payload.get("source_type", "manual_upload")
    if not is_supported_document_filename(filename):
        handler._send_json({"error": "Upload a .docx Word document or text-based PDF."}, status=400)
        return
    if not isinstance(content_base64, str) or not content_base64:
        handler._send_json({"error": "Provide a document to import."}, status=400)
        return
    if not isinstance(source_type, str) or not source_type.strip():
        source_type = "manual_upload"
    source_type = source_type.strip()
    default_board_column = HTTP_MATTER_SOURCE_COLUMNS.get(source_type)
    if default_board_column is None:
        handler._send_json({"error": "Unsupported matter source."}, status=400)
        return
    board_column = _manual_upload_board_column(payload, default_board_column)
    if board_column is None:
        handler._send_json({"error": "Unsupported manual upload stage."}, status=400)
        return

    try:
        document_bytes = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        handler._send_json({"error": "The uploaded document could not be decoded."}, status=400)
        return

    try:
        ensure_document_size(document_bytes)
    except DocumentSizeError:
        handler._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
        return

    try:
        matter = create_matter_from_document_func(
            filename=filename,
            document_bytes=document_bytes,
            source_type=source_type,
            board_column=board_column,
            intake_metadata=matter_intake_metadata(payload, filename),
            owner_user_id=request_owner_user_id(handler),
        )
    except (DocxExtractionError, PdfExtractionError, ValueError) as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except DocumentSizeError:
        handler._send_json({"error": DOCUMENT_TOO_LARGE_MESSAGE}, status=400)
        return
    except ParagraphAlignmentError:
        handler._send_json({"error": "The extracted document paragraphs could not be aligned to the extracted text."}, status=400)
        return
    except ActiveReviewEngineError as error:
        handler._send_json({"error": str(error)}, status=502)
        return

    handler._send_json({"matter": matter_view.public_matter(matter)}, status=201)


def _manual_upload_board_column(payload: dict, default_board_column: str) -> str | None:
    requested_board_column = payload.get("board_column")
    if requested_board_column in (None, ""):
        return default_board_column
    if not isinstance(requested_board_column, str):
        return None
    requested_board_column = requested_board_column.strip()
    if requested_board_column not in MANUAL_UPLOAD_BOARD_COLUMNS:
        return None
    return requested_board_column


def matter_intake_metadata(payload: dict, filename: str) -> dict[str, str]:
    sender = clean_intake_text(payload.get("sender"))
    sender = gmail_integration.recipient_email(sender) if sender else ""
    metadata = {
        "sender": sender or "Manual upload",
        "subject": clean_intake_text(payload.get("subject")) or Path(filename).stem or "Untitled NDA",
        "received_at": clean_intake_text(payload.get("received_at")),
        "message_snippet": clean_intake_text(payload.get("message_snippet")) or f"Manual upload of {Path(filename).name or 'NDA document'}.",
        "attachment_filename": clean_intake_text(payload.get("attachment_filename")) or filename,
    }
    reply_to = clean_intake_text(payload.get("reply_to"))
    reply_to = gmail_integration.recipient_email(reply_to) if reply_to else ""
    if reply_to:
        metadata["reply_to"] = reply_to
    return metadata


def clean_intake_text(value: object, max_length: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_length]


def handle_matter_stage_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/stage")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    board_column = payload.get("board_column", "")
    if not isinstance(board_column, str) or board_column not in MATTER_BOARD_COLUMNS:
        handler._send_json({"error": "Unsupported matter stage."}, status=400)
        return

    matter = matter_store.update_matter_stage(matter_id, board_column, owner_user_id=request_owner_user_id(handler))
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)})


def handle_matter_reviewed_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/reviewed")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    reviewed = payload.get("reviewed", True)
    if not isinstance(reviewed, bool):
        handler._send_json({"error": "reviewed must be true or false."}, status=400)
        return

    matter = matter_store.update_matter_fields(
        matter_id,
        {"human_reviewed": reviewed},
        owner_user_id=request_owner_user_id(handler),
    )
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)})


def handle_matter_ai_first_review(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/ai-first-review")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    if not _env_flag_enabled(AI_FIRST_REVIEW_FEATURE_FLAG):
        handler._send_json(
            {
                "error": (
                    "AI-first matter review is disabled. "
                    f"Set {AI_FIRST_REVIEW_FEATURE_FLAG}=true to run it."
                )
            },
            status=403,
        )
        return

    try:
        ai_first_review = RepositoryMatterLifecycle(DiskMatterRepository()).run_ai_first_review(
            matter_id,
            owner_user_id=request_owner_user_id(handler),
        )
    except AIAssessorError as error:
        handler._send_json({"error": str(error)}, status=502)
        return
    except MatterNotFoundError:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    except MatterReviewUnavailableError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except (EvidenceProvenanceError, ParagraphAlignmentError, PlaybookTemplateError, ValueError) as error:
        handler._send_json({"error": f"AI-first review could not be completed: {error}"}, status=500)
        return

    handler._send_json({
        "matter": matter_view.public_matter(ai_first_review.matter),
        "ai_first_review_metadata": ai_first_review.matter.get("ai_first_review_metadata"),
        "ai_first_review_result": ai_first_review.review_result,
    })


def handle_matter_summary(handler, path: str) -> None:
    """POST /api/matters/<id>/summary -- on-demand AI summary of one matter.

    Mirrors the other matter routes' auth/ownership shape: it runs after
    _authorize_request (auth) and resolves the matter through matter_store with the
    request's owner_user_id, so a caller can never summarize another tenant's matter.

    Grounding: the summary is derived ONLY from the matter's real document text and
    stored review findings (assembled in matter_summary.build_summary_context); the
    prompt forbids inventing facts. AI degradation is graceful -- when AI is disabled
    / unconfigured / the call fails we return 503 with the friendly, frontend-ready
    message, never a 500/stack trace.
    """
    telemetry.increment("matter_summary_requests")
    matter_id = parse_matter_id(path, suffix="/summary")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    try:
        matter = matter_store.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except matter_store.MatterStoreError as error:
        handler._send_json({"error": str(error)}, status=500)
        return
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    try:
        result = matter_summary.summarize_matter(matter, transport=_matter_summary_transport(handler))
    except matter_summary.MatterSummaryUnavailableError as error:
        # AI off / unconfigured / provider failed -> friendly 503, never a crash.
        handler._send_json({"error": str(error)}, status=503)
        return
    except matter_summary.MatterSummaryError as error:
        # Nothing to summarize (e.g. no document text) -> 400 with a clear message.
        handler._send_json({"error": str(error)}, status=400)
        return

    handler._send_json(result)


def _matter_summary_transport(handler):
    """Test seam: a handler may carry an injected summary transport (no network).

    Production handlers don't set this, so matter_summary builds the real OpenRouter
    transport from the configured reviewer settings.
    """
    return getattr(handler, "matter_summary_transport", None)


def ai_first_review_store_metadata(
    ai_first_review_result: dict,
    *,
    started_at: str,
    completed_at: str,
) -> dict[str, object]:
    return lifecycle_ai_first_review_store_metadata(
        ai_first_review_result,
        started_at=started_at,
        completed_at=completed_at,
    )


def handle_matter_redline_draft_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/redline-draft")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    try:
        matter = RepositoryMatterLifecycle(DiskMatterRepository()).save_redline_draft(
            matter_id,
            payload.get("redline_draft"),
            owner_user_id=request_owner_user_id(handler),
        )
    except RedlineDraftError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except MatterNotFoundError:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    handler._send_json({"matter": matter_view.public_matter(matter)})


def clean_redline_draft(draft: dict) -> dict:
    return lifecycle_clean_redline_draft(draft)


def clean_bool_map(value: object) -> dict[str, bool]:
    return lifecycle_clean_bool_map(value)


def clean_text_map(value: object) -> dict[str, str]:
    return lifecycle_clean_text_map(value)


def clean_dict_list(value: object) -> list[dict]:
    return lifecycle_clean_dict_list(value)


def handle_matter_delete(handler, path: str) -> None:
    matter_id = parse_matter_id(path)
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    owner_user_id = request_owner_user_id(handler)
    # Capture the source bytes before deletion unlinks them, so the render-cache
    # purge below can recompute the (content + owner) cache key. Reading is gated
    # on ownership: a non-owner caller gets None and the matter is not deleted.
    pre_delete = matter_store.get_matter(matter_id, owner_user_id=owner_user_id)
    source_bytes = matter_store.get_source_document_bytes(pre_delete) if pre_delete else None
    source_filename = str(pre_delete.get("source_filename") or "") if pre_delete else ""

    matter = matter_store.delete_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    # Stop tracking any in-flight background render for the now-deleted matter.
    document_rendering.matter_render_coordinator().forget(matter_id)
    if source_bytes is not None:
        # Purge the deleted matter's rendered artifacts so they do not outlive
        # the matter (and cannot be served to a later, unrelated matter).
        document_rendering.purge_render_cache_for_source(
            source_bytes,
            owner_user_id=str(matter.get("owner_user_id") or owner_user_id),
            source_filename=source_filename,
        )
    handler._send_json({"deleted": matter_view.public_matter(matter)})


def handle_demo_reset(handler) -> None:
    removed_count = matter_store.reset_demo_repository(owner_user_id=request_owner_user_id(handler))
    handler._send_json({"removed": removed_count, "matters": []})
