"""Scanned-PDF OCR fallback (DEFAULT-OFF).

``pdf_text.extract_pdf_document`` raises ``PdfExtractionError("No readable text
was found in the PDF. Scanned PDFs need OCR before review.")`` when a PDF has no
text layer -- the genuine capability gap for scanner output / image-only PDFs.
This module is the OCR fallback wired at exactly that branch: when the fallback
is ENABLED and a scanned PDF arrives, the no-text pages are rasterized to PNG
images and an OCR provider transcribes them to plain text, which is fed back
through ``pdf_text``'s existing TEXT-ONLY paragraph splitter. OCR returns flat
text, so the never-merge-safe text heuristics in ``pdf_text`` stay in force
exactly as they do for the visitor-unsupported flat-text path already there.

Design constraints (this is a COST + LATENCY + PRIVACY path -- the app has had a
prior Gmail/AI cost-storm, and counterparty NDAs are confidential):

- DEFAULT-OFF. Gated behind ``NDA_PDF_OCR_ENABLED`` (default false). When OFF,
  ``resolve_ocr_provider()`` returns ``None`` and the existing scanned-reject in
  ``extract_pdf_document`` is UNCHANGED. Nothing is rasterized, nothing is sent.

- Provider chosen pragmatically: REUSE the existing OpenRouter setup with a
  VISION model. No new API key and no new dependency -- ``OPENROUTER_API_KEY`` is
  already configured for review/Gmail, and PyMuPDF/fitz already rasterizes pages
  for the visual pane. (LLMWhisperer / Tesseract would each add a key or system
  dependency; OpenRouter-vision is the lowest-friction option that degrades
  gracefully when unconfigured.)

- BOUNDED. The page count OCR'd is capped (``MAX_OCR_PAGES``), each page's
  pixmap is byte-budgeted at the same ceiling the visual rasterizer uses, and
  the provider call has a per-call timeout. A 100-page scanned bomb cannot fan
  out into 100 vision calls.

- Provider-agnostic, stubbable seam. ``OcrProvider`` is a minimal callable
  (page PNG bytes -> page text); tests inject a deterministic provider across
  the real seam, so no live HTTP/key is ever contacted in tests. This mirrors
  the ``ai_review`` / ``ai_verifier`` provider seams already in the codebase.

- FAIL-SAFE, NEVER SILENTLY EMPTY. A disabled/unconfigured/failing OCR provider
  degrades to the EXISTING scanned-reject error -- it never returns empty or
  garbage text that the AI then "reviews". ``resolve_ocr_provider`` never raises;
  ``ocr_pdf_text`` returns ``None`` (caller re-raises the original clear error)
  whenever OCR cannot produce real text.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Protocol

# Reuse the existing OpenRouter plumbing -- same endpoint, key resolution, TLS
# context and model sanitiser the AI review path already uses. No new transport.
from .ai_review import (
    OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
    _configured_api_key,
    _openrouter_response_text,
    _sanitize_model_name,
    _trusted_https_context,
)

# --- Public seam ---------------------------------------------------------------

# An OCR provider maps a single page's PNG image bytes -> that page's recovered
# plain text (or ``None`` / "" when nothing readable was recovered). Implementations
# raise ``OcrError`` on transport/config failure; callers treat that the same as an
# empty result and fall back to the unchanged scanned-reject.
OcrProvider = Callable[[bytes], Optional[str]]


class _OcrProviderProtocol(Protocol):
    def __call__(self, image_png: bytes) -> Optional[str]:  # pragma: no cover - typing only
        ...


class OcrError(RuntimeError):
    """Raised by a provider on a transport/config failure (never on empty text)."""


# --- Config / bounds -----------------------------------------------------------

NDA_PDF_OCR_ENABLED_ENV = "NDA_PDF_OCR_ENABLED"
NDA_PDF_OCR_MODEL_ENV = "NDA_PDF_OCR_MODEL"
NDA_PDF_OCR_MAX_PAGES_ENV = "NDA_PDF_OCR_MAX_PAGES"
NDA_PDF_OCR_TIMEOUT_ENV = "NDA_PDF_OCR_TIMEOUT_SECONDS"
NDA_PDF_OCR_DPI_ENV = "NDA_PDF_OCR_DPI"

# A vision-capable default. Overridable via NDA_PDF_OCR_MODEL. Kept distinct from
# the review model so OCR can be pointed at a cheaper vision model independently.
DEFAULT_OCR_MODEL = "google/gemini-2.5-flash"

# Hard ceiling on pages OCR'd per document, regardless of the env override. This is
# the cost/latency tourniquet: a scanned PDF can be up to MAX_PDF_PAGES (100) pages,
# and we will NOT fan that into 100 vision calls. The env override may only LOWER it.
MAX_OCR_PAGES = 20
DEFAULT_OCR_PAGES = 20

# Per-page vision call timeout (seconds). Bounds total latency to roughly
# pages * timeout in the worst case.
DEFAULT_OCR_TIMEOUT_SECONDS = 60

# Rasterization DPI for the OCR images. 200 DPI is a good OCR/cost balance for a
# US-Letter page (~1700x2200 px). The pixmap is still byte-budgeted below.
DEFAULT_OCR_DPI = 200
MIN_OCR_DPI = 72
# Per-page pixmap byte ceiling -- the SAME ceiling document_rendering uses for the
# visual pane, so the OCR rasterize path and the visual rasterize path bound peak
# decoded RSS to one shared number. A page whose budget cannot be met even at
# MIN_OCR_DPI is skipped rather than rasterized.
MAX_OCR_PAGE_PIXMAP_BYTES = 96 * 1024 * 1024
_PIXMAP_CHANNELS = 3  # RGB, alpha=False


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def ocr_enabled() -> bool:
    """True iff the OCR fallback is turned on (default OFF)."""

    return _env_flag(NDA_PDF_OCR_ENABLED_ENV, default=False)


def _configured_max_pages() -> int:
    # The env override may only LOWER the hard ceiling, never raise it.
    return max(1, min(MAX_OCR_PAGES, _env_int(NDA_PDF_OCR_MAX_PAGES_ENV, DEFAULT_OCR_PAGES)))


def _configured_timeout() -> int:
    return max(1, _env_int(NDA_PDF_OCR_TIMEOUT_ENV, DEFAULT_OCR_TIMEOUT_SECONDS))


def _configured_dpi() -> int:
    return max(MIN_OCR_DPI, _env_int(NDA_PDF_OCR_DPI_ENV, DEFAULT_OCR_DPI))


def _configured_model() -> str:
    return _sanitize_model_name(os.environ.get(NDA_PDF_OCR_MODEL_ENV, "").strip() or DEFAULT_OCR_MODEL)


# --- OpenRouter vision provider ------------------------------------------------


class OpenRouterVisionOcrProvider:
    """OCR one page image via an OpenRouter vision chat-completion.

    Reuses the existing OpenRouter endpoint, API key, TLS context and model
    sanitiser. The page PNG is sent as a base64 data URL alongside a strict
    transcription instruction; the model's text reply is the page's recovered
    text. Never logs the document bytes, the API key, or the recovered text.
    """

    def __init__(self, *, api_key: str, model: str, timeout_seconds: int) -> None:
        cleaned_key = str(api_key or "").strip()
        if not cleaned_key:
            raise OcrError("OpenRouter API key is not configured for OCR.")
        self.api_key = cleaned_key
        self.model = _sanitize_model_name(model or DEFAULT_OCR_MODEL)
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_OCR_TIMEOUT_SECONDS))

    def __call__(self, image_png: bytes) -> Optional[str]:
        if not image_png:
            return None
        data_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an OCR transcription engine. Transcribe ALL legible text "
                        "from the supplied page image verbatim, preserving reading order and "
                        "line breaks. Do not summarize, translate, interpret, or add commentary. "
                        "If the page contains no legible text, reply with an empty response."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this page."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            "temperature": 0,
        }
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "nda-automation/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=_trusted_https_context()
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # Read only the status; the body may echo content. Never log it.
            raise OcrError(f"OCR provider returned HTTP {error.code}.") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise OcrError(f"OCR provider request failed: {type(error).__name__}.") from error
        text = _openrouter_response_text(payload)
        return text or None


def resolve_ocr_provider() -> Optional[OcrProvider]:
    """Return the configured OCR provider, or ``None`` when OCR is unavailable.

    NEVER RAISES. Returns ``None`` (and the caller falls back to the unchanged
    scanned-reject) when:
      * the OCR fallback flag is OFF (default), or
      * no OpenRouter API key is configured, or
      * the provider could not be constructed for any reason.
    """

    try:
        if not ocr_enabled():
            return None
        api_key = _configured_api_key("openrouter")
        if not api_key:
            return None
        return OpenRouterVisionOcrProvider(
            api_key=api_key,
            model=_configured_model(),
            timeout_seconds=_configured_timeout(),
        )
    except Exception:
        # Resolution must never break review on its own infrastructure gap.
        return None


def ocr_status() -> dict[str, object]:
    """Best-effort introspection of the OCR fallback config (no secrets)."""

    enabled = ocr_enabled()
    has_key = bool(_configured_api_key("openrouter"))
    return {
        "enabled": enabled,
        "configured": bool(enabled and has_key),
        "model": _configured_model(),
        "max_pages": _configured_max_pages(),
        "timeout_seconds": _configured_timeout(),
        "dpi": _configured_dpi(),
    }


# --- Rasterize + OCR -----------------------------------------------------------


def _budgeted_dpi(width_pts: float, height_pts: float, requested_dpi: int) -> Optional[int]:
    """Clamp DPI so the page pixmap fits the byte budget; None if impossible.

    pixmap_bytes = (w_pts/72 * dpi) * (h_pts/72 * dpi) * channels. Solve for the
    largest dpi <= requested whose pixmap fits MAX_OCR_PAGE_PIXMAP_BYTES, floored
    at MIN_OCR_DPI. Returns None when even MIN_OCR_DPI would exceed the budget
    (the page is skipped rather than rasterized).
    """

    if width_pts <= 0 or height_pts <= 0:
        return None
    area_in = (width_pts / 72.0) * (height_pts / 72.0)
    if area_in <= 0:
        return None
    max_pixels = MAX_OCR_PAGE_PIXMAP_BYTES / _PIXMAP_CHANNELS
    # max_pixels = area_in * dpi^2  ->  dpi = sqrt(max_pixels / area_in)
    import math

    budget_dpi = int(math.sqrt(max_pixels / area_in))
    dpi = min(int(requested_dpi), budget_dpi)
    if dpi < MIN_OCR_DPI:
        return None
    return dpi


def ocr_pdf_text(
    data: bytes,
    *,
    provider: Optional[OcrProvider] = None,
    fitz_module: Any | None = None,
) -> Optional[str]:
    """Rasterize the PDF's pages and OCR them into assembled plain text.

    Returns the recovered text (pages joined by the page separator the text
    splitter understands) when OCR produced any real text, else ``None`` so the
    caller re-raises the original clear scanned-reject error. NEVER returns empty
    or whitespace-only text (that would let the AI "review" nothing).

    Bounded: at most ``MAX_OCR_PAGES`` pages are rasterized + OCR'd; each page's
    pixmap is byte-budgeted; the provider call carries a per-call timeout. Any
    per-page or provider failure degrades to ``None`` (never a partial garbage
    result that masquerades as a full document) UNLESS at least one page produced
    real text -- a usable transcription of the readable pages is preferable to a
    hard reject, and the caller's quality report flags the OCR recovery.

    The provider defaults to ``resolve_ocr_provider()`` (None when OCR is OFF or
    unconfigured -> this returns None). Tests inject a deterministic provider.
    """

    active_provider = provider if provider is not None else resolve_ocr_provider()
    if active_provider is None:
        return None

    if fitz_module is None:
        try:
            import fitz  # type: ignore[import-not-found]

            fitz_module = fitz
        except ImportError:
            return None

    document = None
    page_texts: list[str] = []
    try:
        document = fitz_module.open(stream=data, filetype="pdf")
        page_count = int(getattr(document, "page_count", 0) or 0)
        if page_count <= 0:
            return None
        max_pages = _configured_max_pages()
        requested_dpi = _configured_dpi()
        inspected = min(page_count, max_pages)
        for page_index in range(inspected):
            try:
                page = document.load_page(page_index)
                rect = page.rect
                effective_dpi = _budgeted_dpi(
                    float(rect.width), float(rect.height), requested_dpi
                )
                if effective_dpi is None:
                    # Page too large to rasterize within budget -- skip it. Never
                    # OCR an unbounded pixmap.
                    continue
                scale = effective_dpi / 72.0
                matrix = fitz_module.Matrix(scale, scale)
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                image_png = pixmap.tobytes("png")
            except Exception:
                # One bad page must not blind the whole document; skip it.
                continue
            try:
                page_text = active_provider(image_png)
            except OcrError:
                # Provider transport/config failure -- abandon OCR entirely rather
                # than return a document missing arbitrary pages.
                return None
            except Exception:
                return None
            if page_text and page_text.strip():
                page_texts.append(page_text.strip())
    except Exception:
        return None
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass

    if not page_texts:
        # No readable text recovered -> let the caller re-raise the clear error.
        return None
    # Join pages with a blank-line separator; the text splitter treats this as a
    # page/paragraph boundary, preserving the never-merge-safe heuristics.
    assembled = "\n\n".join(page_texts).strip()
    return assembled or None


__all__ = [
    "DEFAULT_OCR_MODEL",
    "MAX_OCR_PAGES",
    "NDA_PDF_OCR_ENABLED_ENV",
    "OcrError",
    "OcrProvider",
    "OpenRouterVisionOcrProvider",
    "ocr_enabled",
    "ocr_pdf_text",
    "ocr_status",
    "resolve_ocr_provider",
]
