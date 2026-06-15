from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from . import app_settings, export_service, matter_store, user_store
from .http_auth import (
    AUTH_ALLOWED_HOSTS_ENV,
    AUTH_NOT_CONFIGURED_MESSAGE,
    _auth_method_configured,
    _auth_required_for_host,
    _basic_auth_configured,
    _env_flag_enabled,
    _google_oauth_configured,
    _is_loopback_host,
)
from .rate_limit import _rate_limit_per_window

DURABLE_DATA_DIR_REQUIRED_MESSAGE = "Public deployments must set NDA_DATA_DIR to a durable storage path."
EPHEMERAL_DATA_DIR_MESSAGE = "NDA_DATA_DIR points at ephemeral storage; use a persistent disk or external store."
EPHEMERAL_EXPORTS_DIR_MESSAGE = "NDA_EXPORTS_DIR points at ephemeral storage; use a persistent disk or disable saved export URLs."
EPHEMERAL_USERS_PATH_MESSAGE = "NDA_USERS_PATH points at ephemeral storage; use persistent storage for users and sessions."
GOOGLE_OAUTH_REDIRECT_URI_ENV = "NDA_GOOGLE_OAUTH_REDIRECT_URI"
GMAIL_OAUTH_REDIRECT_URI_ENV = "NDA_GMAIL_OAUTH_REDIRECT_URI"
GMAIL_LEGACY_TOKEN_PATH_ENVS = ("NDA_GMAIL_INBOUND_TOKEN_PATH", "NDA_GMAIL_OUTBOUND_TOKEN_PATH")
AI_REVIEW_ENABLED_ENV = "NDA_AI_REVIEW_ENABLED"
AI_PROVIDER_ENV = "NDA_AI_PROVIDER"
AI_MODEL_ENV = "NDA_AI_MODEL"
ACTIVE_REVIEW_ENGINE_ENV = "NDA_ACTIVE_REVIEW_ENGINE"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
GMAIL_TRIAGE_MODEL_ENV = "NDA_GMAIL_TRIAGE_MODEL"
GMAIL_INTAKE_MODEL_ENV = "NDA_GMAIL_INTAKE_MODEL"
DEFAULT_GMAIL_INTAKE_MODEL = "deepseek/deepseek-v4-flash"


def _validate_public_auth(host: str) -> None:
    if not _auth_required_for_host(host):
        return
    if not _auth_method_configured():
        raise RuntimeError(AUTH_NOT_CONFIGURED_MESSAGE)


def _validate_public_storage(host: str) -> None:
    if _is_loopback_host(host) or _env_flag_enabled("NDA_ALLOW_EPHEMERAL_DATA"):
        return
    if not os.environ.get("NDA_DATA_DIR"):
        raise RuntimeError(DURABLE_DATA_DIR_REQUIRED_MESSAGE)
    if _is_ephemeral_storage_path(matter_store.DATA_DIR):
        raise RuntimeError(EPHEMERAL_DATA_DIR_MESSAGE)
    if export_service.EXPORTS_DIR is not None and _is_ephemeral_storage_path(export_service.EXPORTS_DIR):
        raise RuntimeError(EPHEMERAL_EXPORTS_DIR_MESSAGE)
    if os.environ.get("NDA_USERS_PATH") and _is_ephemeral_storage_path(user_store.users_path()):
        raise RuntimeError(EPHEMERAL_USERS_PATH_MESSAGE)


