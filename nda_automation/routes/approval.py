from __future__ import annotations

import logging
from urllib.parse import unquote

from .. import (
    approval,
    artifact_service,
    matter_document_artifacts,
    matter_view,
    pdf_export_service,
    redline_export_service,
    telemetry,
)
from ..docx_export import DOCX_MIME, DocxExportError
from ..docx_text import DocxExtractionError
from ..matter_lifecycle import MatterApprovalBlockedError, MatterNotFoundError, RepositoryMatterLifecycle
from ..matter_repository import DiskMatterRepository, MatterRepository
from ..pdf_text import PdfExtractionError
from ..checker import ParagraphAlignmentError
from .common import parse_matter_id, request_owner_user_id

logger = logging.getLogger(__name__)


def _repository(handler) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    if repository is not None:
        return repository
    return DiskMatterRepository()


def parse_clause_decision_path(path: str) -> tuple[str, str] | None:
    """Split /api/matters/{id}/clauses/{clauseId}/decision into its two ids."""
    prefix = "/api/matters/"
    marker = "/clauses/"
    suffix = "/decision"
    if not path.startswith(prefix) or marker not in path or not path.endswith(suffix):
        return None
    remainder = path[len(prefix):-len(suffix)]
    raw_matter_id, _, raw_clause_id = remainder.partition(marker)
    matter_id = unquote(raw_matter_id).strip("/")
    clause_id = unquote(raw_clause_id).strip("/")
    if not matter_id or "/" in matter_id or not clause_id or "/" in clause_id:
        return None
    return matter_id, clause_id


def handle_clause_decision(handler, path: str) -> None:
    parsed = parse_clause_decision_path(path)
    if parsed is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return
    matter_id, clause_id = parsed

    payload = handler._read_json_payload()
    if payload is None:
        return

    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return
    if approval.find_clause(matter, clause_id) is None:
        handler._send_json({"error": "Clause not found in this NDA's review."}, status=404)
        return

    try:
        reviewer_decision = approval.normalize_reviewer_decision(
            payload, actor=_request_actor(handler),
        )
    except approval.ReviewerDecisionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    updated_matter = repository.set_clause_reviewer_decision(
        matter_id, clause_id, reviewer_decision, owner_user_id=owner_user_id,
    )
    if updated_matter is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    telemetry.increment("reviewer_decisions_recorded")
    handler._send_json({
        "matter": matter_view.public_matter(updated_matter),
        "clause": approval.public_clause_decision(updated_matter, clause_id),
        "resolution": approval.resolution_summary(updated_matter),
    })


def handle_matter_approve(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix="/approve")
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    owner_user_id = request_owner_user_id(handler)
    actor = _request_actor(handler)
    try:
        approved = RepositoryMatterLifecycle(DiskMatterRepository()).approve_matter(
            matter_id,
            actor=actor,
            owner_user_id=owner_user_id,
        )
    except MatterNotFoundError:
        handler._send_json({"error": "NDA not found."}, status=404)
        return
    except MatterApprovalBlockedError as error:
        telemetry.increment("matter_approvals_blocked")
        handler._send_json(
            {
                "error": "NDA cannot be approved yet.",
                "blocks_approval": error.blocks,
                "resolution": error.resolution,
            },
            status=409,
        )
        return

    telemetry.increment("matter_approvals")
    handler._send_json({
        "matter": matter_view.public_matter(approved.matter),
        "status": "approved",
        "approved_at": approved.approved_at,
        "approver": approved.approver,
        "timeline_event": approved.timeline_event,
        "resolution": approved.resolution,
    })


