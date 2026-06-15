"""Real DocuSign OAuth (Authorization Code Grant) connection layer.

Mirrors :mod:`google_connection` for DocuSign: it owns the OAuth consent URL,
the authorization-code -> access/refresh token exchange, per-user token storage
with on-expiry refresh, and the ``/oauth/userinfo`` lookup that resolves the
signing user's ``accountId`` + eSignature ``base_uri``. The route layer
(:mod:`routes.docusign`) drives the consent redirect + callback; the eSignature
REST client (:class:`docusign_integration.HttpDocuSignClient`) calls
:func:`access_token_for_user` / :func:`account_for_user` to authorize its live
calls.

This is the REAL, operating auth path — there is no demo/stub token here. The
running app authorizes against DocuSign with the credentials the user grants via
"Connect DocuSign".

Configuration (env, mirroring NDA_GOOGLE_OAUTH_*):
    NDA_DOCUSIGN_CLIENT_ID         — DocuSign integration key (OAuth client id)
    NDA_DOCUSIGN_CLIENT_SECRET     — the integration key's secret
    NDA_DOCUSIGN_OAUTH_REDIRECT_URI— exact redirect URI registered on the app
                                     (e.g. http://127.0.0.1:8787/auth/docusign/callback)
    NDA_DOCUSIGN_AUTH_SERVER       — "demo" (account-d.docusign.com, default) or
                                     "production"/"prod" (account.docusign.com)
    NDA_DOCUSIGN_CONNECT_HMAC_KEY  — Connect webhook HMAC secret (optional but
                                     recommended; see routes.docusign webhook)
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import matter_store
from .google_connection import (
    clean_user_token_segment,
    locked_token_file,
    read_token_json,
    write_token_json_unlocked,
)

CLIENT_ID_ENV = "NDA_DOCUSIGN_CLIENT_ID"
CLIENT_SECRET_ENV = "NDA_DOCUSIGN_CLIENT_SECRET"
REDIRECT_URI_ENV = "NDA_DOCUSIGN_OAUTH_REDIRECT_URI"
AUTH_SERVER_ENV = "NDA_DOCUSIGN_AUTH_SERVER"
CONNECT_HMAC_KEY_ENV = "NDA_DOCUSIGN_CONNECT_HMAC_KEY"

# DocuSign OAuth auth servers. Demo (sandbox) vs production are the two account.*
# hosts; the eSignature REST base_uri is resolved per-account from /oauth/userinfo
# (NOT hardcoded), because production accounts live on region-specific hosts.
AUTH_SERVER_DEMO = "account-d.docusign.com"
AUTH_SERVER_PRODUCTION = "account.docusign.com"

# The eSignature signature scope plus the openid scope needed for /oauth/userinfo.
OAUTH_SCOPES = ("signature", "openid")

# Refresh the access token slightly before its true expiry to avoid a race where
# a token that passes the check expires mid-request.
_TOKEN_EXPIRY_SKEW_SECONDS = 120


class DocuSignConnectionError(RuntimeError):
    """A DocuSign OAuth/connection operation could not be completed."""


class DocuSignNotConnectedError(DocuSignConnectionError):
    """The signed-in user has not connected DocuSign (no usable token)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def client_id() -> str:
    return os.environ.get(CLIENT_ID_ENV, "").strip()


def client_secret() -> str:
    return os.environ.get(CLIENT_SECRET_ENV, "")


def configured_redirect_uri() -> str:
    return os.environ.get(REDIRECT_URI_ENV, "").strip()


def oauth_configured() -> bool:
    return bool(client_id() and client_secret())


def auth_server() -> str:
    """The DocuSign OAuth host — demo by default, production when configured.

    Accepts ``demo``/``sandbox`` and ``production``/``prod``/``live`` (and the
    bare hostnames) so the env var is forgiving. Defaults to the demo sandbox so
    a misconfiguration can never accidentally hit production.
    """
    raw = os.environ.get(AUTH_SERVER_ENV, "").strip().lower()
    if raw in {"production", "prod", "live", AUTH_SERVER_PRODUCTION}:
        return AUTH_SERVER_PRODUCTION
    return AUTH_SERVER_DEMO


def is_production() -> bool:
    return auth_server() == AUTH_SERVER_PRODUCTION


def connect_hmac_key() -> str:
    return os.environ.get(CONNECT_HMAC_KEY_ENV, "")


