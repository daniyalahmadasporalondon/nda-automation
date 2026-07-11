from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from nda_automation import document_rendering
from nda_automation.document_rendering import (
    DEFAULT_PAGE_IMAGE_DPI,
    DOCX_CONTENT_TYPE,
    MAX_PAGE_PIXMAP_BYTES,
    MAX_RASTERIZED_PAGES,
    MIN_PAGE_IMAGE_DPI,
    READY_STATUS,
    UNAVAILABLE_STATUS,
    DocumentRenderingError,
    DocxConverterBusy,
    LibreOfficeDocxConverter,
    PdfPageTooLargeToRasterize,
    PyMuPdfPageRenderer,
    RenderedPdfPageImage,
    _budgeted_page_dpi,
    _enforce_render_cache_bound,
    _run_soffice_command,
    _suppressed_mupdf_errors,
    cache_entry_dir,
    document_render_cache_key,
    purge_render_cache_for_source,
    render_pdf_page_image_manifest,
    render_source_document_result,
    render_source_document_to_pdf,
)


class UnavailableDocxConverter:
    name = "fake-unavailable"

    def is_available(self) -> bool:
        return False

    def convert_docx_to_pdf(self, source_path: Path, output_dir: Path, *, timeout_seconds: int) -> Path:
        raise AssertionError("Unavailable converter should not be invoked.")


class CountingDocxConverter:
    name = "fake-docx"

    def __init__(self) -> None:
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def convert_docx_to_pdf(self, source_path: Path, output_dir: Path, *, timeout_seconds: int) -> Path:
        self.calls += 1
        output_path = output_dir / "source.pdf"
        output_path.write_bytes(b"%PDF-1.7\nconverted\n")
        return output_path


class CountingPdfPageRenderer:
    name = "fake-pdf-pages"

    def __init__(self) -> None:
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def render_pdf_to_page_images(self, pdf_path: Path, output_dir: Path, *, dpi: int) -> list[RenderedPdfPageImage]:
        self.calls += 1
        image_path = output_dir / "page-1.png"
        image_path.write_bytes(b"\x89PNG\r\nfake page image\n")
        return [
            RenderedPdfPageImage(
                page_number=1,
                image_path=image_path,
                width=1224,
                height=1584,
                dpi=dpi,
                scale=2.0,
            )
        ]


class ColoredTablePdfDocxConverter:
    name = "fake-colored-table-pdf"

    def is_available(self) -> bool:
        return True

    def convert_docx_to_pdf(self, source_path: Path, output_dir: Path, *, timeout_seconds: int) -> Path:
        output_path = output_dir / "source.pdf"
        output_path.write_bytes(_colored_table_pdf_bytes())
        return output_path


class UnavailablePdfPageRenderer:
    name = "fake-page-unavailable"

    def is_available(self) -> bool:
        return False

    def render_pdf_to_page_images(self, pdf_path: Path, output_dir: Path, *, dpi: int) -> list[RenderedPdfPageImage]:
        raise AssertionError("Unavailable page renderer should not be invoked.")


def _fitz():
    try:
        import fitz
    except ImportError:
        return None
    return fitz


def _colored_table_pdf_bytes() -> bytes:
    fitz = _fitz()
    if fitz is None:
        raise unittest.SkipTest("PyMuPDF is not installed")
    document = fitz.open()
    page = document.new_page(width=216, height=216)
    page.draw_rect(fitz.Rect(36, 36, 108, 108), color=(0.8, 0, 0), fill=(0.8, 0, 0), width=1)
    page.draw_rect(fitz.Rect(108, 36, 180, 108), color=(0, 0, 0.8), fill=(0, 0, 0.8), width=1)
    page.draw_rect(fitz.Rect(36, 108, 108, 180), color=(0, 0.45, 0), fill=(0, 0.45, 0), width=1)
    page.draw_rect(fitz.Rect(108, 108, 180, 180), color=(0, 0, 0), fill=None, width=2)
    page.insert_text((46, 74), "Red", fontsize=14, color=(1, 1, 1))
    return document.tobytes()


def _png_contains_color(image_path: Path, predicate) -> bool:
    fitz = _fitz()
    if fitz is None:
        raise unittest.SkipTest("PyMuPDF is not installed")
    pixmap = fitz.Pixmap(str(image_path))
    channels = pixmap.n
    samples = pixmap.samples
    for index in range(0, len(samples), channels):
        r, g, b = samples[index], samples[index + 1], samples[index + 2]
        if predicate(r, g, b):
            return True
    return False


