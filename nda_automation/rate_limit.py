from __future__ import annotations

import os
import threading
import time

DEFAULT_RATE_LIMIT_PER_MINUTE = 300
RATE_LIMITED_MESSAGE = "Too many requests. Try again shortly."

_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_BUCKETS: dict[tuple[str, str], tuple[float, int]] = {}


def _rate_limit_retry_after(method: str, path: str, client_host: str) -> int:
    bucket_name = _rate_limit_bucket_name(method, path)
    if not bucket_name:
        return 0
    limit = _rate_limit_per_window()
    if limit <= 0:
        return 0
    window_seconds = _rate_limit_window_seconds()
    now = time.monotonic()
    key = (client_host, bucket_name)
    with _RATE_LIMIT_LOCK:
        window_start, count = _RATE_LIMIT_BUCKETS.get(key, (now, 0))
        if now - window_start >= window_seconds:
            window_start = now
            count = 0
        if count >= limit:
            return max(1, int(window_seconds - (now - window_start)))
        _RATE_LIMIT_BUCKETS[key] = (window_start, count + 1)
    return 0


def _rate_limit_bucket_name(method: str, path: str) -> str:
    if method == "GET" and path == "/api/matters/export":
        return "matter-backup"
    if method != "POST":
        return ""
    buckets = {
        "/api/review": "review",
        "/api/review-document": "document-review",
        "/api/matters": "matter-upload",
        "/api/export-review-docx": "docx-export",
        "/api/gmail/send-redline": "gmail-send-redline",
    }
    return buckets.get(path, "")


def _rate_limit_per_window() -> int:
    try:
        return max(0, int(os.environ.get("NDA_RATE_LIMIT_PER_MINUTE", str(DEFAULT_RATE_LIMIT_PER_MINUTE))))
    except ValueError:
        return DEFAULT_RATE_LIMIT_PER_MINUTE


def _rate_limit_window_seconds() -> int:
    try:
        return max(1, int(os.environ.get("NDA_RATE_LIMIT_WINDOW_SECONDS", "60")))
    except ValueError:
        return 60


def _reset_rate_limits() -> None:
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_BUCKETS.clear()
