"""Tests for the scanned-PDF OCR fallback seam (DEFAULT-OFF).

Two layers:
- OFF: with the fallback disabled (the default), a scanned/image-only PDF rejects
  with the UNCHANGED "Scanned PDFs need OCR" message -- byte-for-byte the old path.
- ON (mocked): with the fallback enabled and a deterministic provider injected
  across the real ``resolve_ocr_provider`` seam, a scanned PDF flows through the
  TEXT-ONLY paragraph path and yields reviewable paragraphs.

The HTTP layer of the LLMWhisperer client is mocked end-to-end (submit -> poll ->
retrieve); no live key or endpoint is ever contacted.
"""
import importlib.util
import io
import json
import unittest
from unittest.mock import patch

from nda_automation import ocr_fallback
from nda_automation.ocr_fallback import (
    DEFAULT_OCR_MODE,
    OCR_ENV_API_KEY,
    OCR_ENV_BASE_URL,
    OCR_ENV_ENABLED,
    OCR_ENV_PROVIDER,
    LLMWhispererClient,
    OcrError,
    ocr_fallback_enabled,
    ocr_fallback_status,
    resolve_ocr_provider,
)
from nda_automation.pdf_text import PdfExtractionError, extract_pdf_document

PYPDF_AVAILABLE = importlib.util.find_spec("pypdf") is not None
requires_pypdf = unittest.skipUnless(PYPDF_AVAILABLE, "pypdf is not installed")

# A non-free Pro / self-hosted style base URL used throughout the ON tests.
_PRO_BASE_URL = "https://llmwhisperer.internal.example.com"
_FREE_BASE_URL = "https://llmwhisperer-api.us-central.unstract.com"


def _scanned_pdf_bytes() -> bytes:
    """A valid PDF with NO text layer -- a blank page -- i.e. the scanned case that
    reaches the OCR-reject branch in ``extract_pdf_document``."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with io.BytesIO() as output:
        writer.write(output)
        return output.getvalue()


# A representative layout-preserving OCR result with the LLMWhisperer ``<<<`` page
# separator between two pages.
_OCR_TEXT = (
    "MUTUAL NON-DISCLOSURE AGREEMENT\n"
    "This Agreement is entered into between the parties.\n"
    "<<<\n"
    "1. Confidential Information means any information disclosed by one party\n"
    "to the other.\n"
)


class _FakeOcrProvider:
    """Deterministic OCR provider injected across the real resolve_ocr_provider seam."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def __call__(self, data):
        self.calls.append(data)
        return self.text


# --------------------------------------------------------------------------------
# DEFAULT-OFF: reject behaviour is UNCHANGED.
# --------------------------------------------------------------------------------
class OcrFallbackOffTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(ocr_fallback_enabled())
            self.assertIsNone(resolve_ocr_provider())

    @requires_pypdf
    def test_scanned_pdf_rejects_unchanged_when_off(self):
        data = _scanned_pdf_bytes()
        with patch.dict("os.environ", {OCR_ENV_ENABLED: ""}, clear=False):
            with self.assertRaisesRegex(PdfExtractionError, "Scanned PDFs need OCR"):
                extract_pdf_document(data)

    @requires_pypdf
    def test_scanned_pdf_rejects_unchanged_even_with_keys_but_flag_off(self):
        # Endpoint + key fully configured, but the flag is OFF. The REAL resolver runs
        # (not mocked) and must return None purely because the flag is off -> the old
        # reject stands and no OCR HTTP is ever attempted (proven by the no-network
        # _urlopen guard, which would raise if called).
        data = _scanned_pdf_bytes()
        env = {
            OCR_ENV_ENABLED: "false",
            OCR_ENV_BASE_URL: _PRO_BASE_URL,
            OCR_ENV_API_KEY: "secret-key",
        }

        def _no_network(*_args, **_kwargs):
            raise AssertionError("OCR HTTP must not be attempted when the flag is OFF")

        with patch.dict("os.environ", env, clear=False):
            with patch.object(ocr_fallback, "_urlopen", _no_network):
                with self.assertRaisesRegex(PdfExtractionError, "Scanned PDFs need OCR"):
                    extract_pdf_document(data)