class DocumentRenderingTests(unittest.TestCase):
    def test_pdf_passthrough_writes_cache_and_reuses_existing_artifact(self):
        pdf_bytes = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
        with tempfile.TemporaryDirectory() as cache_dir_name:
            cache_dir = Path(cache_dir_name)

            first = render_source_document_to_pdf(pdf_bytes, source_filename="Source NDA.pdf", cache_dir=cache_dir)
            second = render_source_document_to_pdf(pdf_bytes, source_filename="Renamed NDA.pdf", cache_dir=cache_dir)

            self.assertEqual(first.status, READY_STATUS)
            self.assertFalse(first.cached)
            self.assertEqual(first.source_kind, "pdf")
            self.assertIsNotNone(first.pdf_path)
            self.assertEqual(first.pdf_path.read_bytes(), pdf_bytes)

            self.assertEqual(second.status, READY_STATUS)
            self.assertTrue(second.cached)
            self.assertEqual(second.cache_key, first.cache_key)
            self.assertEqual(second.pdf_path, first.pdf_path)

            metadata = json.loads(first.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], READY_STATUS)
            self.assertEqual(metadata["source_sha256"], first.source_sha256)
            self.assertEqual(metadata["source_kind"], "pdf")
            self.assertEqual(metadata["artifact_path"], f"{first.cache_key}/document.pdf")

    def test_docx_converter_unavailable_returns_clear_status_without_crashing(self):
        docx_bytes = b"not a real docx but enough for extension based routing"
        with tempfile.TemporaryDirectory() as cache_dir_name:
            cache_dir = Path(cache_dir_name)

            result = render_source_document_to_pdf(
                docx_bytes,
                source_filename="Source NDA.docx",
                content_type=DOCX_CONTENT_TYPE,
                cache_dir=cache_dir,
                converter=UnavailableDocxConverter(),
            )

            self.assertEqual(result.status, UNAVAILABLE_STATUS)
            self.assertEqual(result.source_kind, "docx")
            self.assertIsNone(result.pdf_path)
            self.assertEqual(result.error_code, "converter_unavailable")
            self.assertIn("LibreOffice/soffice", result.error_message)
            self.assertTrue(result.metadata_path.is_file())

            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], UNAVAILABLE_STATUS)
            self.assertEqual(metadata["error"]["code"], "converter_unavailable")
            self.assertEqual(metadata["converter"]["name"], "fake-unavailable")

    def test_docx_conversion_uses_deterministic_cache_key_and_reuses_pdf(self):
        docx_bytes = b"PK\x03\x04docx-ish"
        converter = CountingDocxConverter()
        with tempfile.TemporaryDirectory() as cache_dir_name:
            cache_dir = Path(cache_dir_name)

            first = render_source_document_to_pdf(
                docx_bytes,
                source_filename="Source NDA.docx",
                cache_dir=cache_dir,
                converter=converter,
            )
            second = render_source_document_to_pdf(
                docx_bytes,
                source_filename="Source NDA.docx",
                cache_dir=cache_dir,
                converter=converter,
            )

            self.assertEqual(first.status, READY_STATUS)
            self.assertFalse(first.cached)
            self.assertEqual(second.status, READY_STATUS)
            self.assertTrue(second.cached)
            self.assertEqual(converter.calls, 1)
            self.assertEqual(first.cache_key, document_render_cache_key(docx_bytes, source_kind="docx"))
            self.assertEqual(second.pdf_path.read_bytes(), b"%PDF-1.7\nconverted\n")

    def test_pdf_page_manifest_renders_pages_with_fake_renderer_and_reuses_cache(self):
        pdf_bytes = b"%PDF-1.7\nsource pdf\n%%EOF\n"
        renderer = CountingPdfPageRenderer()
        with tempfile.TemporaryDirectory() as cache_dir_name:
            cache_dir = Path(cache_dir_name)
            rendered = render_source_document_to_pdf(pdf_bytes, source_filename="Source NDA.pdf", cache_dir=cache_dir)

            manifest = render_pdf_page_image_manifest(rendered, renderer=renderer, dpi=144)
            cached_manifest = render_pdf_page_image_manifest(rendered, renderer=UnavailablePdfPageRenderer(), dpi=144)

            self.assertEqual(manifest.status, READY_STATUS)
            self.assertFalse(manifest.cached)
            self.assertEqual(renderer.calls, 1)
            self.assertEqual(manifest.dpi, 144)
            self.assertEqual(manifest.scale, 2.0)
            self.assertEqual(len(manifest.pages), 1)
            self.assertEqual(manifest.pages[0].page_number, 1)
            self.assertEqual(manifest.pages[0].width, 1224)
            self.assertEqual(manifest.pages[0].height, 1584)
            self.assertTrue(manifest.pages[0].image_path.is_file())
            self.assertEqual(manifest.pages[0].image_path.read_bytes(), b"\x89PNG\r\nfake page image\n")
            self.assertTrue(manifest.manifest_path.is_file())

            self.assertEqual(cached_manifest.status, READY_STATUS)
            self.assertTrue(cached_manifest.cached)
            self.assertEqual(cached_manifest.pages[0].image_path, manifest.pages[0].image_path)

            metadata = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], READY_STATUS)
            self.assertEqual(metadata["artifact_kind"], "pdf_page_images")
            self.assertEqual(metadata["dpi"], 144)
            self.assertEqual(metadata["scale"], 2.0)
            self.assertEqual(metadata["renderer"]["name"], "fake-pdf-pages")
            self.assertEqual(metadata["pages"][0]["page_number"], 1)
            self.assertEqual(metadata["pages"][0]["image_path"], f"{rendered.cache_key}/pages/page-1.png")

    def test_source_path_render_result_owns_page_manifest_cache(self):
        pdf_bytes = b"%PDF-1.7\nsource pdf\n%%EOF\n"
        renderer = CountingPdfPageRenderer()
        with tempfile.TemporaryDirectory() as cache_dir_name:
            cache_dir = Path(cache_dir_name)
            source_path = cache_dir / "Source NDA.pdf"
            source_path.write_bytes(pdf_bytes)

            # PDF-only cache hits are not enough for the higher-level result:
            # page geometry has to be ready too.
            rendered_pdf = document_rendering.render_source_path_to_pdf(source_path, cache_dir=cache_dir)
            self.assertIsNone(document_rendering.peek_source_path_render_result(source_path, cache_dir=cache_dir))

            result = document_rendering.render_source_path_result(
                source_path,
                cache_dir=cache_dir,
                page_renderer=renderer,
                dpi=144,
            )
            cached = document_rendering.peek_source_path_render_result(source_path, cache_dir=cache_dir, dpi=144)

            self.assertEqual(result.rendered.cache_key, rendered_pdf.cache_key)
            self.assertEqual(result.rendered.status, READY_STATUS)
            self.assertIsNotNone(result.page_manifest)
            self.assertEqual(result.page_manifest.status, READY_STATUS)
            self.assertEqual(renderer.calls, 1)
            self.assertIsNotNone(cached)
            self.assertTrue(cached.rendered.cached)
            self.assertTrue(cached.page_manifest.cached)
            self.assertEqual(cached.page_manifest.pages[0].image_path, result.page_manifest.pages[0].image_path)

    def test_pdf_page_render_preserves_colored_table_pixels(self):
        if _fitz() is None:
            self.skipTest("PyMuPDF is not installed")
        with tempfile.TemporaryDirectory() as cache_dir_name:
            result = render_source_document_result(
                _colored_table_pdf_bytes(),
                source_filename="colored-table.pdf",
                cache_dir=Path(cache_dir_name),
                page_renderer=PyMuPdfPageRenderer(),
                dpi=144,
            )

            self.assertEqual(result.rendered.status, READY_STATUS)
            self.assertIsNotNone(result.page_manifest)
            self.assertEqual(result.page_manifest.status, READY_STATUS)
            image_path = result.page_manifest.pages[0].image_path
            self.assertTrue(_png_contains_color(image_path, lambda r, g, b: r > 150 and g < 80 and b < 80))
            self.assertTrue(_png_contains_color(image_path, lambda r, g, b: b > 150 and r < 80 and g < 80))

    def test_docx_conversion_page_render_preserves_converted_pdf_colored_table_pixels(self):
        if _fitz() is None:
            self.skipTest("PyMuPDF is not installed")
        with tempfile.TemporaryDirectory() as cache_dir_name:
            result = render_source_document_result(
                b"PK\x03\x04docx-ish",
                source_filename="colored-table.docx",
                content_type=DOCX_CONTENT_TYPE,
                cache_dir=Path(cache_dir_name),
                converter=ColoredTablePdfDocxConverter(),
                page_renderer=PyMuPdfPageRenderer(),
                dpi=144,
            )

            self.assertEqual(result.rendered.status, READY_STATUS)
            self.assertEqual(result.rendered.source_kind, "docx")
            self.assertIsNotNone(result.page_manifest)
            self.assertEqual(result.page_manifest.status, READY_STATUS)
            image_path = result.page_manifest.pages[0].image_path
            self.assertTrue(_png_contains_color(image_path, lambda r, g, b: r > 150 and g < 80 and b < 80))
            self.assertTrue(_png_contains_color(image_path, lambda r, g, b: b > 150 and r < 80 and g < 80))

    def test_pdf_page_manifest_reports_renderer_unavailable_without_crashing(self):
        pdf_bytes = b"%PDF-1.7\nsource pdf\n%%EOF\n"
        with tempfile.TemporaryDirectory() as cache_dir_name:
            cache_dir = Path(cache_dir_name)
            rendered = render_source_document_to_pdf(pdf_bytes, source_filename="Source NDA.pdf", cache_dir=cache_dir)

            manifest = render_pdf_page_image_manifest(rendered, renderer=UnavailablePdfPageRenderer(), dpi=144)

            self.assertEqual(manifest.status, UNAVAILABLE_STATUS)
            self.assertEqual(manifest.pages, ())
            self.assertEqual(manifest.error_code, "page_renderer_unavailable")
            self.assertIn("PyMuPDF/fitz", manifest.error_message)
            self.assertTrue(manifest.manifest_path.is_file())

            metadata = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], UNAVAILABLE_STATUS)
            self.assertEqual(metadata["error"]["code"], "page_renderer_unavailable")
            self.assertEqual(metadata["renderer"]["name"], "fake-page-unavailable")

    def test_cache_key_changes_by_source_bytes_kind_and_version(self):
        source_bytes = b"same bytes"

        pdf_key = document_render_cache_key(source_bytes, source_kind="pdf")
        same_pdf_key = document_render_cache_key(source_bytes, source_kind="pdf")
        docx_key = document_render_cache_key(source_bytes, source_kind="docx")
        changed_bytes_key = document_render_cache_key(b"different bytes", source_kind="pdf")
        changed_version_key = document_render_cache_key(source_bytes, source_kind="pdf", cache_version="document-rendering:v2")

        self.assertEqual(pdf_key, same_pdf_key)
        self.assertNotEqual(pdf_key, docx_key)
        self.assertNotEqual(pdf_key, changed_bytes_key)
        self.assertNotEqual(pdf_key, changed_version_key)


