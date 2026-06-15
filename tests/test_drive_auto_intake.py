"""Drive auto-intake: a best-effort Repository matter lifecycle hook.

When a matter is created, the lifecycle files it into Drive automatically
(no manual "Save to Drive" click), gated on the owner having Drive connected AND
the ``auto_intake`` setting being on. The sync runs OFF the intake path; these
tests inject a SYNCHRONOUS runner so the background work is deterministic.
"""
from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from nda_automation import app_settings, drive_integration, telemetry
from nda_automation import matter_lifecycle
from nda_automation.ingestion_service import create_matter_from_document

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
    """A drive_sync_runner that runs the work inline (deterministic in tests)."""
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


@pytest.fixture
def _drive_on():
    """Drive connected + auto_intake enabled (the happy-path gate is open)."""
    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(app_settings, "drive_auto_intake_enabled", return_value=True):
            with patch.object(
                app_settings,
                "drive_settings",
                return_value={"enabled": True, "folder_id": "", "folder_name": "", "auto_intake": True},
            ):
                yield


def _create(in_memory_matters, **kwargs):
    return create_matter_from_document(
        filename="mutual-nda.docx",
        document_bytes=_docx(NDA_PARAGRAPHS),
        repository=in_memory_matters,
        drive_sync_runner=_synchronous_runner,
        **kwargs,
    )


def test_auto_sync_runs_when_connected_and_enabled(in_memory_matters, _drive_on):
    sync = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "sync_matter_folder", sync):
        matter = _create(in_memory_matters)

    # sync_matter_folder was called once, with THIS matter (re-fetched by id).
    assert sync.call_count == 1
    call_kwargs = sync.call_args.kwargs
    assert call_kwargs["matter_id"] == matter["id"]
    assert call_kwargs["matter"]["id"] == matter["id"]
    # Success telemetry.
    counters = telemetry.snapshot()["counters"]
    assert counters.get("drive_auto_intake_synced") == 1
    assert counters.get("drive_auto_intake_skipped", 0) == 0
    assert counters.get("drive_auto_intake_failed", 0) == 0


def test_auto_sync_persists_drive_block(in_memory_matters, _drive_on):
    sync = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "sync_matter_folder", sync):
        matter = _create(in_memory_matters)

    stored = in_memory_matters.get_matter(matter["id"])
    assert stored is not None
    drive_block = stored.get("drive")
    assert isinstance(drive_block, dict)
    assert drive_block["matter_folder_id"] == "folder_123"
    assert drive_block["matter_folder_url"].endswith("folder_123")
    assert drive_block["artifacts"][0]["filename"] == "01_received.docx"
    assert drive_block["synced_at"]  # stamped


def test_skipped_when_not_connected(in_memory_matters):
    sync = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "drive_connected", return_value=False):
        with patch.object(app_settings, "drive_auto_intake_enabled", return_value=True):
            with patch.object(drive_integration, "sync_matter_folder", sync):
                matter = _create(in_memory_matters)

    sync.assert_not_called()
    assert in_memory_matters.get_matter(matter["id"]).get("drive") is None
    assert telemetry.snapshot()["counters"].get("drive_auto_intake_skipped") == 1


def test_skipped_when_auto_intake_disabled(in_memory_matters):
    sync = MagicMock(return_value=_fake_synced())
    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(app_settings, "drive_auto_intake_enabled", return_value=False):
            with patch.object(drive_integration, "sync_matter_folder", sync):
                matter = _create(in_memory_matters)

    sync.assert_not_called()
    assert in_memory_matters.get_matter(matter["id"]).get("drive") is None
    assert telemetry.snapshot()["counters"].get("drive_auto_intake_skipped") == 1


def test_skipped_for_gmail_duplicate(in_memory_matters, _drive_on):
    sync = MagicMock(return_value=_fake_synced())
    docx = _docx(NDA_PARAGRAPHS)
    with patch.object(drive_integration, "sync_matter_folder", sync):
        # First import creates the matter (and syncs once).
        first = create_matter_from_document(
            filename="mutual-nda.docx",
            document_bytes=docx,
            dedupe_gmail=True,
            intake_metadata={"gmail_message_id": "m1", "gmail_thread_id": "t1"},
            repository=in_memory_matters,
            drive_sync_runner=_synchronous_runner,
        )
        sync.reset_mock()
        # Re-importing the same gmail attachment returns the existing duplicate.
        second = create_matter_from_document(
            filename="mutual-nda.docx",
            document_bytes=docx,
            dedupe_gmail=True,
            intake_metadata={"gmail_message_id": "m1", "gmail_thread_id": "t1"},
            repository=in_memory_matters,
            drive_sync_runner=_synchronous_runner,
        )

    assert second.get("_existing_gmail_duplicate") is True
    assert second["id"] == first["id"]
    # The duplicate re-import does NOT trigger another auto-sync.
    sync.assert_not_called()


def test_drive_failure_does_not_break_intake(in_memory_matters, _drive_on):
    sync = MagicMock(side_effect=drive_integration.DriveRateLimitError("slow down"))
    with patch.object(drive_integration, "sync_matter_folder", sync):
        # The synchronous runner runs the failing sync inline; intake must still
        # return the matter (the Drive error is swallowed).
        matter = _create(in_memory_matters)

    assert matter["id"]
    assert in_memory_matters.get_matter(matter["id"]) is not None
    # No drive block persisted on failure; failure telemetry recorded.
    assert in_memory_matters.get_matter(matter["id"]).get("drive") is None
    assert telemetry.snapshot()["counters"].get("drive_auto_intake_failed") == 1


def test_default_runner_is_a_daemon_thread():
    # The production default schedules the work on a daemon thread so it never
    # blocks the intake path or process shutdown.
    captured = {}

    real_thread_cls = matter_lifecycle.threading.Thread

    def fake_thread(*args, **kwargs):
        captured["daemon"] = kwargs.get("daemon")
        captured["name"] = kwargs.get("name")
        thread = real_thread_cls(*args, **kwargs)
        return thread

    ran = []
    with patch.object(matter_lifecycle.threading, "Thread", side_effect=fake_thread):
        matter_lifecycle.run_in_daemon_thread(lambda: ran.append(True))

    assert captured["daemon"] is True
    assert captured["name"] == "drive-auto-intake"
