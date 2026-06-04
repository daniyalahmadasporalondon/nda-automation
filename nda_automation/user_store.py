from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import matter_store

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

USER_STORE_VERSION = 1
SESSION_COOKIE_NAME = "nda_session"
OAUTH_STATE_COOKIE_NAME = "nda_oauth_state"
SESSION_TTL_SECONDS = 14 * 24 * 60 * 60
LOGIN_STATE_TTL_SECONDS = 10 * 60
_USER_STORE_LOCK = threading.RLock()


class UserStoreError(RuntimeError):
    pass


def create_login_state(*, next_path: str = "/") -> str:
    state = secrets.token_urlsafe(32)
    now = _now_epoch()
    with _locked_user_store():
        store = _load_store_unlocked()
        _prune_expired_unlocked(store, now=now)
        store.setdefault("login_states", {})[_token_hash(state)] = {
            "created_at": _iso_from_epoch(now),
            "expires_at": _iso_from_epoch(now + LOGIN_STATE_TTL_SECONDS),
            "next_path": _clean_next_path(next_path),
        }
        _save_store_unlocked(store)
    return state


def consume_login_state(state: str) -> dict[str, Any] | None:
    state_hash = _token_hash(state)
    if not state_hash:
        return None
    now = _now_epoch()
    with _locked_user_store():
        store = _load_store_unlocked()
        _prune_expired_unlocked(store, now=now)
        login_states = store.setdefault("login_states", {})
        state_record = login_states.pop(state_hash, None)
        _save_store_unlocked(store)
    if not isinstance(state_record, dict):
        return None
    return {
        "next_path": _clean_next_path(state_record.get("next_path")),
    }


def upsert_google_user(profile: dict[str, Any]) -> dict[str, Any]:
    subject = str(profile.get("sub") or "").strip()
    if not subject:
        raise UserStoreError("Google identity response did not include a subject.")
    user_id = f"google:{_clean_identity_segment(subject)}"
    now = _iso_from_epoch(_now_epoch())
    user = {
        "id": user_id,
        "provider": "google",
        "provider_subject": subject,
        "email": _clean_email(profile.get("email")),
        "name": _clean_display_text(profile.get("name"), max_length=160),
        "picture": _clean_url(profile.get("picture")),
        "created_at": now,
        "updated_at": now,
        "last_login_at": now,
    }
    with _locked_user_store():
        store = _load_store_unlocked()
        users = store.setdefault("users", {})
        existing = users.get(user_id)
        if isinstance(existing, dict):
            user["created_at"] = str(existing.get("created_at") or now)
        users[user_id] = user
        _save_store_unlocked(store)
    return dict(user)


def create_session(user_id: str) -> str:
    user_id = str(user_id or "").strip()
    if not user_id:
        raise UserStoreError("Session user is required.")
    token = secrets.token_urlsafe(48)
    now = _now_epoch()
    with _locked_user_store():
        store = _load_store_unlocked()
        _prune_expired_unlocked(store, now=now)
        if user_id not in store.setdefault("users", {}):
            raise UserStoreError("Session user does not exist.")
        store.setdefault("sessions", {})[_token_hash(token)] = {
            "user_id": user_id,
            "created_at": _iso_from_epoch(now),
            "expires_at": _iso_from_epoch(now + SESSION_TTL_SECONDS),
        }
        _save_store_unlocked(store)
    return token


def user_for_session_token(token: str) -> dict[str, Any] | None:
    token_hash = _token_hash(token)
    if not token_hash:
        return None
    now = _now_epoch()
    with _locked_user_store():
        store = _load_store_unlocked()
        changed = _prune_expired_unlocked(store, now=now)
        session = store.setdefault("sessions", {}).get(token_hash)
        user = None
        if isinstance(session, dict):
            user_id = str(session.get("user_id") or "")
            user = store.setdefault("users", {}).get(user_id)
        if changed:
            _save_store_unlocked(store)
    return dict(user) if isinstance(user, dict) else None


