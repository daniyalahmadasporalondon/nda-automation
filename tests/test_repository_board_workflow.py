from __future__ import annotations

import pytest

from nda_automation import repository_board_workflow
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.repository_board_workflow import (
    RepositoryBoardWorkflow,
    RepositoryBoardWorkflowError,
)


def _create_matter(repo: InMemoryMatterRepository, **overrides):
    kwargs = {
        "source_filename": "Board NDA.docx",
        "document_bytes": b"PK\x03\x04 fake docx bytes",
        "extracted_text": "This Agreement is mutual.",
        "review_result": {"clauses": [{"id": "mutuality", "decision": "pass"}]},
        "triage": {
            "triage_status": "ready_to_sign",
            "issue_count": 0,
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 0,
        },
        "source_type": "manual_upload",
        "board_column": "in_review",
    }
    kwargs.update(overrides)
    return repo.create_matter(**kwargs)


def test_board_list_and_detail_return_public_payloads():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo, owner_user_id="tenant-a")
    _create_matter(repo, owner_user_id="tenant-b")
    workflow = RepositoryBoardWorkflow(repo)

    listed = workflow.list_board(owner_user_id="tenant-a")
    detailed = workflow.detail_card(matter["id"], owner_user_id="tenant-a")

    assert [item["id"] for item in listed["matters"]] == [matter["id"]]
    assert listed["matters"][0]["board_column"] == "in_review"
    assert listed["matters"][0]["workflow_state"]["phase"] == "review"
    assert "stored_filename" not in listed["matters"][0]
    assert detailed["matter"]["id"] == matter["id"]
    assert "stored_filename" not in detailed["matter"]


def test_board_detail_enforces_owner_scope():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo, owner_user_id="tenant-a")
    workflow = RepositoryBoardWorkflow(repo)

    with pytest.raises(RepositoryBoardWorkflowError) as exc_info:
        workflow.detail_card(matter["id"], owner_user_id="tenant-b")

    assert exc_info.value.status == 404
    assert exc_info.value.payload == {"error": "Matter not found."}


def test_board_move_card_owns_stage_validation_and_public_payload():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo)
    workflow = RepositoryBoardWorkflow(repo)

    response = workflow.move_card(matter["id"], "sent")

    assert response["matter"]["id"] == matter["id"]
    assert response["matter"]["board_column"] == "sent"
    assert response["matter"]["status"] == "active"
    assert repo.get_matter(matter["id"])["board_column"] == "sent"

    with pytest.raises(RepositoryBoardWorkflowError) as exc_info:
        workflow.move_card(matter["id"], "redline_ready")
    assert exc_info.value.status == 400
    assert exc_info.value.payload == {"error": "Unsupported matter stage."}


def test_board_reviewed_command_validates_boolean_and_updates_card():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo)
    workflow = RepositoryBoardWorkflow(repo)

    response = workflow.set_reviewed(matter["id"], True)

    assert response["matter"]["human_reviewed"] is True
    assert repo.get_matter(matter["id"])["human_reviewed"] is True

    with pytest.raises(RepositoryBoardWorkflowError) as exc_info:
        workflow.set_reviewed(matter["id"], "yes")
    assert exc_info.value.status == 400
    assert exc_info.value.payload == {"error": "reviewed must be true or false."}


def test_board_delete_removes_card_and_purges_render_state(monkeypatch):
    repo = InMemoryMatterRepository()
    matter = _create_matter(
        repo,
        document_bytes=b"rendered bytes",
        source_filename="Rendered NDA.pdf",
        owner_user_id="tenant-a",
    )
    workflow = RepositoryBoardWorkflow(repo)
    forgotten: list[str] = []
    purged: list[tuple[bytes, str, str]] = []

    class _Coordinator:
        def forget(self, matter_id: str) -> None:
            forgotten.append(matter_id)

    monkeypatch.setattr(
        repository_board_workflow.document_rendering,
        "matter_render_coordinator",
        lambda: _Coordinator(),
    )
    monkeypatch.setattr(
        repository_board_workflow.document_rendering,
        "purge_render_cache_for_source",
        lambda document_bytes, *, owner_user_id, source_filename: purged.append(
            (document_bytes, owner_user_id, source_filename)
        ),
    )

    response = workflow.delete_card(matter["id"], owner_user_id="tenant-a")

    assert response["deleted"]["id"] == matter["id"]
    assert "stored_filename" not in response["deleted"]
    assert repo.get_matter(matter["id"], owner_user_id="tenant-a") is None
    assert forgotten == [matter["id"]]
    assert purged == [(b"rendered bytes", "tenant-a", "Rendered NDA.pdf")]


def test_board_reset_returns_public_empty_board_payload():
    repo = InMemoryMatterRepository()
    _create_matter(repo, owner_user_id="tenant-a")
    _create_matter(repo, owner_user_id="tenant-a")
    _create_matter(repo, owner_user_id="tenant-b")
    workflow = RepositoryBoardWorkflow(repo)

    response = workflow.reset_board(owner_user_id="tenant-a")

    assert response == {"removed": 2, "matters": []}
    assert len(repo.list_matters(owner_user_id="tenant-a")) == 0
    assert len(repo.list_matters(owner_user_id="tenant-b")) == 1
