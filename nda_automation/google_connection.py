from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

from . import google_identity, matter_store
from .durable_io import fsync_parent_directory

ROLE_TOKEN_ENV = {
    "inbound": "NDA_GMAIL_INBOUND_TOKEN_PATH",
    "outbound": "NDA_GMAIL_OUTBOUND_TOKEN_PATH",
    "drive": "NDA_DRIVE_TOKEN_PATH",
}
ROLE_LOCAL_TOKEN_FILENAME = {
    "inbound": "inbound-token.json",
    "outbound": "outbound-token.json",
    "drive": "drive-token.json",
}
GOOGLE_OAUTH_AUTH_URL = google_identity.GOOGLE_AUTH_URL
GOOGLE_OAUTH_TOKEN_URL = google_identity.GOOGLE_TOKEN_URL
GOOGLE_OAUTH_SCOPES_BY_ROLE = {
    "inbound": ("https://www.googleapis.com/auth/gmail.readonly",),
    # gmail.send authorizes sending, but resolving the outbound account reads
    # emailAddress from Gmail's users.getProfile, which requires gmail.metadata.
    "outbound": (
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.metadata",
    ),
    # Least-privilege Drive access: drive.file lets the app touch only files it
    # creates, never the user's whole Drive.
    "drive": ("https://www.googleapis.com/auth/drive.file",),
}
_TOKEN_LOCK = threading.RLock()


class GoogleConnectionError(RuntimeError):
    pass


def connected_owner_user_id(current_user: object, *, owner_user_id: str) -> str:
    if isinstance(current_user, Mapping) and current_user.get("provider") == "google":
        return str(owner_user_id or "")
    return ""


def login_hint(current_user: object) -> str:
    if not isinstance(current_user, Mapping):
        return ""
    return str(current_user.get("email") or "")


def request_base_url(handler) -> str:
    scheme = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip() or "http"
    host = handler.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    if not host:
        host = handler.headers.get("Host", "").strip()
    if not host:
        server_host, server_port = handler.server.server_address[:2]
        host = f"{server_host}:{server_port}"
    return f"{scheme}://{host}"


def role_token_status(role: str, owner_user_id: str = "") -> dict[str, object]:
    if role not in ROLE_TOKEN_ENV:
        raise GoogleConnectionError("Unsupported Google connection role.")
    owner_user_id = clean_user_token_segment(owner_user_id)
    if owner_user_id:
        local_path = user_token_path_for_role(role, owner_user_id)
        if local_path.is_file():
            return {
                "configured": True,
                "label": f"user_google/{role}-token.json",
                "source": "user_data",
                "scope_status": token_scope_status(role, local_path),
            }
        legacy_path = legacy_user_token_path_for_role(role, owner_user_id)
        if legacy_path.is_file():
            return {
                "configured": True,
                "label": f"user_gmail/{role}-token.json",
                "source": "user_data",
                "scope_status": token_scope_status(role, legacy_path),
            }
        if role == "drive":
            legacy_gmail_path = legacy_gmail_token_with_role_scope(role, owner_user_id=owner_user_id)
            if legacy_gmail_path is not None:
                return {
                    "configured": True,
                    "label": f"user_gmail/{legacy_gmail_path.name} (Drive scope)",
                    "source": "legacy_gmail_scope",
                    "scope_status": token_scope_status(role, legacy_gmail_path),
                }
        return {
            "configured": False,
            "label": f"Connect Google for {role}",
            "source": "missing",
            "scope_status": missing_scope_status(role),
        }
    env_name = ROLE_TOKEN_ENV[role]
    local_label = f"data/google/{ROLE_LOCAL_TOKEN_FILENAME[role]}"
    configured_path = os.environ.get(env_name)
    if configured_path:
        token_path = Path(configured_path).expanduser()
        return {
            "configured": token_path.is_file(),
            "label": env_name,
            "source": "environment",
            "scope_status": token_scope_status(role, token_path) if token_path.is_file() else missing_scope_status(role),
        }
    local_path = matter_store.DATA_DIR / "google" / ROLE_LOCAL_TOKEN_FILENAME[role]
    if local_path.is_file():
        return {
            "configured": True,
            "label": local_label,
            "source": "local_data",
            "scope_status": token_scope_status(role, local_path),
        }
    legacy_path = matter_store.DATA_DIR / "gmail" / ROLE_LOCAL_TOKEN_FILENAME[role]
    if legacy_path.is_file():
        return {
            "configured": True,
            "label": f"data/gmail/{ROLE_LOCAL_TOKEN_FILENAME[role]}",
            "source": "local_data",
            "scope_status": token_scope_status(role, legacy_path),
        }
    if role == "drive":
        legacy_gmail_path = legacy_gmail_token_with_role_scope(role)
        if legacy_gmail_path is not None:
            return {
                "configured": True,
                "label": f"data/gmail/{legacy_gmail_path.name} (Drive scope)",
                "source": "legacy_gmail_scope",
                "scope_status": token_scope_status(role, legacy_gmail_path),
            }
    return {
        "configured": False,
        "label": f"{env_name} or {local_label}",
        "source": "missing",
        "scope_status": missing_scope_status(role),
    }


