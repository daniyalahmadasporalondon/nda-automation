"""Scanned-PDF OCR fallback seam (DEFAULT-OFF).

``extract_pdf_document`` raises ``PdfExtractionError("...Scanned PDFs need OCR...")``
when a PDF has no text layer -- a genuine, scanner-output capability gap. This
module is the OCR fallback for exactly that branch: when the fallback is ENABLED
and a scanned PDF arrives, an OCR provider is asked for *layout-preserving text*,
which is then fed back through the existing TEXT-ONLY paragraph splitter
(``pdf_text._split_pdf_paragraphs`` on plain ``str`` lines). It is NOT a geometry
replacement -- OCR returns flat text, so the never-merge-safe text heuristics stay
in force, exactly as for the visitor-unsupported flat-text path already in
``pdf_text``.

Design constraints:

- DEFAULT-OFF. The fallback is gated behind ``NDA_OCR_FALLBACK_ENABLED`` (default
  false). When OFF, ``resolve_ocr_provider()`` returns ``None`` and the existing
  reject behaviour in ``extract_pdf_document`` is UNCHANGED, byte-for-byte.

- Provider-agnostic seam. ``OcrProvider`` is a minimal protocol -- a callable
  mapping raw PDF bytes to layout-preserving text (or ``None`` when no text could
  be recovered). Tests inject a deterministic provider across the real seam; prod
  resolves an ``LLMWhispererClient``. This mirrors the ``ai_review.AIReviewFn`` /
  ``ai_verifier.VerifierFn`` provider seams already in the codebase.

- Privacy first. Counterparty NDAs are confidential. The client REFUSES to call
  unless a non-free endpoint/key is configured (the free LLMWhisperer tier is
  rejected outright -- never send counterparty NDAs to a free tier), and it NEVER
  logs document content (bytes in, text out; nothing is logged here).

- Fail-safe. A misconfigured or failing OCR provider degrades to the *existing*
  scanned-reject behaviour rather than crashing review: ``resolve_ocr_provider``
  never raises, and the integration point treats any ``OcrError`` /
  empty result as "still scanned, reject as before".
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional, Protocol

# --- Public seam ----------------------------------------------------------------

# An OCR provider maps raw document bytes -> recovered layout-preserving text, or
# ``None`` when the provider ran but recovered nothing reviewable. Implementations
# raise ``OcrError`` on transport/config failure; callers treat that the same as a
# ``None`` result (fall back to the unchanged scanned-reject).
OcrProvider = Callable[[bytes], Optional[str]]


class _OcrProviderProtocol(Protocol):
    def __call__(self, data: bytes) -> Optional[str]:  # pragma: no cover - typing only
        ...


class OcrError(RuntimeError):
    """Raised when the OCR provider cannot be used (config or transport failure).

    This is deliberately distinct from ``PdfExtractionError``: an ``OcrError`` means
    "the OCR fallback itself failed", which the integration point downgrades to the
    pre-existing scanned-reject so a broken accuracy lever never fails review closed.
    """


# --- Environment knobs ----------------------------------------------------------

OCR_ENV_ENABLED = "NDA_OCR_FALLBACK_ENABLED"
OCR_ENV_PROVIDER = "NDA_OCR_PROVIDER"
OCR_ENV_BASE_URL = "NDA_OCR_LLMWHISPERER_BASE_URL"
OCR_ENV_API_KEY = "NDA_OCR_LLMWHISPERER_API_KEY"
OCR_ENV_MODE = "NDA_OCR_LLMWHISPERER_MODE"
OCR_ENV_TIMEOUT = "NDA_OCR_LLMWHISPERER_TIMEOUT_SECONDS"
OCR_ENV_POLL_INTERVAL = "NDA_OCR_LLMWHISPERER_POLL_INTERVAL_SECONDS"

DEFAULT_OCR_PROVIDER = "llmwhisperer"
DEFAULT_OCR_MODE = "high_quality"
DEFAULT_OCR_TIMEOUT_SECONDS = 120
DEFAULT_OCR_POLL_INTERVAL_SECONDS = 3

# Hosted LLMWhisperer free-tier host. Counterparty NDAs must NEVER be sent here, so
# a base URL on this host is refused: a Pro/Subscription host or a self-hosted
# deployment is REQUIRED. There is no safe default base URL -- the operator must
# choose a non-free Pro host or their own self-hosted URL explicitly.
_FREE_TIER_HOST_FRAGMENTS = ("llmwhisperer-api.us-central.unstract.com",)

# LLMWhisperer v2 status values.
_STATUS_PROCESSED = "processed"
_STATUS_PROCESSING = "processing"
_STATUS_DELIVERED = "delivered"
_STATUS_ERROR = "error"


def ocr_fallback_enabled() -> bool:
    """True only when the scanned-PDF OCR fallback is explicitly enabled via env.

    DEFAULT-OFF: an unset / empty / falsey value leaves the existing scanned-reject
    behaviour completely unchanged.
    """
    return str(os.environ.get(OCR_ENV_ENABLED, "")).strip().lower() in {"1", "true", "yes", "on"}


def _ocr_timeout() -> int:
    raw = str(os.environ.get(OCR_ENV_TIMEOUT, "")).strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_OCR_TIMEOUT_SECONDS


def _ocr_poll_interval() -> float:
    raw = str(os.environ.get(OCR_ENV_POLL_INTERVAL, "")).strip()
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_OCR_POLL_INTERVAL_SECONDS)


def _is_free_tier_endpoint(base_url: str) -> bool:
    host = urllib.parse.urlparse(base_url).netloc.lower() or base_url.lower()
    return any(fragment in host for fragment in _FREE_TIER_HOST_FRAGMENTS)


# --- LLMWhisperer client --------------------------------------------------------


class LLMWhispererClient:
    """OCR provider backed by LLMWhisperer (Unstract), hosted Pro OR self-hosted.

    Returns layout-preserving text. The flow is the documented LLMWhisperer v2
    async contract: ``POST /api/v2/whisper`` (submit) -> ``202`` + ``whisper_hash``,
    poll ``GET /api/v2/whisper-status`` until ``processed``, then
    ``GET /api/v2/whisper-retrieve`` -> ``{"result_text": "<layout text>"}``.

    PRIVACY GUARD: construction REFUSES a free-tier endpoint -- counterparty NDAs
    are confidential and must only ever reach a Pro host or a self-hosted
    deployment. Document bytes and recovered text are never logged.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        mode: str = DEFAULT_OCR_MODE,
        timeout_seconds: int = DEFAULT_OCR_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_OCR_POLL_INTERVAL_SECONDS,
    ) -> None:
        cleaned_base = str(base_url or "").strip().rstrip("/")
        cleaned_key = str(api_key or "").strip()
        if not cleaned_base:
            raise OcrError("LLMWhisperer base URL is not configured.")
        if not cleaned_key:
            raise OcrError("LLMWhisperer API key is not configured.")
        if _is_free_tier_endpoint(cleaned_base):
            # Privacy backstop: refuse the free tier for counterparty NDAs. The
            # operator must point at a Pro host or a self-hosted deployment.
            raise OcrError(
                "Refusing to OCR a counterparty NDA via the LLMWhisperer free tier. "
                "Configure a Pro/Subscription host or a self-hosted base URL."
            )
        self.base_url = cleaned_base
        self.api_key = cleaned_key
        self.mode = str(mode or DEFAULT_OCR_MODE).strip() or DEFAULT_OCR_MODE
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_OCR_TIMEOUT_SECONDS))
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds or DEFAULT_OCR_POLL_INTERVAL_SECONDS))

    # -- HTTP plumbing (overridable for tests via the module-level _urlopen seam) --

    def _headers(self) -> dict[str, str]:
        return {
            "unstract-key": self.api_key,
            "User-Agent": "nda-automation/1.0",
        }

    def _submit(self, data: bytes) -> str:
        query = urllib.parse.urlencode({"mode": self.mode, "output_mode": "layout_preserving"})
        request = urllib.request.Request(
            f"{self.base_url}/api/v2/whisper?{query}",
            data=data,
            headers={**self._headers(), "Content-Type": "application/octet-stream"},
            method="POST",
        )
        payload = self._read_json(request)
        whisper_hash = str(payload.get("whisper_hash") or "").strip()
        if not whisper_hash:
            raise OcrError("LLMWhisperer submit returned no whisper_hash.")
        return whisper_hash

    def _poll_until_processed(self, whisper_hash: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        query = urllib.parse.urlencode({"whisper_hash": whisper_hash})
        request = urllib.request.Request(
            f"{self.base_url}/api/v2/whisper-status?{query}",
            headers=self._headers(),
            method="GET",
        )
        while True:
            payload = self._read_json(request)
            status = str(payload.get("status") or "").strip().lower()
            if status in {_STATUS_PROCESSED, _STATUS_DELIVERED}:
                return
            if status == _STATUS_ERROR:
                # Do not surface the provider error verbatim -- it may echo content.
                raise OcrError("LLMWhisperer reported an extraction error.")
            if status not in {_STATUS_PROCESSING, ""}:
                raise OcrError(f"LLMWhisperer returned an unexpected status: {status!r}.")
            if time.monotonic() >= deadline:
                raise OcrError("LLMWhisperer OCR timed out before completion.")
            time.sleep(self.poll_interval_seconds)

    def _retrieve(self, whisper_hash: str) -> Optional[str]:
        query = urllib.parse.urlencode({"whisper_hash": whisper_hash})
        request = urllib.request.Request(
            f"{self.base_url}/api/v2/whisper-retrieve?{query}",
            headers=self._headers(),
            method="GET",
        )
        payload = self._read_json(request)
        return _extract_result_text(payload)

    def _read_json(self, request: urllib.request.Request) -> dict:
        try:
            with _urlopen(request, timeout=self.timeout_seconds, context=_trusted_https_context()) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            # Read+discard the body to release the connection but NEVER log it: an
            # error body can echo submitted document content.
            try:
                error.read()
            except Exception:
                pass
            raise OcrError(f"LLMWhisperer API returned HTTP {error.code}.") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise OcrError("LLMWhisperer API request failed.") from error
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise OcrError("LLMWhisperer API returned non-JSON text.") from error
        return parsed if isinstance(parsed, dict) else {}

    def __call__(self, data: bytes) -> Optional[str]:
        whisper_hash = self._submit(data)
        self._poll_until_processed(whisper_hash)
        text = self._retrieve(whisper_hash)
        if not text or not text.strip():
            return None
        return text


def _extract_result_text(payload: dict) -> Optional[str]:
    """Pull the layout-preserving text out of a whisper-retrieve payload.

    LLMWhisperer v2 returns ``{"result_text": "..."}``; some versions nest it under
    ``{"extraction": {"result_text": "..."}}``. Read both defensively.
    """
    direct = payload.get("result_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    extraction = payload.get("extraction")
    if isinstance(extraction, dict):
        nested = extraction.get("result_text")
        if isinstance(nested, str) and nested.strip():
            return nested
    return None


# Module-level seam so tests can mock the HTTP layer without a live key/endpoint.
def _urlopen(request, timeout, context):  # pragma: no cover - thin urllib shim
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def _trusted_https_context() -> "ssl.SSLContext | None":
    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return ssl.create_default_context(cafile=certifi.where())
    except (OSError, ssl.SSLError):
        return None


# --- Resolver -------------------------------------------------------------------


def resolve_ocr_provider() -> Optional[OcrProvider]:
    """Resolve the active OCR provider, or ``None`` when the fallback is unavailable.

    Returns ``None`` -- meaning "no fallback, keep the existing scanned-reject" --
    when ANY of: the fallback is disabled (default), the provider is unknown, the
    base URL / API key is missing, or the endpoint is a free tier. NEVER raises:
    every misconfiguration degrades to the unchanged reject path.
    """
    if not ocr_fallback_enabled():
        return None
    provider = str(os.environ.get(OCR_ENV_PROVIDER, "")).strip().lower() or DEFAULT_OCR_PROVIDER
    if provider != "llmwhisperer":
        return None
    base_url = str(os.environ.get(OCR_ENV_BASE_URL, "")).strip()
    api_key = str(os.environ.get(OCR_ENV_API_KEY, "")).strip()
    if not base_url or not api_key:
        return None
    mode = str(os.environ.get(OCR_ENV_MODE, "")).strip() or DEFAULT_OCR_MODE
    try:
        return LLMWhispererClient(
            base_url=base_url,
            api_key=api_key,
            mode=mode,
            timeout_seconds=_ocr_timeout(),
            poll_interval_seconds=_ocr_poll_interval(),
        )
    except OcrError:
        return None


def ocr_fallback_status() -> dict[str, object]:
    """Expose the configured OCR fallback resolver WITHOUT making a live API call.

    Mirrors ``ai_verifier.verifier_status`` so a deploy can sanity-check the knobs.
    Never includes the API key value -- only whether one is configured.
    """
    enabled = ocr_fallback_enabled()
    provider = str(os.environ.get(OCR_ENV_PROVIDER, "")).strip().lower() or DEFAULT_OCR_PROVIDER
    base_url = str(os.environ.get(OCR_ENV_BASE_URL, "")).strip()
    api_key_configured = bool(str(os.environ.get(OCR_ENV_API_KEY, "")).strip())
    base_url_configured = bool(base_url)
    free_tier = bool(base_url) and _is_free_tier_endpoint(base_url)
    active = enabled and provider == "llmwhisperer" and base_url_configured and api_key_configured and not free_tier
    reason = ""
    if not active:
        if not enabled:
            reason = "disabled"
        elif provider != "llmwhisperer":
            reason = "unknown_provider"
        elif not base_url_configured:
            reason = "missing_base_url"
        elif not api_key_configured:
            reason = "missing_api_key"
        elif free_tier:
            reason = "free_tier_refused"
    return {
        "enabled": enabled,
        "provider": provider,
        "active": active,
        "mode": str(os.environ.get(OCR_ENV_MODE, "")).strip() or DEFAULT_OCR_MODE,
        "base_url_configured": base_url_configured,
        "api_key_configured": api_key_configured,
        "free_tier_refused": free_tier,
        "inactive_reason": reason,
    }
