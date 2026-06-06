"""Contract tests for the MatterRepository seam, run over BOTH adapters.

The behavioral suite in test_matter_repository.py historically drove only the
InMemoryMatterRepository double, while production ships the disk-backed
matter_store via DiskMatterRepository. That meant the *tested* path and the
*shipped* path could diverge silently: a fix (or a regression) in either
adapter's create/update/delete orchestration would pass the gate as long as the
in-memory double stayed green.

These tests close that gap. Every case is parametrized across both adapters, so
the same contract assertions exercise the shipped DiskMatterRepository
(matter_store) and the InMemoryMatterRepository fast path side by side. The disk
adapter is pointed at an isolated tmp_path per test so it never touches the
shared data dir.
"""
from __future__ import annotations

import pytest

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


@pytest.fixture(params=["disk", "in_memory"])
def repository(request, tmp_path, monkeypatch) -> MatterRepository:
    """A fresh, isolated repository for each adapter under test.

    ``disk`` redirects matter_store's data/uploads paths into tmp_path so the
    SHIPPED adapter runs end to end without disk side effects. ``in_memory`` is
    naturally isolated per instance.
    """
    if request.param == "disk":
        monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
        monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
        monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")
        return DiskMatterRepository()
    return InMemoryMatterRepository()


def test_create_get_list_roundtrip(repository):
    assert repository.list_matters() == []

    matter = repository.create_matter(**_create_kwargs())
    assert matter["id"].startswith("matter_")
    assert matter["source_filename"] == "Mutual NDA.docx"
    assert matter["document_title"] == "Mutual NDA"
    assert matter["board_column"] == "intake"
    assert matter["status"] == "active"
    assert matter["source_type"] == "manual_upload"
    assert matter["triage_status"] == "review"  # triage fields are spread in
    assert matter["headline"] == "Mutual NDA"

    fetched = repository.get_matter(matter["id"])
    assert fetched["id"] == matter["id"]
    assert repository.get_matter("matter_does_not_exist") is None

    listed = repository.list_matters()
    assert [m["id"] for m in listed] == [matter["id"]]


def test_source_document_bytes_roundtrip(repository):
    matter = repository.create_matter(**_create_kwargs(document_bytes=b"the original docx"))
    assert repository.get_source_document_bytes(matter) == b"the original docx"
    # A matter with no stored document yields None.
    assert repository.get_source_document_bytes({"stored_filename": ""}) is None
    assert repository.get_source_document_bytes({"stored_filename": "nope"}) is None


def test_get_matter_returns_a_copy(repository):
    matter = repository.create_matter(**_create_kwargs())
    fetched = repository.get_matter(matter["id"])
    fetched["board_column"] = "mutated"
    # Mutating the returned snapshot must not leak into the store.
    assert repository.get_matter(matter["id"])["board_column"] == "intake"


def test_update_stage_derives_status(repository):
    matter = repository.create_matter(**_create_kwargs())
    matter_id = matter["id"]

    staged = repository.update_matter_stage(matter_id, "signed_closed")
    assert staged["board_column"] == "signed_closed"
    assert staged["status"] == "closed"

    reopened = repository.update_matter_stage(matter_id, "in_review")
    assert reopened["board_column"] == "in_review"
    assert reopened["status"] == "active"

    assert repository.update_matter_stage("matter_missing", "intake") is None


def test_update_fields_filters_and_derives_status(repository):
    matter = repository.create_matter(**_create_kwargs())
    matter_id = matter["id"]

    fielded = repository.update_matter_fields(matter_id, {"board_column": "in_review", "ignored": "x"})
    assert fielded["board_column"] == "in_review"
    assert fielded["status"] == "active"
    assert "ignored" not in fielded

    closed = repository.update_matter_fields(matter_id, {"board_column": "signed_closed"})
    assert closed["status"] == "closed"

    # No recognised fields -> returns the matter unchanged (not None).
    unchanged = repository.update_matter_fields(matter_id, {"ignored": "x"})
    assert unchanged["id"] == matter_id

    signed_off = repository.update_matter_fields(matter_id, {"human_reviewed": True})
    assert signed_off["human_reviewed"] is True


