"""Tests for the DEFAULT-OFF scanned-PDF OCR fallback (nda_automation.pdf_ocr).

The OCR provider is STUBBED in every test -- no live OpenRouter call, no API key,
no network. The fitz rasterization runs for real (it is local + fast on tiny
fixtures) so the rasterize -> OCR -> assemble -> split path is exercised end to
end against a genuine image-only PDF.
"""

import importlib.util
import unittest
from io import BytesIO
from unittest.mock import patch

from nda_automation import pdf_ocr
from nda_automation.pdf_ocr import (
    DEFAULT_OCR_MODEL,
    MAX_OCR_PAGES,
    OcrError,
    OpenRouterVisionOcrProvider,
    ocr_enabled,
    ocr_pdf_text,
    ocr_status,
    resolve_ocr_provider,
)
from nda_automation.pdf_text import PdfExtractionError, extract_pdf_document

PYPDF_AVAILABLE = importlib.util.find_spec("pypdf") is not None
PYMUPDF_AVAILABLE = importlib.util.find_spec("fitz") is not None
requires_pymupdf = unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF is not installed")
requires_pypdf = unittest.skipUnless(PYPDF_AVAILABLE, "pypdf is not installed")


def make_image_only_pdf(pages: int = 1) -> bytes:
    """A PDF whose pages carry ONLY a raster image -- NO text layer at all.

    This is the scanned/image-only shape that hard-fails extract_pdf_document
    with the 'No readable text' error: pypdf's text extraction finds nothing, so
    the OCR fallback is the only way to recover any text.
    """
    import fitz

    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=612, height=792)
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4, 4), False)
        pixmap.set_rect(pixmap.irect, (10 * index, 20, 30))
        page.insert_image(fitz.Rect(50, 50, 560, 742), pixmap=pixmap)
    data = document.tobytes()
    document.close()
    return data


def make_text_pdf(text: str) -> bytes:
    """A normal text-layer PDF -- the fast path; OCR must NEVER run for it."""
    import fitz

    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 720), text, fontsize=12)
    data = document.tobytes()
    document.close()
    return data


class CountingProvider:
    """A deterministic stub OcrProvider that records how many pages it saw."""

    def __init__(self, text_per_page="Confidential Information means all disclosed data."):
        self.text_per_page = text_per_page
        self.calls = 0

    def __call__(self, image_png: bytes):
        self.calls += 1
        return f"{self.text_per_page} Page {self.calls}."


class OcrFallbackOnPathTests(unittest.TestCase):
    """A scanned PDF goes through OCR and yields reviewable text."""

    @requires_pymupdf
    def test_scanned_pdf_recovers_text_via_ocr_provider(self):
        data = make_image_only_pdf(pages=1)
        provider = CountingProvider()

        with patch.object(pdf_ocr, "resolve_ocr_provider", return_value=provider):
            extraction = extract_pdf_document(data)

        self.assertEqual(provider.calls, 1)
        self.assertGreaterEqual(len(extraction.paragraphs), 1)
        joined = " ".join(str(p["text"]) for p in extraction.paragraphs)
        self.assertIn("Confidential Information", joined)
        # Every recovered paragraph is flagged as OCR-sourced.
        self.assertTrue(all(p.get("ocr") is True for p in extraction.paragraphs))
        # Quality carries the ocr_recovered flag + a warning.
        self.assertTrue(extraction.quality.get("ocr_recovered"))
        warning_types = {w.get("type") for w in extraction.quality.get("warnings", [])}
        self.assertIn("pdf_ocr_recovered", warning_types)

    @requires_pymupdf
    def test_ocr_pdf_text_assembles_multiple_pages(self):
        data = make_image_only_pdf(pages=3)
        provider = CountingProvider()

        text = ocr_pdf_text(data, provider=provider)

        self.assertIsNotNone(text)
        self.assertEqual(provider.calls, 3)
        self.assertIn("Page 1.", text)
        self.assertIn("Page 3.", text)


class OcrFallbackOffPathTests(unittest.TestCase):
    """When OFF/unconfigured/failing the SAME clear error re-raises (never empty)."""

    @requires_pymupdf
    def test_disabled_scanned_pdf_still_rejects_with_clear_error(self):
        data = make_image_only_pdf(pages=1)

        # Default-OFF: resolve_ocr_provider returns None, OCR never runs.
        with patch.dict("os.environ", {}, clear=False):
            # Ensure the flag is not set in the test env.
            import os

            os.environ.pop("NDA_PDF_OCR_ENABLED", None)
            with self.assertRaisesRegex(PdfExtractionError, "No readable text"):
                extract_pdf_document(data)

    @requires_pymupdf
    def test_provider_failure_rejects_cleanly_not_empty(self):
        data = make_image_only_pdf(pages=1)

        def failing_provider(image_png: bytes):
            raise OcrError("provider down")

        with patch.object(pdf_ocr, "resolve_ocr_provider", return_value=failing_provider):
            # The clear scanned error must re-raise -- NOT an empty/garbage review.
            with self.assertRaisesRegex(PdfExtractionError, "No readable text"):
                extract_pdf_document(data)

    @requires_pymupdf
    def test_provider_returns_empty_rejects_cleanly(self):
        data = make_image_only_pdf(pages=1)

        def empty_provider(image_png: bytes):
            return "   "  # whitespace only -> treated as no text recovered

        with patch.object(pdf_ocr, "resolve_ocr_provider", return_value=empty_provider):
            with self.assertRaisesRegex(PdfExtractionError, "No readable text"):
                extract_pdf_document(data)

    @requires_pymupdf
    def test_ocr_pdf_text_returns_none_when_no_provider(self):
        data = make_image_only_pdf(pages=1)
        # No provider + OCR disabled -> resolve returns None -> ocr_pdf_text None.
        import os

        os.environ.pop("NDA_PDF_OCR_ENABLED", None)
        self.assertIsNone(ocr_pdf_text(data))


