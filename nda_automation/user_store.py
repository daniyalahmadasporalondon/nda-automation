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
MAX_USER_GMAIL_SYNC_HISTORY = 5
DEFAULT_USER_GMAIL_SYNC = {
    "last_sync_at": "",
    "last_sync_imported_count": 0,
    "last_sync_skipped_count": 0,
    "sync_history": [],
}
_USER_STORE_LOCK = threading.RLock()


class UserStoreError(RuntimeError):
    pass


def create_login_state(*, next_path: str = "/") -> str:
    return create_oauth_state(purpose="login", next_path=next_path)


def create_oauth_state(
    *,
    purpose: str,
    user_id: str = "",
    next_path: str = "/",
    metadata: dict[str, Any] | None = None,
) -> str:
    state = secrets.token_urlsafe(32)
    now = _now_epoch()
    with _locked_user_store():
        store = _load_store_unlocked()
        _prune_expired_unlocked(store, now=now)
        store.setdefault("login_states", {})[_token_hash(state)] = {
            "created_at": _iso_from_epoch(now),
            "expires_at": _iso_from_epoch(now + LOGIN_STATE_TTL_SECONDS),
            "metadata": _clean_metadata(metadata),
            "next_path": _clean_next_path(next_path),
            "purpose": _clean_display_text(purpose, max_length=80),
            "user_id": str(user_id or "").strip(),
        }
        _save_store_unlocked(store)
    return state


def consume_login_state(state: str) -> dict[str, Any] | None:
    return consume_oauth_state(state, purpose="login")


