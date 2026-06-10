from __future__ import annotations

from collections.abc import Callable
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

SettingsNormalizer = Callable[[dict[str, Any]], dict[str, Any]]
SectionUpdater = Callable[[dict[str, Any]], dict[str, Any]]
HistoryNormalizer = Callable[[object], list[dict[str, Any]]]
AuditAppender = Callable[[object, dict[str, Any]], list[dict[str, Any]]]

_SETTINGS_LOCK = threading.RLock()


class OperationalSettingsError(RuntimeError):
    pass


class DiskOperationalSettingsRepository:
    """Disk adapter for operational settings, secrets, and audit history.

    This module owns the implementation facts for app settings persistence:
    data-dir paths, lock files, atomic writes, secret-file rotation, fsyncs, and
    append/prepend ordering for history sections. Higher-level settings grammar
    stays in ``app_settings`` as pure normalization functions.
    """

    def __init__(self, *, fsync_directory_func: Callable[[Path], None] | None = None) -> None:
        self._fsync_directory = fsync_directory_func or fsync_directory

    @property
    def data_dir(self) -> Path:
        return matter_store.DATA_DIR

    def settings_path(self) -> Path:
        return self.data_dir / "app_settings.json"

    def secret_path(self, filename: str) -> Path:
        return self.data_dir / filename

    @contextmanager
    def locked_settings(self):
        with _SETTINGS_LOCK:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / "app_settings.lock").open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def read_settings(self) -> dict[str, Any]:
        with self.locked_settings():
            return self.load_settings_unlocked()

    def read_section(self, section: str, normalizer: SettingsNormalizer) -> dict[str, Any]:
        settings = self.read_settings()
        payload = settings.get(section)
        if not isinstance(payload, dict):
            payload = {}
        return normalizer(payload)

    def update_section(
        self,
        section: str,
        normalizer: SettingsNormalizer,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        return self.update_section_with(
            section,
            normalizer,
            lambda current: {**current, **updates},
        )

    def update_section_with(
        self,
        section: str,
        normalizer: SettingsNormalizer,
        updater: SectionUpdater,
    ) -> dict[str, Any]:
        with self.locked_settings():
            settings = self.load_settings_unlocked()
            payload = settings.get(section)
            if not isinstance(payload, dict):
                payload = {}
            settings[section] = updater(normalizer(payload))
            self.save_settings_unlocked(settings)
            return normalizer(settings[section])

    def prepend_settings_audit(
        self,
        event: dict[str, Any],
        *,
        append_event: AuditAppender,
        normalize_history: HistoryNormalizer,
    ) -> list[dict[str, Any]]:
        with self.locked_settings():
            settings = self.load_settings_unlocked()
            settings["settings_audit"] = append_event(settings.get("settings_audit"), event)
            self.save_settings_unlocked(settings)
            return normalize_history(settings["settings_audit"])

    def read_secret(self, filename: str, label: str) -> str:
        with self.locked_settings():
            return self.read_secret_unlocked(filename, label)

    def save_secret(self, filename: str, api_key: str, label: str) -> None:
        with self.locked_settings():
            self.save_secret_unlocked(filename, api_key, label)

    def clear_secret(self, filename: str) -> None:
        with self.locked_settings():
            try:
                self.secret_path(filename).unlink()
            except FileNotFoundError:
                pass

    def load_settings_unlocked(self) -> dict[str, Any]:
        settings_path = self.settings_path()
        if not settings_path.is_file():
            return {}
        try:
            with settings_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except OSError as exc:
            raise OperationalSettingsError("App settings could not be read.") from exc
        except json.JSONDecodeError as exc:
            raise OperationalSettingsError("App settings are not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise OperationalSettingsError("App settings must contain a JSON object.")
        return payload

    def save_settings_unlocked(self, settings: dict[str, Any]) -> None:
        settings_path = self.settings_path()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = settings_path.with_name(f".{settings_path.name}.tmp")
        try:
            with temporary_path.open("w", encoding="utf-8") as handle:
                json.dump(settings, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, settings_path)
            self._fsync_directory(settings_path.parent)
        except OSError as exc:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise OperationalSettingsError("App settings could not be saved.") from exc

    def read_secret_unlocked(self, filename: str, label: str) -> str:
        secret_path = self.secret_path(filename)
        if not secret_path.is_file():
            return ""
        try:
            with secret_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except OSError as exc:
            raise OperationalSettingsError(f"{label} could not be read.") from exc
        except json.JSONDecodeError as exc:
            raise OperationalSettingsError(f"{label} storage is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise OperationalSettingsError(f"{label} storage must contain a JSON object.")
        return str(payload.get("api_key") or "").strip()

    def save_secret_unlocked(self, filename: str, api_key: str, label: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        secret_path = self.secret_path(filename)
        temporary_path = secret_path.with_name(f".{secret_path.name}.tmp")
        try:
            fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"api_key": api_key}, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, secret_path)
            try:
                os.chmod(secret_path, 0o600)
            except OSError:
                pass
            self._fsync_directory(secret_path.parent)
        except OSError as exc:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise OperationalSettingsError(f"{label} could not be saved.") from exc


def fsync_directory(path: Path) -> None:
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