# --------------------------------------------------------------------------------
# ON (mocked): fallback recovers paragraphs through the text-only path.
# --------------------------------------------------------------------------------
class OcrFallbackOnTests(unittest.TestCase):
    @requires_pypdf
    def test_scanned_pdf_recovered_via_ocr_when_on(self):
        data = _scanned_pdf_bytes()
        provider = _FakeOcrProvider(_OCR_TEXT)
        env = {OCR_ENV_ENABLED: "true"}
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ocr_fallback, "resolve_ocr_provider", return_value=provider):
                extraction = extract_pdf_document(data)

        # The provider saw the raw PDF bytes.
        self.assertEqual(provider.calls, [data])
        texts = [p["text"] for p in extraction.paragraphs]
        joined = "\n".join(texts)
        self.assertIn("MUTUAL NON-DISCLOSURE AGREEMENT", joined)
        self.assertIn("Confidential Information", joined)
        # Page separator drove a real page break.
        self.assertEqual(extraction.paragraphs[0]["page_number"], 1)
        self.assertTrue(any(p["page_number"] == 2 for p in extraction.paragraphs))
        # Every recovered paragraph is flagged as OCR-sourced.
        self.assertTrue(all(p.get("ocr") for p in extraction.paragraphs))
        # Quality flags the OCR recovery.
        self.assertTrue(extraction.quality["ocr_recovered"])
        warning_types = {w["type"] for w in extraction.quality["warnings"]}
        self.assertIn("pdf_text_recovered_via_ocr", warning_types)

    @requires_pypdf
    def test_provider_failure_falls_back_to_unchanged_reject(self):
        data = _scanned_pdf_bytes()

        def _boom(_data):
            raise OcrError("transport down")

        env = {OCR_ENV_ENABLED: "true"}
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ocr_fallback, "resolve_ocr_provider", return_value=_boom):
                with self.assertRaisesRegex(PdfExtractionError, "Scanned PDFs need OCR"):
                    extract_pdf_document(data)

    @requires_pypdf
    def test_empty_ocr_result_falls_back_to_unchanged_reject(self):
        data = _scanned_pdf_bytes()
        provider = _FakeOcrProvider("   \n  \n")  # only whitespace -> nothing recovered
        env = {OCR_ENV_ENABLED: "true"}
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ocr_fallback, "resolve_ocr_provider", return_value=provider):
                with self.assertRaisesRegex(PdfExtractionError, "Scanned PDFs need OCR"):
                    extract_pdf_document(data)

    @requires_pypdf
    def test_text_pdf_never_invokes_ocr(self):
        # A PDF WITH a real text layer must extract natively and NEVER touch OCR.
        from tests.test_pdf_text import make_pdf  # reuse the existing PDF builder

        data = make_pdf("This Agreement shall be governed by the laws of California.")
        provider = _FakeOcrProvider(_OCR_TEXT)
        env = {OCR_ENV_ENABLED: "true"}
        with patch.dict("os.environ", env, clear=False):
            with patch.object(ocr_fallback, "resolve_ocr_provider", return_value=provider):
                extraction = extract_pdf_document(data)
        self.assertEqual(provider.calls, [])
        self.assertFalse(extraction.quality["ocr_recovered"])
        self.assertIn("California", extraction.paragraphs[0]["text"])


