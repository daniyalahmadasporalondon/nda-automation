from __future__ import annotations

import base64
import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer

from nda_automation import server as server_module
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.server import NdaAutomationHandler


class QuietHandler(NdaAutomationHandler):
    def log_message(self, *args, **kwargs):
        return


def _create_matter(repo: InMemoryMatterRepository, **overrides):
    kwargs = {
        "source_filename": "Route NDA.docx",
        "document_bytes": b"PK\x03\x04 fake docx bytes",
        "extracted_text": "This Agreement is mutual.",
        "review_result": {"clauses": [{"id": "mutuality", "decision": "pass"}]},
        "triage": {"triage_status": "review"},
        "source_type": "manual_upload",
        "board_column": "in_review",
    }
    kwargs.update(overrides)
    return repo.create_matter(**kwargs)


class DashboardAssistantRouteTests(unittest.TestCase):
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
        self.repository = InMemoryMatterRepository()
        QuietHandler.matter_repository = self.repository

    def tearDown(self):
        if hasattr(QuietHandler, "matter_repository"):
            del QuietHandler.matter_repository

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

    def assistant(self, query, *, headers=None):
        return self.request("POST", "/api/dashboard/assistant", {"query": query}, headers=headers)

    def test_requires_auth(self):
        with self._env(self.auth_env()):
            status, payload, _ = self.assistant("How many are in review?")

        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], server_module.AUTH_REQUIRED_MESSAGE)

    def test_answers_repository_question_with_owner_scope(self):
        _create_matter(self.repository, owner_user_id="nda-admin", source_filename="Mine NDA.docx")
        _create_matter(self.repository, owner_user_id="other-user", source_filename="Other NDA.docx")

        with self._env(self.auth_env()):
            status, payload, _ = self.assistant(
                "How many are in review?",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["intent"], "repository_question")
        self.assertEqual(payload["question"], "count_in_review")
        self.assertEqual(payload["answer"]["count"], 1)
        self.assertEqual([citation["title"] for citation in payload["citations"]], ["Mine NDA"])

    def test_last_sent_repository_question_uses_send_stamps(self):
        sent = _create_matter(
            self.repository,
            owner_user_id="nda-admin",
            source_filename="Sent NDA.docx",
            board_column="sent",
        )
        self.repository.update_matter_fields(
            sent["id"],
            {
                "last_outbound_at": "2026-04-05T09:00:00+00:00",
                "last_outbound_to": "legal@example.com",
            },
            owner_user_id="nda-admin",
        )

        with self._env(self.auth_env()):
            status, payload, _ = self.assistant(
                "When was the last NDA sent to me?",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["intent"], "repository_question")
        self.assertEqual(payload["question"], "last_sent")
        self.assertEqual(payload["answer"]["sent_at"], "2026-04-05T09:00:00+00:00")
        self.assertEqual(payload["answer"]["recipient"], "legal@example.com")
        self.assertEqual(payload["citations"][0]["matter_id"], sent["id"])

    def test_generate_nda_command_is_confirmation_required_and_does_not_persist(self):
        with self._env(self.auth_env()):
            status, payload, _ = self.assistant(
                "Generate an NDA for Acme",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["intent"], "draft_action_request")
        self.assertEqual(payload["action"], "open_generator")
        self.assertIs(payload["requires_confirmation"], True)
        self.assertEqual(payload["side_effects"], [])
        self.assertEqual(self.repository.list_matters(owner_user_id="nda-admin"), [])

    def test_search_filter_intent_reuses_existing_search_response_shape(self):
        with self._env(self.auth_env() | {"OPENROUTER_API_KEY": ""}):
            status, payload, _ = self.assistant(
                "Show Acme pending approval",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["intent"], "search_filter")
        self.assertEqual(payload["search"]["filters"]["status"], "awaiting_approval")
        self.assertEqual(payload["search"]["filters"]["text"], "Acme")
        self.assertTrue(payload["search"]["deterministic"])

    def test_unsupported_intent_returns_clear_response(self):
        with self._env(self.auth_env()):
            status, payload, _ = self.assistant(
                "Tell me a joke",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["intent"], "unsupported")
        self.assertIn("cannot do that request yet", payload["message"].lower())

    def test_non_string_query_is_a_400(self):
        with self._env(self.auth_env()):
            status, payload, _ = self.request(
                "POST",
                "/api/dashboard/assistant",
                {"query": 42},
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "query must be a string.")

    def _env(self, env):
        return _EnvPatch(env)


class _EnvPatch:
    def __init__(self, env):
        self.env = env
        self._original = {}

    def __enter__(self):
        for key, value in self.env.items():
            self._original[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, exc_type, exc, tb):
        for key, value in self._original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False
