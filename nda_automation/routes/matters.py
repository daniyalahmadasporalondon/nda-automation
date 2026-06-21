from __future__ import annotations

import base64
import binascii
import logging
import re
from pathlib import Path

from .. import gmail_integration, matter_render_job, matter_store, matter_summary, matter_view, pdf_export_service, telemetry
from ..checker import ParagraphAlignmentError
from ..document_limits import DocumentSizeError, DOCUMENT_TOO_LARGE_MESSAGE, ensure_document_size
from ..docx_text import DocxExtractionError, extract_docx_paragraphs
from ..ai_assessor import ai_first_review_enabled
from ..ingestion_service import (
    create_matter_from_document,
    enqueue_on_demand_review,
    is_supported_document_filename,
)
from ..matter_lifecycle import (
    MatterNotFoundError,
    RedlineDraftError,
    RepositoryMatterLifecycle,
    ai_first_review_store_metadata as lifecycle_ai_first_review_store_metadata,
    clean_bool_map as lifecycle_clean_bool_map,
    clean_dict_list as lifecycle_clean_dict_list,
    clean_redline_draft as lifecycle_clean_redline_draft,
    clean_text_map as lifecycle_clean_text_map,
)
from ..matter_repository import DiskMatterRepository, MatterRepository, MatterRepositoryError
from ..pdf_text import PdfExtractionError
from ..repository_board_workflow import RepositoryBoardWorkflow, RepositoryBoardWorkflowError
from ..review_document import STRUCTURAL_METADATA_KEYS, align_document_paragraphs
from ..review_engine import (
    REVIEW_ENGINE_AI_FIRST,
    ActiveReviewEngineError,
    review_nda_with_active_engine,
)
from ..review_staleness import review_result_is_stale, review_result_staleness
from ..review_state import review_was_ai_executed
from .common import parse_matter_id, request_owner_user_id

logger = logging.getLogger(__name__)

HTTP_MATTER_SOURCE_COLUMNS = {"manual_upload": "in_review"}
MANUAL_UPLOAD_BOARD_COLUMNS = {"gmail_demo", "in_review", "reviewed", "sent"}


def _repository(handler) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    if repository is not None:
        return repository
    return DiskMatterRepository()


def _repository_board_workflow(handler) -> RepositoryBoardWorkflow:
    workflow = getattr(handler, "repository_board_workflow", None)
    if workflow is not None:
        return workflow
    return RepositoryBoardWorkflow(_repository(handler))


def _send_repository_board_error(
    handler,
    error: RepositoryBoardWorkflowError,
    *,
    send_body: bool = True,
) -> None:
    handler._send_json(error.payload, status=error.status, send_body=send_body)


def handle_matter_list(handler, *, send_body: bool = True) -> None:
    try:
        payload = _repository_board_workflow(handler).list_board(owner_user_id=request_owner_user_id(handler))
    except RepositoryBoardWorkflowError as error:
        _send_repository_board_error(handler, error, send_body=send_body)
        return
    handler._send_json(payload, send_body=send_body)


