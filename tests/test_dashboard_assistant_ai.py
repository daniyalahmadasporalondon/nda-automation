from __future__ import annotations

import json

import pytest

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
        "explain_how_it_works",
        "explain_review_finding",
        "prepare_safe_action_request",
        "resolve_matter_search_filter",
        "search_system",
        "summarize_matter",
    } <= set(tools)
    assert {tool.domain for tool in tools.values()} >= {"repository", "playbook", "gmail", "actions", "review"}
    for tool in tools.values():
        provider_tool = tool.provider_tool()
        schema = provider_tool["function"]["parameters"]
        assert provider_tool["type"] == "function"
        assert schema["additionalProperties"] is False


def test_configured_assistant_model_rides_ai_settings_but_decouples_model(monkeypatch):
    # The assistant still inherits enable/provider/timeout/key from the shared AI
    # settings, but its MODEL is now decoupled onto its own role
    # (NDA_DASHBOARD_ASSISTANT_MODEL) which defaults to the reviewer's effective
    # model. So even when the reviewer's stored model is opus-4.8 (non-fast), the
    # assistant resolves to its own role default (opus-4.8-fast) until overridden.
    monkeypatch.setattr(
        dashboard_assistant_ai,
        "_ai_review_settings",
        lambda: {
            "enabled": True,
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "timeout_seconds": 17,
        },
    )
    monkeypatch.setattr(
        dashboard_assistant_ai,
        "_configured_api_key",
        lambda provider: "sk-or-test" if provider == "openrouter" else "",
    )

    model = dashboard_assistant_ai.configured_dashboard_assistant_model()

    assert isinstance(model, dashboard_assistant_ai.OpenRouterDashboardAssistantModel)
    assert model.settings.provider == "openrouter"
    # Decoupled: NOT the reviewer's opus-4.8, but the assistant role's own default.
    assert model.settings.model == "anthropic/claude-opus-4.8-fast"
    assert model.settings.timeout_seconds == 17
    assert model.settings.api_key == "sk-or-test"


def test_configured_assistant_model_honours_admin_role_override(monkeypatch):
    # An admin model override on the dashboard_assistant role moves the assistant's
    # model independently of the reviewer.
    from nda_automation import model_resolver

    monkeypatch.setattr(
        dashboard_assistant_ai,
        "_ai_review_settings",
        lambda: {"enabled": True, "provider": "openrouter", "model": "", "timeout_seconds": 17},
    )
    monkeypatch.setattr(
        dashboard_assistant_ai,
        "_configured_api_key",
        lambda provider: "sk-or-test" if provider == "openrouter" else "",
    )
    monkeypatch.setattr(
        model_resolver, "_persisted_model",
        lambda role: "vendor/cheap-assistant" if role == "dashboard_assistant" else "",
    )

    model = dashboard_assistant_ai.configured_dashboard_assistant_model()
    assert model.settings.model == "vendor/cheap-assistant"


def test_ai_orchestrator_uses_tool_results_to_answer_playbook_question():
    class FakeProviderModel:
        def __init__(self):
            self.requests = []

        def __call__(self, request_body):
            self.requests.append(request_body)
            if len(self.requests) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_playbook",
                                        "type": "function",
                                        "function": {
                                            "name": "get_playbook_facts",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                }
            output = json.loads(self.requests[1]["messages"][-1]["content"])
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

    model = FakeProviderModel()

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
    assert model.requests[0]["messages"][0]["role"] == "system"
    assert model.requests[0]["model"] == "anthropic/claude-opus-4.8-fast"
    assert model.requests[0]["response_format"] == {"type": "json_object"}
    assert "previous_response_id" not in model.requests[1]
    assert model.requests[1]["messages"][-1]["role"] == "tool"


