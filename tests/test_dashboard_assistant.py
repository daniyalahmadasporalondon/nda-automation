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


def test_capability_catalog_exposes_major_command_center_domains():
    names = {capability.name for capability in dashboard_assistant.ASSISTANT_CAPABILITIES}
    domains = {capability.domain for capability in dashboard_assistant.ASSISTANT_CAPABILITIES}

    assert {
        "matter_search_filter",
        "count_in_review",
        "last_sent",
        "playbook_clause_count",
        "outbound_email_templates",
        "generate_nda",
        "gmail_sync",
        "drive_export",
        "open_admin",
        "review_workflow",
    } <= names
    assert {"repository", "playbook", "generation", "review", "gmail", "drive", "admin"} <= domains


def test_playbook_clause_count_answers_from_active_playbook_provider():
    playbook = {
        "name": "Test NDA Playbook",
        "version": "2026.1",
        "clauses": [{"id": "mutuality"}, {"id": "governing_law"}],
    }
    phrasings = [
        "How many clauses do we have?",
        "How many playbook clauses do we have?",
        "How many clauses are in the playbook?",
        "count playbook clauses",
    ]

    for phrasing in phrasings:
        response = dashboard_assistant.handle_dashboard_assistant_command(
            phrasing,
            repository=InMemoryMatterRepository(),
            playbook_provider=lambda: playbook,
        )

        assert response["intent"] == "system_question"
        assert response["domain"] == "playbook"
        assert response["question"] == "playbook_clause_count"
        assert response["answer"]["count"] == 2
        assert response["answer"]["playbook_name"] == "Test NDA Playbook"
        assert "2 clauses" in response["answer"]["text"]


def test_document_specific_clause_count_remains_unsupported():
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "How many clauses are in this NDA?",
        repository=InMemoryMatterRepository(),
        search_resolver=lambda _query: {"filters": None, "fallback": True},
        playbook_provider=lambda: {"clauses": [{"id": "mutuality"}]},
    )

    assert response["intent"] == "unsupported"


def test_playbook_governing_law_question_reads_approved_options():
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "What approved governing laws are in the playbook?",
        repository=InMemoryMatterRepository(),
        playbook_provider=lambda: {
            "clauses": [
                {
                    "id": "governing_law",
                    "rules": {
                        "approved_options": [
                            {"id": "england_and_wales", "label": "England and Wales"},
                            {"id": "delaware", "label": "Delaware"},
                        ],
                    },
                }
            ],
        },
    )

    assert response["intent"] == "system_question"
    assert response["domain"] == "playbook"
    assert response["question"] == "approved_governing_laws"
    assert response["answer"]["options"] == ["England and Wales", "Delaware"]


def test_email_template_question_answers_from_outbound_contract():
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "What is the message template that we have for emails that we send?",
        repository=InMemoryMatterRepository(),
    )

    assert response["intent"] == "system_question"
    assert response["domain"] == "gmail"
    assert response["question"] == "outbound_email_templates"
    assert response["answer"]["templates"][0]["context"] == "redline_send"
    assert "Please find attached the redlined version of Example NDA." in response["answer"]["templates"][0]["body"]
    assert "send_document" in {template["context"] for template in response["answer"]["templates"]}


def test_workflow_requests_return_typed_action_requests_without_side_effects():
    repo = InMemoryMatterRepository()

    playbook = dashboard_assistant.handle_dashboard_assistant_command("Open the Playbook", repository=repo)
    gmail = dashboard_assistant.handle_dashboard_assistant_command("Sync Gmail inbox", repository=repo)
    drive = dashboard_assistant.handle_dashboard_assistant_command("Save this to Drive", repository=repo)

    assert playbook["intent"] == "action_request"
    assert playbook["action"] == "open_playbook"
    assert playbook["requires_confirmation"] is False
    assert playbook["side_effects"] == []
    assert gmail["intent"] == "action_request"
    assert gmail["requires_confirmation"] is True
    assert gmail["side_effects"] == ["gmail_import_or_sync"]
    assert drive["intent"] == "action_request"
    assert drive["requires_confirmation"] is True
    assert drive["side_effects"] == ["drive_upload_or_export"]


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
    assert "search matters" in response["message"].lower()


def test_unknown_system_question_does_not_become_document_search_empty_state():
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "How many company holidays do we have?",
        repository=InMemoryMatterRepository(),
        search_resolver=lambda _query: {"filters": {"text": "company holidays"}, "fallback": False},
    )

    assert response["intent"] == "unsupported"
    assert "search" in response["message"].lower()