# --------------------------------------------------------------------------------
# Resolver + privacy guards.
# --------------------------------------------------------------------------------
class ResolverGuardTests(unittest.TestCase):
    def _env(self, **overrides):
        base = {
            OCR_ENV_ENABLED: "true",
            OCR_ENV_PROVIDER: "llmwhisperer",
            OCR_ENV_BASE_URL: _PRO_BASE_URL,
            OCR_ENV_API_KEY: "secret-key",
        }
        base.update(overrides)
        return base

    def test_resolves_client_when_fully_configured(self):
        with patch.dict("os.environ", self._env(), clear=True):
            provider = resolve_ocr_provider()
        self.assertIsInstance(provider, LLMWhispererClient)
        self.assertEqual(provider.base_url, _PRO_BASE_URL)
        self.assertEqual(provider.mode, DEFAULT_OCR_MODE)

    def test_none_when_disabled(self):
        with patch.dict("os.environ", self._env(**{OCR_ENV_ENABLED: "false"}), clear=True):
            self.assertIsNone(resolve_ocr_provider())

    def test_none_when_base_url_missing(self):
        with patch.dict("os.environ", self._env(**{OCR_ENV_BASE_URL: ""}), clear=True):
            self.assertIsNone(resolve_ocr_provider())

    def test_none_when_api_key_missing(self):
        with patch.dict("os.environ", self._env(**{OCR_ENV_API_KEY: ""}), clear=True):
            self.assertIsNone(resolve_ocr_provider())

    def test_none_for_unknown_provider(self):
        with patch.dict("os.environ", self._env(**{OCR_ENV_PROVIDER: "tesseract"}), clear=True):
            self.assertIsNone(resolve_ocr_provider())

    def test_free_tier_endpoint_is_refused(self):
        # The privacy backstop: a free-tier host must never resolve a provider.
        with patch.dict("os.environ", self._env(**{OCR_ENV_BASE_URL: _FREE_BASE_URL}), clear=True):
            self.assertIsNone(resolve_ocr_provider())

    def test_client_constructor_refuses_free_tier(self):
        with self.assertRaisesRegex(OcrError, "free tier"):
            LLMWhispererClient(base_url=_FREE_BASE_URL, api_key="secret-key")

    def test_client_constructor_requires_key_and_url(self):
        with self.assertRaises(OcrError):
            LLMWhispererClient(base_url="", api_key="secret-key")
        with self.assertRaises(OcrError):
            LLMWhispererClient(base_url=_PRO_BASE_URL, api_key="")

    def test_status_reports_active_and_redacts_key(self):
        with patch.dict("os.environ", self._env(), clear=True):
            status = ocr_fallback_status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["active"])
        self.assertTrue(status["api_key_configured"])
        self.assertEqual(status["inactive_reason"], "")
        # The status must never carry the secret value itself.
        self.assertNotIn("secret-key", json.dumps(status))

    def test_status_reports_free_tier_refusal(self):
        with patch.dict("os.environ", self._env(**{OCR_ENV_BASE_URL: _FREE_BASE_URL}), clear=True):
            status = ocr_fallback_status()
        self.assertFalse(status["active"])
        self.assertTrue(status["free_tier_refused"])
        self.assertEqual(status["inactive_reason"], "free_tier_refused")