class _FakeRect:
    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height


class _FakePixmap:
    """Pixmap stand-in whose dimensions follow the requested scale.

    It records the largest pixel area it was ever asked to materialize so a
    test can assert the renderer never demanded an out-of-budget pixmap.
    """

    max_area_seen = 0

    def __init__(self, width_pts: float, height_pts: float, scale: float) -> None:
        self.width = max(1, int(round(width_pts * scale)))
        self.height = max(1, int(round(height_pts * scale)))
        type(self).max_area_seen = max(type(self).max_area_seen, self.width * self.height)

    def tobytes(self, _fmt: str) -> bytes:
        return b"\x89PNG\r\nfake\n"


# Sentinel mirroring fitz.csRGB so tests can assert the renderer pins RGB.
_FAKE_CSRGB = object()


class _FakePage:
    def __init__(self, width_pts: float, height_pts: float) -> None:
        self.rect = _FakeRect(width_pts, height_pts)
        # Records the colorspace passed at the last get_pixmap call.
        self.last_colorspace: object = None

    def get_pixmap(self, *, matrix, colorspace=None, alpha=False):  # noqa: ANN001 - mirrors fitz signature
        self.last_colorspace = colorspace
        return _FakePixmap(self.rect.width, self.rect.height, matrix.scale)


class _FakeMatrix:
    def __init__(self, sx: float, sy: float) -> None:
        self.scale = sx


class _FakeDocument:
    def __init__(self, pages: list[_FakePage]) -> None:
        self._pages = pages
        self.page_count = len(pages)

    def load_page(self, index: int) -> _FakePage:
        return self._pages[index]

    def close(self) -> None:
        pass


class _FakeFitzModule:
    def __init__(self, pages: list[_FakePage]) -> None:
        self._pages = pages
        self.Matrix = _FakeMatrix
        self.csRGB = _FAKE_CSRGB

    def open(self, _path: str) -> _FakeDocument:
        return _FakeDocument(self._pages)


class BudgetedPageDpiTests(unittest.TestCase):
    def test_default_page_image_dpi_is_screen_legible(self):
        # 200 DPI is the preview default: visually indistinguishable from 288 at
        # display zoom, but ~48% cheaper to rasterize and cache. It must stay in
        # the legible screen-preview band and within the clamp bounds.
        self.assertEqual(DEFAULT_PAGE_IMAGE_DPI, 200)
        self.assertGreaterEqual(DEFAULT_PAGE_IMAGE_DPI, 150)
        self.assertLessEqual(DEFAULT_PAGE_IMAGE_DPI, document_rendering.MAX_PAGE_IMAGE_DPI)
        self.assertGreaterEqual(DEFAULT_PAGE_IMAGE_DPI, document_rendering.MIN_PAGE_IMAGE_DPI)

    def test_letter_and_a4_pages_fit_default_dpi_budget(self):
        for width_pts, height_pts in ((612, 792), (595, 842)):
            with self.subTest(width_pts=width_pts, height_pts=height_pts):
                self.assertEqual(
                    _budgeted_page_dpi(width_pts, height_pts, requested_dpi=DEFAULT_PAGE_IMAGE_DPI),
                    DEFAULT_PAGE_IMAGE_DPI,
                )

    def test_returns_requested_dpi_when_page_fits_budget(self):
        # US Letter (612x792 pts) at 288 DPI is ~23 MB, well under budget.
        self.assertEqual(_budgeted_page_dpi(612, 792, requested_dpi=288), 288)

    def test_zero_or_unknown_rect_renders_at_requested_dpi(self):
        self.assertEqual(_budgeted_page_dpi(0, 0, requested_dpi=192), 192)

    def test_clamps_dpi_down_so_pixmap_stays_within_budget(self):
        # A ~49x49 inch (3528 pt) MediaBox is ~597 MB at 288 DPI but fits the
        # budget at a reduced DPI; the clamp must cut DPI without rejecting, and
        # the resulting pixmap must respect the byte budget.
        clamped = _budgeted_page_dpi(3528, 3528, requested_dpi=288)
        self.assertLess(clamped, 288)
        self.assertGreaterEqual(clamped, MIN_PAGE_IMAGE_DPI)
        area_inches = (3528 / 72) * (3528 / 72)
        pixmap_bytes = area_inches * (clamped**2) * 3
        self.assertLessEqual(pixmap_bytes, MAX_PAGE_PIXMAP_BYTES)

    def test_rejects_page_that_cannot_fit_even_at_floor_dpi(self):
        # Pathological MediaBox: even MIN_PAGE_IMAGE_DPI overflows the budget.
        with self.assertRaises(PdfPageTooLargeToRasterize):
            _budgeted_page_dpi(200000, 200000, requested_dpi=288)

    def test_never_returns_more_than_requested_even_for_tiny_page(self):
        self.assertEqual(_budgeted_page_dpi(10, 10, requested_dpi=72), 72)


class PyMuPdfRasterizationBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakePixmap.max_area_seen = 0

    def test_attacker_mediabox_is_downscaled_not_rasterized_at_full_dpi(self):
        page = _FakePage(3528, 3528)
        renderer = PyMuPdfPageRenderer(fitz_module=_FakeFitzModule([page]))
        with tempfile.TemporaryDirectory() as out_name:
            pages = renderer.render_pdf_to_page_images(Path(out_name), Path(out_name), dpi=288)
        self.assertEqual(len(pages), 1)
        self.assertLess(pages[0].dpi, 288)
        # The realized pixmap must respect the per-page byte budget (RGB = 3B/px).
        self.assertLessEqual(_FakePixmap.max_area_seen * 3, MAX_PAGE_PIXMAP_BYTES)

    def test_normal_page_renders_at_requested_dpi(self):
        page = _FakePage(612, 792)
        renderer = PyMuPdfPageRenderer(fitz_module=_FakeFitzModule([page]))
        with tempfile.TemporaryDirectory() as out_name:
            pages = renderer.render_pdf_to_page_images(Path(out_name), Path(out_name), dpi=288)
        self.assertEqual(pages[0].dpi, 288)

    def test_render_produces_valid_image_at_default_dpi(self):
        # The default DPI must still render a valid (PNG-magic) page image.
        page = _FakePage(612, 792)
        renderer = PyMuPdfPageRenderer(fitz_module=_FakeFitzModule([page]))
        with tempfile.TemporaryDirectory() as out_name:
            pages = renderer.render_pdf_to_page_images(
                Path(out_name), Path(out_name), dpi=DEFAULT_PAGE_IMAGE_DPI
            )
            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0].dpi, DEFAULT_PAGE_IMAGE_DPI)
            self.assertGreater(pages[0].width, 0)
            self.assertGreater(pages[0].height, 0)
            self.assertTrue(pages[0].image_path.exists())
            self.assertTrue(pages[0].image_path.read_bytes().startswith(b"\x89PNG"))

    def test_pixmap_is_rendered_in_rgb_colorspace(self):
        # The colorspace must be pinned to fitz.csRGB so the 3-channel byte budget
        # is enforced rather than merely assumed.
        page = _FakePage(612, 792)
        fitz = _FakeFitzModule([page])
        renderer = PyMuPdfPageRenderer(fitz_module=fitz)
        with tempfile.TemporaryDirectory() as out_name:
            renderer.render_pdf_to_page_images(Path(out_name), Path(out_name), dpi=288)
        self.assertIs(page.last_colorspace, fitz.csRGB)
        # The budget assumes 3 channels (RGB), matching the pinned colorspace.
        self.assertEqual(document_rendering.RASTERIZED_PIXMAP_CHANNELS, 3)


class DefaultPageImageDpiResolutionTests(unittest.TestCase):
    """The preview DPI default is overridable via NDA_PAGE_IMAGE_DPI."""

    def _resolve_with_env(self, value: str | None) -> int:
        prior = os.environ.get(document_rendering.PAGE_IMAGE_DPI_ENV_VAR)
        try:
            if value is None:
                os.environ.pop(document_rendering.PAGE_IMAGE_DPI_ENV_VAR, None)
            else:
                os.environ[document_rendering.PAGE_IMAGE_DPI_ENV_VAR] = value
            return document_rendering._resolve_default_page_image_dpi()
        finally:
            if prior is None:
                os.environ.pop(document_rendering.PAGE_IMAGE_DPI_ENV_VAR, None)
            else:
                os.environ[document_rendering.PAGE_IMAGE_DPI_ENV_VAR] = prior

    def test_unset_env_uses_fallback(self):
        self.assertEqual(
            self._resolve_with_env(None),
            document_rendering.DEFAULT_PAGE_IMAGE_DPI_FALLBACK,
        )

    def test_blank_env_uses_fallback(self):
        self.assertEqual(
            self._resolve_with_env("   "),
            document_rendering.DEFAULT_PAGE_IMAGE_DPI_FALLBACK,
        )

    def test_valid_override_is_respected(self):
        self.assertEqual(self._resolve_with_env("150"), 150)

    def test_garbage_env_falls_back(self):
        self.assertEqual(
            self._resolve_with_env("not-a-number"),
            document_rendering.DEFAULT_PAGE_IMAGE_DPI_FALLBACK,
        )

    def test_nonpositive_env_falls_back(self):
        self.assertEqual(
            self._resolve_with_env("0"),
            document_rendering.DEFAULT_PAGE_IMAGE_DPI_FALLBACK,
        )
        self.assertEqual(
            self._resolve_with_env("-50"),
            document_rendering.DEFAULT_PAGE_IMAGE_DPI_FALLBACK,
        )

    def test_override_is_clamped_into_bounds(self):
        self.assertEqual(self._resolve_with_env("99999"), document_rendering.MAX_PAGE_IMAGE_DPI)
        self.assertEqual(
            self._resolve_with_env(str(document_rendering.MIN_PAGE_IMAGE_DPI - 1)),
            document_rendering.MIN_PAGE_IMAGE_DPI,
        )

    def test_override_dpi_is_honored_end_to_end(self):
        # An overridden DPI must flow through to the realized page image.
        page = _FakePage(612, 792)
        renderer = PyMuPdfPageRenderer(fitz_module=_FakeFitzModule([page]))
        override = self._resolve_with_env("180")
        self.assertEqual(override, 180)
        with tempfile.TemporaryDirectory() as out_name:
            pages = renderer.render_pdf_to_page_images(Path(out_name), Path(out_name), dpi=override)
        self.assertEqual(pages[0].dpi, 180)

    def test_page_count_over_cap_is_rejected(self):
        pages = [_FakePage(612, 792) for _ in range(MAX_RASTERIZED_PAGES + 1)]
        renderer = PyMuPdfPageRenderer(fitz_module=_FakeFitzModule(pages))
        with tempfile.TemporaryDirectory() as out_name:
            with self.assertRaises(PdfPageTooLargeToRasterize):
                renderer.render_pdf_to_page_images(Path(out_name), Path(out_name), dpi=192)

    def test_manifest_reports_page_too_large_without_crashing(self):
        pdf_bytes = b"%PDF-1.7\nsource pdf\n%%EOF\n"
        page = _FakePage(200000, 200000)
        renderer = PyMuPdfPageRenderer(fitz_module=_FakeFitzModule([page]))
        with tempfile.TemporaryDirectory() as cache_dir_name:
            cache_dir = Path(cache_dir_name)
            rendered = render_source_document_to_pdf(pdf_bytes, source_filename="Source NDA.pdf", cache_dir=cache_dir)
            manifest = render_pdf_page_image_manifest(rendered, renderer=renderer, dpi=192)
        self.assertEqual(manifest.error_code, "page_too_large_to_rasterize")
        self.assertEqual(manifest.pages, ())


class _RecordingMupdfTools:
    """Mimics fitz.TOOLS.mupdf_display_errors with a settable/gettable flag."""

    def __init__(self) -> None:
        self.state = True  # MuPDF default: errors displayed
        self.history: list[bool] = []

    def mupdf_display_errors(self, on=None):  # noqa: ANN001 - mirrors fitz signature
        if on is None:
            return self.state
        self.state = bool(on)
        self.history.append(self.state)
        return self.state


