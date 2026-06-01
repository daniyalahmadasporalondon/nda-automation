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
    return hmac.compare_digest(supplied_username, username) and hmac.compare_digest(supplied_password, password)


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
