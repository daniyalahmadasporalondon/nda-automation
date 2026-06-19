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


def request_is_admin(*, user_id: str, provider: str, host: str, email: str = "") -> bool:
    """Return whether the authenticated caller may use admin-only endpoints.

    Admin authority comes from TWO sources, env roots first:

    1. The immutable NDA_ADMIN_USERS env set. An entry is matched against EITHER:
         * the caller's ``user_id`` (verbatim) -- this preserves ``google:<sub>``
           ids and basic-auth usernames, fully backward compatible; OR
         * for a Google caller ONLY, the OAuth-VERIFIED ``email`` (normalized the
           SAME way persisted admin emails are). This is what lets the env set be
           configured BY EMAIL: ``NDA_ADMIN_USERS`` may now hold any mix of emails
           and ``google:<sub>`` ids. The email path is gated on
           ``provider == "google"`` so a basic-auth username that merely equals an
           admin email never inherits admin via the *normalized email* match.
       These are the bootstrap admins and can never be removed from the in-app
       manager.
    2. The persisted admin-email list (managed in-app), but ONLY for a Google
       caller whose email is OAuth-VERIFIED. ``email`` must be the session's
       Google-verified address; a basic-auth username that merely equals an admin
       email must never inherit admin (provider gate below), and a request body
       field must never be passed here.

    The env-root email match and the persisted-email match use the SAME
    normalization (``app_settings.normalize_admin_email``) so the two sources
    behave identically.

    When neither source matches we FAIL CLOSED. On a loopback host where
    authentication is not required, the local developer is still trusted. The
    persisted-list lookup imports ``app_settings`` lazily (circular-import safety)
    and any error there fails closed.
    """
    if not _auth_required_for_host(host):
        return True
    admin_ids = _admin_user_ids()
    # Legacy/backward-compatible match: any verbatim env entry == the user_id
    # (``google:<sub>`` ids and basic-auth usernames, including email-shaped ones).
    if admin_ids and str(user_id or "").strip() in admin_ids:
        return True
    # Email grants apply ONLY to a Google-verified email. The provider gate is
    # what stops a basic-auth username colliding with an admin email from
    # inheriting admin via the NORMALIZED-email match. Both the env-root email
    # subset and the persisted-email list are consulted, with one normalization.
    if str(provider or "").strip().lower() == "google":
        normalized_email = _normalize_admin_email_safe(email)
        if normalized_email and (
            normalized_email in _env_admin_emails_safe(admin_ids)
            or normalized_email in _persisted_admin_emails_safe()
        ):
            return True
    return False


def _normalize_admin_email_safe(value: object) -> str:
    """Normalize an email the SAME way persisted admins are, failing to "".

    Reuses ``app_settings.normalize_admin_email`` so env-root and persisted email
    matching share one normalization. Imports lazily (circular-import safety) and
    returns "" on any error so an unparseable email never matches.
    """
    try:
        from . import app_settings

        return app_settings.normalize_admin_email(value)
    except Exception:
        return ""


def _env_admin_emails_safe(admin_ids: set[str]) -> set[str]:
    """The normalized email subset of the NDA_ADMIN_USERS entries.

    Non-email entries (``google:<sub>`` ids, non-email basic-auth usernames)
    normalize to "" and are dropped here -- those are matched against ``user_id``
    by the verbatim/legacy path instead.
    """
    return {normalized for entry in admin_ids if (normalized := _normalize_admin_email_safe(entry))}


def _persisted_admin_emails_safe() -> set[str]:
    """The persisted admin-email set, failing CLOSED (empty) on any error."""
    try:
        from . import app_settings

        return app_settings.persisted_admin_emails()
    except Exception:
        return set()


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
