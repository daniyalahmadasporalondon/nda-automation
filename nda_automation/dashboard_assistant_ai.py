"""Provider-backed Dashboard assistant orchestration.

The deterministic command catalog in ``dashboard_assistant`` is still the safe
fallback. This module adds the production LLM seam using the same OpenRouter
provider settings as the review, summary, and generation AI paths. Tests inject a
fake model transport, so CI never calls the live API.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .ai_review import (
    DEFAULT_OPENROUTER_MODEL,
    OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
    _ai_review_settings,
    _configured_api_key,
    _sanitize_model_name,
    _trusted_https_context,
)
from .openrouter_usage import record_openrouter_usage

SUPPORTED_INTENTS: frozenset[str] = frozenset(
    {
        "system_question",
        "repository_question",
        "draft_action_request",
        "action_request",
        "search_filter",
        "clarification",
        "unsupported",
    }
)


class DashboardAssistantAIUnavailableError(RuntimeError):
    """The configured assistant model could not produce a safe response."""


class DashboardAssistantModel(Protocol):
    """Callable model seam used by tests and the production provider transport."""

    def __call__(self, request_body: dict[str, Any]) -> Mapping[str, Any]:
        """Return a provider-shaped response mapping."""


@dataclass(frozen=True)
class DashboardAssistantAISettings:
    enabled: bool
    provider: str
    model: str
    timeout_seconds: int
    api_key: str


@dataclass(frozen=True)
class DashboardAssistantTool:
    name: str
    domain: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any]], Mapping[str, Any]]

    def provider_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


class OpenRouterDashboardAssistantModel:
    def __init__(self, settings: DashboardAssistantAISettings) -> None:
        self.settings = settings

    def __call__(self, request_body: dict[str, Any]) -> Mapping[str, Any]:
        encoded = json.dumps(request_body).encode("utf-8")
        request = urllib.request.Request(
            OPENROUTER_CHAT_COMPLETIONS_ENDPOINT,
            data=encoded,
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "nda-automation-dashboard-assistant/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.settings.timeout_seconds,
                context=_trusted_https_context(),
            ) as response:
                raw = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
            raise DashboardAssistantAIUnavailableError(str(error)) from error
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DashboardAssistantAIUnavailableError("OpenRouter API returned invalid JSON.") from error
        if not isinstance(parsed, Mapping):
            raise DashboardAssistantAIUnavailableError("OpenRouter API returned a non-object payload.")
        record_openrouter_usage(parsed, feature="dashboard_assistant", model=self.settings.model)
        return parsed


def dashboard_assistant_ai_settings(settings: Mapping[str, Any] | None = None) -> DashboardAssistantAISettings:
    resolved = dict(settings or _ai_review_settings())
    provider = str(resolved.get("provider") or "openrouter").strip().lower()
    timeout = int(resolved.get("timeout_seconds") or 20)
    return DashboardAssistantAISettings(
        enabled=bool(resolved.get("enabled")),
        provider=provider,
        model=_sanitize_model_name(str(resolved.get("model") or DEFAULT_OPENROUTER_MODEL)),
        timeout_seconds=max(1, timeout),
        api_key=_configured_api_key(provider) if provider == "openrouter" else "",
    )


def configured_dashboard_assistant_model(
    *,
    settings: DashboardAssistantAISettings | None = None,
) -> DashboardAssistantModel | None:
    active_settings = settings or dashboard_assistant_ai_settings()
    if not active_settings.enabled or active_settings.provider != "openrouter" or not active_settings.api_key:
        return None
    return OpenRouterDashboardAssistantModel(active_settings)


def run_ai_dashboard_assistant(
    context: Any,
    *,
    model: DashboardAssistantModel | None = None,
    settings: DashboardAssistantAISettings | None = None,
) -> dict[str, Any] | None:
    active_model = model or configured_dashboard_assistant_model(settings=settings)
    if active_model is None:
        return None

    tools = dashboard_assistant_tool_registry(context)
    try:
        first_response = active_model(_initial_request(context.query, tools, settings=settings))
        final = _extract_final_response(first_response)
        if final is not None:
            return validate_dashboard_assistant_response(final, query=context.query)

        tool_calls = _extract_tool_calls(first_response)
        if not tool_calls:
            return None
        tool_outputs = [_execute_tool_call(call, tools) for call in tool_calls]
        second_response = active_model(
            _tool_followup_request(
                context.query,
                tools,
                tool_calls=tool_calls,
                tool_outputs=tool_outputs,
                settings=settings,
            )
        )
        final = _extract_final_response(second_response)
        if final is None:
            return None
        return validate_dashboard_assistant_response(final, query=context.query)
    except DashboardAssistantAIUnavailableError:
        return None


def dashboard_assistant_tool_registry(context: Any) -> dict[str, DashboardAssistantTool]:
    schema_no_args = _strict_schema({})
    return {
        "get_repository_facts": DashboardAssistantTool(
            name="get_repository_facts",
            domain="repository",
            description="Read owner-scoped repository counts, matter phases, and last-sent facts. No side effects.",
            parameters=schema_no_args,
            handler=lambda _args: _repository_facts(context),
        ),
        "get_playbook_facts": DashboardAssistantTool(
            name="get_playbook_facts",
            domain="playbook",
            description="Read active Playbook facts: clause count, name, version, and approved governing-law labels.",
            parameters=schema_no_args,
            handler=lambda _args: _playbook_facts(context),
        ),
        "get_outbound_email_templates": DashboardAssistantTool(
            name="get_outbound_email_templates",
            domain="gmail",
            description="Read outbound email subject/body template rules used by Gmail redline send and Send Document.",
            parameters=schema_no_args,
            handler=lambda _args: _outbound_email_template_facts(),
        ),
        "prepare_safe_action_request": DashboardAssistantTool(
            name="prepare_safe_action_request",
            domain="actions",
            description="Prepare a typed app action request. Side-effectful actions must require user confirmation.",
            parameters=_strict_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "open_generator",
                            "open_repository",
                            "open_playbook",
                            "open_admin",
                            "open_review",
                            "open_gmail_sync",
                            "open_drive_export",
                        ],
                    },
                    "prompt": {"type": "string"},
                },
                required=("action", "prompt"),
            ),
            handler=lambda args: _safe_action_request(context, args),
        ),
        "resolve_matter_search_filter": DashboardAssistantTool(
            name="resolve_matter_search_filter",
            domain="repository",
            description="Translate a matter search/filter query into the existing dashboard search-intent contract.",
            parameters=_strict_schema({"query": {"type": "string"}}, required=("query",)),
            handler=lambda args: _search_filter(context, args),
        ),
    }


def validate_dashboard_assistant_response(payload: Mapping[str, Any], *, query: str) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    response = dict(payload)
    intent = str(response.get("intent") or "").strip()
    if intent not in SUPPORTED_INTENTS:
        return None
    response["intent"] = intent
    response.setdefault("version", 1)
    response.setdefault("query", query)
    if intent in {"action_request", "draft_action_request"}:
        response["requires_confirmation"] = bool(response.get("requires_confirmation"))
        side_effects = response.get("side_effects")
        if not isinstance(side_effects, list):
            side_effects = []
        response["side_effects"] = [str(effect) for effect in side_effects if str(effect).strip()]
        if response["side_effects"]:
            response["requires_confirmation"] = True
    if intent == "clarification":
        response.setdefault("message", "I need one more detail before I can help with that.")
        questions = response.get("questions")
        if not isinstance(questions, list):
            response["questions"] = []
    return response


def _initial_request(
    query: str,
    tools: Mapping[str, DashboardAssistantTool],
    *,
    settings: DashboardAssistantAISettings | None,
) -> dict[str, Any]:
    active_settings = settings or dashboard_assistant_ai_settings()
    return {
        "model": active_settings.model,
        "messages": [
            {"role": "system", "content": _developer_instructions()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": "Answer or prepare an action for QUERY using tools and only real app facts.",
                        "QUERY": query,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        "tools": [tool.provider_tool() for tool in tools.values()],
        "tool_choice": "auto",
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


def _tool_followup_request(
    query: str,
    tools: Mapping[str, DashboardAssistantTool],
    *,
    tool_calls: list[dict[str, Any]],
    tool_outputs: list[Mapping[str, Any]],
    settings: DashboardAssistantAISettings | None,
) -> dict[str, Any]:
    request = _initial_request(query, tools, settings=settings)
    request["messages"].append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_chat_completion_tool_call(call) for call in tool_calls],
        }
    )
    request["messages"].extend(
        {
            "role": "tool",
            "tool_call_id": str(call.get("id") or call.get("call_id") or call["name"]),
            "name": str(call.get("name") or ""),
            "content": json.dumps(output),
        }
        for call, output in zip(tool_calls, tool_outputs, strict=False)
    )
    return request


def _developer_instructions() -> str:
    return (
        "You are the NDA Automation Dashboard assistant. Use tools to answer only from real app facts "
        "or to prepare safe typed action requests. Never fabricate matters, Playbook facts, email templates, "
        "settings, or workflow status. Never claim that a side-effectful action has been performed. "
        "For generation, Gmail sync/import/send, Drive/export/download, review refresh, approve, delete, or settings changes, "
        "return an action_request or draft_action_request with requires_confirmation true. "
        "Do not expose chain-of-thought; return a concise answer, citations/facts, action request, clarification, or unsupported response."
    )


def _strict_schema(
    properties: Mapping[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _extract_final_response(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    direct = raw.get("assistant_response")
    if isinstance(direct, Mapping):
        return direct
    message = _first_choice_message(raw)
    if message is not None:
        content = str(message.get("content") or "").strip()
        parsed = _loads_json_object(content)
        if isinstance(parsed, Mapping):
            return parsed
    return None


def _extract_tool_calls(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in raw.get("tool_calls", []) if isinstance(raw.get("tool_calls"), list) else []:
        if isinstance(item, Mapping):
            calls.append(dict(item))
    message = _first_choice_message(raw)
    tool_calls = message.get("tool_calls") if message is not None else None
    if isinstance(tool_calls, list):
        for item in tool_calls:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function")
            if not isinstance(function, Mapping):
                continue
            calls.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or "{}"),
                }
            )
    return calls


def _execute_tool_call(
    call: Mapping[str, Any],
    tools: Mapping[str, DashboardAssistantTool],
) -> Mapping[str, Any]:
    name = str(call.get("name") or "").strip()
    tool = tools.get(name)
    if tool is None:
        return {"error": "unsupported_tool", "tool": name}
    args = call.get("arguments")
    if isinstance(args, str):
        parsed_args = _loads_json_object(args)
        args = parsed_args if isinstance(parsed_args, Mapping) else {}
    if not isinstance(args, Mapping):
        args = {}
    return tool.handler(args)


def _chat_completion_tool_call(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(call.get("id") or call.get("call_id") or call["name"]),
        "type": "function",
        "function": {
            "name": str(call.get("name") or ""),
            "arguments": call.get("arguments") if isinstance(call.get("arguments"), str) else json.dumps(call.get("arguments") or {}),
        },
    }


def _first_choice_message(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choices = raw.get("choices")
    if not isinstance(choices, Sequence) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    return message if isinstance(message, Mapping) else None


def _loads_json_object(text: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _repository_facts(context: Any) -> Mapping[str, Any]:
    from . import workflow

    matters = context.public_matters
    phase_counts: dict[str, int] = {}
    last_sent: dict[str, Any] | None = None
    for matter in matters:
        phase = _matter_phase(matter)
        if phase:
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        sent_at = str(matter.get("last_outbound_at") or "").strip()
        if sent_at and (last_sent is None or sent_at > str(last_sent.get("last_outbound_at") or "")):
            last_sent = {
                "matter_id": str(matter.get("id") or ""),
                "title": _matter_title(matter),
                "last_outbound_at": sent_at,
                "last_outbound_to": str(matter.get("last_outbound_to") or ""),
            }
    return {
        "domain": "repository",
        "matter_count": len(matters),
        "phase_counts": phase_counts,
        "in_review_count": phase_counts.get(workflow.PHASE_REVIEW, 0),
        "last_sent": last_sent,
    }


def _playbook_facts(context: Any) -> Mapping[str, Any]:
    from . import dashboard_assistant as core

    playbook = context.playbook
    clauses = core._playbook_clauses(playbook)  # noqa: SLF001 - internal read-model reuse.
    laws = core._approved_governing_law_options(playbook)  # noqa: SLF001 - internal read-model reuse.
    return {
        "domain": "playbook",
        "name": str(playbook.get("name") or "Active NDA Playbook"),
        "version": str(playbook.get("version") or ""),
        "clause_count": len(clauses),
        "clause_ids": [str(clause.get("id") or "") for clause in clauses],
        "approved_governing_laws": [
            str(option.get("label") or option.get("id") or "").strip()
            for option in laws
            if str(option.get("label") or option.get("id") or "").strip()
        ],
    }


def _outbound_email_template_facts() -> Mapping[str, Any]:
    from . import gmail_matter_outbox

    sample_matter = {"subject": "Example NDA"}
    return {
        "domain": "gmail",
        "templates": [
            {
                "context": "redline_send",
                "subject_rule": "Use supplied subject, otherwise reply_subject(matter subject/document title).",
                "body": gmail_matter_outbox.default_outbound_body(sample_matter),
                "source": "nda_automation/gmail_matter_outbox.py::default_outbound_body",
            },
            {
                "context": "send_document",
                "subject_rule": "Use supplied subject, otherwise uploaded file stem.",
                "body_rule": "Use supplied body, otherwise shared outbound default body.",
                "source": "nda_automation/routes/send_document.py",
            },
        ],
    }


def _safe_action_request(context: Any, args: Mapping[str, Any]) -> Mapping[str, Any]:
    from . import dashboard_assistant as core

    action = str(args.get("action") or "").strip()
    if action == "open_generator":
        return core.draft_action_request_response(context)
    if action == "open_repository":
        return core.open_repository_response(context)
    if action == "open_playbook":
        return core.open_playbook_response(context)
    if action == "open_admin":
        return core.open_admin_response(context)
    if action == "open_review":
        return core.review_request_response(context)
    if action == "open_gmail_sync":
        return core.gmail_sync_request_response(context)
    if action == "open_drive_export":
        return core.drive_export_request_response(context)
    return {"error": "unsupported_action", "action": action}


def _search_filter(context: Any, args: Mapping[str, Any]) -> Mapping[str, Any]:
    from . import dashboard_assistant as core

    query = str(args.get("query") or context.query)
    search_context = core.AssistantContext(
        query,
        repository=context.repository,
        owner_user_id=context.owner_user_id,
        search_resolver=context.search_resolver,
        playbook_provider=context.playbook_provider,
    )
    return core.search_filter_response(search_context)


def _matter_phase(matter: Mapping[str, Any]) -> str:
    workflow_state = matter.get("workflow_state")
    if isinstance(workflow_state, Mapping):
        return str(workflow_state.get("phase") or "").strip().lower()
    return ""


def _matter_title(matter: Mapping[str, Any]) -> str:
    return str(matter.get("subject") or matter.get("document_title") or matter.get("source_filename") or "Untitled NDA")
