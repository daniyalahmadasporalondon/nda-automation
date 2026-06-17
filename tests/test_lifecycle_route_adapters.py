"""Focused tests for HTTP route adapters backed by RepositoryMatterLifecycle."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from nda_automation import matter_store
from nda_automation.routes import matters as matter_routes


class _FakeHandler:
    current_user_id = ""
    current_user = None

    def __init__(self, payload: dict | None = None):
        self._payload = payload
        self.status = 200
        self.response = None

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, *, status=200, send_body=True):
        self.status = status
        self.response = payload


@pytest.fixture
def isolated_matter_store(monkeypatch):
    with tempfile.TemporaryDirectory() as data_dir:
        data_path = Path(data_dir)
        monkeypatch.setattr(matter_store, "DATA_DIR", data_path)
        monkeypatch.setattr(matter_store, "MATTERS_PATH", data_path / "matters.json")
        monkeypatch.setattr(matter_store, "UPLOADS_DIR", data_path / "uploads")
        yield


def _create_matter(**overrides):
    kwargs = {
        "source_filename": "Mutual NDA.docx",
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
    return matter_store.create_matter(**kwargs)


def test_redline_draft_route_rejects_non_object_draft(isolated_matter_store):
    matter = _create_matter()
    handler = _FakeHandler({"redline_draft": "not a draft"})

    matter_routes.handle_matter_redline_draft_update(
        handler,
        f"/api/matters/{matter['id']}/redline-draft",
    )

    assert handler.status == 400
    assert "object or null" in handler.response["error"]
    assert "redline_draft" not in matter_store.get_matter(matter["id"])


def test_redline_draft_route_persists_cleaned_draft_and_accepts_null(isolated_matter_store):
    matter = _create_matter()
    save_handler = _FakeHandler({
        "redline_draft": {
            "clause_decisions": {" mutuality ": True},
            "export_redline_edits": [
                {"paragraph_id": "p1", "replacement_text": "Mutual language."},
                "ignored",
            ],
            "manual_redline_edits": [
                {
                    "action": "replace_paragraph",
                    "paragraph_id": "p1",
                    "original_text": "Old language.",
                    "replacement_text": "New language.",
                },
            ],
            "review_comments": [{"paragraph_id": "p1", "text": "  Confirm.  "}],
        },
    })

    matter_routes.handle_matter_redline_draft_update(
        save_handler,
        f"/api/matters/{matter['id']}/redline-draft",
    )

    assert save_handler.status == 200
    assert save_handler.response["matter"]["has_redline_draft"] is True
    assert "redline_draft" not in save_handler.response["matter"]
    stored_draft = matter_store.get_matter(matter["id"])["redline_draft"]
    assert stored_draft["clause_decisions"] == {"mutuality": True}
    assert stored_draft["summary"] == {
        "included_redline_count": 1,
        "manual_redline_count": 1,
        "review_comment_count": 1,
    }

    reset_handler = _FakeHandler({"redline_draft": None})
    matter_routes.handle_matter_redline_draft_update(
        reset_handler,
        f"/api/matters/{matter['id']}/redline-draft",
    )

    assert reset_handler.status == 200
    assert reset_handler.response["matter"]["has_redline_draft"] is False
    assert "redline_draft" not in matter_store.get_matter(matter["id"])