def token_path_for_role(
    role: str,
    owner_user_id: str = "",
    *,
    integration_label: str = "Google",
) -> Path:
    if role not in ROLE_TOKEN_ENV:
        raise GoogleConnectionError(f"Unsupported {integration_label} role.")
    owner_user_id = clean_user_token_segment(owner_user_id)
    if owner_user_id:
        local_path = user_token_path_for_role(role, owner_user_id, integration_label=integration_label)
        if local_path.is_file():
            return local_path
        legacy_path = legacy_user_token_path_for_role(role, owner_user_id, integration_label=integration_label)
        if legacy_path.is_file():
            return legacy_path
        if role == "drive":
            legacy_gmail_path = legacy_gmail_token_with_role_scope(role, owner_user_id=owner_user_id)
            if legacy_gmail_path is not None:
                return legacy_gmail_path
        return local_path
    configured_path = os.environ.get(ROLE_TOKEN_ENV[role])
    if configured_path:
        return Path(configured_path).expanduser()
    local_path = matter_store.DATA_DIR / "google" / ROLE_LOCAL_TOKEN_FILENAME[role]
    if local_path.is_file():
        return local_path
    legacy_path = matter_store.DATA_DIR / "gmail" / ROLE_LOCAL_TOKEN_FILENAME[role]
    if legacy_path.is_file():
        return legacy_path
    if role == "drive":
        legacy_gmail_path = legacy_gmail_token_with_role_scope(role)
        if legacy_gmail_path is not None:
            return legacy_gmail_path
    raise GoogleConnectionError(
        f"Set {ROLE_TOKEN_ENV[role]} or add data/google/{ROLE_LOCAL_TOKEN_FILENAME[role]} "
        f"for the {role} {integration_label} account."
    )


def credentials_for_role(
    role: str,
    owner_user_id: str = "",
    *,
    integration_label: str = "Google",
) -> Any:
    token_path = token_path_for_role(role, owner_user_id=owner_user_id, integration_label=integration_label)
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise GoogleConnectionError("Google API packages are not installed.") from exc

    with locked_token_file(token_path):
        if not token_path.is_file():
            raise GoogleConnectionError(f"Set {ROLE_TOKEN_ENV[role]} for the {role} {integration_label} account.")
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path))
        except Exception as exc:
            raise GoogleConnectionError(f"{integration_label} {role} token could not be read.") from exc

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                write_token_json_unlocked(token_path, credentials.to_json())
            except GoogleConnectionError:
                raise
            except Exception as exc:
                raise GoogleConnectionError(f"{integration_label} {role} token could not refresh.") from exc
        if not credentials or not credentials.valid:
            raise GoogleConnectionError(f"{integration_label} {role} token is not valid.")
        return credentials