def test_update_redline_draft_set_and_clear(repository):
    matter = repository.create_matter(**_create_kwargs())
    matter_id = matter["id"]

    drafted = repository.update_redline_draft(matter_id, {"manual_redline_edits": [1, 2]})
    assert drafted["redline_draft"] == {"manual_redline_edits": [1, 2]}

    cleared = repository.update_redline_draft(matter_id, None)
    assert "redline_draft" not in cleared


def test_update_review_resets_human_review_and_clears_draft(repository):
    matter = repository.create_matter(**_create_kwargs())
    matter_id = matter["id"]

    repository.update_matter_fields(matter_id, {"human_reviewed": True})
    repository.update_redline_draft(matter_id, {"manual_redline_edits": [1]})

    reviewed = repository.update_matter_review(matter_id, {"clauses": []}, {"triage_status": "pass"})
    assert reviewed["review_result"] == {"clauses": []}
    assert reviewed["triage_status"] == "pass"
    assert reviewed["human_reviewed"] is False
    assert "redline_draft" not in reviewed


def test_update_ai_first_review_stamps_metadata(repository):
    matter = repository.create_matter(**_create_kwargs())
    matter_id = matter["id"]

    ai_reviewed = repository.update_matter_ai_first_review(
        matter_id,
        {"review_mode": "ai_first_compat", "clauses": []},
        {"status": "completed", "mode": "ai_first_assessor"},
    )
    assert ai_reviewed["ai_first_review_result"] == {"review_mode": "ai_first_compat", "clauses": []}
    assert ai_reviewed["ai_first_review_metadata"]["status"] == "completed"
    assert ai_reviewed["ai_first_review_metadata"]["mode"] == "ai_first_assessor"
    assert "stored_at" in ai_reviewed["ai_first_review_metadata"]

    assert repository.update_matter_ai_first_review("matter_missing", {}, {}) is None


def test_append_timeline_event_is_append_only(repository):
    matter = repository.create_matter(**_create_kwargs())
    matter_id = matter["id"]

    first = repository.append_timeline_event(matter_id, {"type": "created", "at": "2026-01-01"})
    assert first["matter_timeline"] == [{"type": "created", "at": "2026-01-01"}]

    second = repository.append_timeline_event(matter_id, {"type": "sent", "at": "2026-01-02"})
    # The prior event is preserved, the new one appended after it (append-only).
    assert [event["type"] for event in second["matter_timeline"]] == ["created", "sent"]

    # A non-dict event is a no-op that returns the matter unchanged (not None).
    unchanged = repository.append_timeline_event(matter_id, None)
    assert len(unchanged["matter_timeline"]) == 2

    assert repository.append_timeline_event("matter_missing", {"type": "x"}) is None


def test_append_timeline_event_does_not_mutate_other_fields(repository):
    matter = repository.create_matter(**_create_kwargs())
    repository.update_matter_fields(matter["id"], {"human_reviewed": True})

    updated = repository.append_timeline_event(matter["id"], {"type": "approved"})
    # Appending an event must not disturb unrelated state.
    assert updated["human_reviewed"] is True
    assert updated["review_result"] == {"clauses": [{"id": "mutuality", "decision": "pass"}]}


def test_set_and_clear_workflow_error(repository):
    matter = repository.create_matter(**_create_kwargs())
    matter_id = matter["id"]

    errored = repository.set_workflow_error(matter_id, {"phase": "review", "code": "ai_error"})
    assert errored["workflow_error"] == {"phase": "review", "code": "ai_error"}

    cleared = repository.set_workflow_error(matter_id, None)
    assert "workflow_error" not in cleared

    assert repository.set_workflow_error("matter_missing", {"phase": "review"}) is None


