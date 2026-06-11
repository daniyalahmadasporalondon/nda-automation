from __future__ import annotations

import json

from nda_automation import dashboard_assistant, dashboard_assistant_ai
from nda_automation.matter_repository import InMemoryMatterRepository


def test_ai_tool_registry_exposes_strict_major_app_capability_tools():
    context = dashboard_assistant.AssistantContext(
        "What can you do?",
        repository=InMemoryMatterRepository(),
    )

    tools = dashboard_assistant_ai.dashboard_assistant_tool_registry(context)

    assert {
        "get_repository_facts",
        "get_playbook_facts",
        "get_outbound_email_templates",
        "prepare_safe_action_request",
        "resolve_matter_search_filter",
    } <= set(tools)
    assert {tool.domain for tool in tools.values()} >= {"repository", "playbook", "gmail", "actions"}
    for tool in tools.values():
        schema = tool.responses_tool()["parameters"]
        assert tool.responses_tool()["strict"] is True
        assert schema["additionalProperties"] is False


def test_ai_orchestrator_uses_tool_results_to_answer_playbook_question():
    class FakeResponsesModel:
        def __init__(self):
            self.requests = []

        def __call__(self, request_body):
            self.requests.append(request_body)
            if len(self.requests) == 1:
                return {
                    "id": "resp_1",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "get_playbook_facts",
                            "arguments": "{}",
                            "call_id": "call_playbook",
                        }
                    ],
                }
            output = json.loads(self.requests[1]["input"][-1]["output"])
            return {
                "assistant_response": {
                    "intent": "system_question",
                    "domain": "playbook",
                    "question": "playbook_clause_count",
                    "answer": {
                        "text": f"{output['name']} has {output['clause_count']} clauses.",
                        "count": output["clause_count"],
                    },
                    "citations": [{"source": "playbook", "title": output["name"]}],
                }
            }

    model = FakeResponsesModel()

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "How many clauses do we have?",
        repository=InMemoryMatterRepository(),
        playbook_provider=lambda: {
            "name": "Test Playbook",
            "version": "2026.1",
            "clauses": [{"id": "mutuality"}, {"id": "governing_law"}, {"id": "term"}],
        },
        ai_model=model,
    )

    assert response["intent"] == "system_question"
    assert response["domain"] == "playbook"
    assert response["answer"]["count"] == 3
    assert "Test Playbook has 3 clauses" in response["answer"]["text"]
    assert model.requests[0]["reasoning"]["effort"] == "low"
    assert "previous_response_id" in model.requests[1]


def test_ai_orchestrator_can_return_email_template_answer_from_code_read_model():
    class FakeResponsesModel:
        def __init__(self):
            self.requests = []

        def __call__(self, request_body):
            self.requests.append(request_body)
            if len(self.requests) == 1:
                return {
                    "id": "resp_1",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "get_outbound_email_templates",
                            "arguments": "{}",
                            "call_id": "call_email",
                        }
                    ],
                }
            output = json.loads(self.requests[1]["input"][-1]["output"])
            return {
                "assistant_response": {
                    "intent": "system_question",
                    "domain": "gmail",
                    "question": "outbound_email_templates",
                    "answer": {
                        "text": output["templates"][0]["body"],
                        "templates": output["templates"],
                    },
                    "citations": [{"source": "code", "title": output["templates"][0]["source"]}],
                }
            }

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "What is the message template for emails we send?",
        repository=InMemoryMatterRepository(),
        ai_model=FakeResponsesModel(),
    )

    assert response["intent"] == "system_question"
    assert response["domain"] == "gmail"
    assert "Please find attached the redlined version of Example NDA." in response["answer"]["text"]


def test_ai_action_response_is_confirmation_gated_when_side_effectful():
    class FakeResponsesModel:
        def __call__(self, _request_body):
            return {
                "assistant_response": {
                    "intent": "action_request",
                    "domain": "gmail",
                    "action": "open_gmail_sync",
                    "requires_confirmation": False,
                    "side_effects": ["gmail_import_or_sync"],
                    "message": "Review Gmail sync first.",
                }
            }

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "Sync Gmail",
        repository=InMemoryMatterRepository(),
        ai_model=FakeResponsesModel(),
    )

    assert response["intent"] == "action_request"
    assert response["requires_confirmation"] is True
    assert response["side_effects"] == ["gmail_import_or_sync"]


def test_ai_clarification_response_is_preserved_for_ambiguous_system_request():
    class FakeResponsesModel:
        def __call__(self, _request_body):
            return {
                "assistant_response": {
                    "intent": "clarification",
                    "domain": "assistant",
                    "message": "Which queue or workflow should I inspect?",
                    "questions": ["Repository", "Gmail inbox", "Review queue"],
                }
            }

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "What is happening?",
        repository=InMemoryMatterRepository(),
        ai_model=FakeResponsesModel(),
    )

    assert response["intent"] == "clarification"
    assert response["message"] == "Which queue or workflow should I inspect?"
    assert response["questions"] == ["Repository", "Gmail inbox", "Review queue"]


def test_ai_unavailable_falls_back_to_deterministic_catalog():
    class UnavailableModel:
        def __call__(self, _request_body):
            raise dashboard_assistant_ai.DashboardAssistantAIUnavailableError("offline")

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "Generate an NDA for Acme",
        repository=InMemoryMatterRepository(),
        ai_model=UnavailableModel(),
    )

    assert response["intent"] == "draft_action_request"
    assert response["action"] == "open_generator"
    assert response["requires_confirmation"] is True