def delete_session(token: str) -> bool:
    token_hash = _token_hash(token)
    if not token_hash:
        return False
    with _locked_user_store():
        store = _load_store_unlocked()
        removed = store.setdefault("sessions", {}).pop(token_hash, None) is not None
        if removed:
            _save_store_unlocked(store)
    return removed


def public_user(user: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(user, dict):
        return None
    return {
        "id": str(user.get("id") or ""),
        "provider": str(user.get("provider") or ""),
        "email": str(user.get("email") or ""),
        "name": str(user.get("name") or ""),
        "picture": str(user.get("picture") or ""),
    }


def _clean_next_path(value: object) -> str:
    path = str(value or "").strip()
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    if "\\" in path or "\r" in path or "\n" in path:
        return "/"
    return path[:500] or "/"


@contextmanager
def _locked_user_store():
    with _USER_STORE_LOCK:
        matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (matter_store.DATA_DIR / "users.lock").open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _users_path() -> Path:
    return Path(os.environ.get("NDA_USERS_PATH", "")).expanduser() if os.environ.get("NDA_USERS_PATH") else matter_store.DATA_DIR / "users.json"


def _load_store_unlocked() -> dict[str, Any]:
    users_path = _users_path()
    if not users_path.is_file():
        return _empty_store()
    try:
        with users_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise UserStoreError("User store could not be read.") from exc
    except json.JSONDecodeError as exc:
        raise UserStoreError("User store is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise UserStoreError("User store must contain a JSON object.")
    return {
        "version": USER_STORE_VERSION,
        "users": payload.get("users") if isinstance(payload.get("users"), dict) else {},
        "sessions": payload.get("sessions") if isinstance(payload.get("sessions"), dict) else {},
        "login_states": payload.get("login_states") if isinstance(payload.get("login_states"), dict) else {},
    }


def _save_store_unlocked(store: dict[str, Any]) -> None:
    users_path = _users_path()
    users_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = users_path.with_name(f".{users_path.name}.tmp")
    try:
        fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({
                "version": USER_STORE_VERSION,
                "users": store.get("users") if isinstance(store.get("users"), dict) else {},
                "sessions": store.get("sessions") if isinstance(store.get("sessions"), dict) else {},
                "login_states": store.get("login_states") if isinstance(store.get("login_states"), dict) else {},
            }, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, users_path)
        try:
            os.chmod(users_path, 0o600)
        except OSError:
            pass
        _fsync_directory(users_path.parent)
    except OSError as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise UserStoreError("User store could not be saved.") from exc


def _empty_store() -> dict[str, Any]:
    return {"version": USER_STORE_VERSION, "users": {}, "sessions": {}, "login_states": {}}


def _prune_expired_unlocked(store: dict[str, Any], *, now: float) -> bool:
    changed = False
    for key in ("sessions", "login_states"):
        records = store.setdefault(key, {})
        if not isinstance(records, dict):
            store[key] = {}
            changed = True
            continue
        expired = [
            record_key
            for record_key, record in records.items()
            if not isinstance(record, dict) or _epoch_from_iso(record.get("expires_at")) <= now
        ]
        for record_key in expired:
            records.pop(record_key, None)
            changed = True
    return changed


def _token_hash(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_identity_segment(value: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.@:-]+", "-", str(value or "").strip())[:160].strip("-")
    return cleaned or "unknown"


def _clean_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if "@" not in email or len(email) > 254:
        return ""
    return email


def _clean_display_text(value: object, *, max_length: int) -> str:
    return " ".join(str(value or "").split())[:max_length]


def _clean_url(value: object) -> str:
    url = str(value or "").strip()
    if not url.startswith(("https://", "http://")):
        return ""
    return url[:1000]


def _now_epoch() -> float:
    return time.time()


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _epoch_from_iso(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value or "")).timestamp()
    except ValueError:
        return 0.0


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = getattr(os, "O_RDONLY", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
