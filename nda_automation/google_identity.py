from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GOOGLE_OAUTH_CLIENT_ID_ENV = "NDA_GOOGLE_OAUTH_CLIENT_ID"
GOOGLE_OAUTH_CLIENT_SECRET_ENV = "NDA_GOOGLE_OAUTH_CLIENT_SECRET"
GOOGLE_OAUTH_REDIRECT_URI_ENV = "NDA_GOOGLE_OAUTH_REDIRECT_URI"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_IDENTITY_SCOPES = ("openid", "email", "profile")


class GoogleIdentityError(RuntimeError):
    pass


def google_oauth_configured() -> bool:
    return bool(google_client_id() and google_client_secret())


def google_client_id() -> str:
    return os.environ.get(GOOGLE_OAUTH_CLIENT_ID_ENV, "").strip()


def google_client_secret() -> str:
    return os.environ.get(GOOGLE_OAUTH_CLIENT_SECRET_ENV, "")


def configured_redirect_uri() -> str:
    return os.environ.get(GOOGLE_OAUTH_REDIRECT_URI_ENV, "").strip()


def build_google_authorization_url(
    *,
    redirect_uri: str,
    state: str,
    scopes: tuple[str, ...] | list[str] = GOOGLE_IDENTITY_SCOPES,
    access_type: str = "",
    prompt: str = "select_account",
    login_hint: str = "",
) -> str:
    client_id = google_client_id()
    if not client_id:
        raise GoogleIdentityError("Google OAuth client ID is not configured.")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "prompt": prompt,
    }
    # access_type=offline (with prompt=consent) is needed to get a refresh token
    # when the login also grants Gmail/Drive, so background sync keeps working.
    if access_type:
        params["access_type"] = access_type
    if login_hint:
        params["login_hint"] = login_hint
    query = urllib.parse.urlencode(params)
    return f"{GOOGLE_AUTH_URL}?{query}"


def exchange_google_code(code: str, *, redirect_uri: str) -> dict[str, Any]:
    client_id = google_client_id()
    client_secret = google_client_secret()
    if not client_id or not client_secret:
        raise GoogleIdentityError("Google OAuth is not configured.")
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return _json_request(request, "Google OAuth token exchange failed.")


def verify_google_id_token(id_token: str) -> dict[str, Any]:
    if not id_token:
        raise GoogleIdentityError("Google OAuth response did not include an ID token.")
    tokeninfo_url = f"{GOOGLE_TOKENINFO_URL}?{urllib.parse.urlencode({'id_token': id_token})}"
    tokeninfo = _json_request(urllib.request.Request(tokeninfo_url), "Google ID token validation failed.")
    expected_audience = google_client_id()
    if expected_audience and str(tokeninfo.get("aud") or "") != expected_audience:
        raise GoogleIdentityError("Google ID token audience does not match this app.")
    if not str(tokeninfo.get("sub") or "").strip():
        raise GoogleIdentityError("Google ID token did not include a subject.")
    if str(tokeninfo.get("email_verified") or "true").lower() == "false":
        raise GoogleIdentityError("Google account email is not verified.")
    return tokeninfo


def _json_request(request: urllib.request.Request, error_message: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Google returns the real reason (invalid_grant / redirect_uri_mismatch /
        # invalid_client / ...) in the JSON body of the 4xx. The generic wrapper
        # alone makes these undiagnosable, so read the body, surface the reason in
        # the raised message, and log it. The detail is sanitised (single line,
        # length-capped) and carries no client secret, so it is safe to expose.
        detail = _http_error_detail(exc)
        message = f"{error_message} ({detail})" if detail else error_message
        _log_oauth_failure(error_message, status=exc.code, detail=detail)
        raise GoogleIdentityError(message) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        _log_oauth_failure(error_message, status=None, detail=exc.__class__.__name__)
        raise GoogleIdentityError(error_message) from exc
    if not isinstance(payload, dict):
        raise GoogleIdentityError(error_message)
    return payload


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """Extract Google's `error`/`error_description` from a token-endpoint 4xx.

    Returns a short, single-line, secret-free description suitable for both the
    user-facing error and the server log, or "" if the body is unreadable.
    """
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # pragma: no cover - body already consumed / unreadable
        raw = ""
    error_code = ""
    error_description = ""
    if raw:
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict):
            error_code = str(body.get("error") or "").strip()
            error_description = str(body.get("error_description") or "").strip()
    detail = error_code
    if error_description and error_description != error_code:
        detail = f"{error_code}: {error_description}" if error_code else error_description
    if not detail:
        detail = raw.strip()
    return _sanitize_detail(detail)


def _sanitize_detail(detail: str) -> str:
    collapsed = " ".join(str(detail or "").split())
    return collapsed[:200]


def _log_oauth_failure(error_message: str, *, status: int | None, detail: str) -> None:
    status_part = f" status={status}" if status is not None else ""
    detail_part = f" detail={detail}" if detail else ""
    print(f"{error_message}{status_part}{detail_part}", file=sys.stderr)
