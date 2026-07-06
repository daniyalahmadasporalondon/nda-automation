"""Master pause gate for Drive activity, with the legacy-safe invariant.

``drive.enabled`` is a real master gate for all Drive activity now, expressed as
``drive_connected(...) AND drive_active()`` where ``drive_active()`` is
``NOT drive_paused``. The invariant these tests defend (the reason the prior
attempt was BLOCKED): a connected user who NEVER hit the new pause toggle keeps
Drive running, EVEN when the stale legacy on-disk value is ``enabled: false``
(what the old normalizer wrote). The gate reads the DISTINCT ``drive_paused``
signal — an absent/legacy ``enabled`` is IGNORED by the gate.

The connected + never-paused + legacy-``enabled:false`` case (a) is exercised
against the REAL settings read (a legacy ``app_settings.json`` written to disk),
NOT a patched ``drive_settings`` — that is the whole point of the regression.
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from nda_automation import app_settings, drive_integration, matter_store, telemetry
from nda_automation.ingestion_service import create_matter_from_document
from nda_automation.operational_settings_repository import _invalidate_settings_cache

NDA_PARAGRAPHS = [
    "This Mutual Non-Disclosure Agreement is entered into by both parties.",
    "Each party agrees to keep the other party's Confidential Information secret.",
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations survive for three years from disclosure.",
]


def _docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _synchronous_runner(work):
    work()


def _fake_synced():
    return {
        "matter_folder_id": "folder_123",
        "matter_folder_url": "https://drive.google.com/drive/folders/folder_123",
        "synced_count": 1,
        "total_count": 1,
        "artifacts": [
            {
                "artifact_id": "a1",
                "filename": "01_received.docx",
                "drive_file_url": "https://drive.google.com/file/d/file_1/view",
            }
        ],
    }


@pytest.fixture(autouse=True)
def _reset_telemetry():
    telemetry.reset()
    yield
    telemetry.reset()


def _write_legacy_drive_settings(drive_section: dict) -> None:
    """Write a REAL app_settings.json to disk (no patching of drive_settings).

    conftest already points ``matter_store.DATA_DIR`` at an isolated tmp dir. We
    write the raw JSON and invalidate the settings read-cache so the very next
    ``app_settings.drive_settings()`` re-reads it from disk — exercising the real
    normalizer + gate, not a monkeypatched stand-in.
    """
    matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = matter_store.DATA_DIR / "app_settings.json"
    path.write_text(json.dumps({"drive": drive_section}), encoding="utf-8")
    _invalidate_settings_cache()


def _create(in_memory_matters, **kwargs):
    return create_matter_from_document(
        filename="mutual-nda.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        repository=in_memory_matters,
        drive_sync_runner=_synchronous_runner,
        **kwargs,
    )


# --- (a) The regression: legacy enabled:false must NOT read as paused --------
def test_legacy_enabled_false_still_runs_when_connected_and_never_paused(in_memory_matters):
    # Exactly what the OLD normalizer left on disk for a connected user: enabled
    # false, NO drive_paused key. The gate must IGNORE this and still run Drive.
    _write_legacy_drive_settings({"enabled": False, "auto_intake": True})

    # Sanity: the real reads resolve to active (not paused) despite enabled:false.
    assert app_settings.drive_settings()["enabled"] is False
    assert app_settings.drive_settings().get("drive_paused") is False
    assert app_settings.drive_paused() is False
    assert app_settings.drive_active() is True

    sync = MagicMock(return_value=_fake_synced())
    # Only connectivity is patched (a token fact, not a settings read). The gate's
    # settings side runs against the REAL on-disk legacy value written above.
    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(drive_integration, "sync_matter_folder", sync):
            matter = _create(in_memory_matters)

    assert sync.call_count == 1
    assert sync.call_args.kwargs["matter_id"] == matter["id"]
    counters = telemetry.snapshot()["counters"]
    assert counters.get("drive_auto_intake_synced") == 1
    assert counters.get("drive_auto_intake_skipped", 0) == 0


def test_absent_drive_section_still_runs_when_connected(in_memory_matters):
    # An even older install with no drive section at all -> defaults (not paused).
    _write_legacy_drive_settings({})
    assert app_settings.drive_active() is True

    sync = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(drive_integration, "sync_matter_folder", sync):
            _create(in_memory_matters)

    assert sync.call_count == 1


# --- (b) Explicit pause stops Drive -----------------------------------------
def test_explicit_pause_stops_auto_intake(in_memory_matters):
    _write_legacy_drive_settings({"enabled": True, "auto_intake": True, "drive_paused": True})
    assert app_settings.drive_paused() is True
    assert app_settings.drive_active() is False

    sync = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(drive_integration, "sync_matter_folder", sync):
            matter = _create(in_memory_matters)

    sync.assert_not_called()
    assert in_memory_matters.get_matter(matter["id"]).get("drive") is None
    assert telemetry.snapshot()["counters"].get("drive_auto_intake_skipped") == 1


# --- (c) Unpause resumes Drive ----------------------------------------------
def test_unpause_resumes_auto_intake(in_memory_matters):
    # Start paused via the real update path, then unpause via the same path.
    app_settings.update_drive_settings({"drive_paused": True})
    assert app_settings.drive_active() is False
    app_settings.update_drive_settings({"drive_paused": False})
    assert app_settings.drive_active() is True

    sync = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(drive_integration, "sync_matter_folder", sync):
            _create(in_memory_matters)

    assert sync.call_count == 1


# --- (d) Disconnected -> off regardless of pause state ----------------------
def test_disconnected_is_off_even_when_never_paused(in_memory_matters):
    _write_legacy_drive_settings({"enabled": False, "auto_intake": True})
    assert app_settings.drive_active() is True  # active, but...

    sync = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "drive_connected", return_value=False):
        with patch.object(drive_integration, "sync_matter_folder", sync):
            matter = _create(in_memory_matters)

    sync.assert_not_called()  # ...not connected -> off.
    assert in_memory_matters.get_matter(matter["id"]).get("drive") is None
    assert telemetry.snapshot()["counters"].get("drive_auto_intake_skipped") == 1


# --- Archive path honours the same gate --------------------------------------
def test_archive_skips_when_paused_and_runs_when_active(in_memory_matters):
    matter = {"id": "m-archive", "title": "Executed"}

    # Paused -> archive skips (real settings read).
    _write_legacy_drive_settings({"enabled": True, "auto_intake": True, "drive_paused": True})
    sync = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(drive_integration, "sync_matter_folder", sync):
            drive_integration.archive_executed_matter(
                matter=matter,
                matter_id="m-archive",
                repository=in_memory_matters,
                owner_user_id="",
                drive_token_owner_user_id="",
                signed_via="manual",
            )
    sync.assert_not_called()
    assert telemetry.snapshot()["counters"].get("drive_oncomplete_skipped") == 1

    # Legacy enabled:false but never paused -> archive still runs.
    telemetry.reset()
    _write_legacy_drive_settings({"enabled": False, "auto_intake": True})
    sync2 = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(drive_integration, "sync_matter_folder", sync2):
            drive_integration.archive_executed_matter(
                matter=matter,
                matter_id="m-archive",
                repository=in_memory_matters,
                owner_user_id="",
                drive_token_owner_user_id="",
                signed_via="manual",
            )
    assert sync2.call_count == 1