# ---------------------------------------------------------------------------
# Authorization Code Grant
# ---------------------------------------------------------------------------
def build_authorization_url(*, redirect_uri: str, state: str, login_hint: str = "") -> str:
    """The DocuSign consent URL the connect route redirects the user to.

    ``GET https://{authserver}/oauth/auth?response_type=code&scope=...&
    client_id=...&redirect_uri=...&state=...`` — the standard Authorization Code
    Grant request. ``login_hint`` pre-fills the account email when known.
    """
    if not oauth_configured():
        raise DocuSignConnectionError("DocuSign OAuth is not configured.")
    params = {
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"https://{auth_server()}/oauth/auth?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(code: str, *, redirect_uri: str = "") -> dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens.

    ``POST https://{authserver}/oauth/token`` with HTTP Basic auth carrying
    ``base64(integration_key:secret)`` and an ``application/x-www-form-urlencoded``
    body ``grant_type=authorization_code&code=...``. Returns the raw token
    response (``access_token``, ``refresh_token``, ``expires_in``, ``token_type``).
    ``redirect_uri`` is accepted for call-site symmetry; DocuSign's token endpoint
    does not require it on this grant.
    """
    if not oauth_configured():
        raise DocuSignConnectionError("DocuSign OAuth is not configured.")
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code})
    return _token_request(body)


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Mint a fresh access token from a stored refresh token.

    ``POST /oauth/token`` with ``grant_type=refresh_token&refresh_token=...`` and
    the same Basic-auth client credentials.
    """
    if not oauth_configured():
        raise DocuSignConnectionError("DocuSign OAuth is not configured.")
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token})
    return _token_request(body)


