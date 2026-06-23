"""Conversion engine adapters.

Each adapter exposes a single shape:

    NAME: str
    REQUIRED_ENV: tuple[str, ...]
    def available() -> tuple[bool, str]:   # (is_available, reason_if_not)
    def convert(pdf_path: Path, out_path: Path) -> None  # writes a DOCX to out_path

Adapters MUST skip gracefully (``available()`` returns ``False`` with a clear
"missing <ENV>" reason) when their credentials are not set, so the runner can run
end-to-end on just the keyless ``pdf2docx`` baseline.
"""

from __future__ import annotations

from . import adobe, cloudmersive, ilovepdf, pdf2docx_baseline

# Registry order = report order. Baseline first (always available, no creds).
ENGINES = [
    pdf2docx_baseline,
    adobe,
    cloudmersive,
    ilovepdf,
]

__all__ = ["ENGINES", "pdf2docx_baseline", "adobe", "cloudmersive", "ilovepdf"]
