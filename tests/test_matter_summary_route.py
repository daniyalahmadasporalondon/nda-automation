"""Tests for POST /api/matters/<id>/summary — the on-demand AI matter summary.

These drive the real HTTP handler (a threaded test server) the way the dashboard
search bar's Summarize affordance will: post to a matter's summary endpoint and get
back a grounded, plain-English summary. The AI transport is STUBBED (no network) by
patching matter_summary._OpenRouterSummaryTransport with a deterministic fake, so
the assertions are repeatable and key-free, exactly the way the AI-first review and
generation route tests inject their reviewers.

Coverage:
* a successful summary (200 with {summary, model, generated_at}, grounded in the
  matter's real document + review findings),
* the AI-disabled / unavailable path returns the FRIENDLY 503 error, never a 500,
* the ownership/auth check: another tenant's matter is a 404, and an unauthenticated
  caller is a 401.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from nda_automation import matter_store, matter_summary
from nda_automation import server as server_module
from nda_automation import telemetry
from nda_automation.checker import review_nda
from nda_automation.review_engine import ACTIVE_REVIEW_ENGINE_ENV
from nda_automation.server import NdaAutomationHandler

NDA_TEXT = (
    "MUTUAL NON-DISCLOSURE AGREEMENT\n"
    "This Agreement is between Acme Corp and Aspora Inc.\n"
    "1. Confidential Information means any information disclosed by either party.\n"
    "2. This Agreement is governed by the laws of England and Wales.\n"
    "3. The obligations survive for a period of three (3) years.\n"
    "4. Each party shall keep the other party's information confidential.\n"
)

STUB_SUMMARY_TEXT = (
    "Mutual NDA with Acme Corp. Governed by the laws of England and Wales; "
    "3-year survival term. The review flagged blocking issues. "
    "Recommendation: needs human review before sending."
)


class QuietHandler(NdaAutomationHandler):
    def log_message(self, *args, **kwargs):
        return


class _StubSummaryTransport:
    """A network-free stand-in for the OpenRouter summary transport.

    Captures the request body so a test can assert the grounded context actually
    reached the prompt, and returns a fixed chat-completions-shaped response.
    """

    last_request_body: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, request_body):
        type(self).last_request_body = json.loads(json.dumps(request_body))
        return {"choices": [{"message": {"content": STUB_SUMMARY_TEXT}}]}


class MatterSummaryRouteTests(unittest.TestCase):
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
        _StubSummaryTransport.last_request_body = None

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
            ACTIVE_REVIEW_ENGINE_ENV: "deterministic",
        }

    def matter_store_patches(self, data_dir):
        data_path = server_module.Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        )

    def seed_matter(self, *, owner_user_id="nda-admin", with_review=True, extracted_text=NDA_TEXT):
        review_result = review_nda(extracted_text) if with_review else {}
        if with_review and isinstance(review_result, dict):
            # The summary digest only seeds the LLM with review verdicts when an AI
            # review actually ran. Mark this seeded result as ai_first-executed so
            # the end-to-end grounding test reflects an AI-reviewed matter (the
            # deterministic-only path is covered by the matter_summary unit tests).
            review_result["active_review_engine"] = {"executed_engine": "ai_first"}
        return matter_store.create_matter(
            source_filename="acme-nda.txt",
            document_bytes=extracted_text.encode("utf-8"),
            extracted_text=extracted_text,
            review_result=review_result,
            triage={"triage_status": "review"},
            source_type="manual_upload",
            board_column="in_review",
            owner_user_id=owner_user_id,
        )

    def summarize(self, matter_id, *, headers=None):
        return self.request("POST", f"/api/matters/{matter_id}/summary", {}, headers=headers)

    # --- tests ----------------------------------------------------------- #
    def test_requires_auth(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()):
                matter = self.seed_matter()
                # No Authorization header -> 401, never reaches the summary path.
                status, payload, _ = self.summarize(matter["id"])
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], server_module.AUTH_REQUIRED_MESSAGE)

    def test_successful_summary_is_grounded_in_real_matter_data(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            # The matter-summary model is DECOUPLED from the reviewer: on the
            # default path it resolves via resolve_model("matter_summary") and the
            # ``model`` field of _ai_review_settings is no longer consulted (only
            # enable/provider/timeout ride shared AI settings). So pin the model
            # through its real knob, NDA_MATTER_SUMMARY_MODEL, and assert that
            # configured model is what reaches the response — the same role-resolver
            # path production uses.
            summary_env = {**self.auth_env(), "NDA_MATTER_SUMMARY_MODEL": "anthropic/claude-opus-4.8"}
            with p[0], p[1], p[2], patch.dict(os.environ, summary_env), patch.object(
                matter_summary, "_OpenRouterSummaryTransport", _StubSummaryTransport
            ), patch.object(
                matter_summary,
                "_ai_review_settings",
                lambda: {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-opus-4.8", "timeout_seconds": 20},
            ):
                matter = self.seed_matter()
                status, payload, _ = self.summarize(matter["id"], headers=self.basic_auth_headers())

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["summary"], STUB_SUMMARY_TEXT)
        self.assertEqual(payload["model"], "anthropic/claude-opus-4.8")
        self.assertTrue(payload["generated_at"])
        self.assertTrue(payload["grounded_in"]["document"])
        self.assertTrue(payload["grounded_in"]["review_findings"])

        # GROUNDING: the request body the transport saw must contain the matter's
        # REAL document text and review findings — never anything fabricated.
        body = _StubSummaryTransport.last_request_body
        self.assertIsNotNone(body)
        user_message = body["messages"][1]["content"]
        self.assertIn("England and Wales", user_message)  # from the real document
        self.assertIn("REVIEW_FINDINGS", user_message)
        self.assertIn("does_not_meet_requirements", user_message)  # real overall status
        # The grounding instruction is present in the system prompt.
        system_message = body["messages"][0]["content"]
        self.assertIn("not specified", system_message)
        self.assertIn("Do NOT invent", system_message)

    def test_ai_unavailable_returns_friendly_503_not_500(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            # AI disabled -> summarize_matter raises MatterSummaryUnavailableError ->
            # the route returns a friendly 503, NOT a 500 stack trace.
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()), patch.object(
                matter_summary,
                "_ai_review_settings",
                lambda: {"enabled": False, "provider": "openrouter", "model": "anthropic/claude-opus-4.8", "timeout_seconds": 20},
            ):
                matter = self.seed_matter()
                status, payload, _ = self.summarize(matter["id"], headers=self.basic_auth_headers())

        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"], matter_summary.SUMMARY_UNAVAILABLE_MESSAGE)

    def test_provider_failure_degrades_gracefully(self):
        class _BoomTransport(_StubSummaryTransport):
            def __call__(self, request_body):
                raise matter_summary.MatterSummaryUnavailableError()

        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()), patch.object(
                matter_summary, "_OpenRouterSummaryTransport", _BoomTransport
            ), patch.object(
                matter_summary,
                "_ai_review_settings",
                lambda: {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-opus-4.8", "timeout_seconds": 20},
            ):
                matter = self.seed_matter()
                status, payload, _ = self.summarize(matter["id"], headers=self.basic_auth_headers())

        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"], matter_summary.SUMMARY_UNAVAILABLE_MESSAGE)

    def test_other_tenants_matter_is_not_found(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()), patch.object(
                matter_summary, "_OpenRouterSummaryTransport", _StubSummaryTransport
            ), patch.object(
                matter_summary,
                "_ai_review_settings",
                lambda: {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-opus-4.8", "timeout_seconds": 20},
            ):
                # Matter owned by a DIFFERENT user than the authenticated caller.
                other_matter = self.seed_matter(owner_user_id="someone-else")
                status, payload, _ = self.summarize(other_matter["id"], headers=self.basic_auth_headers())
        # The ownership filter hides it: 404, not another tenant's summary.
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"], "NDA not found.")

    def test_missing_matter_is_not_found(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()):
                status, payload, _ = self.summarize("matter_doesnotexist", headers=self.basic_auth_headers())
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"], "NDA not found.")

    def test_matter_without_document_text_is_a_clear_400(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p = self.matter_store_patches(data_dir)
            with p[0], p[1], p[2], patch.dict(os.environ, self.auth_env()), patch.object(
                matter_summary, "_OpenRouterSummaryTransport", _StubSummaryTransport
            ), patch.object(
                matter_summary,
                "_ai_review_settings",
                lambda: {"enabled": True, "provider": "openrouter", "model": "anthropic/claude-opus-4.8", "timeout_seconds": 20},
            ):
                matter = self.seed_matter(with_review=False, extracted_text="   ")
                status, payload, _ = self.summarize(matter["id"], headers=self.basic_auth_headers())
        self.assertEqual(status, 400, payload)
        self.assertIn("no document text", payload["error"])


if __name__ == "__main__":
    unittest.main()