def _token_request(form_body: str) -> dict[str, Any]:
    basic = base64.b64encode(f"{client_id()}:{client_secret()}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"https://{auth_server()}/oauth/token",
        data=form_body.encode("utf-8"),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise DocuSignConnectionError("DocuSign OAuth token exchange failed.") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise DocuSignConnectionError("DocuSign OAuth token exchange returned no access token.")
    return payload


def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Resolve the user's DocuSign account id + eSignature base URI.

    ``GET https://{authserver}/oauth/userinfo`` with a bearer access token returns
    ``{sub, name, email, accounts: [{account_id, is_default, base_uri, ...}]}``.
    The default account's ``base_uri`` is the eSignature REST host for that user.
    """
    request = urllib.request.Request(
        f"https://{auth_server()}/oauth/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise DocuSignConnectionError("DocuSign userinfo lookup failed.") from exc
    if not isinstance(payload, dict):
        raise DocuSignConnectionError("DocuSign userinfo lookup returned no data.")
    return payload


def default_account(userinfo: dict[str, Any]) -> dict[str, str]:
    """Pick the default account ``{account_id, base_uri, account_name, email}``."""
    accounts = userinfo.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise DocuSignConnectionError("DocuSign account has no usable accounts.")
    chosen = None
    for account in accounts:
        if isinstance(account, dict) and account.get("is_default"):
            chosen = account
            break
    if chosen is None:
        chosen = accounts[0] if isinstance(accounts[0], dict) else {}
    account_id = str(chosen.get("account_id") or "").strip()
    base_uri = str(chosen.get("base_uri") or "").strip()
    if not account_id or not base_uri:
        raise DocuSignConnectionError("DocuSign account did not include an account id or base URI.")
    return {
        "account_id": account_id,
        "base_uri": base_uri,
        "account_name": str(chosen.get("account_name") or ""),
        "email": str(userinfo.get("email") or ""),
    }


# ---------------------------------------------------------------------------
# Per-user token storage (mirrors google_connection's file layout)
# ---------------------------------------------------------------------------
DOCUSIGN_TOKEN_FILENAME = "docusign-token.json"
DOCUSIGN_TOKEN_PATH_ENV = "NDA_DOCUSIGN_TOKEN_PATH"


def user_token_path(owner_user_id: str) -> Path:
    owner_segment = clean_user_token_segment(owner_user_id)
    if owner_segment in {"", ".", ".."}:
        raise DocuSignConnectionError("A valid signed-in user is required to store DocuSign tokens.")
    return matter_store.DATA_DIR / "users" / "docusign" / owner_segment / DOCUSIGN_TOKEN_FILENAME


def _shared_token_path() -> Path | None:
    configured = os.environ.get(DOCUSIGN_TOKEN_PATH_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    local = matter_store.DATA_DIR / "docusign" / DOCUSIGN_TOKEN_FILENAME
    return local if local.is_file() else None


def _token_path_for(owner_user_id: str) -> Path:
    owner_segment = clean_user_token_segment(owner_user_id)
    if owner_segment:
        return user_token_path(owner_user_id)
    shared = _shared_token_path()
    if shared is not None:
        return shared
    raise DocuSignNotConnectedError("DocuSign is not connected for this user.")


def save_user_token(owner_user_id: str, token_response: dict[str, Any], account: dict[str, str]) -> None:
    """Persist the access+refresh tokens plus resolved account for ``owner_user_id``.

    Computes an absolute ``expires_at`` epoch from ``expires_in`` so refresh can be
    decided without a clock-skew guess. A re-connect that omits a fresh refresh
    token keeps the previously stored one (DocuSign returns a refresh token on the
    code exchange; subsequent refreshes return a new one).
    """
    owner_segment = clean_user_token_segment(owner_user_id)
    if not owner_segment:
        raise DocuSignConnectionError("A signed-in user is required to connect DocuSign.")
    access_token = str(token_response.get("access_token") or "").strip()
    if not access_token:
        raise DocuSignConnectionError("DocuSign OAuth response did not include an access token.")
    token_path = user_token_path(owner_user_id)
    existing = read_token_json(token_path)
    refresh_token = str(token_response.get("refresh_token") or existing.get("refresh_token") or "").strip()
    if not refresh_token:
        raise DocuSignConnectionError(
            "DocuSign did not return a refresh token. Reconnect DocuSign and approve offline access."
        )
    expires_in = _coerce_int(token_response.get("expires_in"), default=3600)
    payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": str(token_response.get("token_type") or "Bearer"),
        "expires_at": int(time.time()) + max(expires_in, 0),
        "scope": str(token_response.get("scope") or " ".join(OAUTH_SCOPES)),
        "account_id": str(account.get("account_id") or ""),
        "base_uri": str(account.get("base_uri") or ""),
        "account_name": str(account.get("account_name") or ""),
        "email": str(account.get("email") or ""),
        "auth_server": auth_server(),
    }
    with locked_token_file(token_path):
        write_token_json_unlocked(token_path, json.dumps(payload, indent=2) + "\n")


def disconnect_user(owner_user_id: str) -> bool:
    """Remove the user's stored DocuSign token. Returns True when one was removed."""
    owner_segment = clean_user_token_segment(owner_user_id)
    if not owner_segment:
        raise DocuSignConnectionError("A signed-in user is required to disconnect DocuSign.")
    token_path = user_token_path(owner_user_id)
    try:
        token_path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DocuSignConnectionError("DocuSign token could not be removed.") from exc
    return True


def is_connected(owner_user_id: str) -> bool:
    """Whether ``owner_user_id`` has a stored DocuSign token (any validity)."""
    try:
        token_path = _token_path_for(owner_user_id)
    except DocuSignNotConnectedError:
        return False
    return bool(read_token_json(token_path).get("access_token"))


def stored_account(owner_user_id: str) -> dict[str, str]:
    """The resolved account label/id/base_uri for the status panel (or empties)."""
    try:
        token_path = _token_path_for(owner_user_id)
    except DocuSignNotConnectedError:
        return {"account_id": "", "base_uri": "", "account_name": "", "email": ""}
    payload = read_token_json(token_path)
    return {
        "account_id": str(payload.get("account_id") or ""),
        "base_uri": str(payload.get("base_uri") or ""),
        "account_name": str(payload.get("account_name") or ""),
        "email": str(payload.get("email") or ""),
    }


def access_token_for_user(owner_user_id: str) -> str:
    """Return a valid bearer access token, refreshing on expiry, for the user.

    Reads the stored token; if it is within the expiry skew, uses the refresh
    token to mint a new one and persists it. Raises
    :class:`DocuSignNotConnectedError` when the user has no stored token.
    """
    token_path = _token_path_for(owner_user_id)
    with locked_token_file(token_path):
        payload = read_token_json(token_path)
        access_token = str(payload.get("access_token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if not access_token:
            raise DocuSignNotConnectedError("DocuSign is not connected for this user.")
        expires_at = _coerce_int(payload.get("expires_at"), default=0)
        if expires_at - _TOKEN_EXPIRY_SKEW_SECONDS > int(time.time()):
            return access_token
        if not refresh_token:
            # Token is (near) expired and we cannot refresh — force a reconnect.
            raise DocuSignNotConnectedError("DocuSign session expired; reconnect DocuSign.")
        refreshed = refresh_access_token(refresh_token)
        new_access = str(refreshed.get("access_token") or "").strip()
        if not new_access:
            raise DocuSignConnectionError("DocuSign token refresh returned no access token.")
        new_refresh = str(refreshed.get("refresh_token") or refresh_token).strip()
        expires_in = _coerce_int(refreshed.get("expires_in"), default=3600)
        payload.update(
            {
                "access_token": new_access,
                "refresh_token": new_refresh,
                "expires_at": int(time.time()) + max(expires_in, 0),
            }
        )
        write_token_json_unlocked(token_path, json.dumps(payload, indent=2) + "\n")
        return new_access


def account_for_user(owner_user_id: str) -> dict[str, str]:
    """The ``{account_id, base_uri}`` the eSignature client needs for ``owner_user_id``."""
    account = stored_account(owner_user_id)
    if not account.get("account_id") or not account.get("base_uri"):
        raise DocuSignNotConnectedError("DocuSign account is not resolved; reconnect DocuSign.")
    return account


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