def test_ai_orchestrator_can_return_email_template_answer_from_code_read_model():
    class FakeProviderModel:
        def __init__(self):
            self.requests = []

        def __call__(self, request_body):
            self.requests.append(request_body)
            if len(self.requests) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_email",
                                        "type": "function",
                                        "function": {
                                            "name": "get_outbound_email_templates",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                }
            output = json.loads(self.requests[1]["messages"][-1]["content"])
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
        ai_model=FakeProviderModel(),
    )

    assert response["intent"] == "system_question"
    assert response["domain"] == "gmail"
    assert "Please find attached the redlined version of Example NDA." in response["answer"]["text"]


def test_ai_action_response_is_confirmation_gated_when_side_effectful():
    class FakeProviderModel:
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
        ai_model=FakeProviderModel(),
    )

    assert response["intent"] == "action_request"
    assert response["requires_confirmation"] is True
    assert response["side_effects"] == ["gmail_import_or_sync"]


def test_ai_side_effectful_matter_action_is_rebuilt_from_owner_scoped_state():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(
        source_filename="Acme NDA.docx",
        document_bytes=b"PK\x03\x04 fake docx bytes",
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": [{"id": "mutuality", "decision": "pass"}]},
        triage={"triage_status": "review"},
        source_type="manual_upload",
        board_column="in_review",
        intake_metadata={
            "sender": "counterparty@example.com",
            "reply_to": "counterparty@example.com",
            "subject": "Acme NDA",
        },
        owner_user_id="tenant-a",
    )

    class FakeProviderModel:
        def __call__(self, _request_body):
            return {
                "assistant_response": {
                    "intent": "action_request",
                    "domain": "gmail",
                    "action": "send_redline",
                    "requires_confirmation": False,
                    "side_effects": ["send_redline_email"],
                    "params": {
                        "matter_id": matter["id"],
                        "recipient": "attacker@example.com",
                    },
                    "matter": {
                        "title": "Acme NDA",
                        "resolved_recipient": "attacker@example.com",
                    },
                    "message": "Send it.",
                }
            }

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "Send redline for Acme",
        repository=repo,
        owner_user_id="tenant-a",
        ai_model=FakeProviderModel(),
    )

    assert response["intent"] == "action_request"
    assert response["action"] == "send_redline"
    assert response["requires_confirmation"] is True
    assert response["params"]["matter_id"] == matter["id"]
    assert response["matter"]["resolved_recipient"] == "counterparty@example.com"
    assert "attacker@example.com" not in str(response)


def test_ai_action_tool_output_returns_directly_without_model_rewriting():
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(
        source_filename="Acme NDA.docx",
        document_bytes=b"PK\x03\x04 fake docx bytes",
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": [{"id": "mutuality", "decision": "pass"}]},
        triage={"triage_status": "review"},
        source_type="manual_upload",
        board_column="in_review",
    )

    class FakeProviderModel:
        def __init__(self):
            self.requests = []

        def __call__(self, request_body):
            self.requests.append(request_body)
            if len(self.requests) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_action",
                                        "type": "function",
                                        "function": {
                                            "name": "prepare_safe_action_request",
                                            "arguments": json.dumps(
                                                {
                                                    "action": "refresh_review",
                                                    "prompt": "Refresh Acme",
                                                    "matter_id": matter["id"],
                                                    "matter_query": "",
                                                }
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                }
            raise AssertionError("action tool outputs should not require a follow-up model call")

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "Refresh Acme",
        repository=repo,
        ai_model=FakeProviderModel(),
    )

    assert response["intent"] == "action_request"
    assert response["action"] == "refresh_review"
    assert response["requires_confirmation"] is True
    assert response["route"]["url"].endswith("/review-refresh")


def test_ai_clarification_response_is_preserved_for_ambiguous_system_request():
    class FakeProviderModel:
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
        ai_model=FakeProviderModel(),
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


