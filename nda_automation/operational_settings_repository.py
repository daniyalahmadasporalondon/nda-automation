from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import threading
import time
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

# Bounded acquire window for the settings flock (mirrors matter_store's
# _LOCK_TIMEOUT_SECONDS): reads/writes give up rather than block forever behind a
# stalled writer.
SETTINGS_LOCK_TIMEOUT_SECONDS = 10

# Short-TTL read memoization so hot-path/poll/per-review settings reads can skip the
# flock entirely when the on-disk blob is unchanged. Staleness is bounded to the TTL,
# and any on-disk write (including cross-process) is caught by the (mtime_ns, size)
# stat mismatch below.
SETTINGS_READ_CACHE_TTL_SECONDS = 2.0

# Guarded by _SETTINGS_CACHE_LOCK. Either None (empty) or a tuple of
# (mtime_ns, size, ttl_expiry_monotonic, parsed_dict).
_SETTINGS_CACHE: tuple[int, int, float, dict[str, Any]] | None = None
_SETTINGS_CACHE_LOCK = threading.Lock()


def _invalidate_settings_cache() -> None:
    """Drop any memoized settings blob. Called at the end of every writer."""
    global _SETTINGS_CACHE
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE = None


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
    def locked_settings(self, exclusive: bool = True):
        # --- in-process lock (threading.RLock) with timeout ---
        # RLock.acquire(timeout=N) still succeeds immediately when the *same*
        # thread already holds the lock (re-entrancy is preserved), so nested
        # locked_settings() calls from the same thread are unaffected.
        if not _SETTINGS_LOCK.acquire(timeout=SETTINGS_LOCK_TIMEOUT_SECONDS):
            raise OperationalSettingsError(
                "App settings could not be locked within the timeout "
                f"({SETTINGS_LOCK_TIMEOUT_SECONDS}s). A long-running operation "
                "may be holding the lock. Please retry."
            )
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / "app_settings.lock").open("a+", encoding="utf-8") as lock_file:
                # --- cross-process flock with bounded retry ---
                # Use LOCK_NB so we never sleep inside a kernel call; instead we
                # poll in a tight loop and give up after
                # SETTINGS_LOCK_TIMEOUT_SECONDS, matching the in-process timeout
                # above. Readers take a shared lock (LOCK_SH) so concurrent reads
                # proceed in parallel; writers take LOCK_EX.
                if fcntl is not None:
                    lock_mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    _deadline = time.monotonic() + SETTINGS_LOCK_TIMEOUT_SECONDS
                    while True:
                        try:
                            fcntl.flock(lock_file.fileno(), lock_mode | fcntl.LOCK_NB)
                            break
                        except BlockingIOError as exc:
                            if time.monotonic() >= _deadline:
                                raise OperationalSettingsError(
                                    "App settings file lock could not be acquired "
                                    f"within the timeout ({SETTINGS_LOCK_TIMEOUT_SECONDS}s). "
                                    "Another process may be holding the lock. Please retry."
                                ) from exc
                            time.sleep(0.01)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            _SETTINGS_LOCK.release()

    def read_settings(self) -> dict[str, Any]:
        global _SETTINGS_CACHE
        # Fast path: skip the flock entirely when the on-disk blob is unchanged
        # and the cached entry is still within its TTL. Fail-open: any stat error
        # (missing file, etc.) falls through to the normal locked load.
        settings_path = self.settings_path()
        try:
            stat_result = settings_path.stat()
            current_mtime_ns = stat_result.st_mtime_ns
            current_size = stat_result.st_size
        except OSError:
            current_mtime_ns = None
            current_size = None

        if current_mtime_ns is not None:
            with _SETTINGS_CACHE_LOCK:
                cached = _SETTINGS_CACHE
            if cached is not None:
                cached_mtime_ns, cached_size, ttl_expiry, cached_dict = cached
                if (
                    cached_mtime_ns == current_mtime_ns
                    and cached_size == current_size
                    and time.monotonic() < ttl_expiry
                ):
                    return copy.deepcopy(cached_dict)

        with self.locked_settings(exclusive=False):
            settings = self.load_settings_unlocked()
            # Re-stat after the read so the fingerprint reflects the bytes we
            # actually parsed (mirrors matter_store's post-read fingerprinting).
            try:
                stat_result = settings_path.stat()
                stored_mtime_ns = stat_result.st_mtime_ns
                stored_size = stat_result.st_size
            except OSError:
                stored_mtime_ns = None
                stored_size = None

        if stored_mtime_ns is not None:
            with _SETTINGS_CACHE_LOCK:
                _SETTINGS_CACHE = (
                    stored_mtime_ns,
                    stored_size,
                    time.monotonic() + SETTINGS_READ_CACHE_TTL_SECONDS,
                    copy.deepcopy(settings),
                )
        return settings

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
            _invalidate_settings_cache()
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
            _invalidate_settings_cache()
            return normalize_history(settings["settings_audit"])

    def read_secret(self, filename: str, label: str) -> str:
        with self.locked_settings(exclusive=False):
            return self.read_secret_unlocked(filename, label)

    def save_secret(self, filename: str, api_key: str, label: str) -> None:
        with self.locked_settings():
            self.save_secret_unlocked(filename, api_key, label)
            _invalidate_settings_cache()

    def clear_secret(self, filename: str) -> None:
        with self.locked_settings():
            try:
                self.secret_path(filename).unlink()
            except FileNotFoundError:
                pass
            _invalidate_settings_cache()

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
