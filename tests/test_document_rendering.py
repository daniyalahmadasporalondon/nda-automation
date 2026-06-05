from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nda_automation.document_rendering import (
    DOCX_CONTENT_TYPE,
    READY_STATUS,
    UNAVAILABLE_STATUS,
    RenderedPdfPageImage,
    document_render_cache_key,
    render_pdf_page_image_manifest,
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


class UnavailablePdfPageRenderer:
    name = "fake-page-unavailable"

    def is_available(self) -> bool:
        return False

    def render_pdf_to_page_images(self, pdf_path: Path, output_dir: Path, *, dpi: int) -> list[RenderedPdfPageImage]:
        raise AssertionError("Unavailable page renderer should not be invoked.")


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


if __name__ == "__main__":
    unittest.main()
