from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import document_rendering
from .matter_repository import DiskMatterRepository, MatterRepository, MatterRepositoryError

DEFAULT_RENDER_STATUS_POLL_GRACE_SECONDS = 5.0


class MatterRenderJobError(RuntimeError):
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(str(payload.get("error") or "Matter render job failed."))
        self.payload = payload
        self.status = status
        self.headers = headers or {}


@dataclass(frozen=True)
class MatterRenderSource:
    matter: dict[str, Any]
    source_bytes: bytes
    source_filename: str
    owner_user_id: str


@dataclass(frozen=True)
class MatterRenderedDocument:
    matter: dict[str, Any]
    render_result: document_rendering.DocumentRenderResult

    @property
    def rendered(self) -> document_rendering.RenderedDocument:
        return self.render_result.rendered

    @property
    def page_manifest(self) -> document_rendering.RenderedPdfPageImageManifest | None:
        return self.render_result.page_manifest


@dataclass(frozen=True)
class MatterRenderFile:
    path: Path
    content_type: str


def render_status_payload(
    matter_id: str | None,
    *,
    owner_user_id: str = "",
    poll_grace_seconds: float | None = None,
    repository: MatterRepository | None = None,
) -> dict[str, Any]:
    if poll_grace_seconds is None:
        poll_grace_seconds = DEFAULT_RENDER_STATUS_POLL_GRACE_SECONDS
    resolved = resolve_matter_source(matter_id, owner_user_id=owner_user_id, repository=repository)
    matter = resolved.matter
    source_bytes = resolved.source_bytes
    source_filename = resolved.source_filename

    render_result = document_rendering.poll_source_document_render_result(
        matter_id or "",
        source_bytes,
        source_filename=source_filename,
        content_type=source_document_content_type(source_filename),
        owner_user_id=resolved.owner_user_id,
        wait_timeout_seconds=poll_grace_seconds,
    )
    if render_result is not None:
        return {
            "document_render": public_document_render(
                matter_id or "",
                render_result.rendered,
                matter=matter,
                page_manifest=render_result.page_manifest,
            )
        }
    return {"document_render": rendering_in_progress_payload(matter_id or "", source_filename)}


def render_pdf_file(
    matter_id: str | None,
    *,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
) -> MatterRenderFile:
    result = render_matter_document(
        matter_id,
        owner_user_id=owner_user_id,
        include_page_images=False,
        repository=repository,
    )
    rendered = result.rendered
    if rendered.status != document_rendering.READY_STATUS or rendered.pdf_path is None:
        error = rendered.error_message or "Rendered PDF is not available for this matter."
        raise MatterRenderJobError(
            {
                "error": error,
                "document_render": public_document_render(matter_id or "", rendered, matter=result.matter),
            },
            status=409,
        )
    return MatterRenderFile(path=rendered.pdf_path, content_type=document_rendering.PDF_CONTENT_TYPE)


def render_page_image_file(
    matter_id: str | None,
    page_number: int,
    *,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
) -> MatterRenderFile:
    result = render_matter_document(matter_id, owner_user_id=owner_user_id, repository=repository)
    rendered = result.rendered
    if rendered.status != document_rendering.READY_STATUS or rendered.pdf_path is None:
        error = rendered.error_message or "Rendered PDF is not available for this matter."
        raise MatterRenderJobError(
            {
                "error": error,
                "document_render": public_document_render(matter_id or "", rendered, matter=result.matter),
            },
            status=409,
        )
    page_manifest = result.page_manifest
    if page_manifest is None:
        page_manifest = document_rendering.document_render_result(rendered).page_manifest
    if page_manifest.status != document_rendering.READY_STATUS:
        raise MatterRenderJobError(
            {
                "error": page_manifest.error_message or "Rendered page image is not available for this NDA.",
                "document_render": public_document_render(
                    matter_id or "",
                    rendered,
                    matter=result.matter,
                    page_manifest=page_manifest,
                ),
            },
            status=409,
        )
    page = document_rendering.page_image_for_render_result(
        document_rendering.DocumentRenderResult(rendered=rendered, page_manifest=page_manifest),
        page_number,
    )
    if page is None or page.image_path is None:
        raise MatterRenderJobError(
            {
                "error": "Page image not found.",
                "document_render": public_document_render(
                    matter_id or "",
                    rendered,
                    matter=result.matter,
                    page_manifest=page_manifest,
                ),
            },
            status=404,
        )
    return MatterRenderFile(path=page.image_path, content_type=document_rendering.PAGE_IMAGE_CONTENT_TYPE)


