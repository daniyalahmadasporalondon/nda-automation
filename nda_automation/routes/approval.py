from __future__ import annotations

from urllib.parse import unquote

from .. import approval, matter_lifecycle, matter_view, redline_export_service, telemetry
from ..docx_export import DOCX_MIME, DocxExportError
from ..docx_text import DocxExtractionError
from ..pdf_text import PdfExtractionError
from ..checker import ParagraphAlignmentError
from .common import parse_matter_id, request_owner_user_id


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
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    matter_id, clause_id = parsed

    payload = handler._read_json_payload()
    if payload is None:
        return

    owner_user_id = request_owner_user_id(handler)
    repository = matter_lifecycle.repository_for_handler(handler)
    matter = matter_lifecycle.get_matter(repository, matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return
    if approval.find_clause(matter, clause_id) is None:
        handler._send_json({"error": "Clause not found in this matter's review."}, status=404)
        return

    try:
        reviewer_decision = approval.normalize_reviewer_decision(
            payload, actor=_request_actor(handler),
        )
    except approval.ReviewerDecisionError as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    updated_matter = matter_lifecycle.record_clause_decision(
        repository,
        matter_id,
        clause_id,
        reviewer_decision,
        owner_user_id=owner_user_id,
    )
    if updated_matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
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
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    owner_user_id = request_owner_user_id(handler)
    repository = matter_lifecycle.repository_for_handler(handler)
    actor = _request_actor(handler)
    approval_result = matter_lifecycle.approve_matter(
        repository,
        matter_id,
        actor=actor,
        owner_user_id=owner_user_id,
    )
    if approval_result.matter is None:
        handler._send_json({"error": "Matter not found."}, status=404)
        return

    if approval_result.blocks:
        telemetry.increment("matter_approvals_blocked")
        handler._send_json(
            {
                "error": "Matter cannot be approved yet.",
                "blocks_approval": approval_result.blocks,
                "resolution": approval.resolution_summary(approval_result.matter),
            },
            status=409,
        )
        return

    updated_matter = approval_result.matter
    telemetry.increment("matter_approvals")
    handler._send_json({
        "matter": matter_view.public_matter(updated_matter),
        "status": "approved",
        "approved_at": approval_result.approved_at,
        "approver": approval_result.approver,
        "timeline_event": approval_result.timeline_event,
        "resolution": approval.resolution_summary(updated_matter),
    })


def handle_matter_reviewed_docx(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/reviewed-docx")
    if matter_id is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return

    owner_user_id = request_owner_user_id(handler)
    repository = matter_lifecycle.repository_for_handler(handler)
    matter = matter_lifecycle.get_matter(repository, matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "Matter not found."}, status=404, send_body=send_body)
        return
    if str(matter.get("status") or "") != approval.MATTER_STATUS_APPROVED:
        handler._send_json(
            {"error": "Reviewed DOCX is available only after the matter is approved."},
            status=409,
            send_body=send_body,
        )
        return

    export_payload = approval.reviewed_docx_payload(matter)
    try:
        redline_export = redline_export_service.build_matter_redline(
            matter_id,
            export_payload,
            persist=False,
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
        handler._send_json({"error": str(error), "details": error.details}, status=500, send_body=send_body)
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

    telemetry.increment("reviewed_docx_exports")
    handler._send_download(
        redline_export.data,
        redline_export.filename,
        DOCX_MIME,
        headers={
            "X-Export-Verified": redline_export_service.VERIFIED_EXPORT_HEADER,
            "X-Reviewed-Redline-Count": str(len(export_payload["export_redline_edits"])),
        },
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
