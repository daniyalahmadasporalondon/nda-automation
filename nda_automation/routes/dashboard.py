"""Dashboard smart-search and assistant routes.

POST /api/dashboard/search-intent translates a natural-language query into a
validated, structured filter spec (see ``dashboard_search_intent``). The model's
ONLY output is the filter spec -- it never sees or returns matter data -- and the
spec is validated against a fixed allowlist before it leaves the server. The
frontend then applies the validated spec to the real matters deterministically.

Auth/ownership: this runs after ``_authorize_request`` (auth) + CSRF + rate-limit,
exactly like the other POST routes. It reads no matter data at all (the AI sees only
the query string), so there is nothing tenant-scoped to leak here; ownership stays
on the frontend's existing ``state.matters`` (already tenant-filtered by the matters
list route) where the spec is applied.

Graceful degradation: when AI is disabled / unconfigured / the call fails, the
route first returns a deterministic local filter for common queries. Only
unmappable queries return a clean ``{"filters": null, "fallback": true,
"reason": "ai_unavailable"}`` with HTTP 200 (never a 500), so the frontend falls
back to v1 keyword search.

POST /api/dashboard/assistant is the read-only command-bar seam. It can delegate
search/filter intents to the search-intent resolver, answer a small set of
repository questions from real scoped matter facts, or return confirmation-required
action requests. It never generates, sends, exports, deletes, approves, or otherwise
mutates a matter.
"""
from __future__ import annotations

from .. import dashboard_assistant, dashboard_search_intent, telemetry
from ..matter_repository import DiskMatterRepository, MatterRepository, MatterRepositoryError
from .common import request_owner_user_id

# A query is short. Cap the accepted length so a giant body can't be used to drive a
# huge prompt; the translator caps again internally.
MAX_QUERY_CHARS = 2000


def handle_dashboard_search_intent(handler) -> None:
    telemetry.increment("dashboard_search_intent_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    raw_query = payload.get("query", "")
    if not isinstance(raw_query, str):
        handler._send_json({"error": "query must be a string."}, status=400)
        return
    query = raw_query.strip()[:MAX_QUERY_CHARS]

    result = resolve_search_intent(query, transport=_search_intent_transport(handler))
    handler._send_json(result)


def handle_dashboard_assistant(handler) -> None:
    telemetry.increment("dashboard_assistant_requests")
    payload = handler._read_json_payload()
    if payload is None:
        return

    raw_query = payload.get("query", "")
    if not isinstance(raw_query, str):
        handler._send_json({"error": "query must be a string."}, status=400)
        return
    query = raw_query.strip()[:MAX_QUERY_CHARS]
    repository = _repository(handler)
    try:
        result = dashboard_assistant.handle_dashboard_assistant_command(
            query,
            repository=repository,
            owner_user_id=request_owner_user_id(handler),
            search_resolver=lambda search_query: resolve_search_intent(
                search_query,
                transport=_search_intent_transport(handler),
            ),
            ai_model=_dashboard_assistant_model(handler),
        )
    except MatterRepositoryError as error:
        handler._send_json({"error": str(error)}, status=500)
        return

    handler._send_json(result)


def resolve_search_intent(query: str, *, transport=None) -> dict:
    try:
        return dashboard_search_intent.translate_search_intent(query, transport=transport)
    except dashboard_search_intent.DashboardSearchIntentUnavailableError:
        deterministic = dashboard_search_intent.deterministic_search_intent(
            query,
            reason=dashboard_search_intent.FALLBACK_REASON_AI_UNAVAILABLE,
        )
        if not dashboard_search_intent.filter_spec_is_empty(deterministic["filters"]):
            return deterministic
        # AI off / unconfigured / provider failed, and the local parser could not
        # map the query -> graceful fallback signal (200), never a crash. The
        # frontend falls back to v1 keyword search.
        return {
            "filters": None,
            "fallback": True,
            "reason": dashboard_search_intent.FALLBACK_REASON_AI_UNAVAILABLE,
        }


def _repository(handler) -> MatterRepository:
    repository = getattr(handler, "matter_repository", None)
    if repository is not None:
        return repository
    return DiskMatterRepository()


def _search_intent_transport(handler):
    """Test seam: a handler may carry an injected transport (no network).

    Production handlers don't set this, so the translator builds the real OpenRouter
    transport from the configured reviewer settings.
    """
    return getattr(handler, "dashboard_search_intent_transport", None)


def _dashboard_assistant_model(handler):
    """Test seam: route tests may inject an assistant model without live API calls."""
    return getattr(handler, "dashboard_assistant_model", None)