def resolve_matter_source(
    matter_id: str | None,
    *,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
) -> MatterRenderSource:
    if matter_id is None:
        raise MatterRenderJobError({"error": "NDA not found."}, status=404)
    repository = repository or DiskMatterRepository()
    try:
        matter = repository.get_matter(matter_id, owner_user_id=owner_user_id)
    except MatterRepositoryError as error:
        raise MatterRenderJobError({"error": str(error)}, status=500) from error
    if matter is None:
        raise MatterRenderJobError({"error": "NDA not found."}, status=404)
    try:
        source_bytes = repository.get_source_document_bytes(matter)
    except MatterRepositoryError as error:
        raise MatterRenderJobError({"error": str(error)}, status=500) from error
    if source_bytes is None:
        raise MatterRenderJobError({"error": "No source document for this NDA."}, status=404)
    source_filename = str(matter.get("source_filename") or matter.get("stored_filename") or "")
    return MatterRenderSource(
        matter=matter,
        source_bytes=source_bytes,
        source_filename=source_filename,
        owner_user_id=str(matter.get("owner_user_id") or ""),
    )


def rendering_in_progress_payload(matter_id: str, source_filename: str) -> dict[str, Any]:
    source_kind = "pdf" if Path(source_filename).suffix.lower() == ".pdf" else "docx"
    return {
        "status": document_rendering.RENDERING_STATUS,
        "source_kind": source_kind,
        "source_label": "Original PDF" if source_kind == "pdf" else "Converted DOCX",
        "cached": False,
        "matter_id": matter_id,
    }


def render_matter_document(
    matter_id: str | None,
    *,
    owner_user_id: str = "",
    include_page_images: bool = True,
    repository: MatterRepository | None = None,
) -> MatterRenderedDocument:
    resolved = resolve_matter_source(matter_id, owner_user_id=owner_user_id, repository=repository)
    try:
        render_result = document_rendering.render_source_document_result(
            resolved.source_bytes,
            source_filename=resolved.source_filename,
            content_type=source_document_content_type(resolved.source_filename),
            owner_user_id=resolved.owner_user_id,
            include_page_images=include_page_images,
        )
    except document_rendering.DocxConverterBusy as error:
        raise MatterRenderJobError(
            {"error": error.message},
            status=503,
            headers={"Retry-After": "5"},
        ) from error
    return MatterRenderedDocument(matter=resolved.matter, render_result=render_result)


def source_document_content_type(source_filename: str | Path) -> str:
    suffix = Path(str(source_filename)).suffix.lower()
    if suffix == ".pdf":
        return document_rendering.PDF_CONTENT_TYPE
    if suffix == ".docx":
        return document_rendering.DOCX_CONTENT_TYPE
    return ""


def parse_matter_render_page_path(path: str) -> tuple[str, int] | None:
    prefix = "/api/matters/"
    marker = "/render-page/"
    if not path.startswith(prefix) or marker not in path:
        return None
    raw_matter_id, raw_page_number = path.removeprefix(prefix).split(marker, 1)
    matter_id = unquote(raw_matter_id).strip("/")
    if not matter_id or "/" in matter_id:
        return None
    page_number_value = raw_page_number.strip("/")
    if not page_number_value.isdigit():
        return None
    page_number = int(page_number_value)
    if page_number < 1:
        return None
    return matter_id, page_number


def public_document_render(
    matter_id: str,
    rendered: document_rendering.RenderedDocument,
    *,
    matter: dict[str, Any] | None = None,
    page_manifest: document_rendering.RenderedPdfPageImageManifest | None = None,
) -> dict[str, Any]:
    payload = {
        "status": rendered.status,
        "source_kind": rendered.source_kind,
        "source_label": "Original PDF" if rendered.source_kind == "pdf" else "Converted DOCX",
        "cached": rendered.cached,
        "cache_key": rendered.cache_key,
    }
    if rendered.status == document_rendering.READY_STATUS and rendered.pdf_path is not None:
        payload["pdf_url"] = f"/api/matters/{matter_id}/render-pdf"
        if page_manifest is None:
            page_manifest = document_rendering.document_render_result(rendered).page_manifest
        attach_public_page_image_manifest(payload, matter_id, page_manifest)
        if matter is not None:
            payload["document_overlay"] = public_document_overlay(matter, matter_id, page_manifest)
    if rendered.error_code:
        payload["error"] = rendered.error_message
        payload["error_code"] = rendered.error_code
    return payload


def attach_public_page_image_manifest(
    payload: dict[str, Any],
    matter_id: str,
    page_manifest: document_rendering.RenderedPdfPageImageManifest,
) -> None:
    public_page_images = public_page_image_manifest(matter_id, page_manifest)
    payload["page_images"] = public_page_images
    payload["page_image_status"] = page_manifest.status
    payload["pages"] = public_page_images["pages"]
    if page_manifest.dpi is not None:
        payload["dpi"] = page_manifest.dpi
    if page_manifest.scale is not None:
        payload["scale"] = page_manifest.scale
    if page_manifest.error_code:
        payload["page_image_error"] = page_manifest.error_message
        payload["page_image_error_code"] = page_manifest.error_code


