"""Tests for the deeper Repository matter lifecycle module."""
from __future__ import annotations

from unittest.mock import patch

from nda_automation import (
    app_settings,
    artifact_registry,
    drive_integration,
    gmail_integration,
    redline_export_service,
    telemetry,
)
from nda_automation.matter_lifecycle import MatterSendBlockedError, RedlineDraftError, RepositoryMatterLifecycle
from nda_automation.matter_repository import InMemoryMatterRepository


def _create_kwargs(**overrides):
    kwargs = {
        "source_filename": "Mutual NDA.docx",
        "document_bytes": b"PK\x03\x04 fake docx bytes",
        "extracted_text": "This Agreement is mutual.",
        "review_result": {"clauses": [{"id": "mutuality", "decision": "pass"}]},
        "triage": {"triage_status": "ready_to_sign"},
        "source_type": "manual_upload",
        "board_column": "in_review",
    }
    kwargs.update(overrides)
    return kwargs


def _sync_runner(work):
    work()


def test_complete_intake_registers_original_artifact_and_timeline():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())

    with patch.object(drive_integration, "drive_connected", return_value=False):
        RepositoryMatterLifecycle(repo).complete_intake(
            matter,
            drive_sync_runner=_sync_runner,
        )

    stored = repo.get_matter(matter["id"])
    artifacts = artifact_registry.matter_artifacts(stored)
    assert [artifact.role for artifact in artifacts] == [artifact_registry.ROLE_ORIGINAL]
    assert stored["current_artifact_id"] == artifacts[0].id
    assert [event["type"] for event in stored["matter_timeline"]] == [
        "created",
        "review_completed",
    ]


def test_complete_intake_skips_duplicate_gmail_matter():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())
    duplicate = {**matter, "_existing_gmail_duplicate": True}

    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(app_settings, "drive_auto_intake_enabled", return_value=True):
            RepositoryMatterLifecycle(repo).complete_intake(
                duplicate,
                drive_sync_runner=_sync_runner,
            )

    stored = repo.get_matter(matter["id"])
    assert artifact_registry.matter_artifacts(stored) == []
    assert "matter_timeline" not in stored


def test_complete_intake_keeps_timeline_when_artifact_backfill_fails():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())

    with patch("nda_automation.matter_lifecycle.artifact_service.backfill_matter", side_effect=ValueError("bad artifact")):
        with patch.object(drive_integration, "drive_connected", return_value=False):
            RepositoryMatterLifecycle(repo).complete_intake(
                matter,
                drive_sync_runner=_sync_runner,
            )

    stored = repo.get_matter(matter["id"])
    assert "artifacts" not in stored
    assert [event["type"] for event in stored["matter_timeline"]] == [
        "created",
        "review_completed",
    ]


def test_complete_intake_drive_failure_is_fail_soft():
    telemetry.reset()
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())

    with patch.object(drive_integration, "drive_connected", return_value=True):
        with patch.object(app_settings, "drive_auto_intake_enabled", return_value=True):
            with patch.object(app_settings, "drive_settings", return_value={"folder_id": ""}):
                with patch.object(drive_integration, "sync_matter_folder", side_effect=RuntimeError("drive down")):
                    RepositoryMatterLifecycle(repo).complete_intake(
                        matter,
                        drive_sync_runner=_sync_runner,
                    )

    stored = repo.get_matter(matter["id"])
    assert stored["id"] == matter["id"]
    assert stored.get("drive") is None
    assert telemetry.snapshot()["counters"].get("drive_auto_intake_failed") == 1
    telemetry.reset()


def test_save_redline_draft_rejects_non_object_value():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())

    try:
        RepositoryMatterLifecycle(repo).save_redline_draft(matter["id"], "not a draft")
    except RedlineDraftError as error:
        assert "object or null" in str(error)
    else:
        raise AssertionError("Expected RedlineDraftError")

    assert "redline_draft" not in repo.get_matter(matter["id"])