# --------------------------------------------------------------------------------
# LLMWhisperer client async flow, HTTP fully mocked.
# --------------------------------------------------------------------------------
class _FakeHttpResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LLMWhispererClientHttpTests(unittest.TestCase):
    def _client(self):
        return LLMWhispererClient(
            base_url=_PRO_BASE_URL,
            api_key="secret-key",
            mode="high_quality",
            timeout_seconds=5,
            poll_interval_seconds=0.0,
        )

    def test_submit_poll_retrieve_happy_path(self):
        responses = [
            {"status": "processing", "whisper_hash": "abc123"},  # submit
            {"status": "processing"},  # poll #1
            {"status": "processed"},  # poll #2
            {"result_text": "RECOVERED OCR TEXT"},  # retrieve
        ]
        requests_seen = []

        def _fake_urlopen(request, timeout, context):
            requests_seen.append(request)
            return _FakeHttpResponse(responses[len(requests_seen) - 1])

        with patch.object(ocr_fallback, "_urlopen", _fake_urlopen):
            text = self._client()(b"%PDF-1.4 scanned bytes")

        self.assertEqual(text, "RECOVERED OCR TEXT")
        # Submit went to /whisper with the high_quality + layout_preserving query.
        submit_url = requests_seen[0].full_url
        self.assertIn("/api/v2/whisper?", submit_url)
        self.assertIn("mode=high_quality", submit_url)
        self.assertIn("output_mode=layout_preserving", submit_url)
        # The API key is sent via the unstract-key header (not the URL).
        self.assertEqual(requests_seen[0].headers.get("Unstract-key"), "secret-key")
        self.assertNotIn("secret-key", submit_url)
        # Status + retrieve carried the whisper_hash.
        self.assertIn("/api/v2/whisper-status?", requests_seen[1].full_url)
        self.assertIn("whisper_hash=abc123", requests_seen[1].full_url)
        self.assertIn("/api/v2/whisper-retrieve?", requests_seen[-1].full_url)

    def test_retrieve_reads_nested_extraction_result_text(self):
        responses = [
            {"status": "processed", "whisper_hash": "h"},  # submit
            {"status": "processed"},  # poll
            {"extraction": {"result_text": "NESTED TEXT"}},  # retrieve
        ]
        seen = []

        def _fake_urlopen(request, timeout, context):
            seen.append(request)
            return _FakeHttpResponse(responses[len(seen) - 1])

        with patch.object(ocr_fallback, "_urlopen", _fake_urlopen):
            text = self._client()(b"bytes")
        self.assertEqual(text, "NESTED TEXT")

    def test_error_status_raises_ocr_error_without_leaking_content(self):
        responses = [
            {"status": "processing", "whisper_hash": "h"},  # submit
            {"status": "error", "message": "page 3 contained SECRET clause text"},  # poll
        ]
        seen = []

        def _fake_urlopen(request, timeout, context):
            seen.append(request)
            return _FakeHttpResponse(responses[len(seen) - 1])

        with patch.object(ocr_fallback, "_urlopen", _fake_urlopen):
            with self.assertRaises(OcrError) as ctx:
                self._client()(b"bytes")
        # The provider's error message (which may echo document content) must NOT be
        # propagated verbatim.
        self.assertNotIn("SECRET clause text", str(ctx.exception))

    def test_timeout_raises_ocr_error(self):
        # Always "processing" -> never completes -> times out.
        def _fake_urlopen(request, timeout, context):
            if "/whisper?" in request.full_url:
                return _FakeHttpResponse({"status": "processing", "whisper_hash": "h"})
            return _FakeHttpResponse({"status": "processing"})

        client = LLMWhispererClient(
            base_url=_PRO_BASE_URL,
            api_key="secret-key",
            timeout_seconds=1,
            poll_interval_seconds=0.0,
        )
        with patch.object(ocr_fallback, "_urlopen", _fake_urlopen):
            with self.assertRaisesRegex(OcrError, "timed out"):
                client(b"bytes")

    def test_missing_whisper_hash_raises(self):
        def _fake_urlopen(request, timeout, context):
            return _FakeHttpResponse({"status": "processing"})  # no whisper_hash

        with patch.object(ocr_fallback, "_urlopen", _fake_urlopen):
            with self.assertRaisesRegex(OcrError, "whisper_hash"):
                self._client()(b"bytes")

    def test_empty_result_text_returns_none(self):
        responses = [
            {"status": "processed", "whisper_hash": "h"},
            {"status": "processed"},
            {"result_text": "   "},
        ]
        seen = []

        def _fake_urlopen(request, timeout, context):
            seen.append(request)
            return _FakeHttpResponse(responses[len(seen) - 1])

        with patch.object(ocr_fallback, "_urlopen", _fake_urlopen):
            self.assertIsNone(self._client()(b"bytes"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
