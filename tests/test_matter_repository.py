"""Tests for the matter-persistence seam (MatterRepository + adapters)."""
from __future__ import annotations

from pathlib import Path
import threading
from unittest.mock import patch

from nda_automation import matter_repository as matter_repository_module
from nda_automation import matter_store
from nda_automation.matter_repository import (
    DiskMatterRepository,
    InMemoryMatterRepository,
    MatterRepository,
)


def _create_kwargs(**overrides):
    kwargs = dict(
        source_filename="Mutual NDA.docx",
        document_bytes=b"PK\x03\x04 fake docx bytes",
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": [{"id": "mutuality", "decision": "pass"}]},
        triage={"triage_status": "review", "headline": "Mutual NDA"},
        source_type="manual_upload",
        board_column="intake",
    )
    kwargs.update(overrides)
    return kwargs


def test_both_adapters_satisfy_protocol():
    assert isinstance(DiskMatterRepository(), MatterRepository)
    assert isinstance(InMemoryMatterRepository(), MatterRepository)


def test_create_get_list_roundtrip():
    repo = InMemoryMatterRepository()
    assert repo.list_matters() == []

    matter = repo.create_matter(**_create_kwargs())
    assert matter["id"].startswith("matter_")
    assert matter["source_filename"] == "Mutual NDA.docx"
    assert matter["board_column"] == "intake"
    assert matter["status"] == "active"
    assert matter["triage_status"] == "review"  # triage fields are spread in

    fetched = repo.get_matter(matter["id"])
    assert fetched["id"] == matter["id"]
    assert repo.get_matter("matter_does_not_exist") is None

    listed = repo.list_matters()
    assert [m["id"] for m in listed] == [matter["id"]]


def test_source_document_bytes_roundtrip():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs(document_bytes=b"the original docx"))
    assert repo.get_source_document_bytes(matter) == b"the original docx"
    # A matter with no stored document yields None.
    assert repo.get_source_document_bytes({"stored_filename": ""}) is None
    assert repo.get_source_document_bytes({"stored_filename": "nope"}) is None


def test_get_matter_returns_a_copy():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())
    fetched = repo.get_matter(matter["id"])
    fetched["board_column"] = "mutated"
    # Mutating the returned snapshot must not leak into the store.
    assert repo.get_matter(matter["id"])["board_column"] == "intake"


def test_updates_stage_fields_redline_review():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())
    matter_id = matter["id"]

    staged = repo.update_matter_stage(matter_id, "signed_closed")
    assert staged["board_column"] == "signed_closed"
    assert staged["status"] == "closed"

    # board_column via update_matter_fields also derives status
    fielded = repo.update_matter_fields(matter_id, {"board_column": "in_review", "ignored": "x"})
    assert fielded["board_column"] == "in_review"
    assert fielded["status"] == "active"
    assert "ignored" not in fielded

    # no recognised fields -> returns the matter unchanged (not None)
    unchanged = repo.update_matter_fields(matter_id, {"ignored": "x"})
    assert unchanged["id"] == matter_id

    drafted = repo.update_redline_draft(matter_id, {"manual_redline_edits": [1, 2]})
    assert drafted["redline_draft"] == {"manual_redline_edits": [1, 2]}
    cleared = repo.update_redline_draft(matter_id, None)
    assert "redline_draft" not in cleared

    signed_off = repo.update_matter_fields(matter_id, {"human_reviewed": True})
    assert signed_off["human_reviewed"] is True
    stale_draft = repo.update_redline_draft(
        matter_id,
        {
            "redline_decisions": {"old-redline-id": False},
            "template_selections": {"old-redline-id": "india"},
        },
    )
    assert "redline_draft" in stale_draft

    reviewed = repo.update_matter_review(matter_id, {"clauses": []}, {"triage_status": "pass"})
    assert reviewed["review_result"] == {"clauses": []}
    assert reviewed["triage_status"] == "pass"
    assert reviewed["human_reviewed"] is False
    assert "redline_draft" not in reviewed

    ai_reviewed = repo.update_matter_ai_first_review(
        matter_id,
        {"review_mode": "ai_first_compat", "clauses": []},
        {"status": "completed", "mode": "ai_first_assessor"},
    )
    assert ai_reviewed["ai_first_review_result"] == {"review_mode": "ai_first_compat", "clauses": []}
    assert ai_reviewed["ai_first_review_metadata"]["status"] == "completed"
    assert ai_reviewed["ai_first_review_metadata"]["mode"] == "ai_first_assessor"
    assert "stored_at" in ai_reviewed["ai_first_review_metadata"]
    assert ai_reviewed["review_result"] == {"clauses": []}
    assert ai_reviewed["triage_status"] == "pass"

    assert repo.update_matter_stage("matter_missing", "intake") is None
    assert repo.update_matter_ai_first_review("matter_missing", {}, {}) is None


