"""Cloudmersive -- Convert PDF to DOCX.

Verified from api.cloudmersive.com docs (2026-06):
  POST {base}/convert/pdf/to/docx
  header: Apikey: <CLOUDMERSIVE_API_KEY>
  body:   multipart/form-data, field name "inputFile" = the PDF
  -> raw DOCX bytes (application/octet-stream)

Creds (ENV ONLY): CLOUDMERSIVE_API_KEY.
Self-hosted: set CLOUDMERSIVE_BASE_URL to your container base
(e.g. http://localhost:8080); defaults to the public host api.cloudmersive.com.
The path is identical for the self-hosted container.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import _http

NAME = "cloudmersive"
REQUIRED_ENV = ("CLOUDMERSIVE_API_KEY",)

_DEFAULT_BASE = "https://api.cloudmersive.com"


def _base_url() -> str:
    return os.environ.get("CLOUDMERSIVE_BASE_URL", _DEFAULT_BASE).rstrip("/")


def available() -> tuple[bool, str]:
    # A self-hosted container can be configured to run without an API key, but the
    # public host always requires one. We require the key unless a non-default
    # self-hosted base URL is explicitly set.
    has_key = bool(os.environ.get("CLOUDMERSIVE_API_KEY", "").strip())
    self_hosted = _base_url() != _DEFAULT_BASE
    if has_key or self_hosted:
        return True, ""
    return False, "missing CLOUDMERSIVE_API_KEY (or set CLOUDMERSIVE_BASE_URL for self-hosted)"


def convert(pdf_path: Path, out_path: Path) -> None:
    headers: dict[str, str] = {}
    api_key = os.environ.get("CLOUDMERSIVE_API_KEY", "").strip()
    if api_key:
        headers["Apikey"] = api_key
    _s, _h, docx_bytes = _http.post_multipart(
        f"{_base_url()}/convert/pdf/to/docx",
        files={"inputFile": (Path(pdf_path).name, _http.read_pdf(pdf_path), "application/pdf")},
        headers=headers,
    )
    out_path.write_bytes(docx_bytes)