def handle_matter_review(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/review")
    matter = _matter_for_review_response(handler, matter_id, send_body=send_body)
    if matter is None:
        return
    handler._send_json(_matter_review_payload(matter, matter_id), send_body=send_body)


def _review_tab_ai_only_engine(text, **kwargs):
    """Review-tab review engine: AI is the ONLY reviewer.

    The user-facing Review workstation must NEVER fall back to the deterministic
    review. We pin ``force_engine=ai_first`` so this call site ignores the active
    engine config (which can select ``deterministic`` via env/runtime settings) and
    always runs the AI reviewer. When the AI reviewer cannot run (AI disabled / key
    missing / provider error) ``review_nda_with_active_engine`` raises
    ``ActiveReviewEngineError`` (fail-closed, no deterministic review produced);
    ``refresh_review`` then leaves the matter unreviewed and the route surfaces a
    notification rather than a deterministic verdict.

    This is intentionally a thin, review-tab-LOCAL wrapper: the shared
    ``review_nda_with_active_engine`` and the inbound/ingestion call sites are left
    exactly as they are.
    """
    return review_nda_with_active_engine(text, force_engine=REVIEW_ENGINE_AI_FIRST, **kwargs)


def _ai_first_review_enabled() -> bool:
    """Route-local seam over the AI-availability predicate (patchable in tests).

    Delegates to ``ai_assessor.ai_first_review_enabled`` -- a cheap, OFFLINE check of
    whether the AI reviewer is enabled. The async review-refresh route consults this
    BEFORE enqueuing so it can keep the synchronous ``ai_review_unavailable``
    notification when AI is OFF (rather than enqueue a job that would only fail).
    """
    return ai_first_review_enabled()


def handle_matter_review_refresh(handler, path: str) -> None:
    """POST /api/matters/<id>/review-refresh -- enqueue the AI review ASYNC (202).

    This is the explicit, user-initiated AI refresh. It used to run the heavy AI
    pipeline SYNCHRONOUSLY inside the request (~145-245s -> broken pipe). It now
    ENQUEUES the review onto the storm-hardened inbound worker pool (bounded
    concurrency / dedup / idempotency / 256-cap queue / recovery sweep) and returns
    IMMEDIATELY. The route NEVER blocks on the pipeline and never sends the heavy
    review result; the board + review polls carry the live ``review_status`` and the
    finished review lands on a later poll.

    The 202 contract:
      * not stale (a fresh AI review already exists) -> 200
        ``{review_status:"idle", matter}`` (no job enqueued).
      * AI reviewer unavailable (cheap offline check) -> 200 with today's
        ``ai_review_unavailable`` notification (no job enqueued -- it would only
        fail).
      * stale + a fresh job scheduled -> 202
        ``{review_status:"in_progress", job_scheduled:true, matter}``.
      * stale + already pending (dedup) -> 202
        ``{review_status:"in_progress", job_scheduled:false, matter}``.
      * the bounded queue is full -> 503 ``{error, review_status:"idle"}``.
      * missing/owner-mismatched matter -> 404.

    Staleness uses the BROAD offline signal (``review_may_be_stale``):
    playbook/engine drift OR no AI review exists yet OR the matter text changed.
    The async job runs the AI-ONLY engine (fail-closed AI-first) -- exactly the
    Review-tab contract -- so it never produces a deterministic verdict.
    """
    matter_id = parse_matter_id(path, suffix="/review-refresh")
    matter = _matter_for_review_response(handler, matter_id, send_body=True)
    if matter is None:
        return

    owner_user_id = request_owner_user_id(handler)

    may_be_stale, _reasons = _review_may_be_stale(
        matter,
        playbook_stale=review_result_is_stale(matter.get("review_result")),
    )

    # Not stale: a fresh AI review already exists. Nothing to enqueue -- report idle
    # and return the matter as-is (no heavy work, no block).
    if not may_be_stale:
        payload = _matter_review_payload(matter, matter_id, was_stale=False, refresh_attempted=True)
        payload["review_status"] = "idle"
        payload["job_scheduled"] = False
        handler._send_json(payload)
        return

    # AI reviewer OFF (cheap offline check): keep today's synchronous notification
    # rather than enqueue a job that would only fail closed. No deterministic verdict
    # is ever produced; the stored review is untouched.
    if not _ai_first_review_enabled():
        telemetry.increment("review_tab_ai_unavailable")
        payload = _matter_review_payload(matter, matter_id, was_stale=True, refresh_attempted=True)
        payload["review_status"] = "idle"
        payload["job_scheduled"] = False
        payload["ai_review_unavailable"] = True
        payload["ai_review_unavailable_message"] = (
            "Review can't be completed — no AI reviewer available."
        )
        handler._send_json(payload)
        return

    # Stale + AI available: ENQUEUE the async review on the shared pool and return
    # immediately. The route NEVER runs the engine inline.
    scheduled, already_pending, queue_full = enqueue_on_demand_review(
        str(matter_id or ""), owner_user_id, repository=_repository(handler)
    )

    if queue_full:
        # The bounded review queue is saturated. Surface a retryable 503; the matter
        # is reported idle (no job is owning it) so a later click can retry.
        handler._send_json(
            {
                "error": "The review queue is busy. Please try again in a moment.",
                "review_status": "idle",
                "job_scheduled": False,
            },
            status=503,
        )
        return

    # Re-read so the payload reflects the in_progress stamp written by the enqueue.
    refreshed_matter = _matter_for_review_response(handler, matter_id, send_body=True)
    if refreshed_matter is None:
        return
    payload = _matter_review_payload(refreshed_matter, matter_id, was_stale=True, refresh_attempted=True)
    payload["review_status"] = "in_progress"
    payload["job_scheduled"] = bool(scheduled and not already_pending)
    handler._send_json(payload, status=202)


def _matter_for_review_response(handler, matter_id: str | None, *, send_body: bool) -> dict | None:
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return None
    repository = _repository(handler)
    try:
        matter = repository.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except MatterRepositoryError as error:
        logger.warning("Matter load failed (review response): %s", error)
        handler._send_json(
            {"error": matter_store.friendly_matter_store_message(error)}, status=500, send_body=send_body
        )
        return None
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return None
    return _with_restored_paragraph_structure(matter, repository=repository)


def _restored_review_result_paragraphs(
    matter: dict,
    *,
    repository: MatterRepository,
) -> list[dict] | None:
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
    source_filename = str(matter.get("source_filename") or matter.get("stored_filename") or "")
    if Path(source_filename).suffix.casefold() != ".docx":
        return None
    try:
        source_bytes = repository.get_source_document_bytes(matter)
    except MatterRepositoryError:
        return None
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


def _with_restored_paragraph_structure(matter: dict, *, repository: MatterRepository) -> dict:
    merged = _restored_review_result_paragraphs(matter, repository=repository)
    if merged is None:
        return matter
    review_result = matter.get("review_result")
    return {**matter, "review_result": {**review_result, "paragraphs": merged}}


def _normalize_review_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _matter_has_ai_review(review_result: object) -> bool:
    """True when the matter's STORED review was produced by the AI-first engine.

    A deterministically-generated review (e.g. outbound generation, which pins the
    deterministic engine and defers AI to on-demand) carries an
    ``active_review_engine.executed_engine`` that is not ``ai_first`` -- so "no AI
    review exists" is true and the matter should advertise ``review_may_be_stale``.
    A missing/empty review is likewise "no AI review".
    """
    return review_was_ai_executed(review_result)


def _matter_review_text_changed(matter: dict, review_result: object) -> bool:
    """True when the matter's current text differs from what the review was run on.

    Cheap + OFFLINE: compares the matter's current ``extracted_text`` to the text
    snapshot the stored review recorded (``review_result['extracted_text']``). When
    the review never recorded its source text we cannot prove a change, so we return
    False (the engine/playbook/no-AI signals still apply).
    """
    if not isinstance(review_result, dict):
        return False
    review_text = review_result.get("extracted_text")
    if not isinstance(review_text, str) or not review_text.strip():
        return False
    return _normalize_review_text(matter.get("extracted_text")) != _normalize_review_text(review_text)


def _review_may_be_stale(matter: dict, *, playbook_stale: bool) -> tuple[bool, list[str]]:
    """Cheap/offline staleness verdict for the matter-fetch response.

    OR of three offline signals (none of which call the AI engine):
      - ``playbook_stale``  -- playbook hash / engine version / structure drift
        (from review_result_staleness).
      - no AI review exists -- the stored review was not produced by the AI engine.
      - text changed        -- the matter text changed since the last review run.
    """
    review_result = matter.get("review_result")
    extra_reasons: list[str] = []
    if not _matter_has_ai_review(review_result):
        extra_reasons.append("no_ai_review")
    if _matter_review_text_changed(matter, review_result):
        extra_reasons.append("matter_text_changed")
    may_be_stale = bool(playbook_stale or extra_reasons)
    return may_be_stale, extra_reasons


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
    may_be_stale, extra_stale_reasons = _review_may_be_stale(matter, playbook_stale=is_stale)
    refreshed = bool(refresh_attempted and was_stale and not is_stale)
    redline_draft_cleared = bool(
        refreshed
        and had_redline_draft
        and not isinstance(matter.get("redline_draft"), dict)
    )
    payload = matter_view.review_matter(matter)
    # Cheap/offline staleness signal. Opening/fetching a matter NEVER runs the AI
    # engine -- it returns the EXISTING stored review and this boolean. The AI
    # review runs only on the explicit POST /api/matters/<id>/review-refresh path.
    # ``review_may_be_stale`` is the BROAD offline signal: playbook/engine drift OR
    # no AI review exists OR the matter text changed since the last review.
    payload["review_may_be_stale"] = may_be_stale
    combined_stale_reasons = list(staleness["stale_reasons"]) + [
        reason for reason in extra_stale_reasons if reason not in staleness["stale_reasons"]
    ]
    payload["review_refresh"] = {
        # ``stale`` keeps its narrow playbook/engine meaning (export/send gate);
        # ``review_may_be_stale`` is the broad open-time indicator.
        "stale": is_stale,
        "review_may_be_stale": may_be_stale,
        "refresh_method": "POST",
        "refresh_url": f"/api/matters/{matter_id}/review-refresh",
        "refreshed": refreshed,
        "redline_draft_cleared": redline_draft_cleared,
        "stale_reasons": combined_stale_reasons,
        "current_playbook": staleness["current_playbook"],
        "review_playbook": staleness["review_playbook"],
        "current_review_engine_version": staleness["current_review_engine_version"],
    }
    if is_stale and staleness.get("message"):
        payload["review_refresh"]["stale_message"] = staleness["message"]
    if redline_draft_cleared:
        payload["review_refresh"]["message"] = "Saved redline draft was cleared because the review was re-analyzed."
    # The async-review lifecycle status (review_status / review_error /
    # review_started_at, TTL-overridden on read) is already merged at the top level
    # of ``matter_view.review_matter``. The 202/200 review-refresh route may overwrite
    # ``review_status`` afterwards (idle / in_progress) to reflect the action just
    # taken; the board + review polls read the stored (overridden) status here.
    return payload


def handle_matter_detail(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path)
    try:
        payload = _repository_board_workflow(handler).detail_card(
            matter_id,
            owner_user_id=request_owner_user_id(handler),
        )
    except RepositoryBoardWorkflowError as error:
        _send_repository_board_error(handler, error, send_body=send_body)
        return
    handler._send_json(payload, send_body=send_body)


def refresh_stale_matter_review(matter: dict) -> dict:
    # The playbook-publish stale-refresh shares the Review-tab AI-only contract:
    # the AI is the ONLY reviewer. We pin ``_review_tab_ai_only_engine``
    # (force_engine=ai_first) exactly as the explicit review-refresh route does, so a
    # playbook publish never re-reviews a stale matter with the deterministic engine.
    # When the AI reviewer cannot run (AI disabled / key missing / provider error)
    # ``refresh_review`` swallows the ``ActiveReviewEngineError`` and returns the
    # matter UNCHANGED -- no deterministic verdict is ever produced.
    return RepositoryMatterLifecycle(DiskMatterRepository()).refresh_review(
        matter,
        review_engine_func=_review_tab_ai_only_engine,
        review_staleness_func=review_result_is_stale,
    ).matter


def handle_matter_source(handler, path: str, *, send_body: bool = True) -> None:
    """Stream a matter's stored original .docx/.pdf for faithful rendering."""
    matter_id = parse_matter_id(path, suffix="/source")
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return
    repository = _repository(handler)
    try:
        matter = repository.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except MatterRepositoryError as error:
        logger.warning("Matter load failed (source stream): %s", error)
        handler._send_json(
            {"error": matter_store.friendly_matter_store_message(error)}, status=500, send_body=send_body
        )
        return
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return
    source_filename = str(matter.get("source_filename") or matter.get("stored_filename") or "")
    try:
        source_bytes = repository.get_source_document_bytes(matter)
    except MatterRepositoryError as error:
        logger.warning("Source bytes load failed: %s", error)
        handler._send_json(
            {"error": matter_store.friendly_matter_store_message(error)}, status=500, send_body=send_body
        )
        return
    if source_bytes is None:
        handler._send_json({"error": "No source document for this NDA."}, status=404, send_body=send_body)
        return
    ext = Path(source_filename).suffix.lower()
    if ext == ".docx":
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif ext == ".pdf":
        mime = "application/pdf"
    else:
        mime = "application/octet-stream"
    handler._send_bytes(source_bytes, filename=source_filename, content_type=mime, send_body=send_body)


def handle_matter_render_status(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/render-status")
    try:
        payload = matter_render_job.render_status_payload(
            matter_id,
            owner_user_id=request_owner_user_id(handler),
            repository=_repository(handler),
        )
    except matter_render_job.MatterRenderJobError as error:
        _send_render_job_error(handler, error, send_body=send_body)
        return
    handler._send_json(payload, send_body=send_body)


def handle_matter_render_pdf(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/render-pdf")
    try:
        result = matter_render_job.render_pdf_file(
            matter_id,
            owner_user_id=request_owner_user_id(handler),
            repository=_repository(handler),
        )
    except matter_render_job.MatterRenderJobError as error:
        _send_render_job_error(handler, error, send_body=send_body)
        return
    handler._send_file(result.path, content_type=result.content_type, send_body=send_body)


def handle_matter_source_pdf(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/source-pdf")
    try:
        result = pdf_export_service.build_matter_source_pdf_export(
            matter_id,
            owner_user_id=request_owner_user_id(handler),
            repository=_repository(handler),
        )
    except pdf_export_service.PdfExportError as error:
        handler._send_json(error.payload, status=error.status, headers=error.headers, send_body=send_body)
        return
    handler._send_download_file(
        result.path,
        result.filename,
        result.content_type,
        headers=result.headers,
        send_body=send_body,
    )


def handle_matter_source_docx(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/source-docx")
    try:
        result = pdf_export_service.build_matter_pdf_source_docx_export(
            matter_id,
            owner_user_id=request_owner_user_id(handler),
            repository=_repository(handler),
        )
    except pdf_export_service.PdfExportError as error:
        handler._send_json(error.payload, status=error.status, headers=error.headers, send_body=send_body)
        return
    handler._send_download(
        result.data,
        result.filename,
        result.content_type,
        headers=result.headers,
        send_body=send_body,
    )


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
            repository=_repository(handler),
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
        handler._send_json({"error": "Unsupported NDA source."}, status=400)
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
        # DEFER the AI review like inbound + generation do. Running the AI review
        # synchronously at create fail-CLOSED (502, no matter saved) whenever the
        # AI reviewer was unavailable, so a transient provider outage silently
        # dropped manually-uploaded NDAs. With defer_ai_review=True the matter is
        # created immediately UN-REVIEWED ("Not reviewed yet") and sits in its
        # source column; the explicit, user-initiated on-demand review (the Review
        # workstation / inspector "Run AI review" button -> POST
        # /api/matters/<id>/review-refresh) runs the AI later. This also keeps the
        # upload path off the synchronous review storm entirely. The frontend
        # surfaces the no-AI notification when that on-demand review cannot run.
        matter = create_matter_from_document_func(
            filename=filename,
            document_bytes=document_bytes,
            source_type=source_type,
            board_column=board_column,
            intake_metadata=matter_intake_metadata(payload, filename),
            owner_user_id=request_owner_user_id(handler),
            defer_ai_review=True,
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
    payload = handler._read_json_payload()
    if payload is None:
        return

    try:
        response = _repository_board_workflow(handler).move_card(
            matter_id,
            payload.get("board_column", ""),
            owner_user_id=request_owner_user_id(handler),
        )
    except RepositoryBoardWorkflowError as error:
        _send_repository_board_error(handler, error)
        return
    handler._send_json(response)


def handle_matter_reviewed_update(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/reviewed")
    payload = handler._read_json_payload()
    if payload is None:
        return

    try:
        response = _repository_board_workflow(handler).set_reviewed(
            matter_id,
            payload.get("reviewed", True),
            owner_user_id=request_owner_user_id(handler),
        )
    except RepositoryBoardWorkflowError as error:
        _send_repository_board_error(handler, error)
        return
    handler._send_json(response)


def handle_matter_counterparty_confirm(handler, path: str) -> None:
    """POST /api/matters/<id>/counterparty -- persist a HUMAN override of the counterparty.

    The AI extracts the counterparty from the NDA preamble and a verifier double-checks
    it; when that is refuted or low-confidence the UI surfaces a "confirm who this is"
    affordance. This endpoint records the human's answer as the authoritative value:
    ``{"name": <given>, "confidence": 1.0, "verified": true, "source": "human"}`` at the
    durable ``matter["intake_metadata"]["counterparty"]`` location, which flips
    ``counterparty_needs_confirmation`` to false in ``public_matter``.

    Auth/CSRF/Origin/host/rate-limit are enforced centrally in server.do_POST before
    dispatch (this route is registered in _POST_EXACT_ROUTES, like every sibling write).
    The owner is taken from the AUTHENTICATED request -- never a client-supplied owner --
    so a caller can never confirm another tenant's matter. A missing/owner-mismatched
    matter returns 404 (the writer returns None) with no write performed.
    """
    matter_id = parse_matter_id(path, suffix="/counterparty")
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        handler._send_json({"error": "Provide a counterparty name to confirm."}, status=400)
        return

    counterparty = {
        "name": name.strip(),
        "confidence": 1.0,
        "verified": True,
        "source": "human",
    }
    try:
        matter = matter_store.update_matter_counterparty(
            matter_id,
            counterparty,
            owner_user_id=request_owner_user_id(handler),
        )
    except matter_store.MatterStoreError as error:
        logger.warning("Counterparty confirm persistence failed: %s", error)
        handler._send_json({"error": matter_store.friendly_matter_store_message(error)}, status=500)
        return
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    telemetry.increment("matter_counterparty_confirmations")
    handler._send_json({"matter": matter_view.public_matter(matter)})


def handle_matter_summary(handler, path: str) -> None:
    """POST /api/matters/<id>/summary -- on-demand AI summary of one matter.

    Mirrors the other matter routes' auth/ownership shape: it runs after
    _authorize_request (auth) and resolves the matter through the repository with the
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
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    repository = _repository(handler)
    try:
        matter = repository.get_matter(matter_id, owner_user_id=request_owner_user_id(handler))
    except MatterRepositoryError as error:
        logger.warning("Matter load failed (summary): %s", error)
        handler._send_json({"error": matter_store.friendly_matter_store_message(error)}, status=500)
        return
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404)
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
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    try:
        matter = RepositoryMatterLifecycle(_repository(handler)).save_redline_draft(
            matter_id,
            payload.get("redline_draft"),
            owner_user_id=request_owner_user_id(handler),
        )
    except RedlineDraftError as error:
        handler._send_json({"error": str(error)}, status=400)
        return
    except MatterNotFoundError:
        handler._send_json({"error": "NDA not found."}, status=404)
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
    try:
        payload = _repository_board_workflow(handler).delete_card(
            matter_id,
            owner_user_id=request_owner_user_id(handler),
        )
    except RepositoryBoardWorkflowError as error:
        _send_repository_board_error(handler, error)
        return
    handler._send_json(payload)


def handle_demo_reset(handler) -> None:
    try:
        payload = _repository_board_workflow(handler).reset_board(owner_user_id=request_owner_user_id(handler))
    except RepositoryBoardWorkflowError as error:
        _send_repository_board_error(handler, error)
        return
    handler._send_json(payload)