def test_delete_and_reset(repository):
    matter = repository.create_matter(**_create_kwargs())
    assert repository.delete_matter("matter_missing") is None

    deleted = repository.delete_matter(matter["id"])
    assert deleted["id"] == matter["id"]
    assert repository.list_matters() == []
    assert repository.get_source_document_bytes(matter) is None

    repository.create_matter(**_create_kwargs())
    repository.create_matter(**_create_kwargs())
    assert repository.reset_demo_repository() == 2
    assert repository.list_matters() == []


def test_find_gmail_attachment_and_dedupe(repository):
    gmail_meta = {
        "gmail_message_id": "msg_1",
        "gmail_attachment_id": "att_1",
        "gmail_account": "ops@example.com",
    }
    first = repository.create_matter(
        **_create_kwargs(source_type="gmail", board_column="gmail_demo", intake_metadata=gmail_meta)
    )
    second = repository.create_matter(
        **_create_kwargs(source_type="gmail", board_column="gmail_demo", intake_metadata=gmail_meta)
    )

    found = repository.find_gmail_attachment("msg_1", "att_1")
    assert found is not None
    assert repository.find_gmail_attachment("", "att_1") is None
    assert repository.find_gmail_attachment("msg_other", "att_other") is None

    removed = repository.deduplicate_gmail_matters()
    assert removed == 1
    remaining = repository.list_matters()
    assert len(remaining) == 1
    # The survivor is rank-determined; assert one valid duplicate remains.
    assert remaining[0]["id"] in {first["id"], second["id"]}
    assert remaining[0]["gmail_message_id"] == "msg_1"


def test_gmail_dedupe_keeps_same_filename_when_hashes_conflict(repository):
    for attachment_id, attachment_sha256 in [("att_1", "hash_a"), ("att_2", "hash_b")]:
        repository.create_matter(
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

    removed = repository.deduplicate_gmail_matters()

    assert removed == 0
    assert len(repository.list_matters()) == 2


def test_export_backup_shape(repository):
    repository.create_matter(**_create_kwargs())
    backup = repository.export_matters_backup()
    assert backup["version"] == 1
    assert backup["matter_count"] == 1
    assert len(backup["matters"]) == 1
    assert backup["documents"][0]["present"] is True
    assert backup["documents"][0]["size_bytes"] > 0


def test_owner_scoping_isolates_tenants(repository):
    """Ownership scoping must behave identically on both adapters.

    This is the load-bearing security contract: a matter owned by tenant A must
    never be readable, listable, updatable, or deletable under tenant B. Running
    it over the shipped disk adapter means a cross-tenant fix in matter_store
    (or a regression in either adapter) is caught here, not just on the double.
    """
    owned = repository.create_matter(**_create_kwargs(owner_user_id="tenant-a"))
    matter_id = owned["id"]
    assert owned["owner_user_id"] == "tenant-a"

    # The owner sees the matter; a different tenant does not.
    assert repository.get_matter(matter_id, owner_user_id="tenant-a") is not None
    assert repository.get_matter(matter_id, owner_user_id="tenant-b") is None
    assert [m["id"] for m in repository.list_matters(owner_user_id="tenant-a")] == [matter_id]
    assert repository.list_matters(owner_user_id="tenant-b") == []

    # A foreign tenant cannot mutate or delete another tenant's matter.
    assert repository.update_matter_stage(matter_id, "in_review", owner_user_id="tenant-b") is None
    assert repository.update_matter_fields(matter_id, {"board_column": "in_review"}, owner_user_id="tenant-b") is None
    assert repository.delete_matter(matter_id, owner_user_id="tenant-b") is None
    # ...and the matter is untouched after the rejected writes.
    assert repository.get_matter(matter_id, owner_user_id="tenant-a")["board_column"] == "intake"

    # The owner can.
    assert repository.delete_matter(matter_id, owner_user_id="tenant-a") is not None
    assert repository.get_matter(matter_id, owner_user_id="tenant-a") is None
