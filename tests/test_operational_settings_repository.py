from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from nda_automation import app_settings, matter_store
from nda_automation import operational_settings_repository as osr
from nda_automation.operational_settings_repository import (
    DiskOperationalSettingsRepository,
    OperationalSettingsError,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # The read cache is module-level global state; reset it around every test so
    # cases don't leak cached blobs into each other.
    osr._invalidate_settings_cache()
    yield
    osr._invalidate_settings_cache()


def test_disk_repository_updates_sections_and_audit_history(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    repository = DiskOperationalSettingsRepository()

    updated = repository.update_section(
        "drive",
        app_settings.drive_settings_from_payload,
        {"enabled": True, "folder_id": "folder_123"},
    )

    assert updated == {
        "enabled": True,
        "folder_id": "folder_123",
        "folder_name": "",
        "auto_intake": True,
        "drive_paused": False,
    }
    assert repository.read_section("drive", app_settings.drive_settings_from_payload) == updated

    first = app_settings.settings_audit_event_from_payload({
        "recorded_at": "2026-06-10T10:00:00+00:00",
        "actor": "admin",
        "action": "first",
        "changes": [{"setting": "drive.enabled", "before": "false", "after": "true"}],
    })
    second = app_settings.settings_audit_event_from_payload({
        "recorded_at": "2026-06-10T11:00:00+00:00",
        "actor": "admin",
        "action": "second",
        "changes": [{"setting": "drive.folder_id", "before": "", "after": "folder_123"}],
    })
    repository.prepend_settings_audit(
        first,
        append_event=app_settings._prepend_settings_audit_event,
        normalize_history=app_settings.settings_audit_history_from_payload,
    )
    history = repository.prepend_settings_audit(
        second,
        append_event=app_settings._prepend_settings_audit_event,
        normalize_history=app_settings.settings_audit_history_from_payload,
    )

    assert [event["action"] for event in history] == ["second", "first"]


def test_disk_repository_rotates_and_clears_secret_files(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    repository = DiskOperationalSettingsRepository()

    repository.save_secret("example_secret.json", "first-secret", "Example secret")
    secret_path = tmp_path / "example_secret.json"

    assert repository.read_secret("example_secret.json", "Example secret") == "first-secret"
    assert secret_path.is_file()
    if hasattr(Path, "chmod"):
        assert secret_path.stat().st_mode & 0o777 == 0o600

    repository.save_secret("example_secret.json", "second-secret", "Example secret")
    assert repository.read_secret("example_secret.json", "Example secret") == "second-secret"

    repository.clear_secret("example_secret.json")
    assert repository.read_secret("example_secret.json", "Example secret") == ""


def test_read_cache_hit_skips_reopening_file(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    repository = DiskOperationalSettingsRepository()

    repository.update_section(
        "drive",
        app_settings.drive_settings_from_payload,
        {"enabled": True, "folder_id": "folder_123"},
    )

    # First read populates the cache (and re-opens the file); count only the
    # loads that follow.
    first = repository.read_settings()
    assert first["drive"]["folder_id"] == "folder_123"

    calls = {"count": 0}
    real_load = DiskOperationalSettingsRepository.load_settings_unlocked

    def _counting_load(self):
        calls["count"] += 1
        return real_load(self)

    monkeypatch.setattr(
        DiskOperationalSettingsRepository, "load_settings_unlocked", _counting_load
    )

    # Second read within the TTL, file unchanged: must be served from cache.
    second = repository.read_settings()
    assert calls["count"] == 0
    assert second["drive"]["folder_id"] == "folder_123"
    # Returned object is a deepcopy: mutating it must not poison the cache.
    second["drive"]["folder_id"] = "mutated"
    third = repository.read_settings()
    assert third["drive"]["folder_id"] == "folder_123"
    assert calls["count"] == 0


def test_read_cache_invalidated_on_write(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    repository = DiskOperationalSettingsRepository()

    repository.update_section(
        "drive",
        app_settings.drive_settings_from_payload,
        {"enabled": True, "folder_id": "folder_123"},
    )
    assert repository.read_settings()["drive"]["folder_id"] == "folder_123"

    # A write must bust the cache so the next read observes the new value.
    repository.update_section(
        "drive",
        app_settings.drive_settings_from_payload,
        {"enabled": True, "folder_id": "folder_456"},
    )
    assert repository.read_settings()["drive"]["folder_id"] == "folder_456"


def test_read_cache_busted_by_external_mtime_change(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    repository = DiskOperationalSettingsRepository()

    repository.update_section(
        "drive",
        app_settings.drive_settings_from_payload,
        {"enabled": True, "folder_id": "folder_123"},
    )
    assert repository.read_settings()["drive"]["folder_id"] == "folder_123"

    # Externally rewrite the file (bypassing the repository writers, so the cache
    # is NOT invalidated) and bump its mtime. The stat mismatch must bust the
    # cache even though we are still within the TTL.
    settings_path = repository.settings_path()
    fresh = json.loads(settings_path.read_text(encoding="utf-8"))
    fresh["drive"]["folder_id"] = "external_789"
    settings_path.write_text(json.dumps(fresh), encoding="utf-8")
    future = time.time() + 10
    os.utime(settings_path, (future, future))

    assert repository.read_settings()["drive"]["folder_id"] == "external_789"


def test_read_settings_bounded_acquire_raises_on_stalled_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    repository = DiskOperationalSettingsRepository()
    repository.update_section(
        "drive",
        app_settings.drive_settings_from_payload,
        {"enabled": True},
    )
    # Ensure the cache fast-path can't short-circuit the lock: clear it and make
    # every flock acquisition fail as if a writer were stalled.
    osr._invalidate_settings_cache()
    monkeypatch.setattr(osr, "SETTINGS_LOCK_TIMEOUT_SECONDS", 0.2)

    if osr.fcntl is None:  # pragma: no cover - Windows portability fallback.
        pytest.skip("fcntl unavailable on this platform")

    def _always_blocked(fileno, operation):
        raise BlockingIOError("locked")

    monkeypatch.setattr(osr.fcntl, "flock", _always_blocked)

    started = time.monotonic()
    with pytest.raises(OperationalSettingsError):
        repository.read_settings()
    elapsed = time.monotonic() - started
    # Bounded, not hung: should give up around the (patched) short timeout.
    assert elapsed < 5


def test_concurrent_shared_reads_complete_without_deadlock(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    repository = DiskOperationalSettingsRepository()
    repository.update_section(
        "drive",
        app_settings.drive_settings_from_payload,
        {"enabled": True, "folder_id": "folder_123"},
    )
    # Defeat the read cache so both readers actually take the shared flock.
    osr._invalidate_settings_cache()
    monkeypatch.setattr(osr, "SETTINGS_READ_CACHE_TTL_SECONDS", 0.0)

    results: list[dict] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _reader():
        try:
            barrier.wait(timeout=5)
            results.append(repository.read_settings())
        except BaseException as exc:  # noqa: BLE001 - surface any thread failure
            errors.append(exc)

    threads = [threading.Thread(target=_reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert len(results) == 2
    assert all(result["drive"]["folder_id"] == "folder_123" for result in results)


def test_read_settings_fails_open_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    repository = DiskOperationalSettingsRepository()

    # No app_settings.json present: stat() raises and we fall through to the
    # normal locked load, which returns {} without raising.
    assert not repository.settings_path().exists()
    assert repository.read_settings() == {}