def public_page_image_manifest(
    matter_id: str,
    page_manifest: document_rendering.RenderedPdfPageImageManifest,
) -> dict[str, Any]:
    payload = {
        "status": page_manifest.status,
        "cached": page_manifest.cached,
        "pages": [public_page_image(matter_id, page) for page in page_manifest.pages],
    }
    if page_manifest.dpi is not None:
        payload["dpi"] = page_manifest.dpi
    if page_manifest.scale is not None:
        payload["scale"] = page_manifest.scale
    if page_manifest.error_code:
        payload["error"] = page_manifest.error_message
        payload["error_code"] = page_manifest.error_code
    return payload


def public_page_image(matter_id: str, page: document_rendering.RenderedPdfPageImage) -> dict[str, Any]:
    payload = {
        "page_number": page.page_number,
        "image_url": f"/api/matters/{matter_id}/render-page/{page.page_number}",
    }
    if page.width is not None:
        payload["width"] = page.width
    if page.height is not None:
        payload["height"] = page.height
    if page.dpi is not None:
        payload["dpi"] = page.dpi
    if page.scale is not None:
        payload["scale"] = page.scale
    return payload


def public_document_overlay(
    matter: dict[str, Any],
    matter_id: str,
    page_manifest: document_rendering.RenderedPdfPageImageManifest,
) -> dict[str, Any]:
    public_pages = [public_page_image(matter_id, page) for page in page_manifest.pages]
    if page_manifest.status != document_rendering.READY_STATUS:
        return {
            "version": 1,
            "status": "unavailable",
            "precision": "none",
            "fallback_mode": "text_dom_scroll",
            "pages": public_pages,
            "anchors": [],
            "warnings": [page_manifest.error_message or "Page image metadata is unavailable."],
        }

    review_result = matter.get("review_result") if isinstance(matter.get("review_result"), dict) else {}
    paragraphs = review_result.get("paragraphs", []) if isinstance(review_result, dict) else []
    clauses = review_result.get("clauses", []) if isinstance(review_result, dict) else []
    redlines = review_result.get("redline_edits", []) if isinstance(review_result, dict) else []
    page_numbers = {page.page_number for page in page_manifest.pages}
    paragraphs_by_id = {
        str(paragraph.get("id")): paragraph
        for paragraph in paragraphs
        if isinstance(paragraph, dict) and paragraph.get("id") is not None
    }
    anchors: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for clause in clauses if isinstance(clauses, list) else []:
        if not isinstance(clause, dict):
            continue
        clause_id = str(clause.get("id") or "")
        matched_paragraph_ids = clause.get("matched_paragraph_ids", [])
        if not isinstance(matched_paragraph_ids, list):
            continue
        for paragraph_id in matched_paragraph_ids:
            paragraph_id = str(paragraph_id)
            anchor = page_level_overlay_anchor(
                paragraphs_by_id.get(paragraph_id),
                target_type="evidence",
                clause_id=clause_id,
                paragraph_id=paragraph_id,
                page_numbers=page_numbers,
            )
            if anchor is None:
                continue
            key = ("evidence", clause_id, paragraph_id)
            if key in seen:
                continue
            seen.add(key)
            anchors.append(anchor)

    for redline in redlines if isinstance(redlines, list) else []:
        if not isinstance(redline, dict):
            continue
        paragraph_id = str(redline.get("paragraph_id") or "")
        redline_id = str(redline.get("id") or "")
        anchor = page_level_overlay_anchor(
            paragraphs_by_id.get(paragraph_id),
            target_type="redline",
            clause_id=str(redline.get("clause_id") or ""),
            paragraph_id=paragraph_id,
            page_numbers=page_numbers,
            redline_id=redline_id,
        )
        if anchor is None:
            continue
        key = ("redline", redline_id, paragraph_id)
        if key in seen:
            continue
        seen.add(key)
        anchors.append(anchor)

    warnings: list[str] = []
    if not anchors:
        warnings.append("No page-level evidence anchors were available for this review.")
    return {
        "version": 1,
        "status": "partial" if anchors else "unavailable",
        "precision": "page" if anchors else "none",
        "fallback_mode": "text_dom_scroll",
        "pages": public_pages,
        "anchors": anchors,
        "warnings": warnings,
    }


def page_level_overlay_anchor(
    paragraph: dict[str, Any] | None,
    *,
    target_type: str,
    clause_id: str,
    paragraph_id: str,
    page_numbers: set[int],
    redline_id: str = "",
) -> dict[str, Any] | None:
    if not isinstance(paragraph, dict):
        return None
    page_number = paragraph.get("page_number")
    if not isinstance(page_number, int) or page_number not in page_numbers:
        return None
    anchor: dict[str, Any] = {
        "target_type": target_type,
        "clause_id": clause_id,
        "paragraph_id": paragraph_id,
        "page_number": page_number,
        "boxes": [],
        "confidence": 0.6,
        "confidence_reason": "Page-level match only; no verified text coordinates.",
        "fallback": {
            "mode": "text_dom_scroll",
            "selector": f"[data-paragraph-id=\"{paragraph_id}\"]",
        },
    }
    if redline_id:
        anchor["redline_id"] = redline_id
    return anchor
