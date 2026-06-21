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
import re
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
# Explicit opt-in that permits the PUBLIC, session-less /api/docusign/webhook to be
# processed WITHOUT an HMAC signature (because CONNECT_HMAC_KEY_ENV is unset). This
# exists ONLY so local/demo testing keeps working without configuring a Connect key.
# It must NEVER be set on a public deployment: with no key the webhook is
# unauthenticated, so any caller could forge a "completed" event and flip a matter
# to executed. The webhook fails CLOSED unless this flag is set OR the server is
# bound to a loopback interface (the trusted local developer). See
# routes/docusign._verify_hmac.
ALLOW_UNSIGNED_WEBHOOK_ENV = "NDA_DOCUSIGN_ALLOW_UNSIGNED_WEBHOOK"

# A SINGLE default Aspora signatory (name + email) used for ALL Aspora signing
# entities when routing an envelope. The per-entity registry signatory is a
# ``[Authorised Signatory]`` placeholder with no email, so DocuSign cannot route
# Aspora's signing copy from it; when BOTH of these are set the workflow uses this
# one identity as the Aspora recipient on every generated NDA (see
# :func:`docusign_workflow._aspora_signer`).
ASPORA_SIGNER_NAME_ENV = "NDA_DOCUSIGN_ASPORA_SIGNER_NAME"
ASPORA_SIGNER_EMAIL_ENV = "NDA_DOCUSIGN_ASPORA_SIGNER_EMAIL"

# Local-dev DocuSign owner. In NO-LOGIN mode (loopback bind, no Basic/Google auth
# configured — see http_auth._auth_required_for_host) there is no signed-in user,
# so request_owner_user_id() is "". Matter access treats that empty owner as the
# single-tenant wildcard, but DocuSign tokens are keyed per owner and an empty
# owner cannot be stored (save_user_token rejects it) — so OAuth consent would
# complete yet the token-save would fail, leaving connected:false. To make the
# local connect STICK, the OAuth lifecycle resolves a STABLE owner id for this
# mode only. This NEVER affects a deployment with real auth: when auth IS required
# the resolver returns the request owner unchanged and the empty-owner branch is
# never taken (an authenticated request always carries a real owner).
LOCAL_DEV_OWNER_ENV = "NDA_DOCUSIGN_LOCAL_DEV_OWNER"
DEFAULT_LOCAL_DEV_OWNER = "local-dev"

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


class DocuSignCredentialError(DocuSignConnectionError):
    """DocuSign rejected the APP credentials (integration key / secret).

    Raised when the OAuth server answers a token exchange/refresh with an
    authentication/credential error (``invalid_client``, ``unauthorized_client``,
    ``access_denied`` for the app, or HTTP 401). This is distinct from a
    transient/network failure: it points the operator at the configured
    ``NDA_DOCUSIGN_CLIENT_ID`` / ``NDA_DOCUSIGN_CLIENT_SECRET`` (a typo'd key or
    secret) rather than at "DocuSign is down". The message NEVER echoes the secret.
    """


class DocuSignTransientError(DocuSignConnectionError):
    """DocuSign could not be reached / answered transiently (network, timeout, 5xx).

    Distinct from :class:`DocuSignCredentialError`: retrying may succeed and the
    credentials are not implicated. Surfaced as "temporarily unreachable, try again".
    """


class DocuSignNotConnectedError(DocuSignConnectionError):
    """The signed-in user has not connected DocuSign (no usable token)."""


class DocuSignReconnectRequiredError(DocuSignNotConnectedError):
    """The user's DocuSign authorization is DEAD and a fresh reconnect is required.

    Raised when a stored refresh token can no longer mint an access token because
    the consent was REVOKED or the refresh token expired beyond renewal — DocuSign
    answers the refresh with ``invalid_grant`` / a 400 on the refresh path. A
    subclass of :class:`DocuSignNotConnectedError` so every existing ``needs_connect``
    handler still catches it, but distinct so the status panel + send-for-signature
    path can prompt "Reconnect DocuSign" instead of a generic outage blip.
    """


# DocuSign OAuth ``error`` codes that indicate the APP credentials are wrong (a
# typo'd integration key or secret), NOT a transient outage. The HTTP-401 status is
# the stronger, status-level signal and is handled explicitly alongside these.
_CREDENTIAL_OAUTH_ERRORS = frozenset(
    {"invalid_client", "unauthorized_client", "access_denied"}
)