def test_save_redline_draft_persists_cleaned_draft():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())
    raw_draft = {
        "clause_decisions": {" governing_law ": 1, "": True},
        "redline_decisions": {"redline-1": 0},
        "template_selections": {" law ": " California ", "blank": "   "},
        "reviewed_clause_ids": {"mutuality": "yes"},
        "export_redline_edits": [
            {"paragraph_id": "p1", "replacement_text": "California law applies."},
            "not a redline",
        ],
        "manual_redline_edits": [
            {
                "id": "manual-1",
                "action": "replace_paragraph",
                "paragraph_id": "p1",
                "original_text": "Old sentence.",
                "replacement_text": "New sentence.",
                "whole_paragraph": False,
                "unexpected": "dropped",
            },
            {"action": "replace_paragraph", "paragraph_id": "", "original_text": "Missing id"},
            "not a manual redline",
        ],
        "review_comments": [
            {
                "id": "comment-1",
                "paragraph_id": "p1",
                "text": "  Confirm fallback position.  ",
                "selected_text": "Old sentence",
            },
            {"text": "missing scope"},
        ],
    }

    updated = RepositoryMatterLifecycle(repo).save_redline_draft(matter["id"], raw_draft)

    draft = updated["redline_draft"]
    assert draft["clause_decisions"] == {"governing_law": True}
    assert draft["redline_decisions"] == {"redline-1": False}
    assert draft["template_selections"] == {"law": "California"}
    assert draft["reviewed_clause_ids"] == {"mutuality": True}
    assert draft["export_redline_edits"] == [
        {"paragraph_id": "p1", "replacement_text": "California law applies."}
    ]
    assert len(draft["manual_redline_edits"]) == 1
    manual_redline = draft["manual_redline_edits"][0]
    assert manual_redline["paragraph_id"] == "p1"
    assert manual_redline["whole_paragraph"] is False
    assert "unexpected" not in manual_redline
    assert draft["review_comments"] == [
        {
            "id": "comment-1",
            "text": "Confirm fallback position.",
            "paragraph_id": "p1",
            "selected_text": "Old sentence",
        }
    ]
    assert draft["summary"] == {
        "included_redline_count": 1,
        "manual_redline_count": 1,
        "review_comment_count": 1,
    }
    assert repo.get_matter(matter["id"])["redline_draft"] == draft


def test_save_redline_draft_accepts_null_to_clear_existing_draft():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(**_create_kwargs())
    lifecycle = RepositoryMatterLifecycle(repo)
    lifecycle.save_redline_draft(matter["id"], {"manual_redline_edits": []})

    updated = lifecycle.save_redline_draft(matter["id"], None)

    assert "redline_draft" not in updated
    assert "redline_draft" not in repo.get_matter(matter["id"])


def test_send_redline_preflights_before_export_or_email():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(
        **_create_kwargs(
            intake_metadata={"reply_to": "counterparty@example.com"},
            review_result={"clauses": [{"id": "mutuality", "decision": "pass"}]},
        )
    )

    with patch.object(app_settings, "gmail_role_enabled", return_value=True):
        with patch.object(
            gmail_integration,
            "validate_outbound_send_ready",
            side_effect=gmail_integration.GmailIntegrationError("preflight failed"),
        ) as validate_send:
            with patch.object(redline_export_service, "build_matter_redline") as build_redline:
                with patch.object(gmail_integration, "send_redline_email") as send_email:
                    try:
                        RepositoryMatterLifecycle(repo).send_redline(
                            matter["id"],
                            {"matter_id": matter["id"], "confirm_send": True},
                            to="counterparty@example.com",
                            confirmed_recipient="counterparty@example.com",
                        )
                    except gmail_integration.GmailIntegrationError as error:
                        assert "preflight failed" in str(error)
                    else:
                        raise AssertionError("Expected GmailIntegrationError")

    validate_send.assert_called_once()
    build_redline.assert_not_called()
    send_email.assert_not_called()


def test_send_redline_rechecks_human_review_gate_after_export():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(
        **_create_kwargs(
            intake_metadata={"reply_to": "counterparty@example.com"},
            review_result={"clauses": [{"id": "mutuality", "decision": "review"}]},
        )
    )
    repo.update_matter_fields(matter["id"], {"human_reviewed": True})
    redline_export = redline_export_service.RedlineExport(
        data=b"redline docx",
        filename="Mutual-NDA-redlined.docx",
    )
    events: list[str] = []

    def export_and_reopen_review(*_args, **_kwargs):
        events.append("export")
        repo.update_matter_fields(matter["id"], {"human_reviewed": False})
        return redline_export

    with patch.object(app_settings, "gmail_role_enabled", return_value=True):
        with patch.object(gmail_integration, "validate_outbound_send_ready", side_effect=lambda *_args, **_kwargs: events.append("preflight")):
            with patch.object(redline_export_service, "build_matter_redline", side_effect=export_and_reopen_review):
                with patch.object(gmail_integration, "send_redline_email") as send_email:
                    try:
                        RepositoryMatterLifecycle(repo).send_redline(
                            matter["id"],
                            {"matter_id": matter["id"], "confirm_send": True},
                            to="counterparty@example.com",
                            confirmed_recipient="counterparty@example.com",
                        )
                    except MatterSendBlockedError as error:
                        assert "human review" in str(error)
                    else:
                        raise AssertionError("Expected MatterSendBlockedError")

    assert events == ["preflight", "export"]
    send_email.assert_not_called()


