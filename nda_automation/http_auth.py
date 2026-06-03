from __future__ import annotations

import base64
import binascii
import hmac
import os

AUTH_REALM = "nda-automation"
AUTH_REQUIRED_MESSAGE = "Authentication required."
AUTH_NOT_CONFIGURED_MESSAGE = "Authentication is required but NDA_AUTH_USERNAME and NDA_AUTH_PASSWORD are not configured."


def _basic_auth_matches(header: str, username: str, password: str) -> bool:
    prefix = "Basic "
    if not header.startswith(prefix):
        return False
    try:
        decoded = base64.b64decode(header[len(prefix) :], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    supplied_username, separator, supplied_password = decoded.partition(":")
    if not separator:
        return False
    return hmac.compare_digest(
        supplied_username.encode("utf-8"),
        username.encode("utf-8"),
    ) and hmac.compare_digest(
        supplied_password.encode("utf-8"),
        password.encode("utf-8"),
    )


def _auth_required_for_host(host: str) -> bool:
    if _env_flag_enabled("NDA_REQUIRE_AUTH"):
        return True
    if os.environ.get("NDA_AUTH_USERNAME") or os.environ.get("NDA_AUTH_PASSWORD"):
        return True
    return not _is_loopback_host(host)


def _is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _env_flag_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


AUTH_ALLOWED_HOSTS_ENV = "NDA_ALLOWED_HOSTS"
HOST_NOT_ALLOWED_MESSAGE = "Request host is not allowed."


def host_header_allowed(host_header: str, bind_host: str) -> bool:
    configured = _configured_allowed_hosts()
    # A public bind (0.0.0.0/::) without an explicit allowlist relies on basic
    # auth, so do not reject by Host there. A loopback bind always enforces the
    # allowlist, which is what defeats DNS-rebinding against the local server.
    if bind_host in {"0.0.0.0", "::", ""} and not configured:
        return True
    host = _host_only(host_header)
    if not host:
        # HTTP/1.0 clients may omit Host; a loopback server is not reachable
        # cross-origin, so an absent Host is not a rebinding vector.
        return True
    allowed = {"localhost", "127.0.0.1", "::1"}
    if bind_host:
        allowed.add(bind_host)
    allowed |= configured
    return host in allowed


def _host_only(host_header: str) -> str:
    value = str(host_header or "").strip()
    if value.startswith("["):  # IPv6 literal, e.g. [::1]:8787
        end = value.find("]")
        return value[1:end] if end != -1 else value
    return value.split(":", 1)[0]


def _configured_allowed_hosts() -> set[str]:
    raw = os.environ.get(AUTH_ALLOWED_HOSTS_ENV, "")
    return {value.strip() for value in raw.split(",") if value.strip()}