def _deployment_status_for_host(host: str) -> dict[str, object]:
    auth_required = _auth_required_for_host(host)
    basic_auth_configured = _basic_auth_configured()
    google_oauth_configured = _google_oauth_configured()
    auth_configured = basic_auth_configured or google_oauth_configured
    data_dir_configured = bool(os.environ.get("NDA_DATA_DIR"))
    data_dir_ephemeral = _is_ephemeral_storage_path(matter_store.DATA_DIR)
    exports_dir = export_service.EXPORTS_DIR
    exports_dir_ephemeral = exports_dir is not None and _is_ephemeral_storage_path(exports_dir)
    users_path_configured = bool(os.environ.get("NDA_USERS_PATH"))
    users_path_ephemeral = users_path_configured and _is_ephemeral_storage_path(user_store.users_path())
    rate_limit_per_minute = _rate_limit_per_window()
    data_dir_check = _deployment_data_dir_check(host, data_dir_configured, data_dir_ephemeral)
    users_path_check = _deployment_users_path_check(host, users_path_configured, users_path_ephemeral)
    public_host = not _is_loopback_host(host)
    allowed_hosts_configured = bool(os.environ.get(AUTH_ALLOWED_HOSTS_ENV, "").strip())
    google_redirect_uri = os.environ.get(GOOGLE_OAUTH_REDIRECT_URI_ENV, "").strip()
    gmail_redirect_uri = os.environ.get(GMAIL_OAUTH_REDIRECT_URI_ENV, "").strip()
    legacy_gmail_token_paths_configured = any(os.environ.get(env_name, "").strip() for env_name in GMAIL_LEGACY_TOKEN_PATH_ENVS)
    ai_env = _deployment_ai_env_status()
    gmail_triage_env = _deployment_gmail_triage_env_status(public_host)
    gmail_intake_env = _deployment_gmail_intake_env_status()
    checks = [
        {
            "id": "auth",
            "ok": (not auth_required) or auth_configured,
            "message": _deployment_auth_message(auth_required, auth_configured),
        },
        {
            "id": "google_identity",
            "ok": (not public_host) or google_oauth_configured,
            "message": _deployment_google_identity_message(public_host, google_oauth_configured),
        },
        {
            "id": "allowed_hosts",
            "ok": (not public_host) or allowed_hosts_configured,
            "message": _deployment_allowed_hosts_message(public_host, allowed_hosts_configured),
        },
        {
            "id": "oauth_redirects",
            "ok": _deployment_oauth_redirects_ok(public_host, google_oauth_configured, google_redirect_uri, gmail_redirect_uri),
            "message": _deployment_oauth_redirects_message(public_host, google_oauth_configured, google_redirect_uri, gmail_redirect_uri),
        },
        {
            "id": "data_dir",
            "ok": data_dir_check["ok"],
            "message": data_dir_check["message"],
        },
        {
            "id": "users_path",
            "ok": users_path_check["ok"],
            "message": users_path_check["message"],
        },
        {
            "id": "exports_dir",
            "ok": not exports_dir_ephemeral,
            "message": "Saved export storage is durable or disabled." if not exports_dir_ephemeral else "Saved export storage points at ephemeral storage.",
        },
        {
            "id": "gmail_token_mode",
            "ok": (not public_host) or not legacy_gmail_token_paths_configured,
            "message": (
                "Per-user Gmail OAuth tokens are used; legacy shared Gmail token paths are unset."
                if not legacy_gmail_token_paths_configured
                else "Legacy shared Gmail token path env vars are set; unset them for per-user hosted Gmail."
            ),
        },
        {
            "id": "ai_review_env",
            "ok": ai_env["ok"],
            "message": ai_env["message"],
        },
        {
            "id": "gmail_triage_ai",
            "ok": gmail_triage_env["ok"],
            "message": gmail_triage_env["message"],
        },
        {
            # Informational only (never fails the gate): the intake classifier reuses
            # OPENROUTER_API_KEY and falls open if NDA_GMAIL_INTAKE_MODEL is unset.
            # This check reports only what it can verify WITHOUT a live API call --
            # key presence and the resolved model slug. It deliberately does NOT
            # assert the classifier is actually reachable / the model slug valid
            # (a bad slug, rate-limit, or OpenRouter outage is observed at sync time
            # via the per-sync ai_intake tallies, not here).
            "id": "gmail_intake_ai",
            "ok": gmail_intake_env["ok"],
            "configured": gmail_intake_env["configured"],
            "message": gmail_intake_env["message"],
        },
        {
            "id": "rate_limit",
            "ok": rate_limit_per_minute > 0,
            "message": "Expensive endpoint rate limiting is enabled." if rate_limit_per_minute > 0 else "Expensive endpoint rate limiting is disabled.",
        },
    ]
    return {
        "host": host,
        "public_host": public_host,
        "auth_required": auth_required,
        "auth_configured": auth_configured,
        "basic_auth_configured": basic_auth_configured,
        "google_oauth_configured": google_oauth_configured,
        "allowed_hosts_configured": allowed_hosts_configured,
        "google_oauth_redirect_uri_configured": bool(google_redirect_uri),
        "gmail_oauth_redirect_uri_configured": bool(gmail_redirect_uri),
        "data_dir_configured": data_dir_configured,
        "data_dir_ephemeral": data_dir_ephemeral,
        "users_path_configured": users_path_configured,
        "users_path_ephemeral": users_path_ephemeral,
        "exports_dir_configured": exports_dir is not None,
        "exports_dir_ephemeral": exports_dir_ephemeral,
        "legacy_gmail_token_paths_configured": legacy_gmail_token_paths_configured,
        "ai_review_env_configured": ai_env["configured"],
        "gmail_triage_ai_configured": gmail_triage_env["configured"],
        "gmail_intake_ai_configured": gmail_intake_env["configured"],
        "rate_limit_per_minute": rate_limit_per_minute,
        "health_check_path": "/healthz",
        "status": "ok" if all(bool(check["ok"]) for check in checks) else "needs_attention",
        "checks": checks,
    }