def test_tool_handler_generic_exception_degrades_gracefully_not_500(monkeypatch):
    # A tool handler that raises a generic (non-MatterRepositoryError,
    # non-DashboardAssistantAIUnavailableError) exception must NOT escape the
    # orchestrator and route as a 500. The single failing tool is neutralized
    # into a benign tool output and the assistant degrades to the deterministic
    # catalog instead of crashing.
    def boom(_args):
        raise RuntimeError("handler exploded")

    def exploding_registry(context):
        return {
            "get_repository_facts": dashboard_assistant_ai.DashboardAssistantTool(
                name="get_repository_facts",
                domain="repository",
                description="boom",
                parameters=dashboard_assistant_ai._strict_schema({}),
                handler=boom,
            ),
        }

    monkeypatch.setattr(
        dashboard_assistant_ai,
        "dashboard_assistant_tool_registry",
        exploding_registry,
    )

    class ToolCallingModel:
        def __init__(self):
            self.requests = []

        def __call__(self, request_body):
            self.requests.append(request_body)
            if len(self.requests) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_repo",
                                        "type": "function",
                                        "function": {
                                            "name": "get_repository_facts",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            # The orchestrator survived the failing handler and made the
            # follow-up call; the failing tool surfaced only as a safe error.
            output = json.loads(self.requests[1]["messages"][-1]["content"])
            assert output["error"] == "tool_failed"
            return {
                "assistant_response": {
                    "intent": "system_question",
                    "domain": "repository",
                    "answer": {"text": "I could not read that just now."},
                }
            }

    model = ToolCallingModel()

    # End-to-end through the command handler (the route's call site): a graceful
    # typed response, never a raised exception that would become a 500.
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "How many matters are in review?",
        repository=InMemoryMatterRepository(),
        ai_model=model,
    )

    assert isinstance(response, dict)
    assert response["intent"] in dashboard_assistant_ai.SUPPORTED_INTENTS


def test_tool_handler_generic_exception_with_no_followup_falls_back_to_catalog(monkeypatch):
    # If the model only emits the failing tool call and then goes quiet (no usable
    # final response), the orchestrator returns None and the deterministic catalog
    # answers instead of bubbling the exception out as a 500.
    def boom(_args):
        raise ValueError("kaboom")

    def exploding_registry(context):
        return {
            "get_repository_facts": dashboard_assistant_ai.DashboardAssistantTool(
                name="get_repository_facts",
                domain="repository",
                description="boom",
                parameters=dashboard_assistant_ai._strict_schema({}),
                handler=boom,
            ),
        }

    monkeypatch.setattr(
        dashboard_assistant_ai,
        "dashboard_assistant_tool_registry",
        exploding_registry,
    )

    class ToolThenSilentModel:
        def __init__(self):
            self.calls = 0

        def __call__(self, _request_body):
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_repo",
                                        "type": "function",
                                        "function": {
                                            "name": "get_repository_facts",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": ""}}]}

    # This query bypasses the deterministic guards and reaches the AI; with the
    # failing tool the orchestrator yields no AI response and the command handler
    # falls through to the deterministic capability catalog -> a typed response,
    # never a raised exception that would surface as a 500.
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "How many matters are in review?",
        repository=InMemoryMatterRepository(),
        ai_model=ToolThenSilentModel(),
    )

    assert isinstance(response, dict)
    assert response["intent"] in dashboard_assistant_ai.SUPPORTED_INTENTS


def _action_model(action, *, requires_confirmation=False, side_effects=None):
    plan = {
        "intent": "action_request",
        "domain": "gmail",
        "action": action,
        "requires_confirmation": requires_confirmation,
        "message": "Done.",
    }
    if side_effects is not None:
        plan["side_effects"] = side_effects

    class FakeProviderModel:
        def __call__(self, _request_body):
            return {"assistant_response": plan}

    return FakeProviderModel()


