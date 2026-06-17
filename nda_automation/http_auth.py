from __future__ import annotations

import base64
import binascii
import hmac
import os

AUTH_REALM = "nda-automation"
AUTH_REQUIRED_MESSAGE = "Authentication required."
AUTH_NOT_CONFIGURED_MESSAGE = "Authentication is required but neither Google OAuth nor HTTP Basic auth is configured."
ADMIN_REQUIRED_MESSAGE = "Administrator access is required."
ADMIN_USERS_ENV = "NDA_ADMIN_USERS"


def _admin_user_ids() -> set[str]:
    raw = os.environ.get(ADMIN_USERS_ENV, "")
    return {value.strip() for value in raw.split(",") if value.strip()}


def request_is_admin(*, user_id: str, provider: str, host: str) -> bool:
    """Return whether the authenticated caller may use admin-only endpoints.

    Admin identities are explicitly listed in NDA_ADMIN_USERS. When no list is
    configured we FAIL CLOSED: no authenticated caller is admin. An empty
    admin list used to fall back to "any HTTP Basic caller is admin", which on a
    deployment that shares one Basic credential across all users silently made
    every authenticated user an administrator. On a loopback host where
    authentication is not required, the local developer is still trusted,
    matching how the rest of the app treats loopback.
    """
    if not _auth_required_for_host(host):
        return True
    admin_ids = _admin_user_ids()
    if not admin_ids:
        # Fail closed: with no configured admin identities, real authenticated
        # callers get no admin access. (Loopback dev is handled above.)
        return False
    return str(user_id or "").strip() in admin_ids


def _basic_auth_matches(header: str, username: str, password: str) -> bool:
    credentials = _basic_auth_credentials(header)
    if credentials is None:
        return False
    supplied_username, supplied_password = credentials
    return hmac.compare_digest(
        supplied_username.encode("utf-8"),
        username.encode("utf-8"),
    ) and hmac.compare_digest(
        supplied_password.encode("utf-8"),
        password.encode("utf-8"),
    )


def _basic_auth_credentials(header: str) -> tuple[str, str] | None:
    prefix = "Basic "
    if not header.startswith(prefix):
        return None
    try:
        decoded = base64.b64decode(header[len(prefix) :], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    supplied_username, separator, supplied_password = decoded.partition(":")
    if not separator:
        return None
    return supplied_username, supplied_password


def _auth_required_for_host(host: str) -> bool:
    if _env_flag_enabled("NDA_REQUIRE_AUTH"):
        return True
    if _basic_auth_configured() or _google_oauth_configured():
        return True
    return not _is_loopback_host(host)


def _auth_method_configured() -> bool:
    return _basic_auth_configured() or _google_oauth_configured()


def _basic_auth_configured() -> bool:
    return bool(os.environ.get("NDA_AUTH_USERNAME", "").strip() and os.environ.get("NDA_AUTH_PASSWORD", ""))


def _google_oauth_configured() -> bool:
    return bool(
        os.environ.get("NDA_GOOGLE_OAUTH_CLIENT_ID", "").strip()
        and os.environ.get("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "")
    )


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