def test_delete_and_reset():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())
    assert repo.delete_matter("matter_missing") is None

    deleted = repo.delete_matter(matter["id"])
    assert deleted["id"] == matter["id"]
    assert repo.list_matters() == []
    assert repo.get_source_document_bytes(matter) is None

    repo.create_matter(**_create_kwargs())
    repo.create_matter(**_create_kwargs())
    assert repo.reset_demo_repository() == 2
    assert repo.list_matters() == []


def test_isolation_between_instances():
    repo_a = InMemoryMatterRepository()
    repo_b = InMemoryMatterRepository()
    matter = repo_a.create_matter(**_create_kwargs())
    assert repo_a.get_matter(matter["id"]) is not None
    assert repo_b.get_matter(matter["id"]) is None
    assert repo_b.list_matters() == []


def test_find_gmail_attachment_and_dedupe():
    repo = InMemoryMatterRepository()
    gmail_meta = {
        "gmail_message_id": "msg_1",
        "gmail_attachment_id": "att_1",
        "gmail_account": "ops@example.com",
    }
    first = repo.create_matter(
        **_create_kwargs(source_type="gmail", board_column="gmail_demo", intake_metadata=gmail_meta)
    )
    second = repo.create_matter(
        **_create_kwargs(source_type="gmail", board_column="gmail_demo", intake_metadata=gmail_meta)
    )

    found = repo.find_gmail_attachment("msg_1", "att_1")
    assert found is not None
    assert repo.find_gmail_attachment("", "att_1") is None
    assert repo.find_gmail_attachment("msg_other", "att_other") is None

    removed = repo.deduplicate_gmail_matters()
    assert removed == 1
    remaining = repo.list_matters()
    assert len(remaining) == 1
    # The survivor is rank-determined; assert one valid duplicate remains.
    assert remaining[0]["id"] in {first["id"], second["id"]}
    assert remaining[0]["gmail_message_id"] == "msg_1"


def test_gmail_dedupe_uses_key_index_without_pairwise_matching():
    repo = InMemoryMatterRepository()
    gmail_meta = {
        "gmail_message_id": "msg_1",
        "gmail_attachment_id": "att_1",
        "gmail_account": "ops@example.com",
    }
    repo.create_matter(**_create_kwargs(source_type="gmail", board_column="gmail_demo", intake_metadata=gmail_meta))
    repo.create_matter(**_create_kwargs(source_type="gmail", board_column="gmail_demo", intake_metadata=gmail_meta))

    patch_target = (
        matter_repository_module
        if hasattr(matter_repository_module, "_gmail_attachments_match")
        else matter_store
    )
    with patch.object(patch_target, "_gmail_attachments_match", side_effect=AssertionError("pairwise matcher called")):
        removed = repo.deduplicate_gmail_matters()

    assert removed == 1
    assert len(repo.list_matters()) == 1


def test_gmail_dedupe_keeps_same_filename_when_hashes_conflict():
    repo = InMemoryMatterRepository()
    for attachment_id, attachment_sha256 in [("att_1", "hash_a"), ("att_2", "hash_b")]:
        repo.create_matter(
            **_create_kwargs(
                source_type="gmail",
                board_column="gmail_demo",
                intake_metadata={
                    "attachment_filename": "Counterparty NDA.docx",
                    "gmail_attachment_id": attachment_id,
                    "gmail_attachment_sha256": attachment_sha256,
                    "gmail_message_id": "msg_1",
                },
            )
        )

    removed = repo.deduplicate_gmail_matters()

    assert removed == 0
    assert len(repo.list_matters()) == 2


def test_export_backup_shape():
    repo = InMemoryMatterRepository()
    repo.create_matter(**_create_kwargs())
    backup = repo.export_matters_backup()
    assert backup["version"] == 1
    assert backup["matter_count"] == 1
    assert len(backup["matters"]) == 1
    assert backup["documents"][0]["present"] is True
    assert backup["documents"][0]["size_bytes"] > 0


def test_disk_inmemory_parity(tmp_path, monkeypatch):
    """The same create on both adapters yields equivalent stable fields + bytes."""
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
    monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")

    disk = DiskMatterRepository()
    mem = InMemoryMatterRepository()
    kwargs = _create_kwargs(document_bytes=b"shared bytes")

    disk_matter = disk.create_matter(**kwargs)
    mem_matter = mem.create_matter(**kwargs)

    stable_fields = [
        "source_filename",
        "document_title",
        "status",
        "board_column",
        "source_type",
        "extracted_text",
        "review_result",
        "triage_status",
        "headline",
    ]
    for field in stable_fields:
        assert disk_matter[field] == mem_matter[field], field

    assert disk.get_source_document_bytes(disk_matter) == b"shared bytes"
    assert mem.get_source_document_bytes(mem_matter) == b"shared bytes"


