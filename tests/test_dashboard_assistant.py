from __future__ import annotations

from nda_automation import dashboard_assistant, dashboard_search_intent, workflow
from nda_automation.matter_repository import InMemoryMatterRepository


def _create_matter(repo: InMemoryMatterRepository, **overrides):
    kwargs = {
        "source_filename": "Acme NDA.docx",
        "document_bytes": b"PK\x03\x04 fake docx bytes",
        "extracted_text": "This Agreement is mutual.",
        "review_result": {"clauses": [{"id": "mutuality", "decision": "pass"}]},
        "triage": {"triage_status": "review"},
        "source_type": "manual_upload",
        "board_column": "in_review",
    }
    kwargs.update(overrides)
    return repo.create_matter(**kwargs)


def test_count_in_review_answers_from_owner_scoped_public_matter_facts():
    repo = InMemoryMatterRepository()
    _create_matter(repo, owner_user_id="tenant-a", source_filename="Acme NDA.docx")
    _create_matter(repo, owner_user_id="tenant-a", source_filename="Globex NDA.docx")
    _create_matter(repo, owner_user_id="tenant-b", source_filename="Other NDA.docx")

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "How many are in review?",
        repository=repo,
        owner_user_id="tenant-a",
    )

    assert response["intent"] == "repository_question"
    assert response["question"] == "count_in_review"
    assert response["answer"] == {
        "text": "2 documents are in review.",
        "count": 2,
        "phase": workflow.PHASE_REVIEW,
    }
    assert [citation["title"] for citation in response["citations"]] == [
        "Globex NDA",
        "Acme NDA",
    ]


def test_last_sent_answers_from_real_send_stamps_only():
    repo = InMemoryMatterRepository()
    older = _create_matter(
        repo,
        source_filename="Older NDA.docx",
        board_column="sent",
        owner_user_id="tenant-a",
    )
    newer = _create_matter(
        repo,
        source_filename="Newer NDA.docx",
        board_column="sent",
        owner_user_id="tenant-a",
    )
    repo.update_matter_fields(
        older["id"],
        {
            "last_outbound_at": "2026-01-02T10:00:00+00:00",
            "last_outbound_to": "old@example.com",
        },
        owner_user_id="tenant-a",
    )
    repo.update_matter_fields(
        newer["id"],
        {
            "last_outbound_at": "2026-02-03T12:30:00+00:00",
            "last_outbound_to": "new@example.com",
        },
        owner_user_id="tenant-a",
    )

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "When was the last NDA sent?",
        repository=repo,
        owner_user_id="tenant-a",
    )

    assert response["intent"] == "repository_question"
    assert response["question"] == "last_sent"
    assert response["answer"]["sent_at"] == "2026-02-03T12:30:00+00:00"
    assert response["answer"]["recipient"] == "new@example.com"
    assert response["answer"]["matter_id"] == newer["id"]
    assert response["citations"] == [
        {
            "matter_id": newer["id"],
            "title": "Newer NDA",
            "workflow_phase": workflow.PHASE_SENT,
            "last_outbound_at": "2026-02-03T12:30:00+00:00",
        }
    ]


def test_generate_nda_returns_confirmation_required_action_without_side_effects():
    repo = InMemoryMatterRepository()

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "Generate an NDA for Acme",
        repository=repo,
    )

    assert response["intent"] == "draft_action_request"
    assert response["action"] == "open_generator"
    assert response["requires_confirmation"] is True
    assert response["side_effects"] == []
    assert response["generator"]["prefill"]["source"] == "dashboard_assistant"
    assert response["generator"]["prefill"]["prompt"] == "Generate an NDA for Acme"
    assert repo.list_matters() == []


def test_search_filter_delegates_to_search_resolver():
    repo = InMemoryMatterRepository()

    def search_resolver(query: str):
        assert query == "Show Acme pending approval"
        return dashboard_search_intent.deterministic_search_intent(query)

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "Show Acme pending approval",
        repository=repo,
        search_resolver=search_resolver,
    )

    assert response["intent"] == "search_filter"
    assert response["search"]["filters"]["status"] == workflow.STATUS_AWAITING_APPROVAL
    assert response["search"]["filters"]["text"] == "Acme"


def test_unsupported_intent_returns_clear_message():
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "Tell me a joke",
        repository=InMemoryMatterRepository(),
        search_resolver=lambda _query: {"filters": None, "fallback": True},
    )

    assert response["intent"] == "unsupported"
    assert "cannot do that request yet" in response["message"].lower()