class OcrFastPathUntouchedTests(unittest.TestCase):
    """A text-layer PDF still uses the fast path; OCR is never invoked."""

    @requires_pypdf
    def test_text_pdf_does_not_trigger_ocr(self):
        data = make_text_pdf("This Agreement shall be governed by the laws of California.")
        provider = CountingProvider()

        with patch.object(pdf_ocr, "resolve_ocr_provider", return_value=provider):
            extraction = extract_pdf_document(data)

        # OCR provider was NEVER called -- the text layer was extracted directly.
        self.assertEqual(provider.calls, 0)
        self.assertFalse(extraction.quality.get("ocr_recovered"))
        self.assertTrue(all(not p.get("ocr") for p in extraction.paragraphs))


class OcrPageCapTests(unittest.TestCase):
    """The page cap is enforced -- a scanned bomb cannot fan out unbounded."""

    @requires_pymupdf
    def test_pages_ocr_capped_at_configured_max(self):
        data = make_image_only_pdf(pages=5)
        provider = CountingProvider()

        # Lower the cap to 2 via the env override; only 2 pages may be OCR'd.
        with patch.dict("os.environ", {"NDA_PDF_OCR_MAX_PAGES": "2"}):
            text = ocr_pdf_text(data, provider=provider)

        self.assertIsNotNone(text)
        self.assertEqual(provider.calls, 2)

    @requires_pymupdf
    def test_env_override_cannot_exceed_hard_ceiling(self):
        # An override ABOVE the hard ceiling is clamped DOWN to MAX_OCR_PAGES.
        with patch.dict("os.environ", {"NDA_PDF_OCR_MAX_PAGES": "9999"}):
            self.assertEqual(pdf_ocr._configured_max_pages(), MAX_OCR_PAGES)


class OcrConfigTests(unittest.TestCase):
    def test_default_off(self):
        import os

        os.environ.pop("NDA_PDF_OCR_ENABLED", None)
        self.assertFalse(ocr_enabled())

    def test_resolve_returns_none_when_disabled(self):
        import os

        os.environ.pop("NDA_PDF_OCR_ENABLED", None)
        self.assertIsNone(resolve_ocr_provider())

    def test_resolve_returns_none_when_enabled_but_no_key(self):
        with patch.dict("os.environ", {"NDA_PDF_OCR_ENABLED": "true"}):
            with patch.object(pdf_ocr, "_configured_api_key", return_value=""):
                self.assertIsNone(resolve_ocr_provider())

    def test_resolve_returns_provider_when_enabled_and_keyed(self):
        with patch.dict("os.environ", {"NDA_PDF_OCR_ENABLED": "1"}):
            with patch.object(pdf_ocr, "_configured_api_key", return_value="sk-test"):
                provider = resolve_ocr_provider()
        self.assertIsInstance(provider, OpenRouterVisionOcrProvider)

    def test_status_reports_config_without_secrets(self):
        with patch.dict("os.environ", {"NDA_PDF_OCR_ENABLED": "true"}):
            with patch.object(pdf_ocr, "_configured_api_key", return_value="sk-secret"):
                status = ocr_status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["configured"])
        self.assertEqual(status["model"], DEFAULT_OCR_MODEL)
        # The key value must never appear in the status payload.
        self.assertNotIn("sk-secret", str(status))


class OpenRouterVisionProviderTests(unittest.TestCase):
    """The vision provider builds a correct request and reads the reply -- no live HTTP."""

    def test_requires_api_key(self):
        with self.assertRaises(OcrError):
            OpenRouterVisionOcrProvider(api_key="", model=DEFAULT_OCR_MODEL, timeout_seconds=60)

    def test_transcribes_via_mocked_openrouter(self):
        provider = OpenRouterVisionOcrProvider(api_key="sk-test", model=DEFAULT_OCR_MODEL, timeout_seconds=30)

        class FakeResponse:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        captured = {}

        def fake_urlopen(request, timeout=None, context=None):
            captured["body"] = request.data
            captured["headers"] = dict(request.header_items())
            payload = {"choices": [{"message": {"content": "Recovered page text."}}]}
            import json

            return FakeResponse(json.dumps(payload).encode("utf-8"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = provider(b"\x89PNG fake image bytes")

        self.assertEqual(result, "Recovered page text.")
        # The request carried an image_url data URL and the Authorization header.
        import json

        body = json.loads(captured["body"].decode("utf-8"))
        content = body["messages"][1]["content"]
        kinds = {part.get("type") for part in content}
        self.assertIn("image_url", kinds)
        auth = {k.lower(): v for k, v in captured["headers"].items()}.get("Authorization".lower())
        self.assertEqual(auth, "Bearer sk-test")

    def test_http_error_raises_ocrerror_without_body(self):
        import urllib.error

        provider = OpenRouterVisionOcrProvider(api_key="sk-test", model=DEFAULT_OCR_MODEL, timeout_seconds=30)

        def raise_http(request, timeout=None, context=None):
            raise urllib.error.HTTPError(
                url="x", code=429, msg="Too Many", hdrs=None, fp=BytesIO(b"secret body")
            )

        with patch("urllib.request.urlopen", side_effect=raise_http):
            with self.assertRaises(OcrError) as ctx:
                provider(b"img")
        # The status code is reported; the response body is NOT echoed.
        self.assertIn("429", str(ctx.exception))
        self.assertNotIn("secret body", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
