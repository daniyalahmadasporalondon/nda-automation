from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from typing import Any

from . import matter_store

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

_SETTINGS_LOCK = threading.RLock()
MAX_GMAIL_SYNC_HISTORY = 5
DEFAULT_GMAIL_SETTINGS = {
    "inbound_enabled": True,
    "outbound_enabled": True,
    "sync_frequency": "10_minutes",
    "last_sync_at": "",
    "last_sync_imported_count": 0,
    "last_sync_skipped_count": 0,
    "sync_history": [],
}
DEFAULT_AI_SETTINGS = {
    "enabled": None,
    "provider": "",
    "model": "",
}
AI_API_KEY_FILENAME = "ai_api_key.json"
MAX_AI_API_KEY_LENGTH = 2000
GMAIL_SYNC_FREQUENCIES = {
    "always_on": 60,
    "10_minutes": 10 * 60,
    "30_minutes": 30 * 60,
    "1_hour": 60 * 60,
    "2_hours": 2 * 60 * 60,
}


class AppSettingsError(RuntimeError):
    pass


def gmail_settings() -> dict[str, Any]:
    settings = _load_settings()
    gmail = settings.get("gmail")
    if not isinstance(gmail, dict):
        gmail = {}
    return gmail_settings_from_payload(gmail)


def ai_settings() -> dict[str, Any]:
    settings = _load_settings()
    ai_review = settings.get("ai_review")
    if not isinstance(ai_review, dict):
        ai_review = {}
    return ai_settings_from_payload(ai_review)


def update_ai_settings(updates: dict[str, Any]) -> dict[str, Any]:
    cleaned = {
        key: value
        for key, value in updates.items()
        if _valid_ai_setting(key, value)
    }
    if not cleaned:
        return ai_settings()

    with _locked_settings():
        settings = _load_settings_unlocked()
        ai_review = settings.get("ai_review")
        if not isinstance(ai_review, dict):
            ai_review = {}
        settings["ai_review"] = {**ai_settings_from_payload(ai_review), **cleaned}
        _save_settings_unlocked(settings)
        return ai_settings_from_payload(settings["ai_review"])


def stored_ai_api_key() -> str:
    with _locked_settings():
        return _stored_ai_api_key_unlocked()


def save_ai_api_key(api_key: str) -> None:
    cleaned_key = str(api_key or "").strip()
    if not cleaned_key:
        raise AppSettingsError("AI API key is required.")
    if len(cleaned_key) > MAX_AI_API_KEY_LENGTH:
        raise AppSettingsError("AI API key is too long.")

    with _locked_settings():
        _save_ai_api_key_unlocked(cleaned_key)


def clear_ai_api_key() -> None:
    with _locked_settings():
        try:
            _ai_api_key_path().unlink()
        except FileNotFoundError:
            pass


def update_gmail_settings(updates: dict[str, Any]) -> dict[str, Any]:
    cleaned = {
        key: value
        for key, value in updates.items()
        if _valid_gmail_setting(key, value)
    }
    if not cleaned:
        return gmail_settings()

    with _locked_settings():
        settings = _load_settings_unlocked()
        gmail = settings.get("gmail")
        if not isinstance(gmail, dict):
            gmail = {}
        settings["gmail"] = {**gmail_settings_from_payload(gmail), **cleaned}
        _save_settings_unlocked(settings)
        return gmail_settings_from_payload(settings["gmail"])


def gmail_role_enabled(role: str) -> bool:
    key = f"{role}_enabled"
    return gmail_settings().get(key, True)


def gmail_sync_interval_seconds(frequency: object | None = None) -> int:
    frequency_key = frequency if isinstance(frequency, str) else gmail_settings()["sync_frequency"]
    return GMAIL_SYNC_FREQUENCIES.get(frequency_key, GMAIL_SYNC_FREQUENCIES[DEFAULT_GMAIL_SETTINGS["sync_frequency"]])


def record_gmail_sync(
    result: dict[str, Any],
    *,
    synced_at: str,
    started_at: str = "",
    finished_at: str = "",
) -> dict[str, Any]:
    imported = result.get("imported") if isinstance(result.get("imported"), list) else []
    skipped = result.get("skipped") if isinstance(result.get("skipped"), list) else []
    sync_run = _sync_history_entry(
        result,
        started_at=started_at or synced_at,
        finished_at=finished_at or synced_at,
        status="success",
    )
    with _locked_settings():
        settings = _load_settings_unlocked()
        gmail = settings.get("gmail")
        if not isinstance(gmail, dict):
            gmail = {}
        current_gmail = gmail_settings_from_payload(gmail)
        settings["gmail"] = {
            **current_gmail,
            "last_sync_at": synced_at,
            "last_sync_imported_count": len(imported),
            "last_sync_skipped_count": len(skipped),
            "sync_history": _prepend_sync_history(current_gmail.get("sync_history"), sync_run),
        }
        _save_settings_unlocked(settings)
        return gmail_settings_from_payload(settings["gmail"])