def test_disk_store_migrates_legacy_matters_json_to_record_files(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
    monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")
    legacy_matter = {
        "id": "matter_legacy",
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-01T00:00:00+00:00",
        "source_filename": "Legacy NDA.docx",
        "stored_filename": "matter_legacy-Legacy-NDA.docx",
        "board_column": "gmail_demo",
        "status": "active",
    }
    matter_store._save_matters([legacy_matter])
    repo = DiskMatterRepository()

    updated = repo.update_matter_stage("matter_legacy", "in_review")

    record_path = tmp_path / "matters" / "matter_legacy.json"
    assert updated["board_column"] == "in_review"
    assert record_path.is_file()
    assert not matter_store.MATTERS_PATH.exists()
    assert (tmp_path / "matters.json.legacy").is_file()
    assert repo.get_matter("matter_legacy")["board_column"] == "in_review"


def test_disk_store_prefers_legacy_file_until_migration_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
    monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")
    legacy_matter = {
        "id": "matter_legacy",
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-01T00:00:00+00:00",
        "source_filename": "Legacy NDA.docx",
        "stored_filename": "matter_legacy-Legacy-NDA.docx",
        "board_column": "gmail_demo",
        "status": "active",
    }
    matter_store._save_matters([legacy_matter])
    (tmp_path / "matters").mkdir()
    matter_store._write_matter_record({
        **legacy_matter,
        "id": "matter_partial",
        "board_column": "signed_closed",
    })
    repo = DiskMatterRepository()

    listed_before_migration = repo.list_matters()
    updated = repo.update_matter_stage("matter_legacy", "in_review")

    assert [matter["id"] for matter in listed_before_migration] == ["matter_legacy"]
    assert updated["board_column"] == "in_review"
    assert repo.get_matter("matter_legacy")["board_column"] == "in_review"


def test_disk_create_update_delete_do_not_use_monolithic_save(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
    monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")
    repo = DiskMatterRepository()

    with patch.object(matter_store, "_save_matters", side_effect=AssertionError("monolithic save used")):
        matter = repo.create_matter(**_create_kwargs())
        updated = repo.update_matter_stage(matter["id"], "in_review")
        deleted = repo.delete_matter(matter["id"])

    assert matter["id"] == updated["id"] == deleted["id"]
    assert not matter_store.MATTERS_PATH.exists()
    assert list((tmp_path / "matters").glob("*.json")) == []


def test_disk_create_does_not_hold_store_lock_while_writing_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
    monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")

    # The source-doc bytes are staged through matter_store._write_bytes_atomic
    # (tmp+fsync+replace+dir-fsync durability), before the store lock is taken.
    # Hook that helper to prove two concurrent creates can both be mid-upload-write
    # at the same time (i.e. the write is NOT serialized under the store lock).
    original_write_bytes_atomic = matter_store._write_bytes_atomic
    first_upload_started = threading.Event()
    second_upload_started = threading.Event()
    release_first_upload = threading.Event()
    write_count_lock = threading.Lock()
    upload_write_count = 0
    errors: list[BaseException] = []

    def delayed_write_bytes_atomic(path: Path, data: bytes) -> None:
        nonlocal upload_write_count
        path = Path(path)
        if path.parent == matter_store.UPLOADS_DIR and path.name.startswith("matter_"):
            with write_count_lock:
                upload_write_count += 1
                write_number = upload_write_count
            if write_number == 1:
                first_upload_started.set()
                if not release_first_upload.wait(timeout=3):
                    raise AssertionError("timed out waiting to release first upload write")
            elif write_number == 2:
                second_upload_started.set()
        original_write_bytes_atomic(path, data)

    monkeypatch.setattr(matter_store, "_write_bytes_atomic", delayed_write_bytes_atomic)

    def create_matter(document_bytes: bytes) -> None:
        try:
            matter_store.create_matter(**_create_kwargs(document_bytes=document_bytes))
        except BaseException as error:  # noqa: BLE001 - surfaced below for the worker thread
            errors.append(error)

    first_writer = threading.Thread(target=create_matter, args=(b"slow upload bytes",))
    first_writer.start()
    assert first_upload_started.wait(timeout=2)

    second_writer = threading.Thread(target=create_matter, args=(b"second upload bytes",))
    second_writer.start()
    second_started_while_first_upload_paused = second_upload_started.wait(timeout=1)

    release_first_upload.set()
    first_writer.join(timeout=2)
    second_writer.join(timeout=2)

    assert second_started_while_first_upload_paused
    assert not first_writer.is_alive()
    assert not second_writer.is_alive()
    assert errors == []
    assert len(matter_store.list_matters()) == 2