def consume_oauth_state(state: str, *, purpose: str, user_id: str = "") -> dict[str, Any] | None:
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
    if str(state_record.get("purpose") or "") != _clean_display_text(purpose, max_length=80):
        return None
    expected_user_id = str(user_id or "").strip()
    if expected_user_id and str(state_record.get("user_id") or "") != expected_user_id:
        return None
    return {
        "metadata": state_record.get("metadata") if isinstance(state_record.get("metadata"), dict) else {},
        "next_path": _clean_next_path(state_record.get("next_path")),
        "user_id": str(state_record.get("user_id") or ""),
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
            user["gmail_sync"] = gmail_sync_status_from_payload(existing.get("gmail_sync"))
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


def list_users() -> list[dict[str, Any]]:
    now = _now_epoch()
    with _locked_user_store():
        store = _load_store_unlocked()
        changed = _prune_expired_unlocked(store, now=now)
        users = [
            dict(user)
            for user in store.setdefault("users", {}).values()
            if isinstance(user, dict)
        ]
        if changed:
            _save_store_unlocked(store)
    return sorted(users, key=lambda user: (str(user.get("email") or ""), str(user.get("id") or "")))


def gmail_sync_status(user_id: str) -> dict[str, Any]:
    user_id = str(user_id or "").strip()
    if not user_id:
        return gmail_sync_status_from_payload(None)
    now = _now_epoch()
    with _locked_user_store():
        store = _load_store_unlocked()
        changed = _prune_expired_unlocked(store, now=now)
        user = store.setdefault("users", {}).get(user_id)
        sync = user.get("gmail_sync") if isinstance(user, dict) else None
        if changed:
            _save_store_unlocked(store)
    return gmail_sync_status_from_payload(sync)


def record_user_gmail_sync(
    user_id: str,
    result: dict[str, Any],
    *,
    synced_at: str,
    started_at: str = "",
    finished_at: str = "",
) -> dict[str, Any]:
    sync_run = _sync_history_entry(
        result,
        started_at=started_at or synced_at,
        finished_at=finished_at or synced_at,
        status="success",
    )
    return _record_user_gmail_sync_run(user_id, result, sync_run=sync_run, synced_at=synced_at)


def record_user_gmail_sync_error(
    user_id: str,
    error: str,
    *,
    started_at: str,
    finished_at: str,
    query: str = "",
) -> dict[str, Any]:
    result = {"imported": [], "skipped": [], "query": query}
    sync_run = _sync_history_entry(
        result,
        started_at=started_at,
        finished_at=finished_at,
        status="error",
        error=error,
    )
    return _record_user_gmail_sync_run(user_id, result, sync_run=sync_run, synced_at=finished_at)


def gmail_sync_status_from_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    return {
        "last_sync_at": str(payload.get("last_sync_at") or DEFAULT_USER_GMAIL_SYNC["last_sync_at"]),
        "last_sync_imported_count": _nonnegative_int(
            payload.get("last_sync_imported_count"),
            DEFAULT_USER_GMAIL_SYNC["last_sync_imported_count"],
        ),
        "last_sync_skipped_count": _nonnegative_int(
            payload.get("last_sync_skipped_count"),
            DEFAULT_USER_GMAIL_SYNC["last_sync_skipped_count"],
        ),
        "sync_history": _sync_history_from_payload(payload.get("sync_history")),
    }


def users_path() -> Path:
    return _users_path()


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


def _clean_metadata(value: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, item in list(value.items())[:20]:
        cleaned_key = _clean_display_text(key, max_length=80)
        if not cleaned_key:
            continue
        cleaned[cleaned_key] = _clean_display_text(item, max_length=500)
    return cleaned


def _record_user_gmail_sync_run(
    user_id: str,
    result: dict[str, Any],
    *,
    sync_run: dict[str, Any],
    synced_at: str,
) -> dict[str, Any]:
    user_id = str(user_id or "").strip()
    if not user_id:
        raise UserStoreError("Gmail sync user is required.")
    imported = result.get("imported") if isinstance(result.get("imported"), list) else []
    skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
    with _locked_user_store():
        store = _load_store_unlocked()
        users = store.setdefault("users", {})
        user = users.get(user_id)
        if not isinstance(user, dict):
            raise UserStoreError("Gmail sync user does not exist.")
        current_sync = gmail_sync_status_from_payload(user.get("gmail_sync"))
        user["gmail_sync"] = {
            **current_sync,
            "last_sync_at": str(synced_at or ""),
            "last_sync_imported_count": len(imported),
            "last_sync_skipped_count": len(skipped),
            "sync_history": _prepend_sync_history(current_sync.get("sync_history"), sync_run),
        }
        _save_store_unlocked(store)
    return gmail_sync_status_from_payload(user["gmail_sync"])


def _sync_history_entry(
    result: dict[str, Any],
    *,
    started_at: str,
    finished_at: str,
    status: str,
    error: str = "",
) -> dict[str, Any]:
    imported = result.get("imported") if isinstance(result.get("imported"), list) else []
    skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
    duplicate_count = sum(1 for item in skipped if isinstance(item, dict) and item.get("reason") == "duplicate_attachment")
    deduplicated_count = _nonnegative_int(result.get("deduplicated_count"), 0)
    review_failed_count = sum(1 for item in skipped if isinstance(item, dict) and item.get("reason") == "review_failed")
    return {
        "started_at": str(started_at or ""),
        "finished_at": str(finished_at or ""),
        "query": str(result.get("query") or ""),
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "duplicate_count": duplicate_count,
        "deduplicated_count": deduplicated_count,
        "review_failed_count": review_failed_count,
        "status": "error" if status == "error" else "success",
        "error": str(error or "")[:500],
    }


def _prepend_sync_history(history: object, sync_run: dict[str, Any]) -> list[dict[str, Any]]:
    return [sync_run, *_sync_history_from_payload(history)][:MAX_USER_GMAIL_SYNC_HISTORY]


def _sync_history_from_payload(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    history: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        history.append({
            "started_at": str(item.get("started_at") or ""),
            "finished_at": str(item.get("finished_at") or ""),
            "query": str(item.get("query") or ""),
            "imported_count": _nonnegative_int(item.get("imported_count"), 0),
            "skipped_count": _nonnegative_int(item.get("skipped_count"), 0),
            "duplicate_count": _nonnegative_int(item.get("duplicate_count"), 0),
            "deduplicated_count": _nonnegative_int(item.get("deduplicated_count"), 0),
            "review_failed_count": _nonnegative_int(item.get("review_failed_count"), 0),
            "status": "error" if item.get("status") == "error" else "success",
            "error": str(item.get("error") or "")[:500],
        })
        if len(history) >= MAX_USER_GMAIL_SYNC_HISTORY:
            break
    return history


def _nonnegative_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, parsed)


@contextmanager
def _locked_user_store():
    with _USER_STORE_LOCK:
        # Lock alongside the resolved store file, not always matter_store.DATA_DIR.
        # When NDA_USERS_PATH redirects the store (e.g. test isolation), the lock
        # and any directory creation must follow it so we neither serialize on the
        # wrong directory nor scatter a stray users.lock into the real data dir.
        lock_dir = _users_path().parent
        lock_dir.mkdir(parents=True, exist_ok=True)
        with (lock_dir / "users.lock").open("a+", encoding="utf-8") as lock_file:
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