class _RecordingFitzModule(_FakeFitzModule):
    """Fake fitz that also exposes a TOOLS object and records the toggle state
    that was live at pixmap time, so tests can prove the suppression scope."""

    def __init__(self, pages: list[_FakePage]) -> None:
        super().__init__(pages)
        self.TOOLS = _RecordingMupdfTools()
        self.display_during_pixmap: list[bool] = []
        tools = self.TOOLS
        observed = self.display_during_pixmap
        for page in pages:
            original_get_pixmap = page.get_pixmap

            def get_pixmap(*args, _orig=original_get_pixmap, **kwargs):
                observed.append(tools.state)
                return _orig(*args, **kwargs)

            page.get_pixmap = get_pixmap  # type: ignore[method-assign]


class MupdfErrorSuppressionTests(unittest.TestCase):
    def test_mupdf_errors_silenced_during_rasterize_and_restored_after(self):
        page = _FakePage(612, 792)
        fitz = _RecordingFitzModule([page])
        renderer = PyMuPdfPageRenderer(fitz_module=fitz)
        with tempfile.TemporaryDirectory() as out_name:
            pages = renderer.render_pdf_to_page_images(Path(out_name), Path(out_name), dpi=288)
        self.assertEqual(len(pages), 1)
        # The pixmap was produced while MuPDF error display was OFF.
        self.assertEqual(fitz.display_during_pixmap, [False])
        # ...and the prior (default-True) state was restored afterwards.
        self.assertTrue(fitz.TOOLS.mupdf_display_errors())

    def test_state_restored_even_when_rasterize_raises(self):
        # Page count over cap raises mid-scope; the toggle must still be restored.
        pages = [_FakePage(612, 792) for _ in range(MAX_RASTERIZED_PAGES + 1)]
        fitz = _RecordingFitzModule(pages)
        renderer = PyMuPdfPageRenderer(fitz_module=fitz)
        with tempfile.TemporaryDirectory() as out_name:
            with self.assertRaises(PdfPageTooLargeToRasterize):
                renderer.render_pdf_to_page_images(Path(out_name), Path(out_name), dpi=288)
        self.assertTrue(fitz.TOOLS.mupdf_display_errors())

    def test_context_manager_fails_open_without_tools(self):
        # A fitz build lacking TOOLS must not break rendering.
        module = _FakeFitzModule([])
        self.assertFalse(hasattr(module, "TOOLS"))
        entered = False
        with _suppressed_mupdf_errors(module):
            entered = True
        self.assertTrue(entered)

    def test_context_manager_restores_prior_state(self):
        tools = _RecordingMupdfTools()
        tools.state = True

        class _Mod:
            TOOLS = tools

        with _suppressed_mupdf_errors(_Mod()):
            self.assertFalse(tools.state)  # OFF inside scope
        self.assertTrue(tools.state)  # restored to prior value outside


class SofficeConcurrencyAndTimeoutTests(unittest.TestCase):
    def test_busy_when_no_conversion_slot_is_available(self):
        # Drain every conversion slot so the next acquire cannot succeed, then
        # assert the converter sheds load with DocxConverterBusy within the
        # (shrunk) queue wait rather than blocking indefinitely.
        semaphore = document_rendering._SOFFICE_CONVERSION_SEMAPHORE
        acquired = [semaphore.acquire(blocking=False) for _ in range(document_rendering.MAX_CONCURRENT_SOFFICE_CONVERSIONS)]
        original_wait = document_rendering.CONVERSION_QUEUE_WAIT_SECONDS
        document_rendering.CONVERSION_QUEUE_WAIT_SECONDS = 0.05
        try:
            self.assertTrue(all(acquired))
            converter = LibreOfficeDocxConverter(executable="/nonexistent/soffice")
            with tempfile.TemporaryDirectory() as work_name:
                work = Path(work_name)
                (work / "source.docx").write_bytes(b"PK\x03\x04")
                start = time.monotonic()
                with self.assertRaises(DocxConverterBusy):
                    converter.convert_docx_to_pdf(work / "source.docx", work, timeout_seconds=5)
                # Did not block on a full slot for anywhere near a real timeout.
                self.assertLess(time.monotonic() - start, 2.0)
        finally:
            document_rendering.CONVERSION_QUEUE_WAIT_SECONDS = original_wait
            for ok in acquired:
                if ok:
                    semaphore.release()

    def test_slot_is_released_after_conversion_error(self):
        # A failing conversion must not leak its semaphore slot.
        converter = LibreOfficeDocxConverter(executable="/nonexistent/soffice")
        with tempfile.TemporaryDirectory() as work_name:
            work = Path(work_name)
            (work / "source.docx").write_bytes(b"PK\x03\x04")
            with self.assertRaises(DocumentRenderingError):
                converter.convert_docx_to_pdf(work / "source.docx", work, timeout_seconds=5)
        # All slots free again: we can acquire the full count without blocking.
        semaphore = document_rendering._SOFFICE_CONVERSION_SEMAPHORE
        grabbed = [semaphore.acquire(blocking=False) for _ in range(document_rendering.MAX_CONCURRENT_SOFFICE_CONVERSIONS)]
        try:
            self.assertTrue(all(grabbed))
        finally:
            for ok in grabbed:
                if ok:
                    semaphore.release()

    def test_busy_propagates_and_is_not_persisted_as_a_render_failure(self):
        # Busy is transient backpressure: it must reach the caller and must NOT
        # poison the cache with a permanent failure metadata file.
        semaphore = document_rendering._SOFFICE_CONVERSION_SEMAPHORE
        acquired = [semaphore.acquire(blocking=False) for _ in range(document_rendering.MAX_CONCURRENT_SOFFICE_CONVERSIONS)]
        original_wait = document_rendering.CONVERSION_QUEUE_WAIT_SECONDS
        document_rendering.CONVERSION_QUEUE_WAIT_SECONDS = 0.05
        try:
            converter = LibreOfficeDocxConverter(executable="/nonexistent/soffice")
            with tempfile.TemporaryDirectory() as cache_name:
                cache_dir = Path(cache_name)
                with self.assertRaises(DocxConverterBusy):
                    render_source_document_to_pdf(
                        b"PK\x03\x04docx-ish",
                        source_filename="Source NDA.docx",
                        content_type=DOCX_CONTENT_TYPE,
                        cache_dir=cache_dir,
                        converter=converter,
                    )
                # No failure metadata persisted anywhere under the cache root.
                self.assertEqual(list(cache_dir.rglob("metadata.json")), [])
        finally:
            document_rendering.CONVERSION_QUEUE_WAIT_SECONDS = original_wait
            for ok in acquired:
                if ok:
                    semaphore.release()

    @unittest.skipUnless(os.name == "posix", "process-group kill is POSIX-only")
    def test_timeout_kills_the_whole_process_group(self):
        # A hung conversion forks a child that outlives the parent; the timeout
        # must SIGKILL the entire process group, not just the direct child.
        marker_dir = tempfile.mkdtemp()
        child_pid_file = Path(marker_dir) / "child.pid"
        # Parent spawns a long-lived background child, records its PID, then
        # sleeps far past the timeout. If only the parent were killed, the child
        # would survive and we could still signal it.
        script = (
            f"import os, sys, time, subprocess\n"
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(child_pid_file)!r}, 'w').write(str(child.pid))\n"
            f"time.sleep(30)\n"
        )
        start = time.monotonic()
        with self.assertRaises(DocumentRenderingError) as ctx:
            _run_soffice_command([sys.executable, "-c", script], cwd=marker_dir, timeout_seconds=1)
        self.assertEqual(ctx.exception.code, "conversion_timeout")
        # Killed promptly, well under the 30s sleeps.
        self.assertLess(time.monotonic() - start, 10.0)
        # Give the SIGKILL a moment to reap the group, then confirm the forked
        # child is gone (kill(pid, 0) raises ProcessLookupError when reaped).
        deadline = time.monotonic() + 5.0
        child_pid = None
        while time.monotonic() < deadline:
            if child_pid_file.is_file():
                try:
                    child_pid = int(child_pid_file.read_text().strip())
                    break
                except ValueError:
                    pass
            time.sleep(0.05)
        self.assertIsNotNone(child_pid, "child PID was never recorded")
        child_alive = True
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                child_alive = False
                break
            time.sleep(0.05)
        if child_alive:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        self.assertFalse(child_alive, "forked child survived the process-group kill")

    @unittest.skipUnless(os.name == "posix", "RLIMIT preexec is POSIX-only")
    def test_preexec_applies_address_space_rlimit_in_child(self):
        # The child must come up with RLIMIT_AS reduced to our cap (where the
        # platform honors it). Run a probe through the same launch path and read
        # back its own soft limit.
        import resource as _resource

        probe = "import resource,sys; print(resource.getrlimit(resource.RLIMIT_AS)[0])"
        returncode, stdout_bytes, _stderr = _run_soffice_command(
            [sys.executable, "-c", probe], cwd=os.getcwd(), timeout_seconds=10
        )
        self.assertEqual(returncode, 0)
        child_soft = int(stdout_bytes.decode().strip())
        if child_soft == _resource.RLIM_INFINITY:
            self.skipTest("platform does not honor RLIMIT_AS (e.g. macOS)")
        self.assertLessEqual(child_soft, document_rendering.CONVERSION_MEMORY_LIMIT_BYTES)