def _deployment_auth_message(auth_required: bool, auth_configured: bool) -> str:
    if _google_oauth_configured():
        return "Google OAuth login is configured."
    if _basic_auth_configured():
        return "HTTP Basic auth is configured."
    if auth_required:
        return "No login method is configured."
    return "Authentication is not required for this host."


def _deployment_data_dir_check(host: str, data_dir_configured: bool, data_dir_ephemeral: bool) -> dict[str, object]:
    if data_dir_configured and not data_dir_ephemeral:
        return {"ok": True, "message": "Matter data uses configured durable storage."}
    if _is_loopback_host(host):
        return {"ok": True, "message": "Local deployment may use local matter data storage."}
    if _env_flag_enabled("NDA_ALLOW_EPHEMERAL_DATA"):
        return {"ok": True, "message": "Ephemeral matter data is explicitly allowed."}
    return {"ok": False, "message": "Matter data is not on configured durable storage."}


def _deployment_users_path_check(host: str, users_path_configured: bool, users_path_ephemeral: bool) -> dict[str, object]:
    if users_path_configured and users_path_ephemeral:
        return {"ok": False, "message": "User/session storage points at ephemeral storage."}
    if users_path_configured:
        return {"ok": True, "message": "User/session storage uses a configured path."}
    if _is_loopback_host(host):
        return {"ok": True, "message": "Local users default to local matter data storage."}
    return {"ok": True, "message": "User/session storage defaults to NDA_DATA_DIR/users.json."}


def _deployment_google_identity_message(public_host: bool, google_oauth_configured: bool) -> str:
    if google_oauth_configured:
        return "Google OAuth login is configured for per-user accounts."
    if public_host:
        return "Set Google OAuth client ID and secret for per-user login and Gmail."
    return "Google OAuth login is optional for local development."


def _deployment_allowed_hosts_message(public_host: bool, allowed_hosts_configured: bool) -> str:
    if not public_host:
        return "Host allowlist is optional for loopback development."
    if allowed_hosts_configured:
        return "Request host allowlist is configured."
    return f"Set {AUTH_ALLOWED_HOSTS_ENV} to the deployed Render hostname."


def _deployment_oauth_redirects_ok(
    public_host: bool,
    google_oauth_configured: bool,
    google_redirect_uri: str,
    gmail_redirect_uri: str,
) -> bool:
    if not public_host or not google_oauth_configured:
        return True
    return _https_redirect_uri(google_redirect_uri) and _https_redirect_uri(gmail_redirect_uri)


def _deployment_oauth_redirects_message(
    public_host: bool,
    google_oauth_configured: bool,
    google_redirect_uri: str,
    gmail_redirect_uri: str,
) -> str:
    if not public_host:
        return "Fixed OAuth redirect URIs are optional for loopback development."
    if not google_oauth_configured:
        return "OAuth redirect URIs are checked after Google OAuth is configured."
    missing = []
    if not google_redirect_uri:
        missing.append(GOOGLE_OAUTH_REDIRECT_URI_ENV)
    if not gmail_redirect_uri:
        missing.append(GMAIL_OAUTH_REDIRECT_URI_ENV)
    if missing:
        return f"Set fixed HTTPS OAuth redirect URI env vars: {', '.join(missing)}."
    if not _https_redirect_uri(google_redirect_uri) or not _https_redirect_uri(gmail_redirect_uri):
        return "OAuth redirect URIs must be absolute HTTPS URLs for public Render deployments."
    return "Google login and Gmail OAuth redirect URIs are configured."


