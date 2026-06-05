from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nda_automation.document_rendering import (
    DOCX_CONTENT_TYPE,
    READY_STATUS,
    UNAVAILABLE_STATUS,
    document_render_cache_key,
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
