"""Read-only Dashboard assistant command handling.

This is intentionally narrower than a general chat assistant. It classifies a
short command/query into one of the supported Dashboard intents, answers only
from scoped repository facts, and returns action requests instead of performing
side effects.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import dashboard_search_intent, gmail_matter_outbox, matter_view, playbook_runtime, workflow
from .matter_repository import MatterRepository
from .untrusted_text import neutralize_untrusted_text

DASHBOARD_ASSISTANT_VERSION = 1
MAX_QUERY_CHARS = 500
MAX_CITATIONS = 5

AssistantSearchResolver = Callable[[str], dict[str, Any]]
PlaybookProvider = Callable[[], Mapping[str, Any]]
CapabilityMatcher = Callable[["AssistantContext"], bool]
CapabilityHandler = Callable[["AssistantContext"], dict[str, Any]]


@dataclass(frozen=True)
class AssistantCapability:
    name: str
    domain: str
    intent: str
    description: str
    matcher: CapabilityMatcher
    handler: CapabilityHandler
    side_effectful: bool = False


class AssistantContext:
    def __init__(
        self,
        query: str,
        *,
        repository: MatterRepository,
        owner_user_id: str = "",
        search_resolver: AssistantSearchResolver | None = None,
        playbook_provider: PlaybookProvider | None = None,
    ) -> None:
        self.query = query
        self.lowered = query.lower()
        self.repository = repository
        self.owner_user_id = owner_user_id
        self.search_resolver = search_resolver
        self.playbook_provider = playbook_provider or _active_playbook
        self._public_matters: list[dict[str, Any]] | None = None
        self._playbook: Mapping[str, Any] | None = None

    @property
    def public_matters(self) -> list[dict[str, Any]]:
        if self._public_matters is None:
            self._public_matters = _public_matters(
                self.repository.list_matters(owner_user_id=self.owner_user_id)
            )
        return self._public_matters

    @property
    def playbook(self) -> Mapping[str, Any]:
        if self._playbook is None:
            self._playbook = self.playbook_provider()
        return self._playbook


def handle_dashboard_assistant_command(
    query: str,
    *,
    repository: MatterRepository,
    owner_user_id: str = "",
    search_resolver: AssistantSearchResolver | None = None,
    playbook_provider: PlaybookProvider | None = None,
    ai_model: Any | None = None,
) -> dict[str, Any]:
    """Return a typed, side-effect-free Dashboard assistant response."""
    cleaned_query = _clean_query(query)
    if not cleaned_query:
        return unsupported_response(cleaned_query, message=_capability_prompt())

    context = AssistantContext(
        cleaned_query,
        repository=repository,
        owner_user_id=owner_user_id,
        search_resolver=search_resolver,
        playbook_provider=playbook_provider,
    )
    ai_response = _ai_assistant_response(context, ai_model=ai_model)
    if ai_response is not None:
        return ai_response

    for capability in ASSISTANT_CAPABILITIES:
        if capability.matcher(context):
            return capability.handler(context)

    return unsupported_response(cleaned_query)


def _ai_assistant_response(context: AssistantContext, *, ai_model: Any | None) -> dict[str, Any] | None:
    try:
        from .dashboard_assistant_ai import run_ai_dashboard_assistant
    except Exception:  # noqa: BLE001 - deterministic command catalog is the safe fallback.
        return None
    return run_ai_dashboard_assistant(context, model=ai_model)


def count_in_review_response(context: AssistantContext) -> dict[str, Any]:
    matches = [
        matter for matter in context.public_matters
        if _matter_phase(matter) == workflow.PHASE_REVIEW
    ]
    count = len(matches)
    noun = "document is" if count == 1 else "documents are"
    return {
        "intent": "repository_question",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "repository",
        "question": "count_in_review",
        "answer": {
            "text": f"{count} {noun} in review.",
            "count": count,
            "phase": workflow.PHASE_REVIEW,
        },
        "citations": _matter_citations(matches),
    }


def last_sent_response(context: AssistantContext) -> dict[str, Any]:
    sent = [
        matter for matter in context.public_matters
        if _parse_timestamp(matter.get("last_outbound_at")) is not None
    ]
    sent.sort(
        key=lambda matter: _parse_timestamp(matter.get("last_outbound_at")) or datetime.min,
        reverse=True,
    )
    if not sent:
        return {
            "intent": "repository_question",
            "version": DASHBOARD_ASSISTANT_VERSION,
            "query": context.query,
            "domain": "repository",
            "question": "last_sent",
            "answer": {
                "text": "No sent NDAs were found in the repository.",
                "count": 0,
            },
            "citations": [],
        }

    matter = sent[0]
    sent_at = str(matter.get("last_outbound_at") or "")
    title = _matter_title(matter)
    recipient = str(matter.get("last_outbound_to") or "").strip()
    recipient_phrase = f" to {recipient}" if recipient else ""
    return {
        "intent": "repository_question",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "repository",
        "question": "last_sent",
        "answer": {
            "text": f'The last NDA sent was "{title}"{recipient_phrase} on {sent_at}.',
            "sent_at": sent_at,
            "recipient": recipient,
            "matter_id": str(matter.get("id") or ""),
        },
        "citations": _matter_citations([matter]),
    }


def playbook_clause_count_response(context: AssistantContext) -> dict[str, Any]:
    clauses = _playbook_clauses(context.playbook)
    count = len(clauses)
    noun = "clause" if count == 1 else "clauses"
    name = str(context.playbook.get("name") or "Active NDA Playbook")
    version = str(context.playbook.get("version") or "")
    return {
        "intent": "system_question",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "playbook",
        "question": "playbook_clause_count",
        "answer": {
            "text": f"{name} has {count} {noun}.",
            "count": count,
            "playbook_name": name,
            "playbook_version": version,
        },
        "citations": [
            {
                "source": "playbook",
                "title": name,
                "version": version,
            }
        ],
    }


def playbook_governing_laws_response(context: AssistantContext) -> dict[str, Any]:
    laws = _approved_governing_law_options(context.playbook)
    labels = [str(option.get("label") or option.get("id") or "").strip() for option in laws]
    labels = [label for label in labels if label]
    count = len(labels)
    if labels:
        text = f"The active Playbook has {count} approved governing-law options: {', '.join(labels)}."
    else:
        text = "The active Playbook does not define approved governing-law options."
    return {
        "intent": "system_question",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "playbook",
        "question": "approved_governing_laws",
        "answer": {
            "text": text,
            "count": count,
            "options": labels,
        },
        "citations": [{"source": "playbook", "title": str(context.playbook.get("name") or "Active NDA Playbook")}],
    }


def outbound_email_template_response(context: AssistantContext) -> dict[str, Any]:
    sample_subject = "Example NDA"
    return {
        "intent": "system_question",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "gmail",
        "question": "outbound_email_templates",
        "answer": {
            "text": (
                "Outbound redline emails default to a reply-style subject and a short Aspora Legal body. "
                "Dashboard Send Document can override subject/body; if body is blank it currently reuses the same default body."
            ),
            "templates": [
                {
                    "context": "redline_send",
                    "subject_rule": "Use the supplied subject, otherwise prefix the matter subject/document title with Re:.",
                    "body": gmail_matter_outbox.default_outbound_body({"subject": sample_subject}),
                },
                {
                    "context": "send_document",
                    "subject_rule": "Use the supplied subject, otherwise use the uploaded file stem.",
                    "body_rule": "Use the supplied body, otherwise the shared outbound default body is used.",
                },
            ],
        },
        "citations": [
            {"source": "code", "title": "nda_automation/gmail_matter_outbox.py"},
            {"source": "code", "title": "nda_automation/routes/send_document.py"},
        ],
    }


def assistant_capabilities_response(context: AssistantContext) -> dict[str, Any]:
    capabilities = [
        {
            "name": capability.name,
            "domain": capability.domain,
            "intent": capability.intent,
            "description": capability.description,
            "side_effectful": capability.side_effectful,
        }
        for capability in ASSISTANT_CAPABILITIES
        if capability.name != "capability_catalog"
    ]
    domains = sorted({capability["domain"] for capability in capabilities})
    return {
        "intent": "system_question",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "assistant",
        "question": "capability_catalog",
        "answer": {
            "text": "I can search matters, answer repository and Playbook questions, explain outbound email templates, and start safe app workflows with confirmation.",
            "domains": domains,
            "capabilities": capabilities,
        },
        "citations": [],
    }


def draft_action_request_response(context: AssistantContext) -> dict[str, Any]:
    return {
        "intent": "draft_action_request",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "generation",
        "action": "open_generator",
        "requires_confirmation": True,
        "message": "I can help start an NDA draft. Open the Generator, review the intake, then choose Generate when you are ready.",
        "generator": {
            "prefill": _generator_prefill(context.query),
            "missing_fields": [
                "signing_entity",
                "counterparty_name",
                "counterparty_registered_office",
                "purpose",
            ],
        },
        "side_effects": [],
    }


def action_request_response(
    context: AssistantContext,
    *,
    action: str,
    domain: str,
    label: str,
    message: str,
    target_tab: str,
    requires_confirmation: bool,
    side_effects: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "intent": "action_request",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": domain,
        "action": action,
        "label": label,
        "requires_confirmation": requires_confirmation,
        "message": message,
        "target": {"tab": target_tab},
        "side_effects": list(side_effects),
    }


def open_repository_response(context: AssistantContext) -> dict[str, Any]:
    return action_request_response(
        context,
        action="open_repository",
        domain="repository",
        label="Open Repository",
        message="I can open the Repository so you can inspect matters and choose the next step.",
        target_tab="repository",
        requires_confirmation=False,
    )


def open_playbook_response(context: AssistantContext) -> dict[str, Any]:
    return action_request_response(
        context,
        action="open_playbook",
        domain="playbook",
        label="Open Playbook",
        message="I can open the Playbook authoring area. Publishing changes still requires the Playbook workflow.",
        target_tab="playbook",
        requires_confirmation=False,
    )


def open_admin_response(context: AssistantContext) -> dict[str, Any]:
    return action_request_response(
        context,
        action="open_admin",
        domain="admin",
        label="Open Admin",
        message="I can open Admin settings/status. Changing settings still requires the relevant Admin controls.",
        target_tab="admin",
        requires_confirmation=False,
    )


def gmail_sync_request_response(context: AssistantContext) -> dict[str, Any]:
    return action_request_response(
        context,
        action="open_gmail_sync",
        domain="gmail",
        label="Review Gmail sync",
        message="I can take you to the Gmail controls. Sync/import is not started from the assistant response.",
        target_tab="admin",
        requires_confirmation=True,
        side_effects=("gmail_import_or_sync",),
    )


def review_request_response(context: AssistantContext) -> dict[str, Any]:
    return action_request_response(
        context,
        action="open_review",
        domain="review",
        label="Open Review",
        message="I can open the Repository/Review workflow. Running or refreshing review still requires an explicit matter action.",
        target_tab="repository",
        requires_confirmation=True,
        side_effects=("review_or_refresh",),
    )


def drive_export_request_response(context: AssistantContext) -> dict[str, Any]:
    return action_request_response(
        context,
        action="open_drive_export",
        domain="drive",
        label="Review Drive/export options",
        message="I can take you to the relevant matter/export controls. Nothing is uploaded or downloaded from the assistant response.",
        target_tab="repository",
        requires_confirmation=True,
        side_effects=("drive_upload_or_export",),
    )


def search_filter_response(context: AssistantContext) -> dict[str, Any]:
    search = _search_response(context.query, search_resolver=context.search_resolver)
    if search and not search.get("fallback") and search.get("filters") is not None:
        filters = search.get("filters")
        if isinstance(filters, Mapping) and not dashboard_search_intent.filter_spec_is_empty(filters):
            return {
                "intent": "search_filter",
                "version": DASHBOARD_ASSISTANT_VERSION,
                "query": context.query,
                "domain": "repository",
                "message": "Search filters are ready.",
                "search": search,
            }
    return unsupported_response(
        context.query,
        message="I could not map that to a supported assistant command or matter search filter.",
    )


def unsupported_response(query: str, *, message: str | None = None) -> dict[str, Any]:
    return {
        "intent": "unsupported",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": query,
        "message": message
        or _capability_prompt(),
    }


def _search_response(query: str, *, search_resolver: AssistantSearchResolver | None) -> dict[str, Any] | None:
    if search_resolver is not None:
        return search_resolver(query)
    try:
        return dashboard_search_intent.deterministic_search_intent(query)
    except Exception:  # noqa: BLE001 - unsupported is safer than surfacing parser internals
        return None


def _public_matters(matters: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(matter_view.public_matter(matter)) for matter in matters]


def _looks_like_count_in_review_question(lowered: str) -> bool:
    return _contains_any(lowered, ("how many", "number of", "count")) and _contains_any(
        lowered,
        ("in review", "under review", "reviewing"),
    )


def _looks_like_last_sent_question(lowered: str) -> bool:
    return _contains_any(lowered, ("last", "latest", "most recent", "recent")) and _contains_any(
        lowered,
        ("sent", "sent to me", "sent out"),
    )


def _looks_like_generation_request(lowered: str) -> bool:
    return _contains_any(lowered, ("generate", "create", "draft", "prepare", "start")) and _contains_any(
        lowered,
        ("nda", "non-disclosure", "agreement"),
    )


def _looks_like_playbook_clause_count_question(lowered: str) -> bool:
    if not _contains_any(lowered, ("clause", "clauses")) or not _contains_any(
        lowered,
        ("how many", "number of", "count", "do we have"),
    ):
        return False
    if "playbook" in lowered:
        return True
    if _contains_any(lowered, ("this nda", "the nda", "this contract", "the contract", "this document", "the document")):
        return False
    return _contains_any(lowered, ("do we have", "we have", "our clauses", "our nda clauses"))


def _looks_like_playbook_law_question(lowered: str) -> bool:
    return "playbook" in lowered and _contains_any(lowered, ("governing law", "governing-law", "law options", "approved laws"))


def _looks_like_email_template_question(lowered: str) -> bool:
    return _contains_any(lowered, ("email", "message")) and _contains_any(
        lowered,
        ("template", "default body", "default subject", "send"),
    )


def _looks_like_capability_question(lowered: str) -> bool:
    return _contains_any(lowered, ("what can you do", "capabilities", "commands", "help me do"))


def _looks_like_open_repository_request(lowered: str) -> bool:
    return _contains_any(lowered, ("open", "go to", "show")) and _contains_any(lowered, ("repository", "board", "kanban"))


def _looks_like_open_playbook_request(lowered: str) -> bool:
    return _contains_any(lowered, ("open", "go to", "show", "edit", "publish")) and "playbook" in lowered


def _looks_like_open_admin_request(lowered: str) -> bool:
    return _contains_any(lowered, ("open", "go to", "show")) and _contains_any(lowered, ("admin", "settings", "status"))


def _looks_like_gmail_sync_request(lowered: str) -> bool:
    return _contains_any(lowered, ("gmail", "inbox", "intake", "import", "sync")) and _contains_any(
        lowered,
        ("sync", "import", "check", "pull", "fetch"),
    )


def _looks_like_review_request(lowered: str) -> bool:
    return _contains_any(lowered, ("review", "ai review")) and _contains_any(
        lowered,
        ("run", "start", "refresh", "open", "status"),
    )


def _looks_like_drive_export_request(lowered: str) -> bool:
    return _contains_any(lowered, ("drive", "export", "download")) and _contains_any(
        lowered,
        ("save", "upload", "export", "download", "send"),
    )


def _looks_like_search_request(lowered: str) -> bool:
    return _contains_any(
        lowered,
        (
            "approval",
            "approved",
            "awaiting",
            "counterparty",
            "doc",
            "document",
            "find",
            "in review",
            "issue",
            "list",
            "matter",
            "nda",
            "pending",
            "review",
            "search",
            "sent",
            "show",
            "signature",
            "stuck",
        ),
    )


def _contains_any(text: str, needles: Sequence[str]) -> bool:
    return any(needle in text for needle in needles)


def _clean_query(query: str) -> str:
    return neutralize_untrusted_text(str(query or ""), max_chars=MAX_QUERY_CHARS).strip()


def _matter_phase(matter: Mapping[str, Any]) -> str:
    workflow_state = matter.get("workflow_state")
    if isinstance(workflow_state, Mapping):
        return str(workflow_state.get("phase") or "").strip().lower()
    return ""


def _matter_title(matter: Mapping[str, Any]) -> str:
    return str(
        matter.get("subject")
        or matter.get("document_title")
        or matter.get("source_filename")
        or "Untitled NDA"
    )


def _matter_citations(matters: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for matter in matters[:MAX_CITATIONS]:
        citation = {
            "matter_id": str(matter.get("id") or ""),
            "title": _matter_title(matter),
            "workflow_phase": _matter_phase(matter),
        }
        sent_at = str(matter.get("last_outbound_at") or "").strip()
        if sent_at:
            citation["last_outbound_at"] = sent_at
        citations.append(citation)
    return citations


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _generator_prefill(query: str) -> dict[str, str]:
    return {
        "source": "dashboard_assistant",
        "prompt": query,
    }


def _active_playbook() -> Mapping[str, Any]:
    return playbook_runtime.ensure_active_playbook_bundle().playbook


def _playbook_clauses(playbook: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    clauses = playbook.get("clauses") if isinstance(playbook, Mapping) else []
    if not isinstance(clauses, list):
        return []
    return [clause for clause in clauses if isinstance(clause, Mapping)]


def _approved_governing_law_options(playbook: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for clause in _playbook_clauses(playbook):
        if str(clause.get("id") or "") != "governing_law":
            continue
        rules = clause.get("rules")
        if not isinstance(rules, Mapping):
            return []
        options = rules.get("approved_options")
        if not isinstance(options, list):
            return []
        return [option for option in options if isinstance(option, Mapping)]
    return []


def _capability_prompt() -> str:
    return (
        "I can search matters, answer repository, Playbook, Gmail/email-template and system questions, "
        "or prepare safe action requests for generation, review, Gmail, Drive/export, Admin and Playbook workflows."
    )


ASSISTANT_CAPABILITIES: tuple[AssistantCapability, ...] = (
    AssistantCapability(
        name="capability_catalog",
        domain="assistant",
        intent="system_question",
        description="Explain the command-center domains the Dashboard assistant supports.",
        matcher=lambda context: _looks_like_capability_question(context.lowered),
        handler=assistant_capabilities_response,
    ),
    AssistantCapability(
        name="generate_nda",
        domain="generation",
        intent="draft_action_request",
        description="Open/prefill the Generator after explicit confirmation; never silently generate.",
        matcher=lambda context: _looks_like_generation_request(context.lowered),
        handler=draft_action_request_response,
        side_effectful=True,
    ),
    AssistantCapability(
        name="playbook_clause_count",
        domain="playbook",
        intent="system_question",
        description="Answer clause-count questions from the active Playbook.",
        matcher=lambda context: _looks_like_playbook_clause_count_question(context.lowered),
        handler=playbook_clause_count_response,
    ),
    AssistantCapability(
        name="approved_governing_laws",
        domain="playbook",
        intent="system_question",
        description="List approved governing-law options from the active Playbook.",
        matcher=lambda context: _looks_like_playbook_law_question(context.lowered),
        handler=playbook_governing_laws_response,
    ),
    AssistantCapability(
        name="outbound_email_templates",
        domain="gmail",
        intent="system_question",
        description="Explain outbound email subject/body defaults from the Gmail outbox/send-document contracts.",
        matcher=lambda context: _looks_like_email_template_question(context.lowered),
        handler=outbound_email_template_response,
    ),
    AssistantCapability(
        name="count_in_review",
        domain="repository",
        intent="repository_question",
        description="Count owner-scoped matters currently in review.",
        matcher=lambda context: _looks_like_count_in_review_question(context.lowered),
        handler=count_in_review_response,
    ),
    AssistantCapability(
        name="last_sent",
        domain="repository",
        intent="repository_question",
        description="Answer last-sent questions from recorded outbound stamps.",
        matcher=lambda context: _looks_like_last_sent_question(context.lowered),
        handler=last_sent_response,
    ),
    AssistantCapability(
        name="open_playbook",
        domain="playbook",
        intent="action_request",
        description="Open the Playbook workspace without publishing changes.",
        matcher=lambda context: _looks_like_open_playbook_request(context.lowered),
        handler=open_playbook_response,
    ),
    AssistantCapability(
        name="open_repository",
        domain="repository",
        intent="action_request",
        description="Open the Repository board.",
        matcher=lambda context: _looks_like_open_repository_request(context.lowered),
        handler=open_repository_response,
    ),
    AssistantCapability(
        name="open_admin",
        domain="admin",
        intent="action_request",
        description="Open Admin/settings/status surfaces.",
        matcher=lambda context: _looks_like_open_admin_request(context.lowered),
        handler=open_admin_response,
    ),
    AssistantCapability(
        name="gmail_sync",
        domain="gmail",
        intent="action_request",
        description="Prepare a confirmation-gated Gmail sync/import workflow request.",
        matcher=lambda context: _looks_like_gmail_sync_request(context.lowered),
        handler=gmail_sync_request_response,
        side_effectful=True,
    ),
    AssistantCapability(
        name="review_workflow",
        domain="review",
        intent="action_request",
        description="Prepare a confirmation-gated review/open-review workflow request.",
        matcher=lambda context: _looks_like_review_request(context.lowered),
        handler=review_request_response,
        side_effectful=True,
    ),
    AssistantCapability(
        name="drive_export",
        domain="drive",
        intent="action_request",
        description="Prepare a confirmation-gated Drive/export/download workflow request.",
        matcher=lambda context: _looks_like_drive_export_request(context.lowered),
        handler=drive_export_request_response,
        side_effectful=True,
    ),
    AssistantCapability(
        name="matter_search_filter",
        domain="repository",
        intent="search_filter",
        description="Translate matter-search/filter requests into the existing dashboard search contract.",
        matcher=lambda context: _looks_like_search_request(context.lowered),
        handler=search_filter_response,
    ),
)
