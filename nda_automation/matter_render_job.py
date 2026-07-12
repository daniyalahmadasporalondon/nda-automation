from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import artifact_registry, document_rendering
from .matter_repository import DiskMatterRepository, MatterRepository, MatterRepositoryError

DEFAULT_RENDER_STATUS_POLL_GRACE_SECONDS = 5.0

# Phase 2 async render. When enabled, the byte-serving render GETs
# (/render-pdf, /render-page/{n}) NO LONGER run the (slow) soffice/rasterize
# pipeline inline on the HTTP request thread. Instead they serve a warm cache
# hit immediately, or -- on a cache miss -- schedule the render in the shared
# background coordinator (the same one /render-status already drives) and shed
# the request with 503 + Retry-After so the web thread is freed. The FE already
# polls /render-status (with capped re-polling) and only fetches the bytes once
# the status reports ready, at which point the byte GET is a warm-cache hit.
#
# Default OFF: byte-identical to the historical inline behavior. This flag gates
# a change to the CORE render request model, so it must never alter behavior
# when unset.
ASYNC_RENDER_ENV = "NDA_ASYNC_RENDER"


def async_render_enabled() -> bool:
    """True when NDA_ASYNC_RENDER selects the off-thread byte-serving render path."""
    return os.environ.get(ASYNC_RENDER_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class MatterRenderJobError(RuntimeError):
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(str(payload.get("error") or "NDA render job failed."))
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
        document_render = public_document_render(
            matter_id or "",
            render_result.rendered,
            matter=matter,
            page_manifest=render_result.page_manifest,
        )
    else:
        document_render = rendering_in_progress_payload(matter_id or "", source_filename)
    # FE seam (frozen): the PDF faithful-render branch lights up when a canonical
    # working DOCX (Approach C) exists for this matter. Reported in BOTH the ready and
    # in-progress payloads so the FE can switch as soon as the converted DOCX is ready,
    # independent of the page-image render state.
    document_render["working_docx_ready"] = matter_has_working_docx(matter)
    return {"document_render": document_render}


def matter_has_working_docx(matter: dict[str, Any] | None) -> bool:
    """True when the matter carries a persisted role="working" DOCX artifact.

    The artifact's presence is the readiness signal: ``register_working_docx`` stores
    the bytes before appending the record, so a registered working artifact always has
    retrievable bytes. A native DOCX matter (or a PDF whose ingest conversion failed)
    has none -> False.
    """
    if not isinstance(matter, dict):
        return False
    return artifact_registry.latest_artifact_for_role(matter, artifact_registry.ROLE_WORKING) is not None


def render_pdf_file(
    matter_id: str | None,
    *,
    owner_user_id: str = "",
    repository: MatterRepository | None = None,
    cache_dir: Path | None = None,
) -> MatterRenderFile:
    result = _rendered_document_for_serving(
        matter_id,
        owner_user_id=owner_user_id,
        include_page_images=False,
        repository=repository,
        cache_dir=cache_dir,
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
    cache_dir: Path | None = None,
) -> MatterRenderFile:
    result = _rendered_document_for_serving(
        matter_id,
        owner_user_id=owner_user_id,
        include_page_images=True,
        repository=repository,
        cache_dir=cache_dir,
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
    cache_dir: Path | None = None,
) -> MatterRenderedDocument:
    resolved = resolve_matter_source(matter_id, owner_user_id=owner_user_id, repository=repository)
    try:
        render_result = document_rendering.render_source_document_result(
            resolved.source_bytes,
            source_filename=resolved.source_filename,
            content_type=source_document_content_type(resolved.source_filename),
            owner_user_id=resolved.owner_user_id,
            include_page_images=include_page_images,
            cache_dir=cache_dir,
        )
    except document_rendering.DocxConverterBusy as error:
        raise MatterRenderJobError(
            {"error": error.message},
            status=503,
            headers={"Retry-After": "5"},
        ) from error
    return MatterRenderedDocument(matter=resolved.matter, render_result=render_result)


def _rendered_document_for_serving(
    matter_id: str | None,
    *,
    owner_user_id: str = "",
    include_page_images: bool = True,
    repository: MatterRepository | None = None,
    cache_dir: Path | None = None,
) -> MatterRenderedDocument:
    """Obtain the rendered document a byte-serving GET needs.

    Flag OFF (default): identical to ``render_matter_document`` -- the render runs
    INLINE on the caller's thread (byte-identical to the historical behavior).

    Flag ON (``NDA_ASYNC_RENDER``): the slow soffice/rasterize pipeline never runs
    on this thread. A warm cache hit is returned immediately; a cache miss
    schedules the render in the shared background coordinator and sheds the
    request with 503 + Retry-After (see ``_peek_or_schedule_render``).
    """
    if not async_render_enabled():
        return render_matter_document(
            matter_id,
            owner_user_id=owner_user_id,
            include_page_images=include_page_images,
            repository=repository,
            cache_dir=cache_dir,
        )
    resolved = resolve_matter_source(matter_id, owner_user_id=owner_user_id, repository=repository)
    render_result = _peek_or_schedule_render(
        resolved,
        matter_id or "",
        include_page_images=include_page_images,
        cache_dir=cache_dir,
    )
    return MatterRenderedDocument(matter=resolved.matter, render_result=render_result)


def _peek_or_schedule_render(
    resolved: MatterRenderSource,
    matter_id: str,
    *,
    include_page_images: bool,
    cache_dir: Path | None = None,
) -> document_rendering.DocumentRenderResult:
    """Return a warm cached render, or schedule a background render and shed (503).

    Never runs the (slow) convert/rasterize on the caller's thread:

    * A ready cache entry is returned immediately (read-only peek).
    * On a miss, a single background render is ensured for this matter (the same
      per-matter de-duped coordinator, on-disk cache, and concurrency semaphores
      that /render-status already uses) and a ``MatterRenderJobError`` with status
      503 + ``Retry-After`` is raised so the web thread is freed. The FE re-polls
      /render-status and re-fetches the bytes once the status reports ready.

    The background render always includes page images so a later /render-status
    peek (which requires the page manifest) becomes a hit, matching the status
    endpoint's readiness contract.
    """
    content_type = source_document_content_type(resolved.source_filename)
    cached = document_rendering.peek_source_document_render_result(
        resolved.source_bytes,
        source_filename=resolved.source_filename,
        content_type=content_type,
        cache_dir=cache_dir,
        owner_user_id=resolved.owner_user_id,
        include_page_images=include_page_images,
    )
    if cached is not None:
        return cached
    document_rendering.ensure_source_document_render_in_flight(
        matter_id,
        resolved.source_bytes,
        source_filename=resolved.source_filename,
        content_type=content_type,
        cache_dir=cache_dir,
        owner_user_id=resolved.owner_user_id,
        include_page_images=True,
    )
    raise MatterRenderJobError(
        {
            "error": "Document render is in progress.",
            "document_render": rendering_in_progress_payload(matter_id, resolved.source_filename),
        },
        status=503,
        headers={"Retry-After": "5"},
    )


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