_SENT_STUB = {
    "message_id": "msg_123",
    "outbound_account": "legal@aspora.com",
    "sent_at": "2026-06-05T12:00:00+00:00",
    "subject": "NDA",
    "thread_id": "thread_123",
    "to": "counterparty@example.com",
}


def _attempt_send(repo, matter_id):
    """Drive send_redline through export+email mocks; return whether it sent.

    Mirrors the real call path (preflight -> export -> send) with everything
    downstream of the gate stubbed, so the result reflects ONLY the send gate.
    Returns True if send_redline_email was invoked, False if the gate blocked.
    """
    export = redline_export_service.RedlineExport(data=b"docx", filename="NDA-redlined.docx")
    with patch.object(app_settings, "gmail_role_enabled", return_value=True):
        with patch.object(gmail_integration, "validate_outbound_send_ready"):
            with patch.object(redline_export_service, "build_matter_redline", return_value=export):
                with patch.object(gmail_integration, "send_redline_email", return_value=dict(_SENT_STUB)) as send_email:
                    try:
                        RepositoryMatterLifecycle(repo).send_redline(
                            matter_id,
                            {"matter_id": matter_id, "confirm_send": True},
                            to="counterparty@example.com",
                            confirmed_recipient="counterparty@example.com",
                        )
                    except MatterSendBlockedError:
                        return False
    return send_email.called


def _matter_with_review(repo, review_result):
    return repo.create_matter(
        **_create_kwargs(
            intake_metadata={"reply_to": "counterparty@example.com"},
            review_result=review_result,
        )
    )


def test_send_gate_pass_state_is_freely_sendable():
    # PRESERVED behavior: an all-pass review has no send block.
    repo = InMemoryMatterRepository()
    matter = _matter_with_review(repo, {"clauses": [{"id": "mutuality", "decision": "pass"}]})

    assert _attempt_send(repo, matter["id"]) is True


def test_send_gate_needs_review_blocked_until_reviewed():
    # PRESERVED behavior: needs-review is blocked until a human marks it reviewed.
    repo = InMemoryMatterRepository()
    matter = _matter_with_review(repo, {"clauses": [{"id": "mutuality", "decision": "review"}]})

    assert _attempt_send(repo, matter["id"]) is False

    repo.update_matter_fields(matter["id"], {"human_reviewed": True})
    assert _attempt_send(repo, matter["id"]) is True


def test_send_gate_fail_state_blocked_until_resolved():
    # THE BLOCKER FIX: a failed (check) review -- the AI rejected a required
    # clause (e.g. an unapproved governing law) -- must NOT be sendable until a
    # human resolves it. counts.review == 0, so this used to false-clear the gate
    # and the failed NDA could be emailed.
    repo = InMemoryMatterRepository()
    matter = _matter_with_review(repo, {"clauses": [{"id": "governing_law", "decision": "fail"}]})

    assert _attempt_send(repo, matter["id"]) is False

    # Cleared the same way needs-review is: a human engages the matter.
    repo.update_matter_fields(matter["id"], {"human_reviewed": True})
    assert _attempt_send(repo, matter["id"]) is True


def test_send_gate_fail_state_cleared_by_recorded_approval():
    # The fail block is not permanently wedged: a recorded approval is a stronger
    # human sign-off and also clears the send gate, even without the
    # human_reviewed toggle.
    repo = InMemoryMatterRepository()
    matter = _matter_with_review(repo, {"clauses": [{"id": "governing_law", "decision": "fail"}]})

    assert _attempt_send(repo, matter["id"]) is False

    repo.update_matter_fields(
        matter["id"], {"status": "approved", "approved_at": "2026-06-05T00:00:00+00:00"}
    )
    assert _attempt_send(repo, matter["id"]) is True
