"""Dashboard smart-search (v2) routes.

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
"""
from __future__ import annotations

from .. import dashboard_search_intent, telemetry

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

    try:
        result = dashboard_search_intent.translate_search_intent(
            query, transport=_search_intent_transport(handler)
        )
    except dashboard_search_intent.DashboardSearchIntentUnavailableError:
        deterministic = dashboard_search_intent.deterministic_search_intent(
            query,
            reason=dashboard_search_intent.FALLBACK_REASON_AI_UNAVAILABLE,
        )
        if not dashboard_search_intent.filter_spec_is_empty(deterministic["filters"]):
            handler._send_json(deterministic)
            return
        # AI off / unconfigured / provider failed, and the local parser could not
        # map the query -> graceful fallback signal (200), never a crash. The
        # frontend falls back to v1 keyword search.
        handler._send_json(
            {
                "filters": None,
                "fallback": True,
                "reason": dashboard_search_intent.FALLBACK_REASON_AI_UNAVAILABLE,
            }
        )
        return

    handler._send_json(result)


def _search_intent_transport(handler):
    """Test seam: a handler may carry an injected transport (no network).

    Production handlers don't set this, so the translator builds the real OpenRouter
    transport from the configured reviewer settings.
    """
    return getattr(handler, "dashboard_search_intent_transport", None)