class RenderCachePartitionEvictionPurgeTests(unittest.TestCase):
    def test_owner_partitions_cache_no_cross_tenant_hit(self):
        # Two tenants render byte-identical documents; each must get its OWN
        # entry (cached=False both times) and distinct cache keys, so neither
        # can read the other's rendered artifact.
        pdf_bytes = b"%PDF-1.7\nshared\n%%EOF\n"
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            alice = render_source_document_to_pdf(
                pdf_bytes, source_filename="nda.pdf", cache_dir=cache_dir, owner_user_id="alice"
            )
            bob = render_source_document_to_pdf(
                pdf_bytes, source_filename="nda.pdf", cache_dir=cache_dir, owner_user_id="bob"
            )
            self.assertNotEqual(alice.cache_key, bob.cache_key)
            self.assertFalse(alice.cached)
            self.assertFalse(bob.cached)  # bob did NOT hit alice's entry

            # Same tenant re-render IS a cache hit (dedup still works per-user).
            alice_again = render_source_document_to_pdf(
                pdf_bytes, source_filename="nda.pdf", cache_dir=cache_dir, owner_user_id="alice"
            )
            self.assertTrue(alice_again.cached)
            self.assertEqual(alice_again.cache_key, alice.cache_key)

    def test_lru_eviction_bounds_cache_and_keeps_recent(self):
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            # Seed more entry dirs than the cap with increasing mtimes.
            for i in range(5):
                entry = cache_dir / f"pdf-{i:064d}"
                entry.mkdir(parents=True)
                os.utime(entry, (1000 + i, 1000 + i))
            kept = cache_dir / ("pdf-" + "f" * 64)
            kept.mkdir()
            os.utime(kept, (2000, 2000))  # newest

            _enforce_render_cache_bound(cache_dir, keep=kept, max_entries=3)

            remaining = sorted(p.name for p in cache_dir.iterdir() if p.is_dir())
            self.assertEqual(len(remaining), 3)
            # Newest survivors kept; oldest evicted; the explicit keep survives.
            self.assertIn(kept.name, remaining)
            self.assertIn(f"pdf-{4:064d}", remaining)
            self.assertNotIn(f"pdf-{0:064d}", remaining)

    def test_render_path_evicts_when_over_cap(self):
        # Drive eviction through the real render entrypoint: render more distinct
        # documents than the cap (via a tiny per-call cap) and confirm the cache
        # never exceeds it.
        import nda_automation.document_rendering as module

        original_cap = module.MAX_RENDER_CACHE_ENTRIES
        module.MAX_RENDER_CACHE_ENTRIES = 3
        try:
            with tempfile.TemporaryDirectory() as cache_name:
                cache_dir = Path(cache_name)
                for i in range(8):
                    render_source_document_to_pdf(
                        f"%PDF-1.7\ndoc-{i}\n%%EOF\n".encode(),
                        source_filename=f"nda-{i}.pdf",
                        cache_dir=cache_dir,
                        owner_user_id="alice",
                    )
                    time.sleep(0.01)  # keep mtimes monotonic for deterministic LRU
                entries = [p for p in cache_dir.iterdir() if p.is_dir()]
                self.assertLessEqual(len(entries), 3)
        finally:
            module.MAX_RENDER_CACHE_ENTRIES = original_cap

    def test_cache_hit_refreshes_recency_so_it_survives_eviction(self):
        import nda_automation.document_rendering as module

        original_cap = module.MAX_RENDER_CACHE_ENTRIES
        module.MAX_RENDER_CACHE_ENTRIES = 3
        try:
            with tempfile.TemporaryDirectory() as cache_name:
                cache_dir = Path(cache_name)
                first_bytes = b"%PDF-1.7\nfirst\n%%EOF\n"
                first = render_source_document_to_pdf(
                    first_bytes, source_filename="first.pdf", cache_dir=cache_dir, owner_user_id="alice"
                )
                time.sleep(0.01)
                # Add two more, then re-touch `first` via a cache hit so it is no
                # longer the oldest.
                for i in range(2):
                    render_source_document_to_pdf(
                        f"%PDF-1.7\nfiller-{i}\n%%EOF\n".encode(),
                        source_filename=f"f{i}.pdf",
                        cache_dir=cache_dir,
                        owner_user_id="alice",
                    )
                    time.sleep(0.01)
                hit = render_source_document_to_pdf(
                    first_bytes, source_filename="first.pdf", cache_dir=cache_dir, owner_user_id="alice"
                )
                self.assertTrue(hit.cached)
                time.sleep(0.01)
                # One more push triggers eviction; `first` was just touched, so a
                # filler should be evicted instead.
                render_source_document_to_pdf(
                    b"%PDF-1.7\nfiller-evict\n%%EOF\n",
                    source_filename="evict.pdf",
                    cache_dir=cache_dir,
                    owner_user_id="alice",
                )
                first_entry = cache_entry_dir(cache_dir, first.cache_key)
                self.assertTrue(first_entry.is_dir(), "recently-hit entry was wrongly evicted")
        finally:
            module.MAX_RENDER_CACHE_ENTRIES = original_cap

    def test_purge_removes_only_the_owners_entry(self):
        pdf_bytes = b"%PDF-1.7\nshared\n%%EOF\n"
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            alice = render_source_document_to_pdf(
                pdf_bytes, source_filename="nda.pdf", cache_dir=cache_dir, owner_user_id="alice"
            )
            bob = render_source_document_to_pdf(
                pdf_bytes, source_filename="nda.pdf", cache_dir=cache_dir, owner_user_id="bob"
            )
            self.assertTrue(cache_entry_dir(cache_dir, alice.cache_key).is_dir())
            self.assertTrue(cache_entry_dir(cache_dir, bob.cache_key).is_dir())

            removed = purge_render_cache_for_source(
                pdf_bytes, owner_user_id="alice", source_filename="nda.pdf", cache_dir=cache_dir
            )
            self.assertGreaterEqual(removed, 1)
            # Alice's entry gone; Bob's untouched (no cross-tenant purge).
            self.assertFalse(cache_entry_dir(cache_dir, alice.cache_key).is_dir())
            self.assertTrue(cache_entry_dir(cache_dir, bob.cache_key).is_dir())


class MatterRenderCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        from nda_automation.document_rendering import MatterRenderCoordinator

        self.coordinator = MatterRenderCoordinator()

    def test_concurrent_pollers_share_one_in_flight_render(self):
        # Many simultaneous pollers for the same matter must trigger exactly ONE
        # render (in-flight de-dup), not one per poller.
        calls = []
        calls_lock = threading.Lock()
        release = threading.Event()

        def slow_render():
            with calls_lock:
                calls.append(1)
            release.wait(timeout=5)
            return "done"

        jobs = []
        barrier = threading.Barrier(8)

        def poll():
            barrier.wait()
            jobs.append(self.coordinator.ensure_in_flight("matter_1", slow_render))

        pollers = [threading.Thread(target=poll) for _ in range(8)]
        for t in pollers:
            t.start()
        # Let all pollers attach, then release the single render.
        time.sleep(0.1)
        self.assertEqual(sum(calls), 1, "render ran more than once for one matter")
        release.set()
        for t in pollers:
            t.join(timeout=5)
        # All pollers observed the same job object.
        self.assertEqual(len({id(j) for j in jobs}), 1)
        jobs[0].thread.join(timeout=5)
        self.assertTrue(jobs[0].is_finished())
        self.assertEqual(jobs[0].result, "done")

    def test_different_matters_render_independently(self):
        release = threading.Event()

        def render():
            release.wait(timeout=5)
            return "ok"

        job_a = self.coordinator.ensure_in_flight("matter_a", render)
        job_b = self.coordinator.ensure_in_flight("matter_b", render)
        self.assertIsNot(job_a, job_b)
        self.assertIsNotNone(self.coordinator.in_flight("matter_a"))
        self.assertIsNotNone(self.coordinator.in_flight("matter_b"))
        release.set()
        job_a.thread.join(timeout=5)
        job_b.thread.join(timeout=5)

    def test_finished_job_is_dropped_so_a_later_render_can_start(self):
        first = self.coordinator.ensure_in_flight("matter_1", lambda: "first")
        first.thread.join(timeout=5)
        self.assertTrue(first.is_finished())
        # Registry cleared after completion -> next call starts a NEW job.
        self.assertIsNone(self.coordinator.in_flight("matter_1"))
        second = self.coordinator.ensure_in_flight("matter_1", lambda: "second")
        second.thread.join(timeout=5)
        self.assertIsNot(first, second)
        self.assertEqual(second.result, "second")

    def test_render_error_is_captured_not_raised(self):
        def boom():
            raise RuntimeError("render blew up")

        job = self.coordinator.ensure_in_flight("matter_1", boom)
        job.thread.join(timeout=5)
        self.assertTrue(job.is_finished())
        self.assertIsInstance(job.error, RuntimeError)

    def _error_render_result(self) -> document_rendering.DocumentRenderResult:
        rendered = document_rendering.RenderedDocument(
            status=document_rendering.ERROR_STATUS,
            cache_key="cache-key",
            source_sha256="sha",
            source_kind="docx",
            cache_dir=Path("."),
            error_code="conversion_failed",
            error_message="soffice exploded",
        )
        return document_rendering.DocumentRenderResult(rendered=rendered, page_manifest=None)

    def test_terminal_failure_is_retained_and_next_poll_does_not_respawn(self):
        # A render that FAILS terminally (error-status result) between polls must
        # be retained so the next ensure_in_flight surfaces the SAME failed job
        # instead of kicking off a fresh render (the "rendering forever" loop).
        result = self._error_render_result()
        calls = []

        def render():
            calls.append(1)
            return result

        first = self.coordinator.ensure_in_flight("matter_1", render, identity="v1")
        first.thread.join(timeout=5)
        self.assertTrue(first.is_terminal_failure())
        # Job finished -> in_flight is None, but the retained failure is returned
        # (same object, no second render call).
        self.assertIsNone(self.coordinator.in_flight("matter_1"))
        second = self.coordinator.ensure_in_flight("matter_1", render, identity="v1")
        self.assertIs(second, first)
        self.assertEqual(sum(calls), 1, "a retained terminal failure must not re-render")

    def test_changed_source_identity_discards_retained_failure(self):
        # If the matter's source changes (new identity) after a failure, the stale
        # failure is discarded so the fixed/replaced document can render.
        failing = self._error_render_result()

        def render_fail():
            return failing

        def render_ok():
            return "second-render"

        first = self.coordinator.ensure_in_flight("matter_1", render_fail, identity="v1")
        first.thread.join(timeout=5)
        self.assertTrue(first.is_terminal_failure())
        second = self.coordinator.ensure_in_flight("matter_1", render_ok, identity="v2")
        second.thread.join(timeout=5)
        self.assertIsNot(second, first)
        self.assertEqual(second.result, "second-render")

    def test_transient_none_result_is_not_retained_as_failure(self):
        # A None result (e.g. converter busy) is transient, not terminal: it must
        # NOT be retained -- the next poll should start a fresh render.
        def busy():
            return None

        first = self.coordinator.ensure_in_flight("matter_1", busy, identity="v1")
        first.thread.join(timeout=5)
        self.assertFalse(first.is_terminal_failure())
        second = self.coordinator.ensure_in_flight("matter_1", lambda: "retry", identity="v1")
        second.thread.join(timeout=5)
        self.assertIsNot(second, first)
        self.assertEqual(second.result, "retry")

    def test_poll_surfaces_terminal_status_on_next_poll_not_rendering_forever(self):
        # End-to-end: a DOCX whose converter is unavailable fails deterministically.
        # The FIRST poll returns the terminal error result; a SUBSEQUENT poll must
        # ALSO return a terminal (non-"rendering") result -- surfaced from the
        # retained failure -- rather than re-spawning the render and looping.
        document_rendering.matter_render_coordinator().reset_for_tests()

        class _CountingUnavailable:
            name = "counting-unavailable"

            def __init__(self) -> None:
                self.checks = 0

            def is_available(self) -> bool:
                self.checks += 1
                return False

            def convert_docx_to_pdf(self, *args, **kwargs):
                raise AssertionError("Unavailable converter must not convert.")

        converter = _CountingUnavailable()
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)

            def poll():
                return document_rendering.poll_source_document_render_result(
                    "matter_err",
                    b"PK\x03\x04 not-a-real-docx",
                    source_filename="stub.docx",
                    content_type=DOCX_CONTENT_TYPE,
                    cache_dir=cache_dir,
                    converter=converter,
                    owner_user_id="alice",
                    wait_timeout_seconds=5,
                )

            first = poll()
            self.assertIsNotNone(first, "the terminal failure must be surfaced, not None")
            self.assertNotEqual(first.rendered.status, READY_STATUS)
            self.assertNotEqual(first.rendered.status, document_rendering.RENDERING_STATUS)

            second = poll()
            self.assertIsNotNone(second, "next poll must surface the retained terminal error")
            self.assertNotEqual(second.rendered.status, document_rendering.RENDERING_STATUS)
            # Retained failure => the render did NOT run a second time.
            self.assertEqual(converter.checks, 1, "the failed render must not be re-spawned")
        document_rendering.matter_render_coordinator().reset_for_tests()

    def test_peek_returns_none_until_rendered_then_the_cached_doc(self):
        pdf_bytes = b"%PDF-1.7\npeek\n%%EOF\n"
        with tempfile.TemporaryDirectory() as cache_name:
            cache_dir = Path(cache_name)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                handle.write(pdf_bytes)
                source_path = Path(handle.name)
            try:
                # Nothing rendered yet -> peek is None (no side effects).
                self.assertIsNone(
                    document_rendering.peek_rendered_document(source_path, cache_dir=cache_dir, owner_user_id="alice")
                )
                # Render, then peek finds the cached doc without re-rendering.
                render_source_document_to_pdf(
                    pdf_bytes, source_filename="peek.pdf", cache_dir=cache_dir, owner_user_id="alice"
                )
                peeked = document_rendering.peek_rendered_document(
                    source_path, cache_dir=cache_dir, owner_user_id="alice"
                )
                self.assertIsNotNone(peeked)
                self.assertEqual(peeked.status, READY_STATUS)
                self.assertTrue(peeked.cached)
                # A different tenant still peeks None (per-user partition holds).
                self.assertIsNone(
                    document_rendering.peek_rendered_document(source_path, cache_dir=cache_dir, owner_user_id="bob")
                )
            finally:
                source_path.unlink(missing_ok=True)