def record_gmail_sync_error(
    error: str,
    *,
    started_at: str,
    finished_at: str,
    query: str = "",
) -> dict[str, Any]:
    sync_run = _sync_history_entry(
        {"imported": [], "skipped": [], "query": query},
        started_at=started_at,
        finished_at=finished_at,
        status="error",
        error=error,
    )
    with _locked_settings():
        settings = _load_settings_unlocked()
        gmail = settings.get("gmail")
        if not isinstance(gmail, dict):
            gmail = {}
        current_gmail = gmail_settings_from_payload(gmail)
        settings["gmail"] = {
            **current_gmail,
            "last_sync_at": finished_at,
            "last_sync_imported_count": 0,
            "last_sync_skipped_count": 0,
            "sync_history": _prepend_sync_history(current_gmail.get("sync_history"), sync_run),
        }
        _save_settings_unlocked(settings)
        return gmail_settings_from_payload(settings["gmail"])


def gmail_settings_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_frequency = payload.get("sync_frequency", payload.get("sync_cadence", DEFAULT_GMAIL_SETTINGS["sync_frequency"]))
    sync_frequency = str(raw_frequency or DEFAULT_GMAIL_SETTINGS["sync_frequency"])
    if sync_frequency not in GMAIL_SYNC_FREQUENCIES:
        sync_frequency = DEFAULT_GMAIL_SETTINGS["sync_frequency"]
    return {
        "inbound_enabled": bool(payload.get("inbound_enabled", DEFAULT_GMAIL_SETTINGS["inbound_enabled"])),
        "outbound_enabled": bool(payload.get("outbound_enabled", DEFAULT_GMAIL_SETTINGS["outbound_enabled"])),
        "sync_frequency": sync_frequency,
        "last_sync_at": str(payload.get("last_sync_at") or DEFAULT_GMAIL_SETTINGS["last_sync_at"]),
        "last_sync_imported_count": _nonnegative_int(
            payload.get("last_sync_imported_count"),
            DEFAULT_GMAIL_SETTINGS["last_sync_imported_count"],
        ),
        "last_sync_skipped_count": _nonnegative_int(
            payload.get("last_sync_skipped_count"),
            DEFAULT_GMAIL_SETTINGS["last_sync_skipped_count"],
        ),
        "sync_history": _sync_history_from_payload(payload.get("sync_history")),
    }


def ai_settings_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    enabled = payload.get("enabled", DEFAULT_AI_SETTINGS["enabled"])
    if not isinstance(enabled, bool):
        enabled = None
    provider = str(payload.get("provider") or DEFAULT_AI_SETTINGS["provider"]).strip().lower()
    if provider not in {"", "gemini", "openrouter"}:
        provider = ""
    model = str(payload.get("model") or DEFAULT_AI_SETTINGS["model"]).strip()
    if len(model) > 200:
        model = ""
    return {"enabled": enabled, "provider": provider, "model": model}


def _valid_ai_setting(key: str, value: Any) -> bool:
    if key == "enabled":
        return isinstance(value, bool)
    if key == "provider":
        return isinstance(value, str) and value.strip().lower() in {"", "gemini", "openrouter"}
    if key == "model":
        return isinstance(value, str) and len(value.strip()) <= 200
    return False


def _valid_gmail_setting(key: str, value: Any) -> bool:
    if key in ("inbound_enabled", "outbound_enabled"):
        return isinstance(value, bool)
    if key == "sync_frequency":
        return isinstance(value, str) and value in GMAIL_SYNC_FREQUENCIES
    return False


def _nonnegative_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, parsed)


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
    return [sync_run, *_sync_history_from_payload(history)][:MAX_GMAIL_SYNC_HISTORY]


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
        if len(history) >= MAX_GMAIL_SYNC_HISTORY:
            break
    return history


def _load_settings() -> dict[str, Any]:
    with _locked_settings():
        return _load_settings_unlocked()


@contextmanager
def _locked_settings():
    with _SETTINGS_LOCK:
        matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with (matter_store.DATA_DIR / "app_settings.lock").open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _settings_path():
    return matter_store.DATA_DIR / "app_settings.json"


def _ai_api_key_path():
    return matter_store.DATA_DIR / AI_API_KEY_FILENAME


def _load_settings_unlocked() -> dict[str, Any]:
    settings_path = _settings_path()
    if not settings_path.is_file():
        return {}
    try:
        with settings_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise AppSettingsError("App settings could not be read.") from exc
    except json.JSONDecodeError as exc:
        raise AppSettingsError("App settings are not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise AppSettingsError("App settings must contain a JSON object.")
    return payload


def _stored_ai_api_key_unlocked() -> str:
    api_key_path = _ai_api_key_path()
    if not api_key_path.is_file():
        return ""
    try:
        with api_key_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise AppSettingsError("AI API key could not be read.") from exc
    except json.JSONDecodeError as exc:
        raise AppSettingsError("AI API key storage is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise AppSettingsError("AI API key storage must contain a JSON object.")
    return str(payload.get("api_key") or "").strip()


def _save_ai_api_key_unlocked(api_key: str) -> None:
    api_key_path = _ai_api_key_path()
    matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = api_key_path.with_name(f".{api_key_path.name}.tmp")
    try:
        fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"api_key": api_key}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, api_key_path)
        try:
            os.chmod(api_key_path, 0o600)
        except OSError:
            pass
        _fsync_directory(api_key_path.parent)
    except OSError as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise AppSettingsError("AI API key could not be saved.") from exc


def _save_settings_unlocked(settings: dict[str, Any]) -> None:
    settings_path = _settings_path()
    matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = settings_path.with_name(f".{settings_path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, settings_path)
        _fsync_directory(settings_path.parent)
    except OSError as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise AppSettingsError("App settings could not be saved.") from exc


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
