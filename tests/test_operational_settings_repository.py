from __future__ import annotations

from pathlib import Path

from nda_automation import app_settings, matter_store
from nda_automation.operational_settings_repository import DiskOperationalSettingsRepository


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