class DocumentRenderingDeploymentConfigTests(unittest.TestCase):
    def test_render_blueprint_uses_docker_runtime_for_system_rendering_dependencies(self):
        repo_root = Path(__file__).resolve().parents[1]
        render_yaml = (repo_root / "render.yaml").read_text(encoding="utf-8")

        self.assertIn("runtime: docker", render_yaml)
        self.assertNotIn("buildCommand: python -m pip install", render_yaml)

    def test_dockerfile_bundles_libreoffice_and_metric_compatible_fonts(self):
        repo_root = Path(__file__).resolve().parents[1]
        dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")

        for package in (
            "libreoffice-writer",
            "fontconfig",
            "fonts-crosextra-carlito",
            "fonts-crosextra-caladea",
            "fonts-liberation",
            "fonts-noto-core",
        ):
            with self.subTest(package=package):
                self.assertIn(package, dockerfile)
        self.assertIn("fc-cache -f", dockerfile)
        self.assertNotIn("ttf-mscorefonts-installer", dockerfile)

    def test_dockerfile_smoke_asserts_fitz_imports(self):
        # A broken/missing fitz must FAIL THE BUILD, not ship a silently-degraded
        # image that renders blank pages.
        repo_root = Path(__file__).resolve().parents[1]
        dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("import fitz", dockerfile)
        self.assertIn("fitz.open()", dockerfile)

    def test_pyproject_pins_pymupdf_to_known_good_window(self):
        # The [pdf] extra must not leave PyMuPDF open at >=1.24 (that let the
        # co-resolution land a broken fitz). It must carry a bounded pin.
        repo_root = Path(__file__).resolve().parents[1]
        pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn('"PyMuPDF>=1.24"', pyproject)
        self.assertIn("PyMuPDF>=1.26.7,<1.28", pyproject)


class LoadFitzModuleLoggingTests(unittest.TestCase):
    def test_logs_error_when_fitz_installed_but_import_fails(self):
        # A broken (installed-but-unimportable) fitz is the silent-blank-page root
        # cause; it must leave a loud ERROR trail, not just return None.
        import builtins

        real_import = builtins.__import__

        def exploding_import(name, *args, **kwargs):
            if name == "fitz":
                raise ImportError("libmupdf.so: undefined symbol")
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch.object(builtins, "__import__", exploding_import):
            with self.assertLogs(document_rendering.LOGGER, level="ERROR") as captured:
                self.assertIsNone(document_rendering._load_fitz_module())
        joined = "\n".join(captured.output)
        self.assertIn("failed to import", joined)
        self.assertIn("undefined symbol", joined)

    def test_logs_only_debug_when_fitz_genuinely_absent(self):
        import builtins

        real_import = builtins.__import__

        def missing_import(name, *args, **kwargs):
            if name == "fitz":
                raise ModuleNotFoundError("No module named 'fitz'")
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch.object(builtins, "__import__", missing_import):
            # A genuine absence must NOT raise an ERROR (only DEBUG): assertLogs at
            # ERROR raises if nothing is logged at that level.
            with self.assertRaises(AssertionError):
                with self.assertLogs(document_rendering.LOGGER, level="ERROR"):
                    self.assertIsNone(document_rendering._load_fitz_module())


class StorageExhaustionClassifierTests(unittest.TestCase):
    def test_enospc_and_erofs_are_storage_exhaustion(self):
        import errno as _errno

        for name in ("ENOSPC", "EROFS", "EDQUOT"):
            code = getattr(_errno, name, None)
            if code is None:
                continue
            with self.subTest(errno=name):
                exc = OSError(code, os.strerror(code))
                self.assertTrue(document_rendering._is_storage_exhaustion_error(exc))

    def test_generic_io_error_is_not_storage_exhaustion(self):
        import errno as _errno

        exc = OSError(_errno.EACCES, os.strerror(_errno.EACCES))
        self.assertFalse(document_rendering._is_storage_exhaustion_error(exc))


if __name__ == "__main__":
    unittest.main()