def handle_matter_reviewed_docx(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/reviewed-docx")
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return

    owner_user_id = request_owner_user_id(handler)
    matter = _repository(handler).get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return
    if str(matter.get("status") or "") != approval.MATTER_STATUS_APPROVED:
        handler._send_json(
            {"error": "Reviewed DOCX is available only after the NDA is approved."},
            status=409,
            send_body=send_body,
        )
        return

    try:
        reviewed_docx = matter_document_artifacts.build_reviewed_docx(
            matter_id,
            matter,
            owner_user_id=owner_user_id,
        )
    except redline_export_service.StaleMatterReviewError as error:
        handler._send_json(
            {"error": str(error), "stale_reasons": error.reasons, "review_refresh": error.summary},
            status=409,
            send_body=send_body,
        )
        return
    except redline_export_service.MatterNotFoundError as error:
        handler._send_json({"error": str(error)}, status=404, send_body=send_body)
        return
    except redline_export_service.DocxOpenHealthError as error:
        # Drop the OOXML internals (error.details) from the response; log them.
        logger.error("Reviewed DOCX failed integrity check (approval): %s | details=%s", error, error.details)
        handler._send_json(
            {"error": redline_export_service.DOCX_HEALTH_CLIENT_MESSAGE}, status=500, send_body=send_body
        )
        return
    except redline_export_service.PdfSourceRedlineUnavailableError as error:
        handler._send_json(error.payload, status=error.status, send_body=send_body)
        return
    except (DocxExtractionError, PdfExtractionError) as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    except ParagraphAlignmentError:
        handler._send_json(
            {"error": "The extracted document paragraphs could not be aligned to the extracted text."},
            status=400,
            send_body=send_body,
        )
        return
    except DocxExportError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    except artifact_service.ArtifactRegistryError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return

    telemetry.increment("reviewed_docx_exports")
    redline_export = reviewed_docx.export
    export_headers = redline_export.headers or {}
    is_original_export = bool(
        export_headers.get(redline_export_service.ORIGINAL_EXPORT_MARKER_HEADER)
    )
    if is_original_export:
        # PDF-source matter with no accepted redlines: the original document is served
        # unchanged. There was no lossy reconstruction to fidelity-check, so it is marked
        # honestly as the original -- NEVER as a verified reconstruction.
        verified_value = redline_export_service.ORIGINAL_UNCHANGED_EXPORT_HEADER
    else:
        verified_value = (
            export_headers.get("X-PDF-DOCX-Reconstruction")
            if export_headers.get("X-PDF-DOCX-Reconstruction")
            else redline_export_service.VERIFIED_EXPORT_HEADER
        )
    headers = {
        "X-Export-Verified": verified_value,
        "X-Reviewed-Redline-Count": str(len(reviewed_docx.payload["export_redline_edits"])),
    }
    headers.update(export_headers)
    if reviewed_docx.artifact is not None:
        headers["X-Reviewed-Artifact-ID"] = reviewed_docx.artifact.id
    handler._send_download(
        redline_export.data,
        redline_export.filename,
        redline_export.content_type or DOCX_MIME,
        headers=headers,
        send_body=send_body,
    )


def handle_matter_reviewed_pdf(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/reviewed-pdf")
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return

    owner_user_id = request_owner_user_id(handler)
    matter = _repository(handler).get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return
    if str(matter.get("status") or "") != approval.MATTER_STATUS_APPROVED:
        handler._send_json(
            {"error": "Reviewed PDF is available only after the NDA is approved."},
            status=409,
            send_body=send_body,
        )
        return

    try:
        reviewed_docx = matter_document_artifacts.build_reviewed_docx(
            matter_id,
            matter,
            owner_user_id=owner_user_id,
        )
        reviewed_pdf = pdf_export_service.build_docx_pdf_export(
            reviewed_docx.export.data,
            reviewed_docx.export.filename,
            owner_user_id=owner_user_id,
        )
    except pdf_export_service.PdfExportError as error:
        handler._send_json(error.payload, status=error.status, headers=error.headers, send_body=send_body)
        return
    except redline_export_service.StaleMatterReviewError as error:
        handler._send_json(
            {"error": str(error), "stale_reasons": error.reasons, "review_refresh": error.summary},
            status=409,
            send_body=send_body,
        )
        return
    except redline_export_service.MatterNotFoundError as error:
        handler._send_json({"error": str(error)}, status=404, send_body=send_body)
        return
    except redline_export_service.DocxOpenHealthError as error:
        # Drop the OOXML internals (error.details) from the response; log them.
        logger.error("Reviewed DOCX failed integrity check (approval): %s | details=%s", error, error.details)
        handler._send_json(
            {"error": redline_export_service.DOCX_HEALTH_CLIENT_MESSAGE}, status=500, send_body=send_body
        )
        return
    except redline_export_service.PdfSourceRedlineUnavailableError as error:
        handler._send_json(error.payload, status=error.status, send_body=send_body)
        return
    except (DocxExtractionError, PdfExtractionError) as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    except ParagraphAlignmentError:
        handler._send_json(
            {"error": "The extracted document paragraphs could not be aligned to the extracted text."},
            status=400,
            send_body=send_body,
        )
        return
    except DocxExportError as error:
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return
    except artifact_service.ArtifactRegistryError as error:
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return

    telemetry.increment("reviewed_pdf_exports")
    headers = {
        **reviewed_pdf.headers,
        "X-Reviewed-Redline-Count": str(len(reviewed_docx.payload["export_redline_edits"])),
    }
    if reviewed_docx.artifact is not None:
        headers["X-Reviewed-Artifact-ID"] = reviewed_docx.artifact.id
    handler._send_download_file(
        reviewed_pdf.path,
        reviewed_pdf.filename,
        reviewed_pdf.content_type,
        headers=headers,
        send_body=send_body,
    )


def _request_actor(handler) -> str:
    user = getattr(handler, "current_user", None)
    if isinstance(user, dict):
        for key in ("email", "name", "id"):
            value = str(user.get(key) or "").strip()
            if value:
                return value
    user_id = request_owner_user_id(handler)
    return user_id or "reviewer"
