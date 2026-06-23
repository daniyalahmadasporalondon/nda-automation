"""Minimal stdlib HTTP helpers shared by the external engine adapters.

Uses urllib only (no third-party HTTP dep required to run the harness). Keeps a
single multipart encoder so each adapter stays small. NEVER logs credentials.
"""

from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_TIMEOUT = 180  # external conversions are async / can be slow


class HttpError(RuntimeError):
    """A non-2xx HTTP response or transport failure, with body for diagnosis.

    The message never includes request headers, so API keys cannot leak into a
    results.json or a log line.
    """


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:  # pragma: no cover
            pass
        # Truncate body; never echo the request (which carried the auth header).
        snippet = body[:500].decode("utf-8", "replace")
        raise HttpError(f"{method} {_safe_url(url)} -> HTTP {exc.code}: {snippet}") from None
    except urllib.error.URLError as exc:
        raise HttpError(f"{method} {_safe_url(url)} transport error: {exc.reason}") from None


def _safe_url(url: str) -> str:
    """Strip query strings so a presigned-URL token never lands in an error/log."""
    split = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, "", ""))


def get(url: str, *, headers: dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT):
    return _request("GET", url, headers=headers, timeout=timeout)


def post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
):
    body = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json", **(headers or {})}
    return _request("POST", url, headers=merged, data=body, timeout=timeout)


def post_form(
    url: str,
    fields: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    merged = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    return _request("POST", url, headers=merged, data=body, timeout=timeout)


def put_bytes(
    url: str,
    data: bytes,
    *,
    content_type: str,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
):
    merged = {"Content-Type": content_type, **(headers or {})}
    return _request("PUT", url, headers=merged, data=data, timeout=timeout)


def post_multipart(
    url: str,
    *,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    form_fields: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
):
    """POST multipart/form-data. ``files`` maps field -> (filename, bytes, mime)."""
    boundary = f"----bakeoff{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in (form_fields or {}).items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    for name, (filename, content, mime) in (files or {}).items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    merged = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        **(headers or {}),
    }
    return _request("POST", url, headers=merged, data=body, timeout=timeout)


def read_pdf(pdf_path: Path) -> bytes:
    return Path(pdf_path).read_bytes()