def build_authorization_url(
    *,
    redirect_uri: str,
    role: str,
    state: str,
    login_hint: str = "",
) -> str:
    if not google_identity.google_oauth_configured():
        raise GoogleConnectionError("Google OAuth is not configured.")
    params = {
        "access_type": "offline",
        "client_id": google_identity.google_client_id(),
        "include_granted_scopes": "true",
        "prompt": "select_account consent",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(oauth_scopes_for_role(role)),
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    query = urllib.parse.urlencode(params)
    return f"{GOOGLE_OAUTH_AUTH_URL}?{query}"


def exchange_oauth_code(code: str, *, redirect_uri: str) -> dict[str, Any]:
    if not google_identity.google_oauth_configured():
        raise GoogleConnectionError("Google OAuth is not configured.")
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": google_identity.google_client_id(),
        "client_secret": google_identity.google_client_secret(),
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise GoogleConnectionError("Google OAuth token exchange failed.") from exc
    if not isinstance(payload, dict):
        raise GoogleConnectionError("Google OAuth token exchange failed.")
    return payload


def save_user_oauth_token(owner_user_id: str, token_response: dict[str, Any], *, role: str = "all") -> list[str]:
    owner_user_id = clean_user_token_segment(owner_user_id)
    if not owner_user_id:
        raise GoogleConnectionError("A signed-in user is required to connect Google.")
    access_token = str(token_response.get("access_token") or "").strip()
    if not access_token:
        raise GoogleConnectionError("Google OAuth response did not include an access token.")
    saved_roles = oauth_roles_for_role(role)
    token_payloads: list[tuple[str, Path, dict[str, Any]]] = []
    for save_role in saved_roles:
        token_path = user_token_path_for_role(save_role, owner_user_id)
        existing = read_token_json(token_path)
        legacy_existing = read_token_json(legacy_user_token_path_for_role(save_role, owner_user_id))
        refresh_token = str(
            token_response.get("refresh_token")
            or existing.get("refresh_token")
            or legacy_existing.get("refresh_token")
            or ""
        ).strip()
        if not refresh_token:
            raise GoogleConnectionError("Google did not return a refresh token. Reconnect Google and approve offline access.")
        token_payloads.append((save_role, token_path, {
            "client_id": google_identity.google_client_id(),
            "client_secret": google_identity.google_client_secret(),
            "refresh_token": refresh_token,
            "scopes": list(oauth_scopes_for_role(save_role)),
            "token": access_token,
            "token_uri": GOOGLE_OAUTH_TOKEN_URL,
        }))
    saved: list[str] = []
    for save_role, token_path, token_payload in token_payloads:
        write_token_atomically(token_path, json.dumps(token_payload, indent=2) + "\n")
        saved.append(save_role)
    return saved


def disconnect_user_oauth(owner_user_id: str, *, role: str = "all") -> int:
    owner_user_id = clean_user_token_segment(owner_user_id)
    if not owner_user_id:
        raise GoogleConnectionError("A signed-in user is required to disconnect Google.")
    removed = 0
    for disconnect_role in oauth_roles_for_role(role):
        token_paths = [
            user_token_path_for_role(disconnect_role, owner_user_id),
            legacy_user_token_path_for_role(disconnect_role, owner_user_id),
        ]
        for token_path in token_paths:
            try:
                token_path.unlink()
                fsync_parent_directory(token_path)
                removed += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise GoogleConnectionError("Google token could not be removed.") from exc
    return removed


def oauth_scopes_for_role(role: str) -> tuple[str, ...]:
    roles = oauth_roles_for_role(role)
    scopes: list[str] = []
    for role_name in roles:
        for scope in GOOGLE_OAUTH_SCOPES_BY_ROLE[role_name]:
            if scope not in scopes:
                scopes.append(scope)
    return tuple(scopes)


def oauth_roles_for_role(role: str) -> tuple[str, ...]:
    normalized_role = str(role or "all").strip().lower()
    if normalized_role in {"all", "both"}:
        return ("inbound", "outbound", "drive")
    if normalized_role in GOOGLE_OAUTH_SCOPES_BY_ROLE:
        return (normalized_role,)
    raise GoogleConnectionError("Unsupported Google OAuth role.")


def token_scope_status(role: str, token_path: Path) -> dict[str, object]:
    required = list(oauth_scopes_for_role(role))
    payload = read_token_json(token_path)
    granted = token_scopes(payload)
    missing = [scope for scope in required if scope not in granted]
    return {
        "required": required,
        "granted": granted,
        "missing": missing,
        "ok": not missing,
    }


def missing_scope_status(role: str) -> dict[str, object]:
    return {
        "required": list(oauth_scopes_for_role(role)),
        "granted": [],
        "missing": list(oauth_scopes_for_role(role)),
        "ok": False,
    }


def token_scopes(token_payload: Mapping[str, Any]) -> list[str]:
    scopes = token_payload.get("scopes")
    if isinstance(scopes, str):
        return [scope for scope in scopes.split() if scope]
    if isinstance(scopes, list):
        return [str(scope).strip() for scope in scopes if str(scope).strip()]
    scope = token_payload.get("scope")
    if isinstance(scope, str):
        return [item for item in scope.split() if item]
    return []


def legacy_gmail_token_with_role_scope(role: str, owner_user_id: str = "") -> Path | None:
    if role != "drive":
        return None
    required = set(oauth_scopes_for_role(role))
    owner_segment = clean_user_token_segment(owner_user_id)
    if owner_segment:
        candidates = [
            legacy_user_token_path_for_role("inbound", owner_segment),
            legacy_user_token_path_for_role("outbound", owner_segment),
        ]
    else:
        candidates = [
            matter_store.DATA_DIR / "gmail" / ROLE_LOCAL_TOKEN_FILENAME["inbound"],
            matter_store.DATA_DIR / "gmail" / ROLE_LOCAL_TOKEN_FILENAME["outbound"],
        ]
    for token_path in candidates:
        if not token_path.is_file():
            continue
        if required.issubset(set(token_scopes(read_token_json(token_path)))):
            return token_path
    return None


def connection_setup_status(
    *,
    owner_user_id: str = "",
    connect_url: str,
    integration: str,
) -> dict[str, object]:
    oauth_configured = google_identity.google_oauth_configured()
    signed_in = bool(clean_user_token_segment(owner_user_id))
    if not oauth_configured:
        state = "missing_oauth_config"
        action = "configure_google_oauth"
        message = (
            "Google OAuth is not configured. Set NDA_GOOGLE_OAUTH_CLIENT_ID and "
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET, then restart the app."
        )
    elif not signed_in:
        state = "sign_in_required"
        action = "sign_in_with_google"
        message = f"Sign in with Google before connecting {integration}."
    else:
        state = "ready_to_connect"
        action = "connect_google"
        message = f"Connect {integration} for the signed-in Google account."
    return {
        "state": state,
        "action": action,
        "message": message,
        "google_oauth_configured": oauth_configured,
        "signed_in": signed_in,
        "connect_url": connect_url,
    }


def role_recovery_status(
    role: str,
    *,
    owner_user_id: str = "",
    connect_url: str,
    integration: str,
) -> dict[str, object]:
    token = role_token_status(role, owner_user_id=owner_user_id)
    setup = connection_setup_status(
        owner_user_id=owner_user_id,
        connect_url=connect_url,
        integration=integration,
    )
    if token.get("configured") and (token.get("scope_status") or {}).get("ok", True):
        return {
            "state": "ready",
            "action": "none",
            "message": f"{integration} {role} token is configured.",
            "connect_url": connect_url,
        }
    if token.get("configured"):
        return {
            "state": "missing_scope",
            "action": "reconnect_google",
            "message": f"Reconnect {integration} so Google grants the required {role} scope.",
            "connect_url": connect_url,
            "scope_status": token.get("scope_status") or missing_scope_status(role),
        }
    if setup["state"] != "ready_to_connect":
        return setup
    return {
        "state": "missing_token",
        "action": "connect_google",
        "message": f"Connect {integration} to create a {role} token for this account.",
        "connect_url": connect_url,
        "scope_status": token.get("scope_status") or missing_scope_status(role),
    }


def user_token_path_for_role(
    role: str,
    owner_user_id: str,
    *,
    integration_label: str = "Google",
) -> Path:
    owner_segment = clean_user_token_segment(owner_user_id)
    if owner_segment in {"", ".", ".."}:
        raise GoogleConnectionError(f"A valid signed-in user is required to store {integration_label} tokens.")
    if role not in ROLE_LOCAL_TOKEN_FILENAME:
        raise GoogleConnectionError(f"Unsupported {integration_label} role.")
    return matter_store.DATA_DIR / "users" / "google" / owner_segment / ROLE_LOCAL_TOKEN_FILENAME[role]


def legacy_user_token_path_for_role(
    role: str,
    owner_user_id: str,
    *,
    integration_label: str = "Google",
) -> Path:
    owner_segment = clean_user_token_segment(owner_user_id)
    if owner_segment in {"", ".", ".."}:
        raise GoogleConnectionError(f"A valid signed-in user is required to store {integration_label} tokens.")
    if role not in ROLE_LOCAL_TOKEN_FILENAME:
        raise GoogleConnectionError(f"Unsupported {integration_label} role.")
    return matter_store.DATA_DIR / "users" / "gmail" / owner_segment / ROLE_LOCAL_TOKEN_FILENAME[role]


def clean_user_token_segment(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.@:-]+", "-", str(value or "").strip())[:160].strip("-")


def read_token_json(token_path: Path) -> dict[str, Any]:
    if not token_path.is_file():
        return {}
    try:
        with token_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_token_atomically(token_path: Path, token_json: str) -> None:
    with locked_token_file(token_path):
        write_token_json_unlocked(token_path, token_json)


@contextmanager
def locked_token_file(token_path: Path):
    with _TOKEN_LOCK:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = token_path.with_name(f".{token_path.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def write_token_json_unlocked(token_path: Path, token_json: str) -> None:
    temporary_path = token_path.with_name(f".{token_path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            handle.write(token_json)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, token_path)
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
        fsync_parent_directory(token_path)
    except OSError as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise GoogleConnectionError("Google token could not be saved.") from exc