# DocuSign OAuth ``error`` codes that, ON THE REFRESH PATH, mean the user's stored
# grant is dead (consent revoked / refresh token expired) → reconnect required. On
# the code-exchange path these instead mean a stale/used auth code, so the caller
# passes ``context`` to disambiguate (see :func:`_classify_token_http_error`).
_REVOKED_GRANT_OAUTH_ERRORS = frozenset({"invalid_grant", "consent_required"})


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


# A DocuSign integration key (OAuth client id) is a GUID, e.g.
# ``aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee``. This is a CHEAP, OFFLINE shape check: a
# well-formed key is a valid GUID. It does NOT prove the key is registered on the
# DocuSign side (only the live token exchange can) — but a key that fails this is
# DEFINITELY a typo/paste error, which is the most common misconfiguration. The
# secret has no published public format, so we only check it is non-blank.
_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def client_id_is_wellformed() -> bool:
    """Whether the configured integration key LOOKS like a DocuSign GUID.

    Offline shape check only — see :data:`_GUID_RE`. Returns ``False`` for an unset
    or obviously-malformed (truncated / extra-character / non-GUID) key.
    """
    return bool(_GUID_RE.match(client_id()))


def config_health() -> dict[str, Any]:
    """A structured, OFFLINE config-health signal for the DocuSign status panel.

    Reports whether the OAuth app credentials are PRESENT and well-FORMED, with a
    machine code + human message, so an operator can tell a misconfiguration apart
    from a DocuSign outage WITHOUT a consent flow or any network call. It never
    reads or echoes the secret value (only whether it is set).

    Why no live client-id pre-check: DocuSign's Authorization Code Grant has no
    clean UNAUTHENTICATED endpoint that distinguishes "unknown integration key"
    from "ok" — the ``/oauth/auth`` authorize endpoint returns an interactive
    HTML login/consent page (not a machine-parseable ``invalid_client``) and only
    the token exchange (which needs a user-consented code) yields the definitive
    ``invalid_client``. So this is deliberately offline; the AUTHORITATIVE credential
    verdict comes from the connect-callback token exchange, which now surfaces a
    credential-specific error (see :class:`DocuSignCredentialError`).

    Codes:
        ``ok``                 — both present and the key is GUID-shaped.
        ``missing``            — client id and/or secret not set.
        ``client_id_malformed``— secret set + id set but the id is not a GUID
                                 (almost certainly a typo/paste error).
    """
    has_id = bool(client_id())
    has_secret = bool(client_secret())
    wellformed_id = client_id_is_wellformed()
    if not has_id or not has_secret:
        missing = []
        if not has_id:
            missing.append(CLIENT_ID_ENV)
        if not has_secret:
            missing.append(CLIENT_SECRET_ENV)
        return {
            "code": "missing",
            "client_id_present": has_id,
            "client_secret_present": has_secret,
            "client_id_wellformed": wellformed_id,
            "ok": False,
            "message": (
                "DocuSign OAuth is not configured. Set "
                + " and ".join(missing)
                + ", then restart."
            ),
        }
    if not wellformed_id:
        return {
            "code": "client_id_malformed",
            "client_id_present": True,
            "client_secret_present": True,
            "client_id_wellformed": False,
            "ok": False,
            "message": (
                f"{CLIENT_ID_ENV} is set but is not a valid DocuSign integration "
                "key (expected a GUID like aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee). "
                "Check it for a typo."
            ),
        }
    return {
        "code": "ok",
        "client_id_present": True,
        "client_secret_present": True,
        "client_id_wellformed": True,
        "ok": True,
        "message": "DocuSign app credentials are present and well-formed.",
    }


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


