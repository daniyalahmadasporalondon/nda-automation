"""Read-only Dashboard assistant command handling.

This is intentionally narrower than a general chat assistant. It classifies a
short command/query into one of the supported Dashboard intents, answers only
from scoped repository facts, and returns action requests instead of performing
side effects.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import dashboard_search_intent, gmail_matter_outbox, matter_view, playbook_runtime, review_state, workflow
from .matter_repository import MatterRepository
from .untrusted_text import neutralize_untrusted_text

DASHBOARD_ASSISTANT_VERSION = 1
MAX_QUERY_CHARS = 500
MAX_CITATIONS = 5
MAX_SEARCH_HITS = 8
MAX_SNIPPET_CHARS = 220

READ_ONLY_INTENTS = frozenset(
    {
        "repository_question",
        "system_question",
        "review_finding_explanation",
        "matter_summary",
        "system_search",
        "how_it_works",
        "search_filter",
    }
)

SIDE_EFFECTFUL_ACTIONS = frozenset(
    {
        "refresh_review",
        "run_review",
        "gmail_import",
        "sync_gmail",
        "send_redline",
        "approve_matter",
    }
)

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
        self._owner_matters: list[dict[str, Any]] | None = None
        self._public_matters: list[dict[str, Any]] | None = None
        self._playbook: Mapping[str, Any] | None = None

    @property
    def owner_matters(self) -> list[dict[str, Any]]:
        if self._owner_matters is None:
            self._owner_matters = self.repository.list_matters(owner_user_id=self.owner_user_id)
        return self._owner_matters

    @property
    def public_matters(self) -> list[dict[str, Any]]:
        if self._public_matters is None:
            self._public_matters = _public_matters(self.owner_matters)
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


def explain_review_finding_response(
    context: AssistantContext,
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    matter, matter_error = _resolve_matter(
        context,
        matter_id=str(args.get("matter_id") or ""),
        matter_query=str(args.get("matter_query") or context.query),
    )
    if matter is None:
        return clarification_response(
            context,
            matter_error or "Which matter should I explain?",
            questions=_matter_question_options(context),
        )

    clause, clause_error = _resolve_clause(
        context,
        matter,
        clause_id=str(args.get("clause_id") or ""),
        clause_query=str(args.get("clause_query") or context.query),
    )
    if clause is None:
        return clarification_response(
            context,
            clause_error or "Which review finding should I explain?",
            questions=_clause_question_options(matter),
        )

    public = matter_view.public_matter(matter)
    playbook_clause = _playbook_clause_for(context.playbook, clause)
    verdict = _clause_decision(clause)
    evidence = _clause_evidence(clause)
    playbook_position = _playbook_position(playbook_clause)
    clause_name = _clause_name(clause)
    title = _matter_title(public)
    explanation = _plain_language_clause_explanation(
        clause_name=clause_name,
        verdict=verdict,
        reason=_clause_reason(clause),
        evidence=evidence,
        playbook_position=playbook_position,
    )
    return {
        "intent": "review_finding_explanation",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "review",
        "question": "explain_review_finding",
        "answer": {
            "text": f'{clause_name} on "{title}": {explanation}',
            "matter_id": str(matter.get("id") or ""),
            "matter_title": title,
            "clause_id": str(clause.get("id") or ""),
            "clause_name": clause_name,
            "verdict": verdict,
            "evidence": evidence,
            "playbook_position": playbook_position,
            "explanation": explanation,
        },
        "citations": [
            {
                "source": "matter_review",
                "matter_id": str(matter.get("id") or ""),
                "title": title,
                "clause_id": str(clause.get("id") or ""),
                "clause_name": clause_name,
            }
        ],
    }


def summarize_matter_response(
    context: AssistantContext,
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    matter, matter_error = _resolve_matter(
        context,
        matter_id=str(args.get("matter_id") or ""),
        matter_query=str(args.get("matter_query") or context.query),
    )
    if matter is None:
        return clarification_response(
            context,
            matter_error or "Which matter should I summarize?",
            questions=_matter_question_options(context),
        )
    public = matter_view.public_matter(matter)
    title = _matter_title(public)
    workflow_state = public.get("workflow_state") if isinstance(public.get("workflow_state"), Mapping) else {}
    review = public.get("review_state") if isinstance(public.get("review_state"), Mapping) else {}
    phase = str(workflow_state.get("phase") or "").replace("_", " ") or "unknown phase"
    status_label = str(workflow_state.get("label") or workflow_state.get("status") or "").strip()
    next_action = _workflow_next_action(workflow_state) or str(public.get("next_action") or "").strip()
    risk_bits = _matter_risk_bits(public, review)
    if risk_bits:
        risk_text = "; ".join(risk_bits)
    else:
        risk_text = "no current review risks were found in the stored review result"
    next_text = next_action or "open the matter and choose the next workflow step"
    summary = (
        f'"{title}" is in {phase}'
        + (f" ({status_label})" if status_label else "")
        + f". Risk summary: {risk_text}. Next action: {next_text}."
    )
    return {
        "intent": "matter_summary",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "repository",
        "question": "summarize_matter",
        "answer": {
            "text": summary,
            "matter_id": str(matter.get("id") or ""),
            "matter_title": title,
            "phase": str(workflow_state.get("phase") or ""),
            "status": str(workflow_state.get("status") or public.get("status") or ""),
            "next_action": next_text,
            "risks": risk_bits,
            "counts": review.get("counts") if isinstance(review.get("counts"), Mapping) else {},
        },
        "citations": _matter_citations([public]),
    }


def search_system_response(
    context: AssistantContext,
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    query = str(args.get("query") or context.query)
    hits = _search_system_hits(context, query)
    count = len(hits)
    noun = "hit" if count == 1 else "hits"
    if hits:
        text = f"Found {count} owner-scoped {noun} across matters, review clauses, and the Playbook."
    else:
        text = "No owner-scoped matches were found across matters, review clauses, or the Playbook."
    return {
        "intent": "system_search",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "system_search",
        "question": "search_system",
        "answer": {
            "text": text,
            "count": count,
            "hits": hits,
        },
        "citations": _search_hit_citations(hits),
    }


def explain_how_it_works_response(
    context: AssistantContext,
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    topic = _how_it_works_topic(str(args.get("topic") or context.query))
    knowledge = _trusted_how_it_works_knowledge()[topic]
    return {
        "intent": "how_it_works",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "assistant",
        "question": f"how_{topic}_works",
        "answer": {
            "text": knowledge["text"],
            "topic": topic,
            "steps": knowledge["steps"],
            "security": knowledge["security"],
        },
        "citations": knowledge["citations"],
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


def clarification_response(
    context: AssistantContext,
    message: str,
    *,
    questions: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "intent": "clarification",
        "version": DASHBOARD_ASSISTANT_VERSION,
        "query": context.query,
        "domain": "assistant",
        "message": message,
        "questions": list(questions)[:5],
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
    params: Mapping[str, Any] | None = None,
    route: Mapping[str, Any] | None = None,
    matter: Mapping[str, Any] | None = None,
    human_summary: str = "",
    risk_tier: str = "safe",
) -> dict[str, Any]:
    response = {
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
        "risk_tier": risk_tier,
    }
    if params is not None:
        response["params"] = dict(params)
    if route is not None:
        response["route"] = dict(route)
    if matter is not None:
        response["matter"] = dict(matter)
    if human_summary:
        response["human_summary"] = human_summary
    return response


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
        action="gmail_import",
        domain="gmail",
        label="Sync Gmail now",
        message="I can sync Gmail now after you confirm. The existing Gmail import route will enforce account connection and owner scope.",
        target_tab="admin",
        requires_confirmation=True,
        side_effects=("gmail_import",),
        params={"limit": 25},
        route={"method": "POST", "url": "/api/gmail/import"},
        human_summary="Sync Gmail now and import up to 25 matching inbound NDA matters for your connected account.",
        risk_tier="safe_execution",
    )


def review_request_response(
    context: AssistantContext,
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    matter, matter_error = _resolve_matter(
        context,
        matter_id=str(args.get("matter_id") or ""),
        matter_query=str(args.get("matter_query") or context.query),
        allow_single=False,
    )
    if matter is None:
        if context.public_matters:
            return clarification_response(
                context,
                matter_error or "Which matter should I refresh review for?",
                questions=_matter_question_options(context),
            )
        return open_repository_response(context)
    public = matter_view.public_matter(matter)
    matter_ref = _action_matter_ref(public)
    matter_id = str(matter.get("id") or "")
    title = _matter_title(public)
    return action_request_response(
        context,
        action="refresh_review",
        domain="review",
        label="Refresh Review",
        message="I can refresh this matter's review after you confirm. The existing matter review route will enforce ownership.",
        target_tab="review",
        requires_confirmation=True,
        side_effects=("review_refresh",),
        params={"matter_id": matter_id},
        route={"method": "POST", "url": f"/api/matters/{matter_id}/review-refresh"},
        matter=matter_ref,
        human_summary=f'Refresh review for "{title}" against the active Playbook.',
        risk_tier="safe_execution",
    )


def approve_matter_request_response(
    context: AssistantContext,
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    matter, matter_error = _resolve_matter(
        context,
        matter_id=str(args.get("matter_id") or ""),
        matter_query=str(args.get("matter_query") or context.query),
        allow_single=False,
    )
    if matter is None:
        return clarification_response(
            context,
            matter_error or "Which matter should I approve?",
            questions=_matter_question_options(context),
        )
    public = matter_view.public_matter(matter)
    matter_id = str(matter.get("id") or "")
    title = _matter_title(public)
    return action_request_response(
        context,
        action="approve_matter",
        domain="approval",
        label="Approve Matter",
        message="I can approve this matter after hard confirmation. The existing approval route will enforce owner scope and approval blockers.",
        target_tab="review",
        requires_confirmation=True,
        side_effects=("approve_matter",),
        params={"matter_id": matter_id},
        route={"method": "POST", "url": f"/api/matters/{matter_id}/approve"},
        matter=_action_matter_ref(public),
        human_summary=f'Approve the review for "{title}". Server approval blockers still apply.',
        risk_tier="outbound_or_destructive",
    )


def send_redline_request_response(
    context: AssistantContext,
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    matter, matter_error = _resolve_matter(
        context,
        matter_id=str(args.get("matter_id") or ""),
        matter_query=str(args.get("matter_query") or context.query),
        allow_single=False,
    )
    if matter is None:
        return clarification_response(
            context,
            matter_error or "Which matter should I send a redline for?",
            questions=_matter_question_options(context),
        )
    public = matter_view.public_matter(matter)
    recipient = str(public.get("recipient_email") or "").strip()
    matter_id = str(matter.get("id") or "")
    title = _matter_title(public)
    if not recipient:
        return unsupported_response(
            context.query,
            message=f'"{title}" does not have a resolved reply recipient, so I cannot prepare a send action.',
        )
    return action_request_response(
        context,
        action="send_redline",
        domain="gmail",
        label="Send Redline",
        message=(
            "I can send the redline only after hard confirmation. The existing Gmail send route will "
            "re-check readiness, ownership, stale review state, and recipient confirmation."
        ),
        target_tab="repository",
        requires_confirmation=True,
        side_effects=("send_redline_email",),
        params={"matter_id": matter_id, "recipient_source": "resolved_matter_reply_recipient"},
        route={"method": "POST", "url": "/api/gmail/send-redline"},
        matter={**_action_matter_ref(public), "resolved_recipient": recipient},
        human_summary=f'Send the redline for "{title}" to {recipient}. The route must confirm the same recipient.',
        risk_tier="outbound_or_destructive",
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


_TERM_RE = re.compile(r"[a-z0-9][a-z0-9._'-]*")
_RESOLUTION_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "against",
        "all",
        "an",
        "and",
        "approve",
        "approved",
        "clause",
        "clauses",
        "document",
        "email",
        "explain",
        "for",
        "from",
        "gmail",
        "how",
        "import",
        "in",
        "it",
        "matter",
        "nda",
        "ndas",
        "of",
        "refresh",
        "review",
        "run",
        "send",
        "sync",
        "summarize",
        "summary",
        "the",
        "this",
        "to",
        "was",
        "why",
        "with",
    }
)


def _query_terms(text: str, *, keep_stop_words: bool = False) -> list[str]:
    cleaned = neutralize_untrusted_text(str(text or ""), max_chars=MAX_QUERY_CHARS).casefold()
    terms = [term.strip("._'-") for term in _TERM_RE.findall(cleaned)]
    if keep_stop_words:
        return [term for term in terms if term]
    return [term for term in terms if term and term not in _RESOLUTION_STOP_WORDS and len(term) > 1]


def _resolve_matter(
    context: AssistantContext,
    *,
    matter_id: str = "",
    matter_query: str = "",
    allow_single: bool = True,
) -> tuple[dict[str, Any] | None, str]:
    matter_id = matter_id.strip()
    if matter_id:
        matter = context.repository.get_matter(matter_id, owner_user_id=context.owner_user_id)
        if matter is not None:
            return matter, ""
        return None, "I could not find an owner-scoped matter with that id."

    matters = context.owner_matters
    if not matters:
        return None, "No owner-scoped matters are available."
    terms = _query_terms(matter_query)
    if not terms:
        if allow_single and len(matters) == 1:
            return matters[0], ""
        return None, "Which matter should I use?"

    scored = [
        (_matter_match_score(matter, terms), matter)
        for matter in matters
    ]
    scored = [(score, matter) for score, matter in scored if score > 0]
    if not scored:
        return None, "I could not match that to one of your matters."
    scored.sort(key=lambda item: (-item[0], str(item[1].get("created_at") or "")), reverse=False)
    best_score = scored[0][0]
    best = [matter for score, matter in scored if score == best_score]
    if len(best) > 1:
        return None, "That matched more than one matter. Please choose the exact matter."
    return best[0], ""


def _matter_match_score(matter: Mapping[str, Any], terms: Sequence[str]) -> int:
    public = matter_view.public_matter(dict(matter))
    fields = [
        str(matter.get("id") or ""),
        _matter_title(public),
        str(public.get("counterparty") or ""),
        str(public.get("sender") or ""),
        str(public.get("recipient_email") or ""),
        str(public.get("subject") or ""),
    ]
    haystack = " ".join(fields).casefold()
    score = sum(3 for term in terms if term in haystack)
    if terms and all(term in haystack for term in terms):
        score += 4
    matter_id = str(matter.get("id") or "").casefold()
    if matter_id and any(term == matter_id for term in terms):
        score += 100
    return score


def _resolve_clause(
    context: AssistantContext,
    matter: Mapping[str, Any],
    *,
    clause_id: str = "",
    clause_query: str = "",
) -> tuple[Mapping[str, Any] | None, str]:
    review_result = matter.get("review_result")
    clauses = review_result.get("clauses") if isinstance(review_result, Mapping) else []
    clause_list = [clause for clause in clauses if isinstance(clause, Mapping)] if isinstance(clauses, list) else []
    if not clause_list:
        return None, "This matter does not have stored review clauses to explain."

    clause_id = clause_id.strip()
    if clause_id:
        for clause in clause_list:
            if str(clause.get("id") or "").casefold() == clause_id.casefold():
                return clause, ""
        return None, "I could not find that clause in the matter's review result."

    terms = _query_terms(clause_query)
    scored = [(_clause_match_score(clause, terms), clause) for clause in clause_list]
    scored = [(score, clause) for score, clause in scored if score > 0]
    if scored:
        scored.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
        best_score = scored[0][0]
        best = [clause for score, clause in scored if score == best_score]
        if len(best) == 1:
            return best[0], ""

    flagged = [
        clause for clause in clause_list
        if _clause_decision(clause) in {"fail", "review"}
    ]
    if len(flagged) == 1 and _contains_any(context.lowered, ("flagged", "finding", "why", "explain")):
        return flagged[0], ""
    return None, "Which clause or finding should I explain?"


def _clause_match_score(clause: Mapping[str, Any], terms: Sequence[str]) -> int:
    if not terms:
        return 0
    fields = [
        str(clause.get("id") or ""),
        _clause_name(clause),
        _clause_reason(clause),
        str(clause.get("issue_type") or ""),
        str(clause.get("issue_label") or ""),
    ]
    proposed = clause.get("proposed_change")
    if isinstance(proposed, Mapping):
        fields.extend(
            [
                str(proposed.get("issue_summary") or ""),
                str(proposed.get("playbook_rationale") or ""),
            ]
        )
    haystack = " ".join(fields).casefold()
    score = sum(3 for term in terms if term in haystack)
    if terms and all(term in haystack for term in terms):
        score += 4
    clause_id = str(clause.get("id") or "").casefold()
    if clause_id and any(term == clause_id for term in terms):
        score += 100
    return score


def _clause_decision(clause: Mapping[str, Any]) -> str:
    decision = str(clause.get("decision") or "").strip().lower()
    if decision in {"pass", "review", "fail"}:
        return decision
    try:
        return review_state.clause_review_state(dict(clause)).get("decision", "review")
    except Exception:  # noqa: BLE001 - unknown review shape should fail safe.
        return "review"


def _clause_name(clause: Mapping[str, Any]) -> str:
    return str(clause.get("name") or clause.get("clause_name") or clause.get("id") or "Review finding")


def _clause_reason(clause: Mapping[str, Any]) -> str:
    proposed = clause.get("proposed_change")
    if isinstance(proposed, Mapping):
        for key in ("issue_summary", "playbook_rationale"):
            value = str(proposed.get(key) or "").strip()
            if value:
                return value
    for key in ("finding", "decision_reason", "reason", "rationale"):
        value = str(clause.get(key) or "").strip()
        if value:
            return value
    return ""


def _clause_evidence(clause: Mapping[str, Any]) -> dict[str, str]:
    proposed = clause.get("proposed_change")
    if isinstance(proposed, Mapping):
        evidence = proposed.get("evidence")
        if isinstance(evidence, Mapping):
            return {
                "quote": _clean_evidence_text(evidence.get("quote")),
                "paragraph_id": _clean_evidence_text(evidence.get("paragraph_id"), max_chars=80),
            }
    citation = clause.get("citation")
    if isinstance(citation, Mapping):
        quote = _clean_evidence_text(citation.get("quote"))
        paragraph_id = _clean_evidence_text(citation.get("paragraph_id"), max_chars=80)
        if quote or paragraph_id:
            return {"quote": quote, "paragraph_id": paragraph_id}
    structured = clause.get("structured_evidence")
    if isinstance(structured, list):
        for record in structured:
            if not isinstance(record, Mapping):
                continue
            quote = _clean_evidence_text(record.get("matched_text") or record.get("text"))
            paragraph_id = _clean_evidence_text(record.get("paragraph_id"), max_chars=80)
            if quote or paragraph_id:
                return {"quote": quote, "paragraph_id": paragraph_id}
    paragraphs = clause.get("evidence_paragraphs")
    if isinstance(paragraphs, list):
        for paragraph in paragraphs:
            if not isinstance(paragraph, Mapping):
                continue
            quote = _clean_evidence_text(paragraph.get("text"))
            paragraph_id = _clean_evidence_text(paragraph.get("id"), max_chars=80)
            if quote or paragraph_id:
                return {"quote": quote, "paragraph_id": paragraph_id}
    return {"quote": "", "paragraph_id": ""}


def _clean_evidence_text(value: object, *, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    return " ".join(neutralize_untrusted_text(str(value or ""), max_chars=max_chars).split())


def _playbook_clause_for(playbook: Mapping[str, Any], clause: Mapping[str, Any]) -> Mapping[str, Any] | None:
    clause_id = str(clause.get("id") or "").strip().casefold()
    clause_name = _clause_name(clause).casefold()
    for playbook_clause in _playbook_clauses(playbook):
        candidate_id = str(playbook_clause.get("id") or "").strip().casefold()
        candidate_name = str(playbook_clause.get("name") or playbook_clause.get("title") or "").strip().casefold()
        if clause_id and candidate_id == clause_id:
            return playbook_clause
        if clause_name and candidate_name and candidate_name == clause_name:
            return playbook_clause
    return None


def _playbook_position(clause: Mapping[str, Any] | None) -> dict[str, Any]:
    if clause is None:
        return {"summary": "No matching Playbook clause was found for this finding."}
    rules = clause.get("rules") if isinstance(clause.get("rules"), Mapping) else {}
    approved_positions = clause.get("approved_positions")
    if not isinstance(approved_positions, list):
        approved_positions = rules.get("approved_positions") if isinstance(rules.get("approved_positions"), list) else []
    approved_options = rules.get("approved_options") if isinstance(rules.get("approved_options"), list) else []
    option_labels = [
        str(option.get("label") or option.get("id") or "").strip()
        for option in approved_options
        if isinstance(option, Mapping) and str(option.get("label") or option.get("id") or "").strip()
    ]
    preferred = str(clause.get("preferred") or clause.get("preferred_law") or rules.get("preferred") or "").strip()
    wording = str(clause.get("wording") or rules.get("wording") or clause.get("description") or "").strip()
    summary_parts = []
    if wording:
        summary_parts.append(wording)
    if approved_positions:
        summary_parts.append("Approved position: " + "; ".join(str(item) for item in approved_positions[:3]))
    if option_labels:
        summary_parts.append("Approved options: " + ", ".join(option_labels[:6]))
    if preferred:
        summary_parts.append(f"Preferred: {preferred}")
    return {
        "clause_id": str(clause.get("id") or ""),
        "clause_name": str(clause.get("name") or clause.get("title") or clause.get("id") or ""),
        "summary": " ".join(summary_parts) or "The Playbook clause is present but has no short display summary.",
        "approved_positions": [str(item) for item in approved_positions[:6]],
        "approved_options": option_labels[:10],
        "preferred": preferred,
    }


def _plain_language_clause_explanation(
    *,
    clause_name: str,
    verdict: str,
    reason: str,
    evidence: Mapping[str, str],
    playbook_position: Mapping[str, Any],
) -> str:
    verdict_text = {
        "pass": "passed",
        "review": "needs human review",
        "fail": "failed and needs a redline or approval decision",
    }.get(verdict, "needs review")
    parts = [f"The review says this {clause_name} finding {verdict_text}."]
    if reason:
        parts.append(f"Reason: {reason}")
    playbook_summary = str(playbook_position.get("summary") or "").strip()
    if playbook_summary:
        parts.append(f"Playbook position: {playbook_summary}")
    quote = str(evidence.get("quote") or "").strip()
    if quote:
        parts.append(f"Evidence: {quote}")
    return " ".join(parts)


def _workflow_next_action(workflow_state: Mapping[str, Any]) -> str:
    next_action = workflow_state.get("next_action")
    if isinstance(next_action, Mapping):
        return str(next_action.get("label") or next_action.get("description") or "").strip()
    return ""


def _matter_risk_bits(public: Mapping[str, Any], review: Mapping[str, Any]) -> list[str]:
    risks: list[str] = []
    counts = review.get("counts") if isinstance(review.get("counts"), Mapping) else {}
    failed = _safe_int(public.get("requirements_failed") or counts.get("check"))
    needs_review = _safe_int(public.get("requirements_needs_review") or counts.get("review"))
    if failed:
        risks.append(f"{failed} failed requirement{'s' if failed != 1 else ''}")
    if needs_review:
        risks.append(f"{needs_review} requirement{'s' if needs_review != 1 else ''} need human review")
    if public.get("send_block_reason"):
        risks.append(str(public.get("send_block_reason")))
    workflow_state = public.get("workflow_state")
    if isinstance(workflow_state, Mapping) and workflow_state.get("needs_attention") is True:
        reason = _workflow_next_action(workflow_state)
        risks.append(reason or "workflow needs attention")
    return risks


def _safe_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _search_system_hits(context: AssistantContext, query: str) -> list[dict[str, Any]]:
    terms = _query_terms(query, keep_stop_words=False)
    if not terms:
        return []
    hits: list[tuple[int, dict[str, Any]]] = []
    for matter in context.owner_matters:
        public = matter_view.public_matter(matter)
        title = _matter_title(public)
        matter_fields = [
            title,
            str(public.get("counterparty") or ""),
            str(public.get("sender") or ""),
            str(matter.get("extracted_text") or ""),
            str(public.get("message_snippet") or ""),
        ]
        score = _text_score(matter_fields, terms)
        if score:
            hits.append(
                (
                    score,
                    {
                        "type": "matter_content",
                        "matter_id": str(matter.get("id") or ""),
                        "title": title,
                        "snippet": _best_snippet(matter_fields, terms),
                        "score": score,
                    },
                )
            )
        review_result = matter.get("review_result")
        clauses = review_result.get("clauses") if isinstance(review_result, Mapping) else []
        if isinstance(clauses, list):
            for clause in clauses:
                if not isinstance(clause, Mapping):
                    continue
                clause_fields = [
                    str(clause.get("id") or ""),
                    _clause_name(clause),
                    _clause_reason(clause),
                    str(_clause_evidence(clause).get("quote") or ""),
                ]
                clause_score = _text_score(clause_fields, terms)
                if clause_score:
                    hits.append(
                        (
                            clause_score + 2,
                            {
                                "type": "review_clause",
                                "matter_id": str(matter.get("id") or ""),
                                "title": title,
                                "clause_id": str(clause.get("id") or ""),
                                "clause_name": _clause_name(clause),
                                "verdict": _clause_decision(clause),
                                "snippet": _best_snippet(clause_fields, terms),
                                "score": clause_score + 2,
                            },
                        )
                    )
    for clause in _playbook_clauses(context.playbook):
        fields = list(_playbook_search_fields(clause))
        score = _text_score(fields, terms)
        if score:
            hits.append(
                (
                    score + 1,
                    {
                        "type": "playbook_clause",
                        "source": "playbook",
                        "title": str(clause.get("name") or clause.get("title") or clause.get("id") or "Playbook clause"),
                        "clause_id": str(clause.get("id") or ""),
                        "snippet": _best_snippet(fields, terms),
                        "score": score + 1,
                    },
                )
            )
    hits.sort(key=lambda item: (-item[0], str(item[1].get("title") or "")))
    return [hit for _score, hit in hits[:MAX_SEARCH_HITS]]


def _playbook_search_fields(clause: Mapping[str, Any]) -> list[str]:
    fields = [
        str(clause.get("id") or ""),
        str(clause.get("name") or clause.get("title") or ""),
        str(clause.get("description") or ""),
        str(clause.get("wording") or ""),
        str(clause.get("purpose") or ""),
    ]
    rules = clause.get("rules")
    if isinstance(rules, Mapping):
        fields.extend(_flatten_strings(rules))
    fields.extend(_flatten_strings(clause.get("approved_positions")))
    return fields


def _flatten_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_flatten_strings(item))
        return strings
    if isinstance(value, list):
        strings = []
        for item in value:
            strings.extend(_flatten_strings(item))
        return strings
    return []


def _text_score(fields: Sequence[str], terms: Sequence[str]) -> int:
    haystack = " ".join(str(field or "") for field in fields).casefold()
    if not haystack:
        return 0
    matched = [term for term in terms if term in haystack]
    if not matched:
        return 0
    score = len(matched)
    if len(matched) == len(terms):
        score += 3
    return score


def _best_snippet(fields: Sequence[str], terms: Sequence[str]) -> str:
    for field in fields:
        snippet = _snippet_around_terms(str(field or ""), terms)
        if snippet:
            return snippet
    return ""


def _snippet_around_terms(text: str, terms: Sequence[str]) -> str:
    cleaned = " ".join(neutralize_untrusted_text(text, max_chars=4000).split())
    if not cleaned:
        return ""
    lowered = cleaned.casefold()
    positions = [lowered.find(term) for term in terms if term and lowered.find(term) >= 0]
    if not positions:
        return ""
    index = min(positions)
    start = max(0, index - 70)
    end = min(len(cleaned), index + MAX_SNIPPET_CHARS)
    prefix = "..." if start else ""
    suffix = "..." if end < len(cleaned) else ""
    return f"{prefix}{cleaned[start:end]}{suffix}"[:MAX_SNIPPET_CHARS]


def _search_hit_citations(hits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for hit in hits[:MAX_CITATIONS]:
        citation = {
            "source": str(hit.get("type") or hit.get("source") or "system"),
            "title": str(hit.get("title") or ""),
        }
        matter_id = str(hit.get("matter_id") or "")
        clause_id = str(hit.get("clause_id") or "")
        if matter_id:
            citation["matter_id"] = matter_id
        if clause_id:
            citation["clause_id"] = clause_id
        citations.append(citation)
    return citations


def _trusted_how_it_works_knowledge() -> dict[str, dict[str, Any]]:
    return {
        "review": {
            "text": (
                "Review extracts the uploaded matter, runs the active review engine against the published Playbook, "
                "stores clause verdicts/evidence, and keeps refresh/approval/send behind the existing matter routes."
            ),
            "steps": [
                "Read the stored document text and paragraph structure.",
                "Assess clauses against the active published Playbook.",
                "Store verdicts, evidence, proposed changes, and workflow state on the owner-scoped matter.",
                "Require human review or approval before export/send when the stored gates say so.",
            ],
            "security": "Matter reads are owner-scoped. Refresh, approval, export, and send use existing guarded matter routes.",
            "citations": [
                {"source": "code", "title": "nda_automation/routes/matters.py"},
                {"source": "code", "title": "nda_automation/review_engine.py"},
            ],
        },
        "generation": {
            "text": (
                "Generation starts from structured intake, selects approved Playbook positions, runs the generator safety gate, "
                "and saves the generated document through the generation workflow route."
            ),
            "steps": [
                "Collect structured intake in the Generator.",
                "Build the NDA from the active entity/playbook configuration.",
                "Run the generation self-check and safety gate.",
                "Persist the generated matter/artifact only through /api/generate-nda.",
            ],
            "security": "The assistant can prefill draft context, but generation still goes through the existing generation route and safety gate.",
            "citations": [{"source": "code", "title": "nda_automation/routes/generation.py"}],
        },
        "playbook": {
            "text": (
                "The Playbook is the trusted source for clause positions. Published Playbook runtime metadata is used by review, "
                "generation, staleness checks, and approval blockers."
            ),
            "steps": [
                "Author draft Playbook changes in the Playbook workspace.",
                "Publish to make a validated Playbook snapshot active.",
                "Review and generation read the active snapshot, not matter-provided instructions.",
                "Staleness checks detect when a matter was reviewed against an older snapshot.",
            ],
            "security": "Playbook explanations come from trusted Playbook runtime data, not counterparty matter text.",
            "citations": [{"source": "code", "title": "nda_automation/playbook_runtime.py"}],
        },
        "gmail": {
            "text": (
                "Gmail import and redline send are separate guarded workflows. Import is owner-scoped. Send requires explicit "
                "confirmation and the route verifies the recipient before sending."
            ),
            "steps": [
                "Import uses the connected user's Gmail scope and owner id.",
                "Outbound redline send resolves the matter recipient server-side.",
                "The sender must confirm the exact recipient.",
                "The route re-checks review freshness and send blockers before emailing.",
            ],
            "security": "The model cannot set the send recipient; the route enforces recipient confirmation and Gmail readiness.",
            "citations": [{"source": "code", "title": "nda_automation/routes/gmail.py"}],
        },
        "assistant": {
            "text": (
                "The dashboard assistant reads owner-scoped app state, returns typed responses, and only proposes side-effectful "
                "actions. The browser confirmation then calls existing guarded routes."
            ),
            "steps": [
                "Classify the user's request or let the model choose a registered tool.",
                "Run owner-scoped read tools or build a typed action request.",
                "Force confirmation whenever side_effects is non-empty.",
                "Execute only after the user confirms, through existing app endpoints.",
            ],
            "security": "Untrusted matter text is neutralized for display/search and never becomes an instruction or authority source.",
            "citations": [{"source": "code", "title": "nda_automation/dashboard_assistant.py"}],
        },
    }


def _how_it_works_topic(text: str) -> str:
    lowered = text.casefold()
    if _contains_any(lowered, ("generate", "draft", "generator")):
        return "generation"
    if "playbook" in lowered:
        return "playbook"
    if _contains_any(lowered, ("gmail", "email", "send", "sync", "import")):
        return "gmail"
    if _contains_any(lowered, ("assistant", "you work", "tools")):
        return "assistant"
    return "review"


def _action_matter_ref(public_matter: Mapping[str, Any]) -> dict[str, Any]:
    workflow_state = public_matter.get("workflow_state") if isinstance(public_matter.get("workflow_state"), Mapping) else {}
    return {
        "id": str(public_matter.get("id") or ""),
        "title": _matter_title(public_matter),
        "workflow_phase": str(workflow_state.get("phase") or ""),
        "workflow_status": str(workflow_state.get("status") or ""),
    }


def _matter_question_options(context: AssistantContext) -> list[str]:
    return [_matter_title(matter_view.public_matter(matter)) for matter in context.owner_matters[:5]]


def _clause_question_options(matter: Mapping[str, Any]) -> list[str]:
    review_result = matter.get("review_result")
    clauses = review_result.get("clauses") if isinstance(review_result, Mapping) else []
    if not isinstance(clauses, list):
        return []
    return [_clause_name(clause) for clause in clauses if isinstance(clause, Mapping)][:5]


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


def _looks_like_explain_review_finding_request(lowered: str) -> bool:
    return _contains_any(lowered, ("explain", "why", "what does", "what is")) and _contains_any(
        lowered,
        ("flagged", "finding", "clause", "verdict", "review result", "failed", "needs review"),
    )


def _looks_like_summarize_matter_request(lowered: str) -> bool:
    return _contains_any(lowered, ("summarize", "summary", "brief me", "state of", "status of")) and _contains_any(
        lowered,
        ("matter", "nda", "document", "deal", "contract"),
    )


def _looks_like_search_system_request(lowered: str) -> bool:
    return _contains_any(lowered, ("search", "find", "look for", "where")) and _contains_any(
        lowered,
        ("system", "everything", "contents", "content", "clauses", "playbook", "all matters", "whole"),
    )


def _looks_like_how_it_works_request(lowered: str) -> bool:
    return _contains_any(lowered, ("how does", "how do", "how is", "how are", "explain how")) and _contains_any(
        lowered,
        ("review", "generation", "generate", "playbook", "gmail", "assistant", "work"),
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


def _looks_like_send_redline_request(lowered: str) -> bool:
    return _contains_any(lowered, ("send", "email")) and _contains_any(
        lowered,
        ("redline", "redlined", "reviewed nda", "reviewed document"),
    )


def _looks_like_approve_matter_request(lowered: str) -> bool:
    return _contains_any(lowered, ("approve", "sign off", "mark approved")) and _contains_any(
        lowered,
        ("matter", "review", "nda", "document", "contract"),
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
        name="explain_how_it_works",
        domain="assistant",
        intent="how_it_works",
        description="Explain review, generation, Playbook, Gmail, or assistant workflows from trusted code-owned knowledge.",
        matcher=lambda context: _looks_like_how_it_works_request(context.lowered),
        handler=explain_how_it_works_response,
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
        name="explain_review_finding",
        domain="review",
        intent="review_finding_explanation",
        description="Explain a matter review clause verdict, evidence, and Playbook position from owner-scoped review results.",
        matcher=lambda context: _looks_like_explain_review_finding_request(context.lowered),
        handler=explain_review_finding_response,
    ),
    AssistantCapability(
        name="summarize_matter",
        domain="repository",
        intent="matter_summary",
        description="Summarize an owner-scoped matter's state, risks, and next action.",
        matcher=lambda context: _looks_like_summarize_matter_request(context.lowered),
        handler=summarize_matter_response,
    ),
    AssistantCapability(
        name="search_system",
        domain="system_search",
        intent="system_search",
        description="Search owner-scoped matter contents, review clauses, and the trusted Playbook.",
        matcher=lambda context: _looks_like_search_system_request(context.lowered),
        handler=search_system_response,
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
        description="Prepare a confirmation-gated Gmail sync/import request through /api/gmail/import.",
        matcher=lambda context: _looks_like_gmail_sync_request(context.lowered),
        handler=gmail_sync_request_response,
        side_effectful=True,
    ),
    AssistantCapability(
        name="send_redline",
        domain="gmail",
        intent="action_request",
        description="Prepare a hard-confirmation redline-send request through the existing Gmail send route.",
        matcher=lambda context: _looks_like_send_redline_request(context.lowered),
        handler=send_redline_request_response,
        side_effectful=True,
    ),
    AssistantCapability(
        name="approve_matter",
        domain="approval",
        intent="action_request",
        description="Prepare a hard-confirmation matter approval request through the existing approval route.",
        matcher=lambda context: _looks_like_approve_matter_request(context.lowered),
        handler=approve_matter_request_response,
        side_effectful=True,
    ),
    AssistantCapability(
        name="review_workflow",
        domain="review",
        intent="action_request",
        description="Prepare a confirmation-gated matter review refresh request through the existing review route.",
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
