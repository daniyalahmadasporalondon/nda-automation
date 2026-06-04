from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import export_service, matter_store
from .http_auth import (
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


def _deployment_status_for_host(host: str) -> dict[str, object]:
    auth_required = _auth_required_for_host(host)
    basic_auth_configured = _basic_auth_configured()
    google_oauth_configured = _google_oauth_configured()
    auth_configured = basic_auth_configured or google_oauth_configured
    data_dir_configured = bool(os.environ.get("NDA_DATA_DIR"))
    data_dir_ephemeral = _is_ephemeral_storage_path(matter_store.DATA_DIR)
    exports_dir = export_service.EXPORTS_DIR
    exports_dir_ephemeral = exports_dir is not None and _is_ephemeral_storage_path(exports_dir)
    rate_limit_per_minute = _rate_limit_per_window()
    data_dir_check = _deployment_data_dir_check(host, data_dir_configured, data_dir_ephemeral)
    checks = [
        {
            "id": "auth",
            "ok": (not auth_required) or auth_configured,
            "message": _deployment_auth_message(auth_required, auth_configured),
        },
        {
            "id": "data_dir",
            "ok": data_dir_check["ok"],
            "message": data_dir_check["message"],
        },
        {
            "id": "exports_dir",
            "ok": not exports_dir_ephemeral,
            "message": "Saved export storage is durable or disabled." if not exports_dir_ephemeral else "Saved export storage points at ephemeral storage.",
        },
        {
            "id": "rate_limit",
            "ok": rate_limit_per_minute > 0,
            "message": "Expensive endpoint rate limiting is enabled." if rate_limit_per_minute > 0 else "Expensive endpoint rate limiting is disabled.",
        },
    ]
    return {
        "host": host,
        "public_host": not _is_loopback_host(host),
        "auth_required": auth_required,
        "auth_configured": auth_configured,
        "basic_auth_configured": basic_auth_configured,
        "google_oauth_configured": google_oauth_configured,
        "data_dir_configured": data_dir_configured,
        "data_dir_ephemeral": data_dir_ephemeral,
        "exports_dir_configured": exports_dir is not None,
        "exports_dir_ephemeral": exports_dir_ephemeral,
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
