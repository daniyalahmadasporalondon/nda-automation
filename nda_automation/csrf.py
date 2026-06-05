from __future__ import annotations

import os
from urllib.parse import urlsplit

from .http_auth import _configured_allowed_hosts, _host_only

CSRF_REJECTED_MESSAGE = "Cross-site request blocked."
CSRF_ENFORCE_ENV = "NDA_ENFORCE_CSRF"

# State-changing methods are the only ones that mutate server state, so the
# Origin/Referer cross-site check is scoped to them. Safe (read-only) methods
# are never gated because GET/HEAD cannot be abused to change state.
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def origin_allowed_for_request(
    *,
    method: str,
    origin_header: str,
    referer_header: str,
    host_header: str,
    bind_host: str,
) -> bool:
    """Return whether a state-changing request's origin is same-site.

    The browser attaches ``Origin`` (and, as a fallback, ``Referer``) on
    cross-site form posts and ``fetch`` calls. A forged cross-site request
    therefore carries an origin that does not match the server's own host, so
    rejecting any state-changing request whose declared origin is not in the
    allowed host set defeats CSRF without needing a per-form token. Cached HTTP
    Basic credentials — which ``SameSite`` cookies cannot protect — are covered
    because the check is on the request origin, not on the auth mechanism.
    """
    if method.upper() not in STATE_CHANGING_METHODS:
        return True
    if not _csrf_enforced(bind_host):
        return True
    declared = _request_origin_host(origin_header, referer_header)
    if declared is None:
        # No Origin and no Referer. Same-origin ``fetch``/XHR always send an
        # Origin on state-changing requests, and HTML forms send a Referer, so a
        # request that carries neither is treated as untrusted when enforcement
        # is on for this host.
        return False
    return declared in _allowed_origin_hosts(host_header, bind_host)


def _request_origin_host(origin_header: str, referer_header: str) -> str | None:
    origin = _origin_host(origin_header)
    if origin is not None:
        return origin
    # Some browsers omit Origin on same-origin GET-turned-POST navigations but
    # still send Referer; fall back to it so legitimate form posts are allowed.
    return _origin_host(referer_header)


def _origin_host(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw.lower() == "null":
        return None
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return None
    return _host_only(parts.netloc).lower()


def _allowed_origin_hosts(host_header: str, bind_host: str) -> set[str]:
    allowed = {"localhost", "127.0.0.1", "::1"}
    if bind_host:
        allowed.add(bind_host.lower())
    # The request's own Host header is the canonical public hostname behind the
    # proxy; trusting it here matches how host_header_allowed already validates
    # Host, so same-site requests to the deployed hostname pass.
    request_host = _host_only(host_header).lower()
    if request_host:
        allowed.add(request_host)
    allowed |= {value.lower() for value in _configured_allowed_hosts()}
    return {value for value in allowed if value}


def _csrf_enforced(bind_host: str) -> bool:
    flag = os.environ.get(CSRF_ENFORCE_ENV, "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    # Default on for non-loopback binds (public deployments), off for local
    # development against 127.0.0.1 where there is no cross-site browser context.
    return bind_host not in {"127.0.0.1", "::1", "localhost"}
