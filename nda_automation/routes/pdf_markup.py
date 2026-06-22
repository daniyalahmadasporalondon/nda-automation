"""HTTP routes for user-placed PDF markup on a matter.

Endpoints (registered in ``server.py`` mirroring the other matter routes):

* ``GET    /api/matters/{id}/pdf-annotations``           -> list stored annotations
* ``POST   /api/matters/{id}/pdf-annotations``           -> create one annotation
* ``DELETE /api/matters/{id}/pdf-annotations/{annId}``   -> remove one annotation
* ``GET    /api/matters/{id}/marked-up-pdf``             -> bake annotations into the source PDF

Every route is owner-scoped via ``request_owner_user_id`` and persists through
``repository.update_matter_fields(matter_id, {"pdf_annotations": [...]}, ...)``.
We always re-fetch the matter and mutate the freshly read list before persisting,
never trusting a stale dict.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import unquote

from .. import pdf_markup, telemetry
from ..annotated_pdf_export import (
    ANNOTATED_PDF_MIME,
    ANNOTATED_PDF_VERIFICATION_HEADER,
    annotated_pdf_download_filename,
    build_matter_annotated_pdf,
)
from ..matter_repository import DiskMatterRepository, MatterRepository
from .common import parse_matter_id, request_owner_user_id

_ANNOTATIONS_SUFFIX = "/pdf-annotations"


def _repository(handler) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    if repository is not None:
        return repository
    return DiskMatterRepository()


def parse_annotation_delete_path(path: str) -> tuple[str, str] | None:
    """Split /api/matters/{id}/pdf-annotations/{annId} into its two ids."""
    prefix = "/api/matters/"
    marker = "/pdf-annotations/"
    if not path.startswith(prefix) or marker not in path:
        return None
    remainder = path[len(prefix):]
    raw_matter_id, _, raw_annotation_id = remainder.partition(marker)
    matter_id = unquote(raw_matter_id).strip("/")
    annotation_id = unquote(raw_annotation_id).strip("/")
    if not matter_id or "/" in matter_id or not annotation_id or "/" in annotation_id:
        return None
    return matter_id, annotation_id


def _stored_annotations(matter) -> list:
    annotations = matter.get("pdf_annotations")
    if isinstance(annotations, list):
        return [item for item in annotations if isinstance(item, dict)]
    return []


def _request_author(handler) -> str:
    return request_owner_user_id(handler) or "reviewer"


def handle_pdf_annotations_list(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix=_ANNOTATIONS_SUFFIX)
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return

    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return

    handler._send_json({"annotations": _stored_annotations(matter)}, send_body=send_body)


def handle_pdf_annotation_create(handler, path: str) -> None:
    matter_id = parse_matter_id(path, suffix=_ANNOTATIONS_SUFFIX)
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    payload = handler._read_json_payload()
    if payload is None:
        return

    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    try:
        annotation = pdf_markup.normalize_annotation_input(payload)
    except pdf_markup.PdfMarkupError as error:
        handler._send_json({"error": str(error)}, status=400)
        return

    existing = _stored_annotations(matter)
    if len(existing) >= pdf_markup.MAX_ANNOTATIONS_PER_MATTER:
        handler._send_json(
            {
                "error": (
                    "This matter has reached the maximum of "
                    f"{pdf_markup.MAX_ANNOTATIONS_PER_MATTER} annotations."
                )
            },
            status=409,
        )
        return

    annotation["id"] = f"annot_{uuid.uuid4().hex[:12]}"
    annotation["author"] = _request_author(handler)
    annotation["created_at"] = datetime.now(timezone.utc).isoformat()

    updated_list = existing + [annotation]
    updated_matter = repository.update_matter_fields(
        matter_id, {"pdf_annotations": updated_list}, owner_user_id=owner_user_id
    )
    if updated_matter is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    telemetry.increment("pdf_annotation_added")
    handler._send_json({"annotation": annotation}, status=201)


def handle_pdf_annotation_delete(handler, path: str) -> None:
    parsed = parse_annotation_delete_path(path)
    if parsed is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return
    matter_id, annotation_id = parsed

    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    existing = _stored_annotations(matter)
    remaining = [item for item in existing if str(item.get("id") or "") != annotation_id]
    if len(remaining) == len(existing):
        handler._send_json({"error": "Annotation not found."}, status=404)
        return

    updated_matter = repository.update_matter_fields(
        matter_id, {"pdf_annotations": remaining}, owner_user_id=owner_user_id
    )
    if updated_matter is None:
        handler._send_json({"error": "NDA not found."}, status=404)
        return

    telemetry.increment("pdf_annotation_deleted")
    handler._send_json({"ok": True})


def handle_marked_up_pdf(handler, path: str, *, send_body: bool = True) -> None:
    matter_id = parse_matter_id(path, suffix="/marked-up-pdf")
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return

    telemetry.increment("marked_up_pdf_export_requests")
    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        telemetry.increment("marked_up_pdf_export_failed")
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return

    source_filename = str(matter.get("source_filename") or "")
    if not source_filename.lower().endswith(".pdf"):
        telemetry.increment("marked_up_pdf_export_failed")
        handler._send_json(
            {"error": "Marked-up PDF is available only for PDF NDAs."},
            status=400,
            send_body=send_body,
        )
        return

    source_bytes = repository.get_source_document_bytes(matter)
    if source_bytes is None:
        telemetry.increment("marked_up_pdf_export_failed")
        handler._send_json(
            {"error": "NDA source PDF is missing from storage."},
            status=400,
            send_body=send_body,
        )
        return

    try:
        baked = pdf_markup.bake_user_annotations(source_bytes, _stored_annotations(matter))
    except pdf_markup.PdfMarkupDependencyError as error:
        telemetry.increment("marked_up_pdf_export_failed")
        handler._send_json({"error": str(error)}, status=500, send_body=send_body)
        return
    except pdf_markup.PdfMarkupError as error:
        telemetry.increment("marked_up_pdf_export_failed")
        handler._send_json({"error": str(error)}, status=400, send_body=send_body)
        return

    filename = _marked_up_filename(source_filename)
    annotation_count = len(_stored_annotations(matter))
    handler._send_download(
        baked,
        filename,
        pdf_markup.MARKED_UP_PDF_MIME,
        headers={
            "X-Export-Verified": pdf_markup.MARKED_UP_PDF_VERIFICATION_HEADER,
            "X-PDF-Annotation-Count": str(annotation_count),
        },
        send_body=send_body,
    )


def _marked_up_filename(source_filename: str) -> str:
    base = annotated_pdf_download_filename(source_filename)
    return base.replace("-annotated-review.pdf", "-marked-up.pdf")


def handle_annotated_pdf(handler, path: str, *, send_body: bool = True) -> None:
    """Recovery export: source PDF with the REVIEW redlines baked on as annotations.

    This is the fallback offered by ``PdfSourceRedlineUnavailableError`` when the
    DOCX redline path fails closed for a PDF-source NDA. Because it is a recovery
    route, it must never dead-end: when the review highlights cannot be anchored
    (or the review is missing/stale, or PyMuPDF is unavailable) we fall back to
    returning the unmodified source PDF rather than erroring. We only surface an
    error when there is no usable PDF to return at all (matter not found, the
    source is not a PDF, or the source bytes are missing from storage).
    """
    matter_id = parse_matter_id(path, suffix="/annotated-pdf")
    if matter_id is None:
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return

    telemetry.increment("annotated_pdf_export_requests")
    owner_user_id = request_owner_user_id(handler)
    repository = _repository(handler)
    matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    if matter is None:
        telemetry.increment("annotated_pdf_export_failed")
        handler._send_json({"error": "NDA not found."}, status=404, send_body=send_body)
        return

    source_filename = str(matter.get("source_filename") or "")
    if not source_filename.lower().endswith(".pdf"):
        telemetry.increment("annotated_pdf_export_failed")
        handler._send_json(
            {"error": "Annotated PDF export is available only for PDF NDAs."},
            status=400,
            send_body=send_body,
        )
        return

    source_bytes = repository.get_source_document_bytes(matter)
    if source_bytes is None:
        telemetry.increment("annotated_pdf_export_failed")
        handler._send_json(
            {"error": "NDA source PDF is missing from storage."},
            status=400,
            send_body=send_body,
        )
        return

    annotation_count = 0
    fallback = False
    try:
        export = build_matter_annotated_pdf(
            matter_id, repository=repository, owner_user_id=owner_user_id
        )
        data = export.data
        filename = export.filename
        annotation_count = export.annotation_count
    except Exception:
        # The redline annotations could not be produced (no anchorable review
        # text, stale/missing review, missing optional dependency, render
        # failure). The whole point of this route is a working fallback after
        # the DOCX redline path failed -- so return the original source PDF.
        telemetry.increment("annotated_pdf_export_fallback_source")
        data = source_bytes
        filename = annotated_pdf_download_filename(source_filename)
        fallback = True

    handler._send_download(
        data,
        filename,
        ANNOTATED_PDF_MIME,
        headers={
            "X-Export-Verified": ANNOTATED_PDF_VERIFICATION_HEADER,
            "X-PDF-Annotation-Count": str(annotation_count),
            "X-PDF-Annotation-Fallback": "source-original" if fallback else "none",
        },
        send_body=send_body,
    )
