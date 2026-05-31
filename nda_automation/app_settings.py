from __future__ import annotations

from contextlib import contextmanager
import json
import os
import threading
from typing import Any

from . import matter_store

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev portability.
    fcntl = None

_SETTINGS_LOCK = threading.RLock()
DEFAULT_GMAIL_SETTINGS = {
    "inbound_enabled": True,
    "outbound_enabled": True,
}


class AppSettingsError(RuntimeError):
    pass


def gmail_settings() -> dict[str, bool]:
    settings = _load_settings()
    gmail = settings.get("gmail")
    if not isinstance(gmail, dict):
        gmail = {}
    return {
        "inbound_enabled": bool(gmail.get("inbound_enabled", DEFAULT_GMAIL_SETTINGS["inbound_enabled"])),
        "outbound_enabled": bool(gmail.get("outbound_enabled", DEFAULT_GMAIL_SETTINGS["outbound_enabled"])),
    }


def update_gmail_settings(updates: dict[str, bool]) -> dict[str, bool]:
    cleaned = {
        key: value
        for key, value in updates.items()
        if key in DEFAULT_GMAIL_SETTINGS and isinstance(value, bool)
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


def gmail_settings_from_payload(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "inbound_enabled": bool(payload.get("inbound_enabled", DEFAULT_GMAIL_SETTINGS["inbound_enabled"])),
        "outbound_enabled": bool(payload.get("outbound_enabled", DEFAULT_GMAIL_SETTINGS["outbound_enabled"])),
    }


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
    except OSError as exc:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise AppSettingsError("App settings could not be saved.") from exc
