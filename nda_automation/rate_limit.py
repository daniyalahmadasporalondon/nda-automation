from __future__ import annotations

import os
import threading
import time

DEFAULT_RATE_LIMIT_PER_MINUTE = 300
RATE_LIMITED_MESSAGE = "Too many requests. Try again shortly."
TRUSTED_PROXY_COUNT_ENV = "NDA_TRUSTED_PROXY_COUNT"

_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_BUCKETS: dict[tuple[str, str], tuple[float, int]] = {}


def _rate_limit_client_key(peer_host: str, forwarded_for: str, user_id: str) -> str:
    """Resolve the per-caller key the rate limiter buckets on.

    Behind a reverse proxy (Render), every connection's TCP peer is the proxy,
    so keying on the peer collapses all callers into one shared bucket. We key
    on the authenticated identity when present, then on the real client IP
    derived from a trusted ``X-Forwarded-For``, and only fall back to the TCP
    peer. ``X-Forwarded-For`` is client-spoofable, so we trust it solely when an
    operator declares how many proxies sit in front via NDA_TRUSTED_PROXY_COUNT;
    otherwise an attacker could rotate the header to dodge the limit.
    """
    identity = str(user_id or "").strip()
    if identity:
        return f"user:{identity}"
    return f"ip:{_real_client_ip(peer_host, forwarded_for)}"


def _real_client_ip(peer_host: str, forwarded_for: str) -> str:
    peer = str(peer_host or "").strip() or "unknown"
    proxy_count = _trusted_proxy_count()
    if proxy_count <= 0:
        return peer
    hops = [hop.strip() for hop in str(forwarded_for or "").split(",") if hop.strip()]
    if not hops:
        return peer
    # Each proxy appends the address that connected to it, so the rightmost
    # entry was written by the proxy nearest us. With N trusted proxies in
    # front, the real client is the Nth entry from the right; anything further
    # left is client-supplied and untrusted. Clamp so a short chain falls back
    # to the leftmost (client-most) value instead of indexing out of range.
    index = max(0, len(hops) - proxy_count)
    return hops[min(index, len(hops) - 1)] or peer


def _trusted_proxy_count() -> int:
    try:
        return max(0, int(os.environ.get(TRUSTED_PROXY_COUNT_ENV, "0")))
    except ValueError:
        return 0


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
        "/api/review/ai-draft-validation": "ai-draft-validation",
        "/api/review/ai-second-opinion": "ai-second-opinion",
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