def allow_unsigned_webhook() -> bool:
    """Whether the unsigned-webhook escape hatch is explicitly opted into.

    True only when ``NDA_DOCUSIGN_ALLOW_UNSIGNED_WEBHOOK`` is set to a truthy value.
    This is the EXPLICIT opt-in that lets the public ``/api/docusign/webhook`` be
    processed when no HMAC key is configured. Without it (and off a loopback bind)
    the webhook fails CLOSED. See ``routes/docusign._verify_hmac``.
    """
    return os.environ.get(ALLOW_UNSIGNED_WEBHOOK_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def aspora_default_signer() -> dict[str, str] | None:
    """The single default Aspora signatory ``{name, email}``, or ``None``.

    Reads ``NDA_DOCUSIGN_ASPORA_SIGNER_NAME`` + ``NDA_DOCUSIGN_ASPORA_SIGNER_EMAIL``
    and returns a routable signatory ONLY when BOTH are set (and the email looks
    like a real address). This single identity stands in for the per-entity
    registry signatory (a ``[Authorised Signatory]`` placeholder with no email) so
    Aspora becomes a routable signer on every generated NDA. When either is unset
    returns ``None`` so the caller keeps its current behaviour (omit Aspora when no
    routable email) — fully backward compatible.
    """
    name = os.environ.get(ASPORA_SIGNER_NAME_ENV, "").strip()
    email = os.environ.get(ASPORA_SIGNER_EMAIL_ENV, "").strip()
    if not name or not email or "@" not in email:
        return None
    return {"name": name, "email": email}


def local_dev_owner_user_id() -> str:
    """The stable local-dev owner id used in no-login mode (env-overridable)."""
    configured = os.environ.get(LOCAL_DEV_OWNER_ENV, "").strip()
    return configured or DEFAULT_LOCAL_DEV_OWNER


def _no_login_mode() -> bool:
    """Whether the app is running with authentication effectively disabled.

    True only when no auth method is configured (neither Google OAuth nor HTTP
    Basic) AND auth is not force-enabled via ``NDA_REQUIRE_AUTH``. This is the same
    condition under which ``server._authorize_request`` leaves ``current_user_id``
    empty on a loopback bind (the trusted local developer). A deployment with real
    auth — or any host that forces auth — is NEVER in this mode, so the local-dev
    owner substitution can never fire there.
    """
    from .http_auth import _auth_method_configured, _env_flag_enabled

    if _env_flag_enabled("NDA_REQUIRE_AUTH"):
        return False
    return not _auth_method_configured()


def resolve_owner_user_id(owner_user_id: str, *, host: str = "") -> str:
    """Resolve the DocuSign token owner, substituting a local-dev id in no-login mode.

    Returns ``owner_user_id`` unchanged whenever it is non-empty (every
    authenticated request) OR whenever any auth method is configured / forced
    (production / any configured-auth deployment). Only when the owner is empty AND
    the app is in no-login mode — the loopback no-login developer path — does it
    return the stable :func:`local_dev_owner_user_id`, so the OAuth token can be
    stored and read back under a real owner and ``connected:true`` works locally.

    ``host`` is accepted for call-site symmetry but no-login detection is
    host-agnostic (it keys off whether auth is configured), so token-layer callers
    that have no request host still resolve correctly.

    This is the SINGLE substitution point: it touches DocuSign token ownership only
    and never matter ownership (matter access keeps using the empty-owner wildcard).
    """
    owner = str(owner_user_id or "").strip()
    if owner:
        return owner
    if not _no_login_mode():
        return owner
    return local_dev_owner_user_id()


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
    return _token_request(body, context="exchange")


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Mint a fresh access token from a stored refresh token.

    ``POST /oauth/token`` with ``grant_type=refresh_token&refresh_token=...`` and
    the same Basic-auth client credentials. On a revoked/expired grant (DocuSign
    answers ``invalid_grant`` / a 400 on this path) raises
    :class:`DocuSignReconnectRequiredError` so the caller can prompt a reconnect
    rather than reporting a generic outage.
    """
    if not oauth_configured():
        raise DocuSignConnectionError("DocuSign OAuth is not configured.")
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token})
    return _token_request(body, context="refresh")


def _token_request(form_body: str, *, context: str = "exchange") -> dict[str, Any]:
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
    except urllib.error.HTTPError as exc:
        # An HTTP error from the OAuth server carries a structured reason. A 400/401
        # with an ``invalid_client`` / ``unauthorized_client`` error body (or a bare
        # 401) means the configured app credentials were rejected — a typo'd
        # integration key or secret — NOT an outage. A 5xx is a real transient
        # DocuSign-side failure. Distinguish so the operator isn't sent chasing a
        # phantom outage. The error body is parsed for the OAuth ``error`` code; the
        # secret is never read back or logged.
        raise _classify_token_http_error(exc, context=context) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Could not even reach DocuSign (DNS, connection refused, timeout): transient.
        raise DocuSignTransientError(
            "DocuSign temporarily unreachable, try again."
        ) from exc
    except json.JSONDecodeError as exc:
        # A 2xx with an unparseable body — treat as a transient DocuSign glitch.
        raise DocuSignTransientError(
            "DocuSign returned an unreadable token response, try again."
        ) from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise DocuSignConnectionError("DocuSign OAuth token exchange returned no access token.")
    return payload


def _classify_token_http_error(
    exc: urllib.error.HTTPError, *, context: str = "exchange"
) -> DocuSignConnectionError:
    """Map a DocuSign OAuth-token ``HTTPError`` to a specific error class.

    Reads the (small) OAuth error body for its ``error`` code and combines it with
    the HTTP status and the call ``context`` ("exchange" of an auth code vs
    "refresh" of a stored token):

    * ``invalid_client`` / ``unauthorized_client`` / ``access_denied``, or any 401
      → :class:`DocuSignCredentialError` (check the integration key / secret). This
      takes precedence regardless of context.
    * On the REFRESH path, ``invalid_grant`` / ``consent_required`` (or a 400 with no
      clearer code) → :class:`DocuSignReconnectRequiredError`: the stored grant is
      dead (revoked / expired beyond renewal), the user must reconnect.
    * 5xx → :class:`DocuSignTransientError` (DocuSign-side outage, retry).
    * anything else (e.g. a 400 ``invalid_grant`` on the EXCHANGE path from a
      stale/used auth code) → a generic :class:`DocuSignConnectionError`, NOT
      mis-attributed to the credentials.

    Never raises on a malformed body and never echoes request credentials.
    """
    status = getattr(exc, "code", 0) or 0
    error_code = _read_oauth_error_code(exc)
    if error_code in _CREDENTIAL_OAUTH_ERRORS or status == 401:
        return DocuSignCredentialError(
            "DocuSign rejected the app credentials — check NDA_DOCUSIGN_CLIENT_ID / "
            "NDA_DOCUSIGN_CLIENT_SECRET."
        )
    if context == "refresh" and (error_code in _REVOKED_GRANT_OAUTH_ERRORS or status == 400):
        return DocuSignReconnectRequiredError(
            "Your DocuSign authorization is no longer valid (access was revoked or "
            "expired). Reconnect DocuSign to continue."
        )
    if status >= 500:
        return DocuSignTransientError("DocuSign temporarily unreachable, try again.")
    return DocuSignConnectionError(
        "DocuSign rejected the authorization request. Reconnect DocuSign and try again."
    )


def _read_oauth_error_code(exc: urllib.error.HTTPError) -> str:
    """Best-effort extract of the OAuth ``error`` code from an ``HTTPError`` body.

    DocuSign answers a rejected token request with a small JSON body such as
    ``{"error": "invalid_client", "error_description": "..."}``. Returns the lowercased
    ``error`` code, or "" when the body is absent/unparseable. Only the ``error``
    code is read — never ``error_description`` (which could echo input) and never the
    request credentials.
    """
    try:
        raw = exc.read()
    except (OSError, ValueError):
        return ""
    if not raw:
        return ""
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(body, dict):
        return ""
    return str(body.get("error") or "").strip().lower()


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
    owner_user_id = resolve_owner_user_id(owner_user_id)
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
    owner_user_id = resolve_owner_user_id(owner_user_id)
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
    owner_user_id = resolve_owner_user_id(owner_user_id)
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
            _mark_needs_reconnect_unlocked(token_path, payload)
            raise DocuSignReconnectRequiredError("DocuSign session expired; reconnect DocuSign.")
        try:
            refreshed = refresh_access_token(refresh_token)
        except DocuSignReconnectRequiredError:
            # The stored grant is dead (revoked / expired beyond renewal). Persist a
            # durable ``needs_reconnect`` marker so a CHEAP status read (which does no
            # network refresh) can report it and prompt a reconnect, then re-raise.
            _mark_needs_reconnect_unlocked(token_path, payload)
            raise
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
        # A successful refresh clears any stale reconnect marker.
        payload.pop("needs_reconnect", None)
        write_token_json_unlocked(token_path, json.dumps(payload, indent=2) + "\n")
        return new_access


def _mark_needs_reconnect_unlocked(token_path: Path, payload: dict[str, Any]) -> None:
    """Persist ``needs_reconnect=true`` on the stored token (best-effort).

    Called while the token file lock is already held. A write failure here must
    never mask the underlying reconnect-required error, so it is swallowed.
    """
    try:
        payload = dict(payload)
        payload["needs_reconnect"] = True
        write_token_json_unlocked(token_path, json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass


def needs_reconnect(owner_user_id: str) -> bool:
    """Whether the user's stored DocuSign grant is known to be dead (reconnect needed).

    Reads the durable ``needs_reconnect`` marker persisted when a token refresh last
    failed with a revoked/expired grant. A pure read — does NO network call — so the
    status panel can surface a "Reconnect DocuSign" prompt cheaply. Returns ``False``
    when there is no token or no marker.
    """
    try:
        token_path = _token_path_for(owner_user_id)
    except DocuSignNotConnectedError:
        return False
    return bool(read_token_json(token_path).get("needs_reconnect"))


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