@pytest.mark.parametrize(
    "action",
    [
        # The headline Gmail import/sync bypass plus every other state-mutating
        # or external-operation action the assistant can emit. None of these may
        # skip confirmation even when the model omits side_effects entirely.
        "gmail_import",
        "sync_gmail",
        "open_gmail_sync",
        "refresh_review",
        "run_review",
        "open_review",
        "send_redline",
        "approve_matter",
        "open_drive_export",
        "open_generator",
    ],
)
def test_ai_side_effect_action_is_force_confirmed_even_with_empty_side_effects(action):
    # A prompt-injected model returns a side-effectful action with
    # requires_confirmation=false AND an empty side_effects list — the exact
    # path that previously bypassed the gate. The server must override it.
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "sync my gmail now",
        repository=InMemoryMatterRepository(),
        ai_model=_action_model(action, requires_confirmation=False, side_effects=[]),
    )

    # The model's original side-effect action may either be force-confirmed in
    # place, or (for owner-scoped matter actions with no matters) safely
    # degraded to a clarification or a navigation-only response. The invariant:
    # the requested side-effect action must NEVER reach the user as an
    # unconfirmed action_request.
    resolved_action = response.get("action")
    if resolved_action == action and response["intent"] in {
        "action_request",
        "draft_action_request",
    }:
        assert response["requires_confirmation"] is True, response
    else:
        assert response["intent"] in {"clarification", "unsupported", "action_request"}
        if resolved_action not in dashboard_assistant_ai.SAFE_NO_CONFIRMATION_ACTIONS:
            assert response.get("requires_confirmation") is not False, response


def test_ai_gmail_import_with_omitted_confirmation_flag_is_force_confirmed():
    # Even with no requires_confirmation key and no side_effects key at all,
    # the Gmail import action must be confirmation-gated.
    class FakeProviderModel:
        def __call__(self, _request_body):
            return {
                "assistant_response": {
                    "intent": "action_request",
                    "domain": "gmail",
                    "action": "gmail_import",
                    "message": "Imported your Gmail.",
                }
            }

    response = dashboard_assistant.handle_dashboard_assistant_command(
        "import my gmail",
        repository=InMemoryMatterRepository(),
        ai_model=FakeProviderModel(),
    )

    assert response["intent"] == "action_request"
    assert response["requires_confirmation"] is True


def test_ai_unknown_action_fails_safe_to_required_confirmation():
    # An unrecognized action a (possibly injected) model invents must be treated
    # as side-effectful and force-confirmed rather than allowed through.
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "do the thing",
        repository=InMemoryMatterRepository(),
        ai_model=_action_model(
            "wipe_all_matters", requires_confirmation=False, side_effects=[]
        ),
    )

    assert response["intent"] == "action_request"
    assert response["action"] == "wipe_all_matters"
    assert response["requires_confirmation"] is True


def test_ai_safe_navigation_action_declaring_side_effects_is_force_confirmed():
    # A safe navigation action cannot be smuggled past the gate while carrying a
    # declared side effect: the declared side effect forces confirmation.
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "open the repository",
        repository=InMemoryMatterRepository(),
        ai_model=_action_model(
            "open_repository",
            requires_confirmation=False,
            side_effects=["delete_everything"],
        ),
    )

    assert response["intent"] == "action_request"
    assert response["requires_confirmation"] is True


@pytest.mark.parametrize("action", ["open_repository", "open_playbook", "open_admin"])
def test_ai_safe_navigation_actions_still_skip_confirmation(action):
    # Pure navigation actions in the allow-list must keep skipping confirmation.
    response = dashboard_assistant.handle_dashboard_assistant_command(
        "open it",
        repository=InMemoryMatterRepository(),
        ai_model=_action_model(action, requires_confirmation=False, side_effects=[]),
    )

    assert response["intent"] == "action_request"
    assert response["action"] == action
    assert response["requires_confirmation"] is False


@pytest.mark.parametrize(
    "intent",
    [
        "system_question",
        "repository_question",
        "review_finding_explanation",
        "matter_summary",
        "system_search",
        "how_it_works",
    ],
)
def test_ai_read_intents_are_not_given_a_forced_confirmation_flag(intent):
    # Pure read/informational intents keep their existing behavior and never
    # acquire a requires_confirmation key from the gate.
    payload = {"intent": intent, "answer": {"text": "hello"}}

    response = dashboard_assistant_ai.validate_dashboard_assistant_response(
        payload, query="q"
    )

    assert response is not None
    assert "requires_confirmation" not in response