def _deployment_ai_env_status() -> dict[str, object]:
    enabled = _env_flag_enabled(AI_REVIEW_ENABLED_ENV) or os.environ.get(ACTIVE_REVIEW_ENGINE_ENV, "").strip() == "ai_first"
    provider = os.environ.get(AI_PROVIDER_ENV, "").strip().lower()
    model = os.environ.get(AI_MODEL_ENV, "").strip()
    configured = bool(provider and model and _ai_provider_key_configured(provider))
    if not enabled and not provider and not model:
        return {"ok": True, "configured": False, "message": "AI review can be configured from Admin or environment."}
    if configured:
        return {"ok": True, "configured": True, "message": "AI review provider, model, and server-side API key are configured."}
    return {
        "ok": False,
        "configured": False,
        "message": "Set NDA_AI_PROVIDER, NDA_AI_MODEL, and the matching server-side API key before enabling hosted AI review.",
    }


def _deployment_gmail_triage_env_status(public_host: bool) -> dict[str, object]:
    key_configured = bool(
        os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
        or _stored_key_configured(app_settings.stored_ai_api_key)
    )
    model_configured = bool(os.environ.get(GMAIL_TRIAGE_MODEL_ENV, "").strip())
    configured = key_configured and model_configured
    if configured:
        return {"ok": True, "configured": True, "message": "Gmail OpenRouter triage key and model are configured."}
    if not public_host:
        return {"ok": True, "configured": False, "message": "Gmail OpenRouter triage can be configured later for local development."}
    return {
        "ok": False,
        "configured": False,
        "message": "Set OPENROUTER_API_KEY and NDA_GMAIL_TRIAGE_MODEL for AI-assisted Gmail attachment selection.",
    }


def _deployment_gmail_intake_env_status() -> dict[str, object]:
    # The intake classifier reuses OPENROUTER_API_KEY (same precedence as triage) and
    # defaults the model to deepseek/deepseek-v4-flash, so it is non-blocking and
    # fails open if the env knob is unset.
    key_configured = bool(
        os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
        or _stored_key_configured(app_settings.stored_ai_api_key)
    )
    model = os.environ.get(GMAIL_INTAKE_MODEL_ENV, "").strip() or DEFAULT_GMAIL_INTAKE_MODEL
    if key_configured:
        return {
            "ok": True,
            "configured": True,
            "message": f"Gmail NDA-intake classifier uses OpenRouter model {model}.",
        }
    return {
        "ok": True,
        "configured": False,
        "message": (
            "Gmail NDA-intake classifier is optional; it reuses OPENROUTER_API_KEY and "
            f"defaults NDA_GMAIL_INTAKE_MODEL to {DEFAULT_GMAIL_INTAKE_MODEL}."
        ),
    }


def _ai_provider_key_configured(provider: str) -> bool:
    if provider == "openrouter":
        return bool(os.environ.get(OPENROUTER_API_KEY_ENV, "").strip() or _stored_key_configured(app_settings.stored_ai_api_key))
    return False


def _stored_key_configured(loader) -> bool:
    try:
        return bool(loader())
    except (app_settings.AppSettingsError, OSError):
        return False


def _https_redirect_uri(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _is_ephemeral_storage_path(path: Path) -> bool:
    try:
        resolved_path = path.expanduser().resolve(strict=False)
    except OSError:
        resolved_path = path.expanduser().absolute()
    ephemeral_roots = {
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/var/tmp"),
        Path(tempfile.gettempdir()).expanduser().resolve(strict=False),
    }
    for root in ephemeral_roots:
        try:
            resolved_root = root.resolve(strict=False)
        except OSError:
            resolved_root = root.absolute()
        if resolved_path == resolved_root or resolved_root in resolved_path.parents:
            return True
    return False
