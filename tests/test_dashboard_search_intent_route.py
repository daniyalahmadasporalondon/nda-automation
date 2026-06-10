"""Tests for POST /api/dashboard/search-intent — the v2 AI smart-search bar.

These drive the real HTTP handler (a threaded test server) the way the dashboard
search bar will: post a natural-language query and get back a VALIDATED structured
filter spec. The AI transport is STUBBED (no network) by carrying an injected
transport on the handler, so the assertions are repeatable and key-free.

THE GOLDEN RULE under test: the model's only output is a filter spec, and the
SERVER VALIDATES it against the fixed schema before returning it. So this suite
proves:
* a natural-language query maps to the right validated filters,
* the validator DROPS an out-of-enum status/phase the model returns,
* min_age_days is CLAMPED (and 0/negative disabled),
* AI disabled / a provider failure degrades to deterministic local filters for
  common queries, else the graceful {fallback: true} signal with HTTP 200
  (never a 500),
* the empty query short-circuits with no AI call,
* auth is consistent with the other routes (unauthenticated -> 401).
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer

from nda_automation import dashboard_search_intent
from nda_automation import server as server_module
from nda_automation import telemetry
from nda_automation.server import NdaAutomationHandler


class QuietHandler(NdaAutomationHandler):
    def log_message(self, *args, **kwargs):
        return


# The transport the handler will carry. Each test sets ``response`` (the raw
# chat-completions payload) or ``error`` (an exception to raise) before issuing the
# request, and the captured request body is asserted to prove the model saw ONLY the
# query (never matter data).
class _StubIntentTransport:
    response: dict | None = None
    error: Exception | None = None
    last_request_body: dict | None = None

    def __call__(self, request_body):
        type(self).last_request_body = json.loads(json.dumps(request_body))
        if type(self).error is not None:
            raise type(self).error
        return type(self).response


def _content(spec: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(spec)}}]}


class DashboardSearchIntentRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        server_module._reset_rate_limits()
        telemetry.reset()
        _StubIntentTransport.response = None
        _StubIntentTransport.error = None
        _StubIntentTransport.last_request_body = None
        # Make every QuietHandler instance carry the stub transport (no network).
        QuietHandler.dashboard_search_intent_transport = _StubIntentTransport()

    def tearDown(self):
        if hasattr(QuietHandler, "dashboard_search_intent_transport"):
            del QuietHandler.dashboard_search_intent_transport

    # --- request helpers ------------------------------------------------- #
    def request(self, method, path, body=None, headers=None):
        request_headers = dict(headers or {})
        request_body = body
        if isinstance(body, dict):
            request_body = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(method, path, body=request_body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            payload = json.loads(raw.decode("utf-8")) if "application/json" in content_type else raw
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def basic_auth_headers(self, username="nda-admin", password="secret"):
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def auth_env(self):
        return {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
        }

    def search_intent(self, query, *, headers=None):
        return self.request(
            "POST", "/api/dashboard/search-intent", {"query": query}, headers=headers
        )

    # --- tests ----------------------------------------------------------- #
    def test_requires_auth(self):
        with self._env():
            # No Authorization header -> 401, never reaches the translator.
            status, payload, _ = self.search_intent("anything")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], server_module.AUTH_REQUIRED_MESSAGE)

    def test_query_maps_to_validated_filters_and_interpreted_line(self):
        _StubIntentTransport.response = _content(
            {
                "status": "awaiting_approval",
                "phase": "approval",
                "needs_attention": True,
                "text": "Acme",
                "min_age_days": 7,
                "sort": "oldest",
            }
        )
        with self._env():
            status, payload, _ = self.search_intent(
                "Acme docs awaiting approval, stuck over a week",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(status, 200, payload)
        self.assertNotIn("fallback", payload)
        filters = payload["filters"]
        self.assertEqual(filters["status"], "awaiting_approval")
        self.assertEqual(filters["phase"], "approval")
        self.assertEqual(filters["needs_attention"], True)
        self.assertEqual(filters["text"], "Acme")
        self.assertEqual(filters["min_age_days"], 7)
        self.assertEqual(filters["sort"], "oldest")
        # Dimensions the query didn't set stay null.
        self.assertIsNone(filters["human_gate"])
        self.assertIsNone(filters["has_issues"])
        # The interpreted line is human-readable and describes the applied filter.
        self.assertIn("Approval", payload["interpreted"])
        self.assertIn("older than 7 days", payload["interpreted"])

        # GOLDEN RULE: the model saw ONLY the query — never any matter data.
        body = _StubIntentTransport.last_request_body
        self.assertIsNotNone(body)
        user_message = body["messages"][1]["content"]
        self.assertIn("QUERY", user_message)
        self.assertIn("Acme docs awaiting approval", user_message)
        self.assertNotIn("matter", user_message.lower())  # no matter list leaked in

    def test_validator_drops_out_of_enum_status_and_phase(self):
        # The model hallucinates an invalid status + phase; the SERVER drops them.
        _StubIntentTransport.response = _content(
            {
                "status": "totally_made_up_status",
                "phase": "shipping",
                "text": "Globex",
            }
        )
        with self._env():
            status, payload, _ = self.search_intent(
                "Globex deal", headers=self.basic_auth_headers()
            )

        self.assertEqual(status, 200, payload)
        filters = payload["filters"]
        # Out-of-enum status/phase are DROPPED to null; the valid text survives.
        self.assertIsNone(filters["status"])
        self.assertIsNone(filters["phase"])
        self.assertEqual(filters["text"], "Globex")

    def test_min_age_days_is_clamped_and_bad_values_dropped(self):
        # A nonsense over-large min_age_days clamps to the ceiling; a junk bool
        # for needs_attention is dropped; a negative would disable (tested via 0).
        _StubIntentTransport.response = _content(
            {"min_age_days": 100000, "needs_attention": "sort of"}
        )
        with self._env():
            status, payload, _ = self.search_intent(
                "really old stuff", headers=self.basic_auth_headers()
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["filters"]["min_age_days"], dashboard_search_intent.MAX_MIN_AGE_DAYS)
        self.assertIsNone(payload["filters"]["needs_attention"])  # non-bool dropped

        # A zero/negative min_age_days disables the dimension entirely.
        _StubIntentTransport.response = _content({"min_age_days": 0})
        with self._env():
            status, payload, _ = self.search_intent(
                "fresh", headers=self.basic_auth_headers()
            )
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["filters"]["min_age_days"])

    def test_unmappable_query_returns_all_null_spec(self):
        # The model returns an all-null spec for a query it can't map; the box still
        # "works" (apply nothing), and the interpreted line is the catch-all.
        _StubIntentTransport.response = _content(dict(dashboard_search_intent.NULL_FILTER_SPEC))
        with self._env():
            status, payload, _ = self.search_intent(
                "asdfqwerty", headers=self.basic_auth_headers()
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(dashboard_search_intent.filter_spec_is_empty(payload["filters"]))
        self.assertEqual(payload["interpreted"], "All documents")

    def test_junk_model_output_collapses_to_null_spec_not_500(self):
        # The model returns non-JSON prose; we collapse to the all-null spec rather
        # than crashing — the box still works.
        _StubIntentTransport.response = {
            "choices": [{"message": {"content": "I'm not going to answer that."}}]
        }
        with self._env():
            status, payload, _ = self.search_intent(
                "anything", headers=self.basic_auth_headers()
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(dashboard_search_intent.filter_spec_is_empty(payload["filters"]))

    def test_ai_unavailable_returns_deterministic_filters_not_empty_results(self):
        # AI disabled (no injected transport AND settings disabled) still returns
        # a usable deterministic filter for common queries, instead of forcing the
        # frontend to keyword-search filler words like "show me docs".
        del QuietHandler.dashboard_search_intent_transport  # force the real path
        with self._env(extra={}), _patch_settings(enabled=False):
            status, payload, _ = self.search_intent(
                "show me Acme docs awaiting approval", headers=self.basic_auth_headers()
            )
        self.assertEqual(status, 200, payload)
        self.assertNotIn("fallback", payload)
        self.assertTrue(payload["deterministic"])
        self.assertEqual(payload["reason"], dashboard_search_intent.FALLBACK_REASON_AI_UNAVAILABLE)
        self.assertEqual(payload["filters"]["status"], "awaiting_approval")
        self.assertEqual(payload["filters"]["phase"], "approval")
        self.assertEqual(payload["filters"]["text"], "Acme")

    def test_ai_unavailable_still_returns_fallback_signal_when_unmappable(self):
        del QuietHandler.dashboard_search_intent_transport  # force the real path
        with self._env(extra={}), _patch_settings(enabled=False):
            status, payload, _ = self.search_intent(
                "show me documents", headers=self.basic_auth_headers()
            )
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload["filters"])
        self.assertTrue(payload["fallback"])
        self.assertEqual(payload["reason"], dashboard_search_intent.FALLBACK_REASON_AI_UNAVAILABLE)

    def test_provider_failure_degrades_to_fallback_signal(self):
        _StubIntentTransport.error = dashboard_search_intent.DashboardSearchIntentUnavailableError()
        with self._env():
            status, payload, _ = self.search_intent(
                "Acme docs awaiting approval", headers=self.basic_auth_headers()
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["deterministic"])
        self.assertEqual(payload["reason"], dashboard_search_intent.FALLBACK_REASON_AI_UNAVAILABLE)
        self.assertEqual(payload["filters"]["text"], "Acme")
        self.assertEqual(payload["filters"]["status"], "awaiting_approval")

    def test_unexpected_transport_error_still_degrades_gracefully(self):
        # Even a bare RuntimeError from the transport degrades to deterministic
        # local intent when the query can be mapped.
        _StubIntentTransport.error = RuntimeError("kaboom")
        with self._env():
            status, payload, _ = self.search_intent(
                "Vance pending approval", headers=self.basic_auth_headers()
            )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["deterministic"])
        self.assertEqual(payload["filters"]["status"], "awaiting_approval")
        self.assertEqual(payload["filters"]["text"], "Vance")

    def test_empty_query_short_circuits_with_no_ai_call(self):
        # An empty/whitespace query returns the all-null spec WITHOUT calling the AI.
        _StubIntentTransport.response = _content({"status": "awaiting_approval"})
        with self._env():
            status, payload, _ = self.search_intent("   ", headers=self.basic_auth_headers())
        self.assertEqual(status, 200, payload)
        self.assertTrue(dashboard_search_intent.filter_spec_is_empty(payload["filters"]))
        # The transport must NOT have been called for an empty query.
        self.assertIsNone(_StubIntentTransport.last_request_body)

    def test_non_string_query_is_a_400(self):
        with self._env():
            status, payload, _ = self.request(
                "POST",
                "/api/dashboard/search-intent",
                {"query": 42},
                headers=self.basic_auth_headers(),
            )
        self.assertEqual(status, 400, payload)
        self.assertIn("string", payload["error"])

    # --- env helper ------------------------------------------------------ #
    def _env(self, extra=None):
        env = self.auth_env()
        if extra is not None:
            env.update(extra)
        return _EnvPatch(env)


class _EnvPatch:
    def __init__(self, env):
        self.env = env
        self._original = {}

    def __enter__(self):
        for key, value in self.env.items():
            self._original[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *args):
        for key, original in self._original.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


class _patch_settings:
    """Patch dashboard_search_intent._ai_review_settings to a disabled/enabled stub."""

    def __init__(self, *, enabled):
        self.enabled = enabled
        self._original = None

    def __enter__(self):
        self._original = dashboard_search_intent._ai_review_settings
        dashboard_search_intent._ai_review_settings = lambda: {
            "enabled": self.enabled,
            "provider": "openrouter",
            "model": "x-ai/grok-4.3",
            "timeout_seconds": 20,
        }
        return self

    def __exit__(self, *args):
        dashboard_search_intent._ai_review_settings = self._original


if __name__ == "__main__":
    unittest.main()
