from __future__ import annotations

import os
import threading
import time

DEFAULT_RATE_LIMIT_PER_MINUTE = 300
RATE_LIMITED_MESSAGE = "Too many requests. Try again shortly."
TRUSTED_PROXY_COUNT_ENV = "NDA_TRUSTED_PROXY_COUNT"

# Byte/render GET routes (document source bytes, DOCX->PDF render, page images,
# reconstructed DOCX/PDF) are expensive: each can pin a soffice/pdf2docx slot and
# rasterize pages. They were unbucketed (returning "" below), so one
# authenticated user could hammer them and starve other tenants of both render
# slots. They get their OWN per-caller bucket with a SEPARATE, more generous
# limit so an interactive review (which fans out source + render-status + page
# images for a multi-page doc) is never throttled, while an abusive loop is.
RENDER_GET_BUCKET = "render-bytes"
# 240/min (4/sec) per caller. A single interactive review of a multi-page doc
# bursts source + repeated render-status polls + per-page image fetches, so the
# cap is set well above a realistic review burst while still hard-bounding an
# abusive loop that would otherwise pin both soffice/pdf2docx slots and starve
# other tenants. Operators can tune it via the env knob below.
DEFAULT_RENDER_GET_RATE_LIMIT_PER_MINUTE = 240
RENDER_GET_RATE_LIMIT_ENV = "NDA_RENDER_GET_RATE_LIMIT_PER_MINUTE"

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
    limit = _rate_limit_per_window_for_bucket(bucket_name)
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
    if method == "GET":
        if path == "/api/matters/export":
            return "matter-backup"
        if _is_render_get_path(path):
            return RENDER_GET_BUCKET
        return ""
    if method != "POST":
        return ""
    buckets = {
        "/api/review": "review",
        "/api/review/ai-draft-validation": "ai-draft-validation",
        "/api/review/ai-second-opinion": "ai-second-opinion",
        "/api/matters": "matter-upload",
        "/api/export-review-docx": "docx-export",
        "/api/gmail/send-redline": "gmail-send-redline",
        # The two heaviest endpoints (each fans out to AI generation/assistant
        # work). Given this app's cost-storm history, bucket them so a single
        # caller cannot drive an unbounded AI spend storm.
        "/api/generate-nda": "generate-nda",
        "/api/dashboard/assistant": "dashboard-assistant",
    }
    return buckets.get(path, "")


# Suffixes/segments of the matter byte/render GET routes. These live under
# /api/matters/<id>/... so they cannot be matched by exact path; we match the
# trailing segment (and the /render-page/ infix for per-page image fetches).
# Mirrors the route dispatch in server.do_GET. Every entry here is a route that
# does per-request RENDER work or streams a (potentially large) document, the
# expensive class this bucket exists to bound:
#   * /marked-up-pdf re-opens the PDF in PyMuPDF and stamps every stored
#     annotation on EVERY request (routes/pdf_markup.bake_user_annotations) --
#     unbucketed, an authenticated loop runs unbounded fitz render work.
#   * /signed-document streams the stored executed PDF, but when none is captured
#     yet it triggers a live DocuSign API sync; bucketing bounds both the
#     large-bytes stream and that external-call fan-out per caller.
_RENDER_GET_SUFFIXES = (
    "/source",
    "/source-pdf",
    "/source-docx",
    "/render-status",
    "/render-pdf",
    "/reviewed-docx",
    "/reviewed-pdf",
    "/working-docx",
    "/marked-up-pdf",
    "/signed-document",
)


def _is_render_get_path(path: str) -> bool:
    if not path.startswith("/api/matters/"):
        return False
    if "/render-page/" in path:
        return True
    return any(path.endswith(suffix) for suffix in _RENDER_GET_SUFFIXES)


def _rate_limit_per_window_for_bucket(bucket_name: str) -> int:
    """Resolve the per-window request cap for a bucket.

    Most buckets share the global NDA_RATE_LIMIT_PER_MINUTE cap. The byte/render
    GET bucket has its OWN configurable cap (NDA_RENDER_GET_RATE_LIMIT_PER_MINUTE)
    so the expensive render routes can be throttled independently of (and more
    generously than) the general API, without breaking an interactive review.
    """
    if bucket_name == RENDER_GET_BUCKET:
        return _render_get_rate_limit_per_window()
    return _rate_limit_per_window()


def _render_get_rate_limit_per_window() -> int:
    try:
        return max(
            0,
            int(
                os.environ.get(
                    RENDER_GET_RATE_LIMIT_ENV, str(DEFAULT_RENDER_GET_RATE_LIMIT_PER_MINUTE)
                )
            ),
        )
    except ValueError:
        return DEFAULT_RENDER_GET_RATE_LIMIT_PER_MINUTE


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
