import base64
import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from io import BytesIO
import urllib.error
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, call, patch
from zipfile import ZIP_DEFLATED, ZipFile
import xml.etree.ElementTree as ET

from nda_automation.checker import (
    EvidenceProvenanceError,
    PlaybookTemplateError,
    REVIEW_ENGINE_VERSION,
    load_playbook,
)
from nda_automation import ai_review
from nda_automation import app_settings
from nda_automation import document_rendering
from nda_automation import document_limits
from nda_automation.docx_export import DOCX_MIME
from nda_automation import export_service
from nda_automation import gmail_integration
from nda_automation import gmail_matter_outbox
from nda_automation import google_connection
from nda_automation import ingestion_service
from nda_automation import matter_store
from nda_automation import matter_view
from nda_automation import operational_settings_repository
from nda_automation import deployment as deployment_module
from nda_automation import server as server_module
from nda_automation import telemetry
from nda_automation import user_store
from nda_automation.review_engine import ACTIVE_REVIEW_ENGINE_ENV, ActiveReviewEngineError
from nda_automation.routes import matters as matter_routes
from nda_automation import playbook_runtime
from nda_automation.server import NdaAutomationHandler
from nda_automation.triage import triage_review_result
from tests.docx_redline_contract import assert_docx_redline_contract

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
SOURCE_EXPORT_REPORT_LEAKAGE_PHRASES = [
    "NDA Redline",
    "Review Notes",
    "Clause Findings",
    "Proposed Redline",
    "Overall status:",
    "Requirements passed:",
    "Requirements failed:",
    "Checked at:",
    "The Redlined NDA section contains native Word tracked changes.",
    "source paragraph",
]
PYPDF_AVAILABLE = importlib.util.find_spec("pypdf") is not None
requires_pypdf = unittest.skipUnless(PYPDF_AVAILABLE, "pypdf is not installed")


class QuietNdaAutomationHandler(NdaAutomationHandler):
    def log_message(self, format, *args):
        return


class _FakeUrlopen(BytesIO):
    """urlopen() stand-in: a context manager that yields a readable response."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietNdaAutomationHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    def setUp(self):
        server_module._reset_rate_limits()
        if hasattr(gmail_integration, "_clear_gmail_profile_cache_for_tests"):
            gmail_integration._clear_gmail_profile_cache_for_tests()
        if hasattr(server_module, "_clear_gmail_sync_backoff_for_tests"):
            server_module._clear_gmail_sync_backoff_for_tests()
        telemetry.reset()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        status, payload, _response_headers = self.request_with_headers(method, path, body=body, headers=headers)
        return status, payload

    def request_with_headers(self, method, path, body=None, headers=None):
        request_headers = headers or {}
        request_body = body
        if isinstance(body, dict):
            request_body = json.dumps(body).encode("utf-8")
            request_headers = {"Content-Type": "application/json", **request_headers}
        elif isinstance(body, str):
            request_body = body.encode("utf-8")

        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=request_body, headers=request_headers)
            response = connection.getresponse()
            raw_body = response.read()
            content_type = response.getheader("Content-Type", "")
            if "application/json" in content_type:
                payload = json.loads(raw_body.decode("utf-8"))
            else:
                payload = raw_body
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    @contextmanager
    def deterministic_review_requests(self):
        original = server_module.review_nda_with_active_engine

        def forced_deterministic_review(*args, **kwargs):
            kwargs["force_engine"] = "deterministic"
            return original(*args, **kwargs)

        with patch.object(server_module, "review_nda_with_active_engine", side_effect=forced_deterministic_review):
            yield

    @contextmanager
    def deterministic_matter_intake(self):
        # Force the EAGER intake review onto the OFFLINE deterministic engine so a
        # real (non-AI, no-network) review_result lands on the matter at create.
        # (defer_ai_review no longer yields a deterministic first-pass -- it creates
        # the matter UN-REVIEWED -- and the active-engine env only recognizes
        # ai_first, so we pin the engine by forcing the review call here.)
        from nda_automation import ingestion_service as _ingestion_service

        original_review = _ingestion_service.review_nda_with_active_engine

        def _forced_deterministic_review(*args, **kwargs):
            kwargs["force_engine"] = "deterministic"
            return original_review(*args, **kwargs)

        with patch.object(
            _ingestion_service,
            "review_nda_with_active_engine",
            side_effect=_forced_deterministic_review,
        ):
            yield

    def raw_http_request(self, request_text):
        with socket.create_connection((self.host, self.port), timeout=5) as connection:
            connection.sendall(request_text.encode("utf-8"))
            connection.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        raw_response = b"".join(chunks)
        header_bytes, _, body = raw_response.partition(b"\r\n\r\n")
        status_line = header_bytes.splitlines()[0].decode("iso-8859-1")
        status = int(status_line.split()[1])
        return status, json.loads(body.decode("utf-8"))

    def basic_auth_headers(self, username="nda-admin", password="secret"):
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def cookie_header(self, set_cookie):
        return set_cookie.split(";", 1)[0]

    def google_session_headers(self, *, subject="google-user-123", email="alice@example.com"):
        user = user_store.upsert_google_user({
            "sub": subject,
            "email": email,
            "name": "Alice Example",
            "picture": "https://example.com/alice.png",
        })
        token = user_store.create_session(user["id"])
        return {"Cookie": f"{user_store.SESSION_COOKIE_NAME}={token}"}, user

    @contextmanager
    def acquired_gmail_sync_lock(self):
        yield True

    def assert_saved_export_url_matches_response(self, headers, payload):
        self.assertEqual(headers["X-Export-Verified"], "word-package; track-revisions")
        self.assertIn("X-Export-URL", headers)
        route_status, route_payload, route_headers = self.request_with_headers("GET", headers["X-Export-URL"])

        self.assertEqual(route_status, 200)
        self.assertEqual(route_headers["Content-Type"], DOCX_MIME)
        self.assertEqual(route_headers["Content-Disposition"], headers["Content-Disposition"])
        self.assertEqual(route_payload, payload)
        self.assertNotIn("X-Export-Path", headers)

    def matter_store_patches(self, data_dir):
        data_path = server_module.Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        )

    def seed_reviewed_upload(
        self,
        source_bytes,
        *,
        filename="Seed NDA.docx",
        source_type="manual_upload",
        board_column="in_review",
        intake_metadata=None,
    ):
        """Seed a REVIEWED matter the way upload used to before defer_ai_review.

        The HTTP upload route now defers the AI review (defer_ai_review=True), so it
        no longer produces a review_result at create. Many export/send tests only
        need a matter that already carries a (deterministic) review + redlines as a
        fixture; this seeds exactly that via the ingestion function with the eager
        review path (defer_ai_review=False, default deterministic engine), inside
        whatever matter_store patches the caller has active. Returns the persisted
        matter dict (with review_result), mirroring the old upload-route side effect.
        """
        return ingestion_service.create_matter_from_document(
            filename=filename,
            document_bytes=source_bytes,
            source_type=source_type,
            board_column=board_column,
            intake_metadata=intake_metadata,
        )

    def active_playbook_review_runtime(self):
        runtime = playbook_runtime.ensure_active_playbook_runtime()
        return {
            "active_version_id": str(runtime.get("active_version_id") or ""),
            "active_hash": str(runtime.get("active_hash") or ""),
            "playbook_name": str(runtime.get("playbook_name") or ""),
            "playbook_version": str(runtime.get("playbook_version") or ""),
            "published_at": str(runtime.get("published_at") or ""),
            "published_by": str(runtime.get("published_by") or ""),
            "source": "active",
            "active_source": str(runtime.get("source") or ""),
        }

    def assert_review_payload_contract(self, payload, *, expected_source_type=None):
        for key in [
            "overall_status",
            "checked_at",
            "requirements_passed",
            "requirements_needs_review",
            "requirements_failed",
            "review_state",
            "paragraphs",
            "clauses",
            "redline_edits",
        ]:
            self.assertIn(key, payload)
        self.assertEqual(payload.get("evidence_trust"), {"status": "verified", "errors": []})
        self.assertIn(payload["overall_status"], {"meets_requirements", "needs_review", "does_not_meet_requirements"})
        self.assertIsInstance(payload["requirements_passed"], int)
        self.assertIsInstance(payload["requirements_needs_review"], int)
        self.assertIsInstance(payload["requirements_failed"], int)
        self.assertIsInstance(payload["review_state"], dict)
        self.assertEqual(payload["review_state"]["overall_status"], payload["overall_status"])
        self.assertEqual(payload["review_state"]["counts"]["pass"], payload["requirements_passed"])
        self.assertEqual(payload["review_state"]["counts"]["review"], payload["requirements_needs_review"])
        self.assertEqual(payload["review_state"]["counts"]["check"], payload["requirements_failed"])
        self.assertIsInstance(payload["review_state"].get("reason_codes"), list)
        self.assertIsInstance(payload["review_state"].get("reason_codes_by_state"), dict)
        self.assertIsInstance(payload["paragraphs"], list)
        self.assertIsInstance(payload["clauses"], list)
        self.assertIsInstance(payload["redline_edits"], list)

        paragraphs_by_id = {}
        for paragraph in payload["paragraphs"]:
            for key in ["id", "index", "text", "start", "end"]:
                self.assertIn(key, paragraph)
            self.assertIsInstance(paragraph["id"], str)
            self.assertIsInstance(paragraph["index"], int)
            self.assertIsInstance(paragraph["text"], str)
            self.assertIsInstance(paragraph["start"], int)
            self.assertIsInstance(paragraph["end"], int)
            paragraphs_by_id[paragraph["id"]] = paragraph

        for clause in payload["clauses"]:
            for key in [
                "id",
                "name",
                "requirement",
                "status",
                "decision",
                "decision_reason",
                "reason_code",
                "reason_codes",
                "passes",
                "needs_review",
                "review_state",
                "issue_type",
                "issue_label",
                "what_to_fix",
                "reason",
                "finding",
                "matched_paragraph_ids",
                "matched_text",
                "evidence",
                "evidence_paragraphs",
            ]:
                self.assertIn(key, clause, f"{clause.get('id', 'unknown')} missing {key}")
            self.assertIn(clause["status"], {"match", "check", "not_present"})
            self.assertIn(clause["decision"], {"pass", "review", "fail"})
            self.assertIsInstance(clause["passes"], bool)
            self.assertIsInstance(clause["needs_review"], bool)
            self.assertIsInstance(clause["reason_code"], str)
            self.assertIsInstance(clause["reason_codes"], list)
            self.assertTrue(clause["reason_codes"])
            self.assertEqual(clause["reason_codes"][0], clause["reason_code"])
            self.assertIsInstance(clause["review_state"], dict)
            self.assertEqual(clause["review_state"]["decision"], clause["decision"])
            self.assertEqual(clause["review_state"]["reason_code"], clause["reason_code"])
            self.assertEqual(clause["review_state"]["reason_codes"], clause["reason_codes"])
            if clause["decision"] == "review":
                self.assertEqual(clause["review_state"]["state"], "review")
                self.assertTrue(clause["review_state"]["blocks_send"])
            if clause["decision"] == "fail":
                self.assertEqual(clause["review_state"]["state"], "check")
                self.assertTrue(clause["review_state"]["requires_redline"])
            self.assertEqual(clause["reason"], clause["finding"])
            matched_ids = clause["matched_paragraph_ids"]
            self.assertIsInstance(matched_ids, list)
            expected_paragraphs = [paragraphs_by_id[paragraph_id] for paragraph_id in matched_ids]
            self.assertEqual(clause["matched_text"], "\n\n".join(paragraph["text"] for paragraph in expected_paragraphs))
            self.assertEqual(clause["evidence"], [paragraph["text"] for paragraph in expected_paragraphs])
            self.assertEqual(clause["evidence_paragraphs"], expected_paragraphs)

        for redline in payload["redline_edits"]:
            for key in ["id", "clause_id", "clause_name", "paragraph_id", "paragraph_index", "action", "action_label", "status", "original_text", "replacement_text", "reason"]:
                self.assertIn(key, redline)
            self.assertNotIn("target_position", redline)
            self.assertNotIn("selected_template_id", redline)
            self.assertIn(redline["paragraph_id"], paragraphs_by_id)
            self.assertEqual(redline["paragraph_index"], paragraphs_by_id[redline["paragraph_id"]]["index"])
            self.assertIn(redline["action"], {"replace_paragraph", "insert_after_paragraph", "delete_paragraph"})
            if redline["action"] == "insert_after_paragraph":
                self.assertEqual(redline["original_text"], "")
                self.assertIn("anchor_text", redline)
                self.assertIn("insert_text", redline)
                self.assertTrue(redline["insert_text"].strip())
            elif redline["action"] == "delete_paragraph":
                self.assertEqual(redline["replacement_text"], "")
                self.assertTrue(redline["original_text"].strip())
            else:
                self.assertTrue(redline["original_text"].strip())
                self.assertTrue(redline["replacement_text"].strip())
                self.assertIn("inline_diff_operations", redline)
                self.assertTrue(redline["inline_diff_operations"])
                for operation in redline["inline_diff_operations"]:
                    self.assertIn(operation["type"], {"same", "delete", "insert"})
                    self.assertIsInstance(operation["token"], str)
                for option in redline.get("template_options", []):
                    self.assertIn("inline_diff_operations", option)
                    self.assertTrue(option["inline_diff_operations"])

        if expected_source_type:
            self.assertEqual(payload["source"]["type"], expected_source_type)
            self.assertIn("extracted_text", payload)
            for paragraph in payload["paragraphs"]:
                self.assertIn("source_index", paragraph)

    def malformed_template_playbook(self):
        playbook = deepcopy(load_playbook())
        term = next(clause for clause in playbook["clauses"] if clause["id"] == "term_and_survival")
        term["redline_template"] = "Custom survival language capped at {unknown_placeholder}."
        return playbook

    def test_parse_matter_id_handles_suffixes_and_rejects_nested_paths(self):
        self.assertEqual(server_module._parse_matter_id("/api/matters/matter_123"), "matter_123")
        self.assertEqual(server_module._parse_matter_id("/api/matters/matter%20123"), "matter 123")
        self.assertEqual(
            server_module._parse_matter_id("/api/matters/matter_123/review", suffix="/review"),
            "matter_123",
        )
        self.assertIsNone(server_module._parse_matter_id("/api/matters/matter_123/stage", suffix="/review"))
        self.assertIsNone(server_module._parse_matter_id("/api/matters/"))
        self.assertIsNone(server_module._parse_matter_id("/api/matters/matter_123/stage"))
        self.assertIsNone(server_module._parse_matter_id("/api/matters/matter%2F123"))
        self.assertIsNone(server_module._parse_matter_id("/api/gmail/status"))

    def test_public_bind_requires_auth_even_without_explicit_flag(self):
        with patch.dict(os.environ, {
            "NDA_REQUIRE_AUTH": "",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "",
        }):
            self.assertFalse(server_module._auth_required_for_host("127.0.0.1"))
            self.assertFalse(server_module._auth_required_for_host("localhost"))
            self.assertTrue(server_module._auth_required_for_host("0.0.0.0"))
            self.assertTrue(server_module._auth_required_for_host("::"))

    def test_public_bind_refuses_startup_without_auth_credentials(self):
        with patch.dict(os.environ, {
            "NDA_REQUIRE_AUTH": "",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "",
        }):
            with self.assertRaisesRegex(RuntimeError, "Authentication is required"):
                server_module._validate_public_auth("0.0.0.0")

    def test_public_bind_accepts_startup_with_auth_credentials(self):
        with patch.dict(os.environ, {"NDA_REQUIRE_AUTH": "", "NDA_AUTH_USERNAME": "nda-admin", "NDA_AUTH_PASSWORD": "secret"}):
            server_module._validate_public_auth("0.0.0.0")

    def test_public_bind_accepts_startup_with_google_oauth(self):
        with patch.dict(os.environ, {
            "NDA_REQUIRE_AUTH": "",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "client-id",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
        }):
            server_module._validate_public_auth("0.0.0.0")
            deployment = server_module._deployment_status_for_host("0.0.0.0")

        checks = {check["id"]: check for check in deployment["checks"]}
        self.assertTrue(checks["auth"]["ok"])
        self.assertTrue(deployment["auth_configured"])
        self.assertTrue(deployment["google_oauth_configured"])
        self.assertFalse(deployment["basic_auth_configured"])

    def test_gmail_intake_ai_health_check_reports_only_verifiable_facts(self):
        # The intake health check must be HONEST: it reports key-presence and the
        # resolved model (facts it can verify without a live API call) and surfaces
        # a "configured" flag, rather than the old hardcoded ok:True that asserted
        # health it could not verify.
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "router-key",
            "NDA_GMAIL_INTAKE_MODEL": "deepseek/deepseek-v4-pro",
        }):
            with patch.object(server_module.app_settings, "stored_ai_api_key", return_value=""):
                deployment = server_module._deployment_status_for_host("0.0.0.0")
        checks = {check["id"]: check for check in deployment["checks"]}
        intake = checks["gmail_intake_ai"]
        # Key present -> configured true, model surfaced in the message.
        self.assertTrue(intake["configured"])
        self.assertIn("deepseek/deepseek-v4-pro", intake["message"])
        self.assertTrue(deployment["gmail_intake_ai_configured"])

        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "",
            "NDA_GMAIL_INTAKE_MODEL": "",
        }):
            with patch.object(server_module.app_settings, "stored_ai_api_key", return_value=""):
                deployment = server_module._deployment_status_for_host("0.0.0.0")
        checks = {check["id"]: check for check in deployment["checks"]}
        intake = checks["gmail_intake_ai"]
        # No key -> not configured, but the optional check still does not fail the gate.
        self.assertFalse(intake["configured"])
        self.assertTrue(intake["ok"])
        self.assertFalse(deployment["gmail_intake_ai_configured"])

    def test_required_auth_fails_closed_without_credentials(self):
        with patch.dict(os.environ, {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "",
        }):
            health_status, health_payload = self.request("GET", "/healthz")
            matter_status, matter_payload = self.request("GET", "/api/matters")
            matter_detail_status, matter_detail_payload = self.request("GET", "/api/matters/matter_1")
            matter_review_status, matter_review_payload = self.request("GET", "/api/matters/matter_1/review")
            delete_status, delete_payload = self.request("DELETE", "/api/matters/matter_missing")

        self.assertEqual(health_status, 200)
        self.assertEqual(health_payload, {"status": "ok"})
        self.assertEqual(matter_status, 503)
        self.assertEqual(matter_payload["error"], server_module.AUTH_NOT_CONFIGURED_MESSAGE)
        self.assertEqual(matter_detail_status, 503)
        self.assertEqual(matter_detail_payload["error"], server_module.AUTH_NOT_CONFIGURED_MESSAGE)
        self.assertEqual(matter_review_status, 503)
        self.assertEqual(matter_review_payload["error"], server_module.AUTH_NOT_CONFIGURED_MESSAGE)
        self.assertEqual(delete_status, 503)
        self.assertEqual(delete_payload["error"], server_module.AUTH_NOT_CONFIGURED_MESSAGE)

    def test_required_auth_challenges_and_accepts_basic_credentials(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, auth_env):
            unauth_status, unauth_payload, unauth_headers = self.request_with_headers("GET", "/api/matters")
            bad_status, bad_payload = self.request(
                "GET",
                "/api/matters",
                headers=self.basic_auth_headers(password="wrong"),
            )
            non_ascii_status, non_ascii_payload = self.request(
                "GET",
                "/api/matters",
                headers=self.basic_auth_headers(password="secrét"),
            )
            delete_status, delete_payload = self.request("DELETE", "/api/matters/matter_missing")
            detail_status, detail_payload = self.request("GET", "/api/matters/matter_missing")
            review_status, review_payload = self.request("GET", "/api/matters/matter_missing/review")
            authed_status, authed_payload = self.request(
                "GET",
                "/api/matters",
                headers=self.basic_auth_headers(),
            )
            status_auth_status, status_auth_payload = self.request(
                "GET",
                "/api/auth/status",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(unauth_status, 401)
        self.assertEqual(unauth_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertIn("Basic", unauth_headers["WWW-Authenticate"])
        self.assertEqual(bad_status, 401)
        self.assertEqual(bad_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertEqual(non_ascii_status, 401)
        self.assertEqual(non_ascii_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertEqual(delete_status, 401)
        self.assertEqual(delete_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertEqual(detail_status, 401)
        self.assertEqual(detail_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertEqual(review_status, 401)
        self.assertEqual(review_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertEqual(authed_status, 200)
        self.assertIn("matters", authed_payload)
        self.assertEqual(status_auth_status, 200)
        self.assertTrue(status_auth_payload["authenticated"])
        self.assertEqual(status_auth_payload["user"]["provider"], "basic")
        self.assertEqual(status_auth_payload["user"]["id"], "nda-admin")

    def test_signing_entities_endpoint_is_authed_and_serves_bundles(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, auth_env):
            unauth_status, unauth_payload = self.request("GET", "/api/signing-entities")
            authed_status, authed_payload = self.request(
                "GET",
                "/api/signing-entities",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(unauth_status, 401)
        self.assertEqual(unauth_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertEqual(authed_status, 200)
        entity_ids = {entity["id"] for entity in authed_payload["entities"]}
        self.assertEqual(
            entity_ids,
            {
                "aspora_technology",
                "vance_money",
                "real_transfer",
                "vance_techlabs",
                "nesse_technologies",
                "vance_technologies",
                "aspora_financial_services",
            },
        )
        # The live playbook maps cleanly, so every entity is drift-free.
        self.assertTrue(
            all(row["matches_playbook"] for row in authed_payload["law_mapping"])
        )
        self.assertIn("england_and_wales", authed_payload["playbook_option_ids"])

    def test_google_oauth_session_authenticates_and_scopes_matter_owner(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
            "The Recipient shall keep Confidential Information confidential for five years.",
        ])
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
            "NDA_GOOGLE_OAUTH_REDIRECT_URI": "http://127.0.0.1/auth/google/callback",
            ACTIVE_REVIEW_ENGINE_ENV: "ai_first",
            "NDA_AI_REVIEW_ENABLED": "true",
            "NDA_AI_ASSESSMENT_STUB": "1",
        }
        google_profile = {
            "aud": "google-client",
            "sub": "google-user-123",
            "email": "alice@example.com",
            "name": "Alice Example",
            "picture": "https://example.com/alice.png",
            "email_verified": "true",
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.dict(os.environ, auth_env):
                unauth_status, unauth_payload = self.request("GET", "/api/matters")
                status_status, status_payload = self.request("GET", "/api/auth/status")
                start_status, start_payload, start_headers = self.request_with_headers(
                    "GET",
                    "/auth/google/start?next=/api/matters",
                )
                start_location = start_headers["Location"]
                parsed_start = urlparse(start_location)
                state = parse_qs(parsed_start.query)["state"][0]
                state_cookie = self.cookie_header(start_headers["Set-Cookie"])

                with patch(
                    "nda_automation.routes.auth.google_identity.exchange_google_code",
                    return_value={
                        "id_token": "id-token",
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                    },
                ), patch(
                    "nda_automation.routes.auth.google_identity.verify_google_id_token",
                    return_value=google_profile,
                ):
                    callback_status, callback_payload, callback_headers = self.request_with_headers(
                        "GET",
                        f"/auth/google/callback?code=auth-code&state={state}",
                        headers={"Cookie": state_cookie},
                    )

                session_cookie = self.cookie_header(callback_headers["Set-Cookie"])
                google_owner_user_id = "google:google-user-123"
                connected_token_payloads = {
                    role: json.loads(
                        google_connection.user_token_path_for_role(role, google_owner_user_id).read_text(
                            encoding="utf-8"
                        )
                    )
                    for role in ("inbound", "outbound", "drive")
                }
                drive_settings = app_settings.drive_settings()
                authed_status, authed_payload = self.request(
                    "GET",
                    "/api/auth/status",
                    headers={"Cookie": session_cookie},
                )
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Alice Google NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                    },
                    headers={"Cookie": session_cookie},
                )
                matter = create_payload["matter"]
                stored_matter = matter_store.get_matter(matter["id"], owner_user_id="google:google-user-123")
                logout_status, logout_payload, logout_headers = self.request_with_headers(
                    "POST",
                    "/api/auth/logout",
                    headers={"Cookie": session_cookie},
                )
                after_logout_status, after_logout_payload = self.request(
                    "GET",
                    "/api/matters",
                    headers={"Cookie": self.cookie_header(logout_headers["Set-Cookie"])},
                )

        self.assertEqual(unauth_status, 401)
        self.assertEqual(unauth_payload["login_url"], "/login")
        self.assertEqual(status_status, 200)
        self.assertFalse(status_payload["authenticated"])
        self.assertEqual(start_status, 302)
        self.assertEqual(start_payload, b"")
        self.assertEqual(parsed_start.scheme, "https")
        self.assertEqual(parsed_start.netloc, "accounts.google.com")
        self.assertEqual(parse_qs(parsed_start.query)["client_id"], ["google-client"])
        self.assertEqual(parse_qs(parsed_start.query)["redirect_uri"], ["http://127.0.0.1/auth/google/callback"])
        # The single sign-in requests identity AND the unified Gmail+Drive scopes.
        login_scope = parse_qs(parsed_start.query)["scope"][0]
        self.assertIn("openid", login_scope)
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", login_scope)
        self.assertIn("https://www.googleapis.com/auth/drive.file", login_scope)
        self.assertEqual(callback_status, 302)
        self.assertEqual(callback_payload, b"")
        self.assertEqual(callback_headers["Location"], "/api/matters")
        self.assertIn("nda_session=", callback_headers["Set-Cookie"])
        self.assertEqual(set(connected_token_payloads), {"inbound", "outbound", "drive"})
        self.assertEqual(
            connected_token_payloads["drive"]["scopes"],
            ["https://www.googleapis.com/auth/drive.file"],
        )
        self.assertTrue(drive_settings["enabled"])
        self.assertEqual(authed_status, 200)
        self.assertTrue(authed_payload["authenticated"])
        self.assertEqual(authed_payload["user"]["id"], "google:google-user-123")
        self.assertEqual(authed_payload["user"]["email"], "alice@example.com")
        self.assertEqual(create_status, 201)
        self.assertIsNotNone(stored_matter)
        self.assertEqual(stored_matter["owner_user_id"], "google:google-user-123")
        self.assertEqual(logout_status, 200)
        self.assertFalse(logout_payload["authenticated"])
        self.assertEqual(after_logout_status, 401)
        self.assertEqual(after_logout_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)

    def test_oauth_start_requests_nonce_and_callback_rejects_nonce_mismatch(self):
        """FIX #16: /auth/google/start emits a nonce, and the callback rejects an
        ID token whose nonce does not match the one bound to this login."""
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
            "NDA_GOOGLE_OAUTH_REDIRECT_URI": "http://127.0.0.1/auth/google/callback",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.dict(os.environ, auth_env):
                _start_status, _start_payload, start_headers = self.request_with_headers(
                    "GET", "/auth/google/start?next=/api/matters"
                )
                parsed_start = urlparse(start_headers["Location"])
                start_query = parse_qs(parsed_start.query)
                state = start_query["state"][0]
                state_cookie = self.cookie_header(start_headers["Set-Cookie"])

                # The real verifier runs here (NOT mocked): the stub tokeninfo
                # carries a nonce that does NOT match the one start bound, so the
                # callback must reject it before minting any session.
                from nda_automation import google_identity as gi

                with patch(
                    "nda_automation.routes.auth.google_identity.exchange_google_code",
                    return_value={"id_token": "id-token"},
                ), patch(
                    "urllib.request.urlopen",
                    return_value=_FakeUrlopen(json.dumps({
                        "aud": "google-client",
                        "sub": "google-user-123",
                        "email": "alice@example.com",
                        "email_verified": "true",
                        "iss": "https://accounts.google.com",
                        "exp": str(int(time.time()) + 3600),
                        "nonce": "attacker-supplied-nonce",
                    }).encode("utf-8")),
                ):
                    callback_status, callback_payload, callback_headers = self.request_with_headers(
                        "GET",
                        f"/auth/google/callback?code=auth-code&state={state}",
                        headers={"Cookie": state_cookie},
                    )
                assert gi  # imported for clarity; verification ran via the route

        # Start advertised a nonce on the Google authorization URL.
        self.assertIn("nonce", start_query)
        self.assertTrue(start_query["nonce"][0])
        # Mismatched nonce -> no session minted.
        self.assertEqual(callback_status, 502)
        self.assertIn("nonce", str(callback_payload.get("error", "")).lower())
        self.assertNotIn("Set-Cookie", callback_headers)

    def test_logout_all_revokes_every_session_for_user(self):
        """FIX #23: POST /api/auth/logout-all clears all of the user's sessions,
        not just the current cookie's."""
        auth_env = {"NDA_REQUIRE_AUTH": "true", "NDA_AUTH_USERNAME": "", "NDA_AUTH_PASSWORD": ""}
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.dict(os.environ, auth_env):
                headers, user = self.google_session_headers()
                # A second, sibling session for the same user (another device).
                other_token = user_store.create_session(user["id"])
                other_headers = {"Cookie": f"{user_store.SESSION_COOKIE_NAME}={other_token}"}

                before_status, before_payload = self.request(
                    "GET", "/api/auth/status", headers=other_headers
                )
                logout_status, logout_payload = self.request(
                    "POST", "/api/auth/logout-all", headers=headers
                )
                after_status, after_payload = self.request(
                    "GET", "/api/auth/status", headers=other_headers
                )

        self.assertEqual(before_status, 200)
        self.assertTrue(before_payload["authenticated"])
        self.assertEqual(logout_status, 200)
        self.assertFalse(logout_payload["authenticated"])
        # The sibling session is now dead too: its cookie no longer authenticates.
        self.assertEqual(after_status, 200)
        self.assertFalse(after_payload["authenticated"])

    def test_matter_backup_export_requires_auth_when_auth_is_enabled(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
            # Admin-gated export: the Basic-auth operator must be a listed admin.
            "NDA_ADMIN_USERS": "nda-admin",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Backup NDA.docx",
                    document_bytes=b"backup-source-docx",
                    extracted_text="Sensitive extracted NDA text",
                    review_result={"clauses": [{"id": "governing_law", "status": "check"}]},
                    triage={"triage_status": "legal_review", "issue_count": 1},
                    owner_user_id="nda-admin",
                )
                matter_store.update_redline_draft(
                    matter["id"],
                    {"manual_redline_edits": []},
                    owner_user_id="nda-admin",
                )
                with patch.dict(os.environ, auth_env):
                    unauth_status, unauth_payload = self.request("GET", "/api/matters/export")
                    authed_status, authed_payload, authed_headers = self.request_with_headers(
                        "GET",
                        "/api/matters/export",
                        headers=self.basic_auth_headers(),
                    )

        self.assertEqual(unauth_status, 401)
        self.assertEqual(unauth_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertEqual(authed_status, 200)
        self.assertEqual(authed_headers["Content-Type"], "application/json")
        self.assertIn("attachment; filename=\"nda-matters-backup-", authed_headers["Content-Disposition"])
        self.assertEqual(authed_headers["X-Backup-Contains"], "matter-json")
        self.assertEqual(authed_payload["version"], 1)
        self.assertEqual(authed_payload["matter_count"], 1)
        self.assertEqual(authed_payload["matters"][0]["id"], matter["id"])
        self.assertEqual(authed_payload["matters"][0]["extracted_text"], "Sensitive extracted NDA text")
        self.assertIn("review_result", authed_payload["matters"][0])
        self.assertIn("redline_draft", authed_payload["matters"][0])
        self.assertEqual(authed_payload["documents"][0]["matter_id"], matter["id"])
        self.assertEqual(authed_payload["documents"][0]["stored_filename"], matter["stored_filename"])
        self.assertTrue(authed_payload["documents"][0]["present"])
        self.assertEqual(authed_payload["documents"][0]["size_bytes"], len(b"backup-source-docx"))
        self.assertNotIn("content_base64", authed_payload["documents"][0])

    def test_matter_backup_denies_non_admin_google_user(self):
        # A per-user Google account is authenticated but not an administrator,
        # so the bulk backup (full NDA text dump) must be refused with 403.
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
            "NDA_ADMIN_USERS": "",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                session_headers, _user = self.google_session_headers()
                with patch.dict(os.environ, auth_env):
                    status, payload = self.request(
                        "GET", "/api/matters/export", headers=session_headers
                    )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], server_module.ADMIN_REQUIRED_MESSAGE)

    def test_matter_backup_allows_listed_admin_google_user(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                session_headers, user = self.google_session_headers()
                admin_env = {**auth_env, "NDA_ADMIN_USERS": f"{user['id']}, other@example.com"}
                with patch.dict(os.environ, admin_env):
                    status, payload, headers = self.request_with_headers(
                        "GET", "/api/matters/export", headers=session_headers
                    )
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Backup-Contains"], "matter-json")
        self.assertEqual(payload["version"], 1)

    def test_matter_backup_denies_google_user_not_in_admin_list(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
            "NDA_ADMIN_USERS": "someone-else@example.com",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                session_headers, _user = self.google_session_headers()
                with patch.dict(os.environ, auth_env):
                    status, payload = self.request(
                        "GET", "/api/matters/export", headers=session_headers
                    )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], server_module.ADMIN_REQUIRED_MESSAGE)

    # --- Admin-only AI/personalisation settings mutators ---------------------
    # These endpoints overwrite the SHARED OpenRouter API key, flip the global
    # AI provider/engine, or change global personalisation, so a per-user
    # (non-admin) Google account must be refused even though it is authenticated.

    # Mutating settings endpoints that must be admin-gated, with a valid payload
    # so a non-admin denial is proven by authorization (403) rather than a 400.
    ADMIN_SETTINGS_MUTATORS = (
        ("POST", "/api/ai/api-key", {"api_key": "local-secret-key"}),
        ("DELETE", "/api/ai/api-key", None),
        # Use enabled=False: this exercises the same admin-gated route without
        # tripping the enable-requires-key gate (no key is configured in these
        # auth-focused tests), so the allow case asserts a clean 200 on the gate.
        ("POST", "/api/ai/settings", {"enabled": False}),
        ("POST", "/api/admin/personalisation-settings", {"sign_off": "Best,"}),
        # Gmail settings writes (window/import-limit/search-terms/enable switches)
        # drive the inbound NDA intake fetch, so the whole route is admin-gated. A
        # valid in-band window proves the denial is authorization (403), not a 400.
        ("POST", "/api/gmail/settings", {"inbound_window_days": 30}),
    )

    def _google_oauth_auth_env(self, **overrides):
        env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
        }
        env.update(overrides)
        return env

    def test_ai_settings_mutators_deny_non_admin_google_user(self):
        # NDA_ADMIN_USERS empty + Google OAuth caller => authenticated, not admin.
        auth_env = self._google_oauth_auth_env(NDA_ADMIN_USERS="")
        for method, path, body in self.ADMIN_SETTINGS_MUTATORS:
            with self.subTest(method=method, path=path):
                with tempfile.TemporaryDirectory() as data_dir:
                    patches = self.matter_store_patches(data_dir)
                    with patches[0], patches[1], patches[2]:
                        session_headers, _user = self.google_session_headers()
                        with patch.dict(os.environ, auth_env):
                            status, payload = self.request(
                                method, path, body, headers=session_headers
                            )
                self.assertEqual(status, 403, f"{method} {path} should be admin-gated")
                self.assertEqual(payload["error"], server_module.ADMIN_REQUIRED_MESSAGE)

    def test_ai_settings_mutators_deny_google_user_not_in_admin_list(self):
        auth_env = self._google_oauth_auth_env(NDA_ADMIN_USERS="someone-else@example.com")
        for method, path, body in self.ADMIN_SETTINGS_MUTATORS:
            with self.subTest(method=method, path=path):
                with tempfile.TemporaryDirectory() as data_dir:
                    patches = self.matter_store_patches(data_dir)
                    with patches[0], patches[1], patches[2]:
                        session_headers, _user = self.google_session_headers()
                        with patch.dict(os.environ, auth_env):
                            status, payload = self.request(
                                method, path, body, headers=session_headers
                            )
                self.assertEqual(status, 403, f"{method} {path} should be admin-gated")
                self.assertEqual(payload["error"], server_module.ADMIN_REQUIRED_MESSAGE)

    def test_ai_settings_mutators_allow_listed_admin_google_user(self):
        from nda_automation import ai_review

        # This test isolates the ADMIN GATE: a listed admin must clear it and the
        # handler then succeed (200). The api-key save path now round-trips the key
        # against OpenRouter before persisting, so mock that probe to keep the test
        # focused on authorization (and off the network); key-validation behaviour
        # is covered by the dedicated ApiKeyValidation tests.
        valid_result = ai_review.ApiKeyValidationResult("valid", "OpenRouter key verified.")
        for method, path, body in self.ADMIN_SETTINGS_MUTATORS:
            with self.subTest(method=method, path=path):
                with tempfile.TemporaryDirectory() as data_dir:
                    patches = self.matter_store_patches(data_dir)
                    with patches[0], patches[1], patches[2]:
                        session_headers, user = self.google_session_headers()
                        admin_env = self._google_oauth_auth_env(
                            NDA_ADMIN_USERS=f"{user['id']}, other@example.com"
                        )
                        with patch.dict(os.environ, admin_env), patch.object(
                            server_module.admin_routes.ai_review,
                            "validate_api_key",
                            return_value=valid_result,
                        ):
                            status, _payload = self.request(
                                method, path, body, headers=session_headers
                            )
                # A listed admin clears the gate; the handler then succeeds (200).
                self.assertEqual(status, 200, f"{method} {path} should allow a listed admin")

    # GET counterparts that read the same admin-sensitive surface (deployment
    # status, telemetry, AI settings + audit history, personalisation) and must
    # be admin-gated identically to their mutators, not readable by any
    # authenticated user.
    ADMIN_GET_ENDPOINTS = (
        "/api/deployment/status",
        "/api/telemetry",
        "/api/ai/settings",
        "/api/admin/personalisation-settings",
    )

    def test_admin_get_endpoints_deny_non_admin_google_user(self):
        auth_env = self._google_oauth_auth_env(NDA_ADMIN_USERS="someone-else@example.com")
        for path in self.ADMIN_GET_ENDPOINTS:
            with self.subTest(path=path):
                with tempfile.TemporaryDirectory() as data_dir:
                    patches = self.matter_store_patches(data_dir)
                    with patches[0], patches[1], patches[2]:
                        session_headers, _user = self.google_session_headers()
                        with patch.dict(os.environ, auth_env):
                            status, payload = self.request(
                                "GET", path, headers=session_headers
                            )
                self.assertEqual(status, 403, f"GET {path} should be admin-gated")
                self.assertEqual(payload["error"], server_module.ADMIN_REQUIRED_MESSAGE)

    def test_admin_get_endpoints_allow_listed_admin_google_user(self):
        for path in self.ADMIN_GET_ENDPOINTS:
            with self.subTest(path=path):
                with tempfile.TemporaryDirectory() as data_dir:
                    patches = self.matter_store_patches(data_dir)
                    with patches[0], patches[1], patches[2]:
                        session_headers, user = self.google_session_headers()
                        admin_env = self._google_oauth_auth_env(
                            NDA_ADMIN_USERS=f"{user['id']}, other@example.com"
                        )
                        with patch.dict(os.environ, admin_env):
                            status, _payload = self.request(
                                "GET", path, headers=session_headers
                            )
                self.assertEqual(status, 200, f"GET {path} should allow a listed admin")

    def test_telemetry_exposes_ai_cost_usd_rollup_to_admin(self):
        # Seed cumulative cost counters as record_openrouter_usage would, then assert
        # the admin telemetry payload surfaces the USD per-feature rollup.
        telemetry.increment("openrouter_cost_micro_units", amount=150000)
        telemetry.increment("openrouter_cost_micro_units__feature__review", amount=120000)
        telemetry.increment("openrouter_cost_micro_units__feature__generation", amount=30000)
        telemetry.increment("openrouter_total_tokens", amount=200)
        telemetry.increment("openrouter_total_tokens__feature__review", amount=150)
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                session_headers, user = self.google_session_headers()
                admin_env = self._google_oauth_auth_env(NDA_ADMIN_USERS=user["id"])
                with patch.dict(os.environ, admin_env):
                    status, payload = self.request(
                        "GET", "/api/telemetry", headers=session_headers
                    )
        self.assertEqual(status, 200)
        cost = payload["ai_cost"]
        self.assertEqual(cost["currency"], "USD")
        self.assertEqual(cost["total_usd"], 0.15)
        by_feature = {row["feature"]: row for row in cost["features"]}
        self.assertEqual(by_feature["review"]["cost_usd"], 0.12)
        self.assertEqual(by_feature["generation"]["cost_usd"], 0.03)
        self.assertEqual(by_feature["review"]["total_tokens"], 150)

    def test_telemetry_ai_cost_denied_to_non_admin(self):
        # A non-admin must NOT receive the spend rollup -- the 403 gate fires before
        # any cost data is serialised.
        telemetry.increment("openrouter_cost_micro_units", amount=150000)
        auth_env = self._google_oauth_auth_env(NDA_ADMIN_USERS="someone-else@example.com")
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                session_headers, _user = self.google_session_headers()
                with patch.dict(os.environ, auth_env):
                    status, payload = self.request(
                        "GET", "/api/telemetry", headers=session_headers
                    )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], server_module.ADMIN_REQUIRED_MESSAGE)
        self.assertNotIn("ai_cost", payload)

    # --- Non-admin AI-availability read --------------------------------------
    # USING the AI review is open to every authenticated user (the review-refresh
    # route is not admin-gated). The frontend's "is AI usable?" signal must read a
    # NON-admin endpoint so a non-admin no longer gets the admin-only 403 (and the
    # "Administrator access is required." text) and AI mis-rendered as "off". The
    # endpoint must expose ONLY booleans/engine-name, never the key or config detail.
    AI_AVAILABILITY_NON_SENSITIVE_KEYS = {"ai_enabled", "ai_configured", "active_engine"}
    AI_AVAILABILITY_FORBIDDEN_KEYS = {
        "api_key",
        "api_key_configured",
        "api_key_source",
        "provider",
        "model",
        "settings_audit",
        "stored_key_migration",
        "confidence_threshold",
    }

    def test_ai_availability_allows_non_admin_google_user(self):
        # NDA_ADMIN_USERS empty + Google OAuth caller => authenticated, NOT admin.
        auth_env = self._google_oauth_auth_env(NDA_ADMIN_USERS="")
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                session_headers, _user = self.google_session_headers()
                with patch.dict(os.environ, auth_env):
                    status, payload = self.request(
                        "GET", "/api/ai/availability", headers=session_headers
                    )
        # A non-admin must NOT get the admin 403 (that was the demo-blocking bug).
        self.assertEqual(status, 200)
        self.assertNotEqual(payload.get("error"), server_module.ADMIN_REQUIRED_MESSAGE)
        self.assertIn("ai_enabled", payload)
        self.assertIn("ai_configured", payload)
        self.assertIsInstance(payload["ai_enabled"], bool)
        self.assertIsInstance(payload["ai_configured"], bool)

    def test_ai_availability_does_not_leak_sensitive_config(self):
        auth_env = self._google_oauth_auth_env(NDA_ADMIN_USERS="")
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                session_headers, _user = self.google_session_headers()
                with patch.dict(os.environ, auth_env):
                    status, payload = self.request(
                        "GET", "/api/ai/availability", headers=session_headers
                    )
        self.assertEqual(status, 200)
        # Only the three non-sensitive fields are present; no key/provider/model leak.
        self.assertEqual(set(payload.keys()), self.AI_AVAILABILITY_NON_SENSITIVE_KEYS)
        for forbidden in self.AI_AVAILABILITY_FORBIDDEN_KEYS:
            self.assertNotIn(forbidden, payload)

    def test_ai_availability_reflects_global_enabled_for_non_admin(self):
        # An admin globally enabling AI (key configured) must read through to the
        # non-admin availability endpoint as ai_enabled/ai_configured true.
        auth_env = self._google_oauth_auth_env(
            NDA_ADMIN_USERS="",
            NDA_AI_REVIEW_ENABLED="true",
            OPENROUTER_API_KEY="sk-or-testkey0000000000000000",
        )
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                session_headers, _user = self.google_session_headers()
                with patch.dict(os.environ, auth_env):
                    status, payload = self.request(
                        "GET", "/api/ai/availability", headers=session_headers
                    )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ai_enabled"])
        self.assertTrue(payload["ai_configured"])

    def test_ai_settings_mutators_deny_basic_auth_when_admin_list_empty(self):
        # FAIL-CLOSED: an empty NDA_ADMIN_USERS must NOT make every Basic-auth
        # caller an admin. On a deployment that shares one Basic credential
        # across all users, the old "empty list => Basic is admin" fallback
        # silently granted admin to everyone. With no admin list configured, no
        # real authenticated caller (including Basic auth on a public host) is
        # admin; an operator must list themselves in NDA_ADMIN_USERS.
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
            "NDA_ADMIN_USERS": "",
        }
        for method, path, body in self.ADMIN_SETTINGS_MUTATORS:
            with self.subTest(method=method, path=path):
                with tempfile.TemporaryDirectory() as data_dir:
                    patches = self.matter_store_patches(data_dir)
                    with patches[0], patches[1], patches[2]:
                        with patch.dict(os.environ, auth_env):
                            status, payload = self.request(
                                method, path, body, headers=self.basic_auth_headers()
                            )
                self.assertEqual(
                    status, 403, f"{method} {path} must fail closed with no admin list"
                )
                self.assertEqual(payload["error"], server_module.ADMIN_REQUIRED_MESSAGE)

    def test_authenticated_matter_routes_are_owner_scoped(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
            "The Recipient shall keep Confidential Information confidential for five years.",
        ])
        alice_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "alice@example.com",
            "NDA_AUTH_PASSWORD": "secret",
            # Alice exercises the admin-gated backup export below, so she must be
            # a listed admin under the fail-closed admin gate.
            "NDA_ADMIN_USERS": "alice@example.com",
            ACTIVE_REVIEW_ENGINE_ENV: "ai_first",
            "NDA_AI_REVIEW_ENABLED": "true",
            "NDA_AI_ASSESSMENT_STUB": "1",
        }
        bob_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "bob@example.com",
            "NDA_AUTH_PASSWORD": "secret",
            # Bob exercises the admin-gated backup export below; this test asserts
            # OWNER-SCOPING (he sees zero of Alice's matters), which is orthogonal
            # to the admin gate, so list him as admin to keep that assertion live.
            "NDA_ADMIN_USERS": "bob@example.com",
            ACTIVE_REVIEW_ENGINE_ENV: "ai_first",
            "NDA_AI_REVIEW_ENABLED": "true",
            "NDA_AI_ASSESSMENT_STUB": "1",
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, alice_env):
                    create_status, create_payload = self.request(
                        "POST",
                        "/api/matters",
                        {
                            "filename": "Alice NDA.docx",
                            "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        },
                        headers=self.basic_auth_headers(username="alice@example.com"),
                )
                matter = create_payload["matter"]
                matter_id = matter["id"]
                stored_alice_matter = matter_store.get_matter(matter_id, owner_user_id="alice@example.com")
                self.assertEqual(stored_alice_matter["owner_user_id"], "alice@example.com")

                with patch.dict(os.environ, bob_env):
                    list_status, list_payload = self.request(
                        "GET",
                        "/api/matters",
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )
                    detail_status, detail_payload = self.request(
                        "GET",
                        f"/api/matters/{matter_id}",
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )
                    review_status, review_payload = self.request(
                        "POST",
                        f"/api/matters/{matter_id}/review-refresh",
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )
                    render_status_status, render_status_payload = self.request(
                        "GET",
                        f"/api/matters/{matter_id}/render-status",
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )
                    render_page_status, render_page_payload = self.request(
                        "GET",
                        f"/api/matters/{matter_id}/render-page/1",
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )
                    stage_status, stage_payload = self.request(
                        "POST",
                        f"/api/matters/{matter_id}/stage",
                        {"board_column": "sent"},
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )
                    export_status, export_payload = self.request(
                        "POST",
                        "/api/export-review-docx",
                        {
                            "matter_id": matter_id,
                            "reviewed_text": "This Agreement shall be governed by the laws of California.",
                        },
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )
                    backup_status, backup_payload = self.request(
                        "GET",
                        "/api/matters/export",
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )
                    reset_status, reset_payload = self.request(
                        "POST",
                        "/api/demo/reset",
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )
                    delete_status, delete_payload = self.request(
                        "DELETE",
                        f"/api/matters/{matter_id}",
                        headers=self.basic_auth_headers(username="bob@example.com"),
                    )

                with patch.dict(os.environ, alice_env):
                    alice_list_status, alice_list_payload = self.request(
                        "GET",
                        "/api/matters",
                        headers=self.basic_auth_headers(username="alice@example.com"),
                    )

        self.assertEqual(create_status, 201)
        self.assertEqual(list_status, 200)
        self.assertEqual(list_payload["matters"], [])
        self.assertEqual(detail_status, 404)
        self.assertEqual(detail_payload["error"], "NDA not found.")
        self.assertEqual(review_status, 404)
        self.assertEqual(review_payload["error"], "NDA not found.")
        self.assertEqual(render_status_status, 404)
        self.assertEqual(render_status_payload["error"], "NDA not found.")
        self.assertEqual(render_page_status, 404)
        self.assertEqual(render_page_payload["error"], "NDA not found.")
        self.assertEqual(stage_status, 404)
        self.assertEqual(stage_payload["error"], "NDA not found.")
        self.assertEqual(export_status, 404)
        self.assertEqual(export_payload["error"], "NDA not found.")
        self.assertEqual(backup_status, 200)
        self.assertEqual(backup_payload["matter_count"], 0)
        self.assertEqual(reset_status, 200)
        self.assertEqual(reset_payload["removed"], 0)
        self.assertEqual(delete_status, 404)
        self.assertEqual(delete_payload["error"], "NDA not found.")
        self.assertEqual(alice_list_status, 200)
        self.assertEqual([item["id"] for item in alice_list_payload["matters"]], [matter_id])

    def test_admin_deployment_status_requires_auth_and_omits_secrets(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
            # Admin-gated GET: the Basic-auth operator must be a listed admin.
            "NDA_ADMIN_USERS": "nda-admin",
            "NDA_DATA_DIR": "/var/data",
            "NDA_RATE_LIMIT_PER_MINUTE": "120",
        }
        with patch.dict(os.environ, auth_env):
            unauth_status, unauth_payload = self.request("GET", "/api/deployment/status")
            authed_status, authed_payload = self.request(
                "GET",
                "/api/deployment/status",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(unauth_status, 401)
        self.assertEqual(unauth_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertEqual(authed_status, 200)
        deployment = authed_payload["deployment"]
        self.assertEqual(deployment["health_check_path"], "/healthz")
        self.assertTrue(deployment["auth_required"])
        self.assertTrue(deployment["auth_configured"])
        self.assertEqual(deployment["rate_limit_per_minute"], 120)
        self.assertIn(deployment["status"], {"ok", "needs_attention"})
        self.assertNotIn("secret", json.dumps(deployment).lower())

    def test_state_changing_request_with_cross_site_origin_is_rejected(self):
        csrf_env = {
            "NDA_ENFORCE_CSRF": "true",
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, csrf_env):
            status, payload = self.request(
                "POST",
                "/api/demo/reset",
                headers={
                    **self.basic_auth_headers(),
                    "Origin": "https://evil.example.com",
                },
            )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], server_module.CSRF_REJECTED_MESSAGE)

    def test_state_changing_request_without_origin_or_referer_is_rejected_when_enforced(self):
        csrf_env = {
            "NDA_ENFORCE_CSRF": "true",
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, csrf_env):
            status, payload = self.request(
                "POST",
                "/api/demo/reset",
                headers=self.basic_auth_headers(),
            )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], server_module.CSRF_REJECTED_MESSAGE)

    def test_state_changing_request_with_same_origin_is_allowed_when_enforced(self):
        csrf_env = {
            "NDA_ENFORCE_CSRF": "true",
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, csrf_env):
                    status, payload = self.request(
                        "POST",
                        "/api/demo/reset",
                        headers={
                            **self.basic_auth_headers(),
                            "Host": f"{self.host}:{self.port}",
                            "Origin": f"http://{self.host}:{self.port}",
                        },
                    )
        self.assertEqual(status, 200)
        self.assertIn("removed", payload)

    def test_same_site_referer_is_accepted_when_origin_absent(self):
        csrf_env = {
            "NDA_ENFORCE_CSRF": "true",
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, csrf_env):
                    status, payload = self.request(
                        "POST",
                        "/api/demo/reset",
                        headers={
                            **self.basic_auth_headers(),
                            "Referer": f"http://{self.host}:{self.port}/",
                        },
                    )
        self.assertEqual(status, 200)
        self.assertIn("removed", payload)

    def test_logout_is_protected_from_cross_site_invocation(self):
        csrf_env = {
            "NDA_ENFORCE_CSRF": "true",
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, csrf_env):
            status, payload = self.request(
                "POST",
                "/api/auth/logout",
                headers={"Origin": "https://evil.example.com"},
            )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], server_module.CSRF_REJECTED_MESSAGE)

    def test_csrf_enforcement_off_by_default_on_loopback(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {
                    "NDA_REQUIRE_AUTH": "",
                    "NDA_AUTH_USERNAME": "",
                    "NDA_AUTH_PASSWORD": "",
                }):
                    status, payload = self.request(
                        "POST",
                        "/api/demo/reset",
                        headers={"Origin": "https://evil.example.com"},
                    )
        self.assertEqual(status, 200)
        self.assertIn("removed", payload)

    def test_safe_methods_are_never_csrf_gated(self):
        csrf_env = {
            "NDA_ENFORCE_CSRF": "true",
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
        }
        with patch.dict(os.environ, csrf_env):
            status, _payload = self.request(
                "GET",
                "/api/auth/status",
                headers={"Origin": "https://evil.example.com"},
            )
        self.assertEqual(status, 200)

    def test_public_deployment_status_flags_missing_hardening(self):
        with patch.dict(os.environ, {
            "NDA_REQUIRE_AUTH": "",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "",
            "NDA_GOOGLE_OAUTH_REDIRECT_URI": "",
            "NDA_GMAIL_OAUTH_REDIRECT_URI": "",
            "NDA_ALLOWED_HOSTS": "",
            "NDA_DATA_DIR": "",
            "NDA_USERS_PATH": "",
            "NDA_RATE_LIMIT_PER_MINUTE": "0",
            "NDA_AI_REVIEW_ENABLED": "",
            "NDA_AI_PROVIDER": "",
            "NDA_AI_MODEL": "",
            "OPENROUTER_API_KEY": "",
            "NDA_GMAIL_TRIAGE_MODEL": "",
            "NDA_ALLOW_EPHEMERAL_DATA": "",
        }):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/tmp/nda-automation-data")):
                deployment = server_module._deployment_status_for_host("0.0.0.0")

        checks = {check["id"]: check for check in deployment["checks"]}
        self.assertEqual(deployment["status"], "needs_attention")
        self.assertTrue(deployment["auth_required"])
        self.assertFalse(checks["auth"]["ok"])
        self.assertFalse(checks["google_identity"]["ok"])
        self.assertFalse(checks["allowed_hosts"]["ok"])
        self.assertFalse(checks["data_dir"]["ok"])
        self.assertFalse(checks["gmail_triage_ai"]["ok"])
        self.assertFalse(checks["rate_limit"]["ok"])

    def test_public_deployment_status_accepts_render_hardening_env(self):
        with patch.dict(os.environ, {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_ALLOWED_HOSTS": "nda-example.onrender.com",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
            "NDA_GOOGLE_OAUTH_REDIRECT_URI": "https://nda-example.onrender.com/auth/google/callback",
            "NDA_GMAIL_OAUTH_REDIRECT_URI": "https://nda-example.onrender.com/auth/gmail/callback",
            "NDA_DATA_DIR": "/var/data",
            "NDA_USERS_PATH": "/var/data/users.json",
            "NDA_RATE_LIMIT_PER_MINUTE": "120",
            "NDA_GMAIL_INBOUND_TOKEN_PATH": "",
            "NDA_GMAIL_OUTBOUND_TOKEN_PATH": "",
            "NDA_AI_REVIEW_ENABLED": "true",
            "NDA_AI_PROVIDER": "openrouter",
            "NDA_AI_MODEL": "anthropic/claude-opus-4.8",
            "OPENROUTER_API_KEY": "configured",
            "NDA_GMAIL_TRIAGE_MODEL": "anthropic/claude-opus-4.8",
            "NDA_ALLOW_EPHEMERAL_DATA": "",
        }):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
                with patch.object(export_service, "EXPORTS_DIR", server_module.Path("/var/data/exports")):
                    deployment = server_module._deployment_status_for_host("0.0.0.0")

        checks = {check["id"]: check for check in deployment["checks"]}
        self.assertEqual(deployment["status"], "ok")
        self.assertTrue(deployment["allowed_hosts_configured"])
        self.assertTrue(deployment["google_oauth_redirect_uri_configured"])
        self.assertTrue(deployment["gmail_oauth_redirect_uri_configured"])
        self.assertFalse(deployment["legacy_gmail_token_paths_configured"])
        self.assertTrue(deployment["ai_review_env_configured"])
        self.assertTrue(deployment["gmail_triage_ai_configured"])
        self.assertTrue(checks["oauth_redirects"]["ok"])
        self.assertTrue(checks["users_path"]["ok"])
        self.assertTrue(checks["gmail_token_mode"]["ok"])

    def test_local_deployment_status_message_matches_ok_data_dir_check(self):
        with patch.dict(os.environ, {"NDA_DATA_DIR": "", "NDA_ALLOW_EPHEMERAL_DATA": ""}):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/tmp/nda-automation-data")):
                deployment = server_module._deployment_status_for_host("127.0.0.1")

        checks = {check["id"]: check for check in deployment["checks"]}
        self.assertTrue(checks["data_dir"]["ok"])
        self.assertEqual(checks["data_dir"]["message"], "Local deployment may use local NDA data storage.")

    def test_local_deployment_status_message_matches_ok_auth_check(self):
        with patch.dict(os.environ, {
            "NDA_REQUIRE_AUTH": "",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "",
        }):
            deployment = server_module._deployment_status_for_host("127.0.0.1")

        checks = {check["id"]: check for check in deployment["checks"]}
        self.assertTrue(checks["auth"]["ok"])
        self.assertEqual(checks["auth"]["message"], "Authentication is not required for this host.")

    def test_public_bind_requires_configured_durable_data_dir(self):
        with patch.dict(os.environ, {"NDA_DATA_DIR": "", "NDA_ALLOW_EPHEMERAL_DATA": ""}):
            with self.assertRaisesRegex(RuntimeError, server_module.DURABLE_DATA_DIR_REQUIRED_MESSAGE):
                server_module._validate_public_storage("0.0.0.0")

    def test_public_bind_rejects_ephemeral_data_dir(self):
        with patch.dict(os.environ, {"NDA_DATA_DIR": "/tmp/nda-automation-data", "NDA_ALLOW_EPHEMERAL_DATA": ""}):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/tmp/nda-automation-data")):
                with self.assertRaisesRegex(RuntimeError, server_module.EPHEMERAL_DATA_DIR_MESSAGE):
                    server_module._validate_public_storage("0.0.0.0")

    def test_public_bind_rejects_ephemeral_exports_dir(self):
        with patch.dict(os.environ, {"NDA_DATA_DIR": "/var/data", "NDA_ALLOW_EPHEMERAL_DATA": ""}):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
                with patch.object(export_service, "EXPORTS_DIR", server_module.Path("/tmp/nda-automation-exports")):
                    with self.assertRaisesRegex(RuntimeError, server_module.EPHEMERAL_EXPORTS_DIR_MESSAGE):
                        server_module._validate_public_storage("0.0.0.0")

    def test_public_bind_rejects_ephemeral_users_path(self):
        with patch.dict(os.environ, {
            "NDA_DATA_DIR": "/var/data",
            "NDA_USERS_PATH": "/tmp/nda-users.json",
            "NDA_ALLOW_EPHEMERAL_DATA": "",
        }):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
                with patch.object(export_service, "EXPORTS_DIR", server_module.Path("/var/data/exports")):
                    with self.assertRaisesRegex(RuntimeError, server_module.EPHEMERAL_USERS_PATH_MESSAGE):
                        server_module._validate_public_storage("0.0.0.0")

    def test_public_bind_accepts_persistent_data_paths(self):
        with patch.dict(os.environ, {
            "NDA_DATA_DIR": "/var/data",
            "NDA_USERS_PATH": "/var/data/users.json",
            "NDA_ALLOW_EPHEMERAL_DATA": "",
        }):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
                with patch.object(export_service, "EXPORTS_DIR", server_module.Path("/var/data/exports")):
                    server_module._validate_public_storage("0.0.0.0")

    def test_loopback_allows_ephemeral_data_paths_for_local_tests(self):
        with patch.dict(os.environ, {"NDA_DATA_DIR": "/tmp/nda-automation-data", "NDA_ALLOW_EPHEMERAL_DATA": ""}):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/tmp/nda-automation-data")):
                with patch.object(export_service, "EXPORTS_DIR", server_module.Path("/tmp/nda-automation-exports")):
                    server_module._validate_public_storage("127.0.0.1")

    def test_boot_sentinel_first_boot_empty_dir_is_unknown_not_red(self):
        # FALSE-POSITIVE REGRESSION GUARD: a genuine first boot on a healthy durable
        # disk has no surviving sentinel.  It must NOT be flagged not_persisted (which
        # would drive needs_attention + the red FE banner); it stays advisory unknown.
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        with patch.dict(os.environ, {}, clear=False):
            for name in (*deployment_module.RENDER_DEPLOY_ID_ENVS, *deployment_module.RENDER_INSTANCE_ID_ENVS):
                os.environ.pop(name, None)
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = server_module.Path(tmp) / "data"  # does not exist yet
                verdict = deployment_module.record_data_dir_boot(data_dir)
        self.assertEqual(verdict, deployment_module.DATA_DIR_PERSISTENCE_UNKNOWN)
        self.assertEqual(deployment_module.data_dir_persistence_state(), deployment_module.DATA_DIR_PERSISTENCE_UNKNOWN)

    def test_boot_sentinel_surviving_across_boots_flags_persistent(self):
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = server_module.Path(tmp) / "data"
            first = deployment_module.record_data_dir_boot(data_dir)
            # First boot is advisory unknown -- no prior sentinel to compare against.
            self.assertEqual(first, deployment_module.DATA_DIR_PERSISTENCE_UNKNOWN)
            # Sentinel left by the first boot survives -- POSITIVE proof of durability.
            deployment_module._reset_data_dir_persistence_state_for_tests()
            second = deployment_module.record_data_dir_boot(data_dir)
        self.assertEqual(second, deployment_module.DATA_DIR_PERSISTED)
        self.assertEqual(deployment_module.data_dir_persistence_state(), deployment_module.DATA_DIR_PERSISTED)

    def test_boot_sentinel_wiped_between_boots_with_render_identity_flags_non_persistent(self):
        # A real cross-deploy wipe IS still caught: a sentinel from a DIFFERENT prior
        # deploy survives in name but its retained boot history was wiped under it.
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = server_module.Path(tmp) / "data"
            data_dir.mkdir(parents=True)
            sentinel_path = data_dir / deployment_module.DATA_DIR_SENTINEL_FILENAME
            # Surviving file from a prior deploy, but its durable boot history (count)
            # was wiped to 0 under it -- positive evidence the dir reset across deploys.
            sentinel_path.write_text(
                json.dumps({"boot_count": 0, "first_seen": 1.0, "last_seen": 1.0, "deploy_id": "prior-commit"}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"RENDER_GIT_COMMIT": "current-commit"}, clear=False):
                verdict = deployment_module.record_data_dir_boot(data_dir)
        self.assertEqual(verdict, deployment_module.DATA_DIR_NOT_PERSISTED)

    def test_boot_sentinel_emptied_dir_without_identity_stays_unknown(self):
        # Without Render identity we cannot prove first-boot vs wipe from an empty dir
        # on a single-instance (no auto-restart) service, so default to advisory
        # unknown -- never a false red.
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        with patch.dict(os.environ, {}, clear=False):
            for name in (*deployment_module.RENDER_DEPLOY_ID_ENVS, *deployment_module.RENDER_INSTANCE_ID_ENVS):
                os.environ.pop(name, None)
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = server_module.Path(tmp) / "data"
                deployment_module.record_data_dir_boot(data_dir)
                # Simulate the dir resetting between restarts (sentinel gone entirely).
                for child in data_dir.iterdir():
                    child.unlink()
                deployment_module._reset_data_dir_persistence_state_for_tests()
                verdict = deployment_module.record_data_dir_boot(data_dir)
        self.assertEqual(verdict, deployment_module.DATA_DIR_PERSISTENCE_UNKNOWN)

    def test_boot_sentinel_corrupt_file_degrades_to_unknown_without_raising(self):
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = server_module.Path(tmp) / "data"
            data_dir.mkdir(parents=True)
            (data_dir / deployment_module.DATA_DIR_SENTINEL_FILENAME).write_text("{not json", encoding="utf-8")
            verdict = deployment_module.record_data_dir_boot(data_dir)
        self.assertEqual(verdict, deployment_module.DATA_DIR_PERSISTENCE_UNKNOWN)

    def test_boot_sentinel_never_raises_on_unwritable_path(self):
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        with tempfile.NamedTemporaryFile() as handle:
            # Parent is a regular file, so the data dir cannot be created/read.
            data_dir = server_module.Path(handle.name) / "data"
            # Must return a verdict, not raise.
            verdict = deployment_module.record_data_dir_boot(data_dir)
        self.assertIn(
            verdict,
            {
                deployment_module.DATA_DIR_NOT_PERSISTED,
                deployment_module.DATA_DIR_PERSISTENCE_UNKNOWN,
            },
        )

    def test_deployment_status_unknown_persistence_does_not_fail_gate(self):
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
            deployment = server_module._deployment_status_for_host("0.0.0.0")
        checks = {check["id"]: check for check in deployment["checks"]}
        self.assertEqual(deployment["data_dir_persisted"], deployment_module.DATA_DIR_PERSISTENCE_UNKNOWN)
        self.assertTrue(checks["data_dir_persistence"]["ok"])
        self.assertIsNone(checks["data_dir_persistence"]["persisted"])

    def test_deployment_status_non_persistent_public_host_warns(self):
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        deployment_module._data_dir_persistence_state = deployment_module.DATA_DIR_NOT_PERSISTED
        with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
            deployment = server_module._deployment_status_for_host("0.0.0.0")
        checks = {check["id"]: check for check in deployment["checks"]}
        self.assertEqual(deployment["data_dir_persisted"], deployment_module.DATA_DIR_NOT_PERSISTED)
        self.assertFalse(checks["data_dir_persistence"]["ok"])
        self.assertEqual(deployment["status"], "needs_attention")
        self.assertEqual(checks["data_dir_persistence"]["message"], deployment_module.NON_PERSISTENT_DATA_DIR_WARNING)

    def test_deployment_status_non_persistent_loopback_is_tolerated(self):
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        deployment_module._data_dir_persistence_state = deployment_module.DATA_DIR_NOT_PERSISTED
        with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
            deployment = server_module._deployment_status_for_host("127.0.0.1")
        checks = {check["id"]: check for check in deployment["checks"]}
        self.assertTrue(checks["data_dir_persistence"]["ok"])

    def test_deployment_status_persisted_data_dir_passes(self):
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        deployment_module._data_dir_persistence_state = deployment_module.DATA_DIR_PERSISTED
        with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
            deployment = server_module._deployment_status_for_host("0.0.0.0")
        checks = {check["id"]: check for check in deployment["checks"]}
        self.assertEqual(deployment["data_dir_persisted"], deployment_module.DATA_DIR_PERSISTED)
        self.assertTrue(checks["data_dir_persistence"]["ok"])
        self.assertTrue(checks["data_dir_persistence"]["persisted"])

    # --- Non-durable-storage operational warning (Admin banner) ---------------
    # The loud, user-facing twin of the admin-gated `data_dir_persistence` check:
    # when storage is non-durable the operator must be told -- right where they
    # publish a Playbook / save an entity -- that the edit reverts on redeploy.

    def _operational_warning_codes(self):
        return {w.get("code") for w in server_module.admin_routes._operational_warnings()}

    def test_durability_warning_fires_on_proven_wipe(self):
        # POSITIVE wipe evidence (NOT_PERSISTED verdict) -> the warning fires.
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        deployment_module._data_dir_persistence_state = deployment_module.DATA_DIR_NOT_PERSISTED
        warning = deployment_module.storage_durability_warning()
        self.assertIsNotNone(warning)
        self.assertEqual(warning["code"], deployment_module.STORAGE_NOT_DURABLE_WARNING_CODE)
        self.assertTrue(deployment_module.storage_is_non_durable())

    def test_durability_warning_fires_on_ephemeral_data_dir_path(self):
        # A config regression pointing NDA_DATA_DIR at an ephemeral path (/tmp) is
        # caught deterministically by the path denylist even without a wipe verdict.
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        with patch.dict(os.environ, {"NDA_DATA_DIR": "/tmp/nda"}, clear=False):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/tmp/nda")):
                self.assertTrue(deployment_module.storage_is_non_durable())
                self.assertIsNotNone(deployment_module.storage_durability_warning())

    def test_durability_warning_absent_when_persisted(self):
        # POSITIVE durability proof + a non-ephemeral data dir -> NO warning.
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        deployment_module._data_dir_persistence_state = deployment_module.DATA_DIR_PERSISTED
        with patch.dict(os.environ, {"NDA_DATA_DIR": "/var/data"}, clear=False):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
                self.assertFalse(deployment_module.storage_is_non_durable())
                self.assertIsNone(deployment_module.storage_durability_warning())

    def test_durability_warning_absent_on_first_boot_unknown(self):
        # FALSE-POSITIVE GUARD: a genuine first boot (advisory unknown) on a healthy
        # durable (non-ephemeral) disk must NOT raise the loud warning.
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        # state is UNKNOWN after reset
        with patch.dict(os.environ, {"NDA_DATA_DIR": "/var/data"}, clear=False):
            with patch.object(matter_store, "DATA_DIR", server_module.Path("/var/data")):
                self.assertFalse(deployment_module.storage_is_non_durable())
                self.assertIsNone(deployment_module.storage_durability_warning())

    def test_durability_warning_absent_in_local_dev_without_data_dir(self):
        # No NDA_DATA_DIR (local dev) -> nothing fires.
        deployment_module._reset_data_dir_persistence_state_for_tests()
        self.addCleanup(deployment_module._reset_data_dir_persistence_state_for_tests)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NDA_DATA_DIR", None)
            self.assertFalse(deployment_module.storage_is_non_durable())
            self.assertIsNone(deployment_module.storage_durability_warning())

    def test_operational_warnings_include_durability_code_when_non_durable(self):
        # End-to-end through the prominent _operational_warnings() Admin banner hook.
        # Patch the durability helper so the composition is what's under test (not the
        # verdict plumbing, covered above) and app_settings keeps using the temp dir.
        durable = {
            "code": deployment_module.STORAGE_NOT_DURABLE_WARNING_CODE,
            "message": deployment_module.STORAGE_NOT_DURABLE_WARNING_MESSAGE,
        }
        with patch.object(server_module.admin_routes, "storage_durability_warning", return_value=durable):
            codes = self._operational_warning_codes()
        self.assertIn(deployment_module.STORAGE_NOT_DURABLE_WARNING_CODE, codes)

    def test_operational_warnings_omit_durability_code_when_durable(self):
        # Helper returns None (durable/unknown/local) -> the warning is absent from the
        # banner, and the rest of the warnings plumbing is unaffected.
        with patch.object(server_module.admin_routes, "storage_durability_warning", return_value=None):
            codes = self._operational_warning_codes()
        self.assertNotIn(deployment_module.STORAGE_NOT_DURABLE_WARNING_CODE, codes)

    def test_text_review_rejects_bad_json(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            "{not json",
            {"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Request body must be valid JSON.")

    def test_text_review_rejects_non_object_json(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            "[]",
            {"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Request body must be a JSON object.")

    def test_text_review_rejects_empty_content_length(self):
        status, payload = self.raw_http_request(
            "POST /api/review HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: \r\n"
            "Connection: close\r\n"
            "\r\n"
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Content-Length must be a non-negative integer.")

    def test_text_review_rejects_malformed_content_length(self):
        status, payload = self.raw_http_request(
            "POST /api/review HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: nope\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{}"
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Content-Length must be a non-negative integer.")

    def test_text_review_rejects_negative_content_length(self):
        status, payload = self.raw_http_request(
            "POST /api/review HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: -1\r\n"
            "Connection: close\r\n"
            "\r\n"
            "{}"
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Content-Length must be a non-negative integer.")

    def test_text_review_rejects_oversize_request_body_before_reading(self):
        with patch.object(server_module, "MAX_REQUEST_BODY_BYTES", 8):
            status, payload = self.raw_http_request(
                "POST /api/review HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 9\r\n"
                "Connection: close\r\n"
                "\r\n"
            )

        self.assertEqual(status, 413)
        self.assertEqual(payload["error"], server_module.REQUEST_BODY_TOO_LARGE_MESSAGE)

    def test_disallowed_host_header_is_rejected(self):
        status, payload = self.raw_http_request(
            "GET / HTTP/1.1\r\n"
            "Host: evil.example\r\n"
            "Connection: close\r\n"
            "\r\n"
        )

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], server_module.HOST_NOT_ALLOWED_MESSAGE)

    def test_text_review_rejects_oversize_text(self):
        from nda_automation.document_limits import MAX_REVIEW_TEXT_CHARS, REVIEW_TEXT_TOO_LARGE_MESSAGE

        oversize_text = "a " * (MAX_REVIEW_TEXT_CHARS // 2 + 1)
        status, payload = self.request("POST", "/api/review", {"text": oversize_text})

        self.assertEqual(status, 413)
        self.assertEqual(payload["error"], REVIEW_TEXT_TOO_LARGE_MESSAGE)

    def test_expensive_endpoints_are_rate_limited_per_client(self):
        rate_env = {
            "NDA_RATE_LIMIT_PER_MINUTE": "2",
            "NDA_RATE_LIMIT_WINDOW_SECONDS": "60",
        }
        body = {"text": "This Agreement shall be governed by the laws of California."}
        with patch.dict(os.environ, rate_env):
            first_status, _first_payload = self.request("POST", "/api/review", body)
            second_status, _second_payload = self.request("POST", "/api/review", body)
            third_status, third_payload, third_headers = self.request_with_headers("POST", "/api/review", body)

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(third_status, 429)
        self.assertEqual(third_payload["error"], server_module.RATE_LIMITED_MESSAGE)
        self.assertGreaterEqual(int(third_headers["Retry-After"]), 1)
        self.assertEqual(telemetry.snapshot()["counters"]["rate_limit_hits"], 1)

    def test_rate_limit_buckets_are_keyed_per_authenticated_user(self):
        # Behind a proxy every caller shares one TCP peer, so the rate limiter
        # must isolate buckets by authenticated identity: one user exhausting
        # the limit must not throttle a different signed-in user.
        rate_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_PASSWORD": "secret",
            "NDA_RATE_LIMIT_PER_MINUTE": "1",
            "NDA_RATE_LIMIT_WINDOW_SECONDS": "60",
        }
        body = {"text": "This Agreement shall be governed by the laws of California."}
        with patch.dict(os.environ, {**rate_env, "NDA_AUTH_USERNAME": "alice@example.com"}):
            alice_first, _ = self.request(
                "POST", "/api/review", body,
                headers=self.basic_auth_headers(username="alice@example.com"),
            )
            alice_second, _ = self.request(
                "POST", "/api/review", body,
                headers=self.basic_auth_headers(username="alice@example.com"),
            )
        with patch.dict(os.environ, {**rate_env, "NDA_AUTH_USERNAME": "bob@example.com"}):
            bob_first, _ = self.request(
                "POST", "/api/review", body,
                headers=self.basic_auth_headers(username="bob@example.com"),
            )

        self.assertEqual(alice_first, 200)
        self.assertEqual(alice_second, 429)
        # Bob shares Alice's TCP peer (127.0.0.1) but is a distinct identity, so
        # he gets his own bucket and is not throttled by Alice's traffic.
        self.assertEqual(bob_first, 200)

    def test_background_error_logging_omits_exception_message(self):
        with patch("builtins.print") as mocked_print:
            server_module._log_background_error(
                "Gmail scheduled sync failed",
                RuntimeError("Sensitive extracted NDA text"),
            )

        mocked_print.assert_called_once_with("Gmail scheduled sync failed: RuntimeError")

    def test_telemetry_requires_auth_and_counts_without_sensitive_text(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "nda-admin",
            "NDA_AUTH_PASSWORD": "secret",
            # Admin-gated GET: the Basic-auth operator must be a listed admin.
            "NDA_ADMIN_USERS": "nda-admin",
            "NDA_RATE_LIMIT_PER_MINUTE": "0",
        }
        review_text = "Sensitive counterparty NDA text governed by California."
        with patch.dict(os.environ, auth_env):
            unauth_status, unauth_payload = self.request("GET", "/api/telemetry")
            review_status, _review_payload = self.request(
                "POST",
                "/api/review",
                {"text": review_text},
                headers=self.basic_auth_headers(),
            )
            telemetry_status, telemetry_payload = self.request(
                "GET",
                "/api/telemetry",
                headers=self.basic_auth_headers(),
            )

        self.assertEqual(unauth_status, 401)
        self.assertEqual(unauth_payload["error"], server_module.AUTH_REQUIRED_MESSAGE)
        self.assertEqual(review_status, 200)
        self.assertEqual(telemetry_status, 200)
        counters = telemetry_payload["telemetry"]["counters"]
        self.assertEqual(counters["review_requests"], 1)
        self.assertEqual(counters["http_4xx_responses"], 1)
        self.assertNotIn(review_text, json.dumps(telemetry_payload))
        # The telemetry block is unchanged; the health block is additive.
        self.assertIn("started_at", telemetry_payload["telemetry"])
        health = telemetry_payload["health"]
        self.assertEqual(
            set(health),
            {"review", "inbound_review", "generation", "other", "status", "alerts", "note"},
        )
        self.assertIn(health["status"], {"ok", "warn", "alert"})
        self.assertIsInstance(health["alerts"], list)
        self.assertIn("attempted", health["review"])
        self.assertIn("requests", health["generation"])
        # Health derives from the same snapshot counters the caller sees.
        self.assertEqual(
            health["review"]["attempted"],
            counters.get("active_review_ai_first_attempted", 0),
        )

    def test_export_copy_failure_logging_omits_exception_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            exports_path = server_module.Path(temp_dir) / "not-a-directory"
            exports_path.write_text("blocked", encoding="utf-8")
            with (
                patch.object(export_service, "EXPORTS_DIR", exports_path),
                patch("builtins.print") as mocked_print,
            ):
                saved_path = export_service.persist_export(b"data", "export.docx")

        self.assertIsNone(saved_path)
        mocked_print.assert_called_once_with("Could not save export copy atomically: FileExistsError")
        self.assertEqual(telemetry.snapshot()["counters"]["export_copy_failures"], 1)

    def test_matter_store_save_flushes_to_disk(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(matter_store.os, "fsync", wraps=matter_store.os.fsync) as fsync:
                    matter_store._save_matter_record({"id": "matter_1"})

                saved = json.loads((server_module.Path(data_dir) / "matters" / "matter_1.json").read_text(encoding="utf-8"))

        self.assertEqual(saved, {"id": "matter_1"})
        self.assertGreaterEqual(fsync.call_count, 1)

    def test_app_settings_save_flushes_parent_directory(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            settings_path = server_module.Path(data_dir) / "app_settings.json"
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "_fsync_directory") as fsync_directory:
                    app_settings._save_settings_unlocked({"gmail": {"sync_frequency": "30_minutes"}})

                saved = json.loads(settings_path.read_text(encoding="utf-8"))

        self.assertEqual(saved, {"gmail": {"sync_frequency": "30_minutes"}})
        fsync_directory.assert_called_once_with(server_module.Path(data_dir))

    def test_app_settings_directory_fsync_uses_directory_fd(self):
        with (
            patch.object(operational_settings_repository.os, "open", return_value=123) as directory_open,
            patch.object(operational_settings_repository.os, "fsync") as fsync,
            patch.object(operational_settings_repository.os, "close") as close,
        ):
            operational_settings_repository.fsync_directory(server_module.Path("/tmp/nda-settings-test"))

        directory_open.assert_called_once()
        fsync.assert_called_once_with(123)
        close.assert_called_once_with(123)

    def test_text_review_rejects_empty_text(self):
        status, payload = self.request("POST", "/api/review", {"text": "   "})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Provide NDA text to review.")

    def test_json_payload_rejects_excessively_nested_json(self):
        nested_json = "[" * 100000 + "0" + "]" * 100000

        status, payload = self.request(
            "POST",
            "/api/review",
            nested_json,
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Request body must be valid JSON.")

    def test_text_review_returns_clause_results(self):
        status, payload = self.request(
            "POST",
            "/api/review",
            {"text": "Each party agrees this NDA is governed by the laws of the DIFC for two years."},
        )

        self.assertEqual(status, 200)
        self.assertIn("clauses", payload)
        self.assertIn("redline_edits", payload)
        self.assert_review_payload_contract(payload)

    def test_text_review_uses_active_review_engine_route(self):
        expected = {
            "review_mode": "ai_first_compat",
            "active_review_engine": {"engine": "ai_first"},
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "pass",
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            "redline_edits": [],
        }

        with (
            patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}),
            patch.object(server_module, "review_nda_with_active_engine", return_value=expected) as active_review,
        ):
            status, payload = self.request("POST", "/api/review", {"text": "NDA text"})

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        active_review.assert_called_once_with("NDA text")

    def test_text_review_offline_flag_pins_deterministic_engine_no_ai(self):
        # The live editor's keystroke-debounced clause detection sends offline:true
        # so it NEVER runs the AI reviewer/verifier. The route must force the
        # deterministic (no-AI) engine -- here proven by the force_engine kwarg.
        expected = {
            "active_review_engine": {"engine": "deterministic"},
            "clauses": [],
            "redline_edits": [],
        }
        with (
            patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}),
            patch.object(server_module, "review_nda_with_active_engine", return_value=expected) as active_review,
        ):
            status, payload = self.request("POST", "/api/review", {"text": "NDA text", "offline": True})

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        active_review.assert_called_once_with("NDA text", force_engine="deterministic")

    def test_text_review_reports_active_ai_first_failure(self):
        with (
            patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}),
            patch.object(
                server_module,
                "review_nda_with_active_engine",
                side_effect=ActiveReviewEngineError("AI-first review failed: no key"),
            ),
        ):
            status, payload = self.request("POST", "/api/review", {"text": "NDA text"})

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "AI-first review failed: no key")

    def test_ai_second_opinion_route_updates_selected_clause(self):
        expected = {
            "clause": {"id": "mutuality", "decision": "review"},
            "ai_review": {
                "status": "completed",
                "record_count": 1,
                "mode": "clause_second_opinion",
                "target_clause_id": "mutuality",
            },
            "overall_status": "needs_review",
            "review_state": {"counts": {"pass": 5, "review": 1, "check": 0}},
            "requirements_passed": 5,
            "requirements_needs_review": 1,
            "requirements_failed": 0,
        }
        current_review = {"clauses": [{"id": "mutuality"}], "paragraphs": [{"id": "p1", "index": 1, "text": "Each party."}]}
        with patch.object(server_module, "ai_second_opinion_for_clause", return_value=expected) as second_opinion:
            status, payload = self.request(
                "POST",
                "/api/review/ai-second-opinion",
                {"clause_id": "mutuality", "review_result": current_review},
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        second_opinion.assert_called_once_with(current_review, "mutuality")
        self.assertEqual(telemetry.snapshot()["counters"]["ai_second_opinion_requests"], 1)

    def test_ai_second_opinion_route_rejects_missing_inputs(self):
        missing_clause_status, missing_clause_payload = self.request(
            "POST",
            "/api/review/ai-second-opinion",
            {"review_result": {"clauses": []}},
        )
        missing_review_status, missing_review_payload = self.request(
            "POST",
            "/api/review/ai-second-opinion",
            {"clause_id": "mutuality"},
        )

        self.assertEqual(missing_clause_status, 400)
        self.assertEqual(missing_clause_payload["error"], "Provide a clause id for AI second opinion.")
        self.assertEqual(missing_review_status, 400)
        self.assertEqual(missing_review_payload["error"], "Provide the current review result for AI second opinion.")

    def test_ai_draft_validation_route_validates_redline_draft(self):
        expected = {
            "clause_id": "governing_law",
            "redline_id": "r1",
            "validation": {
                "status": "validated",
                "ai_decision": "pass",
                "ai_confidence": 0.92,
            },
            "ai_review": {
                "status": "completed",
                "mode": "draft_fix_validation",
                "record_count": 1,
                "target_clause_id": "governing_law",
                "redline_id": "r1",
            },
        }
        current_review = {"clauses": [{"id": "governing_law"}], "paragraphs": [{"id": "p1", "index": 1, "text": "California."}]}
        redline = {
            "id": "r1",
            "clause_id": "governing_law",
            "action": "replace_paragraph",
            "original_text": "California.",
            "replacement_text": "This Agreement is governed by the laws of Delaware.",
        }
        with patch.object(server_module, "ai_validate_draft_fix", return_value=expected) as validate_draft:
            status, payload = self.request(
                "POST",
                "/api/review/ai-draft-validation",
                {"clause_id": "governing_law", "review_result": current_review, "redline_edit": redline},
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        validate_draft.assert_called_once_with(current_review, "governing_law", redline)
        self.assertEqual(telemetry.snapshot()["counters"]["ai_draft_validation_requests"], 1)

    def test_ai_draft_validation_route_rejects_missing_inputs(self):
        missing_clause_status, missing_clause_payload = self.request(
            "POST",
            "/api/review/ai-draft-validation",
            {"review_result": {"clauses": []}, "redline_edit": {"id": "r1"}},
        )
        missing_review_status, missing_review_payload = self.request(
            "POST",
            "/api/review/ai-draft-validation",
            {"clause_id": "governing_law", "redline_edit": {"id": "r1"}},
        )
        missing_redline_status, missing_redline_payload = self.request(
            "POST",
            "/api/review/ai-draft-validation",
            {"clause_id": "governing_law", "review_result": {"clauses": []}},
        )

        self.assertEqual(missing_clause_status, 400)
        self.assertEqual(missing_clause_payload["error"], "Provide a clause id for AI draft validation.")
        self.assertEqual(missing_review_status, 400)
        self.assertEqual(missing_review_payload["error"], "Provide the current review result for AI draft validation.")
        self.assertEqual(missing_redline_status, 400)
        self.assertEqual(missing_redline_payload["error"], "Provide a redline draft to validate.")

    def test_review_payload_contract_covers_pass_check_and_missing_flows(self):
        scenarios = [
            ("pass", "Each party may disclose Confidential Information and this Agreement is governed by the laws of the DIFC for two years.\n\nFor Aspora Ltd\nBy: A. Signatory\nTitle: Director\nDate: 2026-05-30\n\nFor Counterparty Ltd\nBy: B. Signatory\nTitle: CEO\nDate: 2026-05-30"),
            ("check", "The confidentiality obligations survive for seven years.\n\nThe Recipient must not circumvent the Company."),
            ("missing", "The parties will discuss a possible transaction."),
        ]

        for name, text in scenarios:
            with self.subTest(name=name):
                status, payload = self.request("POST", "/api/review", {"text": text})

                self.assertEqual(status, 200)
                self.assert_review_payload_contract(payload)

    def test_matter_upload_rejects_xml_dtd_entities(self):
        source_docx = make_unsafe_docx()

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_status, matter_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "unsafe.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "manual_upload",
                    },
                )

        self.assertEqual(matter_status, 400)
        self.assertIn("unsupported XML DTD/entity declarations", matter_payload["error"])

    def test_matter_upload_creates_persisted_manual_matter(self):
        source_docx = make_docx([
            "Intro paragraph.",
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Acme NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                    },
                )
                list_status, list_payload = self.request("GET", "/api/matters")
                matter = payload["matter"]
                fetch_status, fetch_payload = self.request("GET", f"/api/matters/{matter['id']}")
                review_status, review_payload = self.request("GET", f"/api/matters/{matter['id']}/review")
                stored_matter = matter_store.get_matter(matter["id"])
                stored_path = matter_store.UPLOADS_DIR / stored_matter["stored_filename"]
                stored_bytes = stored_path.read_bytes()

        self.assertEqual(status, 201)
        self.assertEqual(matter["source_type"], "manual_upload")
        self.assertEqual(matter["board_column"], "in_review")
        self.assertEqual(matter["source_filename"], "Acme NDA.docx")
        self.assertEqual(matter["document_title"], "Acme NDA")
        self.assertEqual(matter["sender"], "Manual upload")
        self.assertEqual(matter["recipient_email"], "")
        self.assertEqual(matter["can_send_redline"], False)
        self.assertEqual(matter["subject"], "Acme NDA")
        self.assertEqual(matter["attachment_filename"], "Acme NDA.docx")
        self.assertEqual(matter["message_snippet"], "Manual upload of Acme NDA.docx.")
        self.assertIn("received_at", matter)
        # Manual upload now DEFERS the AI review (defer_ai_review=True), mirroring
        # inbound + generation. The matter is created UN-REVIEWED: no AI review has
        # run, so ai_review_ran is False and there is no triage verdict or issue
        # count yet. The operator runs the review on demand (Run AI review /
        # Refresh Review) from the workstation or inspector.
        self.assertIs(matter["ai_review_ran"], False)
        self.assertIn(matter.get("triage_status"), (None, "", "pending"))
        self.assertIn(matter.get("issue_count"), (None, 0))
        self.assertNotIn("review_result", matter)
        self.assertNotIn("extracted_text", matter)
        self.assertNotIn("redline_draft", matter)
        self.assertNotIn("stored_filename", matter)
        self.assertNotIn("gmail_message_id", matter)
        # The matter is persisted un-reviewed: the stored review_result is None.
        self.assertIsNone(stored_matter.get("review_result"))
        self.assertIn("extracted_text", stored_matter)
        self.assertEqual(stored_bytes, source_docx)
        self.assertEqual(list_status, 200)
        self.assertEqual([item["id"] for item in list_payload["matters"]], [matter["id"]])
        self.assertNotIn("extracted_text", list_payload["matters"][0])
        self.assertNotIn("review_result", list_payload["matters"][0])
        self.assertNotIn("redline_draft", list_payload["matters"][0])
        self.assertEqual(fetch_status, 200)
        self.assertEqual(fetch_payload["matter"]["id"], matter["id"])
        self.assertEqual(fetch_payload["matter"]["sender"], "Manual upload")
        self.assertEqual(fetch_payload["matter"]["can_send_redline"], False)
        self.assertNotIn("extracted_text", fetch_payload["matter"])
        self.assertNotIn("review_result", fetch_payload["matter"])
        self.assertNotIn("redline_draft", fetch_payload["matter"])
        self.assertEqual(review_status, 200)
        self.assertEqual(review_payload["matter"]["id"], matter["id"])
        self.assertNotIn("extracted_text", review_payload["matter"])
        self.assertNotIn("review_result", review_payload["matter"])
        self.assertNotIn("redline_draft", review_payload["matter"])
        self.assertIn("extracted_text", review_payload)
        # The deferred matter has no review yet: the review endpoint reports the
        # actionable un-reviewed state (review_may_be_stale + a review-refresh URL +
        # the no-AI/missing-result reasons) instead of carrying a review_result.
        self.assertNotIn("review_result", review_payload)
        self.assertIs(review_payload["matter"]["ai_review_ran"], False)
        self.assertTrue(review_payload["review_may_be_stale"])
        self.assertIn(
            "review-refresh", review_payload["review_refresh"]["refresh_url"]
        )
        self.assertIn("no_ai_review", review_payload["review_refresh"]["stale_reasons"])

    def test_matter_render_status_and_pdf_stream_for_source_pdf(self):
        source_pdf = b"%PDF-1.7\nsource pdf\n%%EOF\n"

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Acme NDA.pdf",
                    document_bytes=source_pdf,
                    extracted_text="PDF text.",
                    review_result={
                        "paragraphs": [{"id": "p1", "index": 1, "page_number": 1, "text": "PDF text."}],
                        "clauses": [{
                            "id": "mutuality",
                            "matched_paragraph_ids": ["p1"],
                            "name": "Mutuality",
                        }],
                        "redline_edits": [],
                    },
                    triage={
                        "triage_status": "ready_to_sign",
                        "next_action": "Ready to sign",
                        "issue_count": 0,
                        "requirements_passed": 1,
                        "requirements_needs_review": 0,
                        "requirements_failed": 0,
                    },
                )
                status, payload, _headers = self.request_with_headers(
                    "GET",
                    f"/api/matters/{matter['id']}/render-status",
                )
                pdf_status, pdf_payload, pdf_headers = self.request_with_headers(
                    "GET",
                    f"/api/matters/{matter['id']}/render-pdf",
                )
                pdf_download_status, pdf_download_payload, pdf_download_headers = self.request_with_headers(
                    "GET",
                    f"/api/matters/{matter['id']}/source-pdf",
                )
                head_status, head_payload, head_headers = self.request_with_headers(
                    "HEAD",
                    f"/api/matters/{matter['id']}/render-pdf",
                )

        self.assertEqual(status, 200)
        self.assertEqual(payload["document_render"]["status"], document_rendering.READY_STATUS)
        self.assertEqual(payload["document_render"]["source_kind"], "pdf")
        self.assertEqual(payload["document_render"]["source_label"], "Original PDF")
        self.assertEqual(payload["document_render"]["pdf_url"], f"/api/matters/{matter['id']}/render-pdf")
        self.assertEqual(pdf_status, 200)
        self.assertEqual(pdf_headers["Content-Type"], document_rendering.PDF_CONTENT_TYPE)
        self.assertEqual(pdf_payload, source_pdf)
        self.assertEqual(pdf_download_status, 200)
        self.assertEqual(pdf_download_headers["Content-Type"], document_rendering.PDF_CONTENT_TYPE)
        self.assertEqual(pdf_download_headers["Content-Disposition"], 'attachment; filename="Acme-NDA.pdf"')
        self.assertEqual(pdf_download_headers["X-PDF-Export-Verified"], "document-to-pdf")
        self.assertEqual(pdf_download_headers["X-PDF-Export-Source-Kind"], "pdf")
        self.assertEqual(pdf_download_payload, source_pdf)
        self.assertEqual(head_status, 200)
        self.assertEqual(head_headers["Content-Type"], document_rendering.PDF_CONTENT_TYPE)
        self.assertEqual(head_headers["Content-Length"], str(len(source_pdf)))
        self.assertEqual(head_payload, b"")

    def test_reviewed_pdf_streams_approved_docx_conversion(self):
        class AvailableConverter:
            name = "test-docx-pdf"

            def is_available(self):
                return True

            def convert_docx_to_pdf(self, source_path, output_dir, *, timeout_seconds):
                output_path = output_dir / "reviewed.pdf"
                output_path.write_bytes(b"%PDF-1.7\nreviewed pdf\n%%EOF\n")
                return output_path

        source_text = "This Agreement shall be governed by the laws of California."
        source_docx = make_docx([source_text])
        review_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_state": {
                "state": "pass",
                "overall_status": "ready_to_sign",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
            "paragraphs": [{"id": "p1", "index": 1, "text": source_text}],
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "pass",
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            "redline_edits": [],
            "extracted_text": source_text,
            "playbook_runtime": self.active_playbook_review_runtime(),
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Reviewed NDA.docx",
                    document_bytes=source_docx,
                    extracted_text=source_text,
                    review_result=review_result,
                    triage={
                        "triage_status": "ready_to_sign",
                        "next_action": "Ready to sign",
                        "issue_count": 0,
                        "requirements_passed": 1,
                        "requirements_needs_review": 0,
                        "requirements_failed": 0,
                    },
                )
                matter_store.update_matter_fields(matter["id"], {"status": "approved"})
                with patch.object(document_rendering, "LibreOfficeDocxConverter", return_value=AvailableConverter()):
                    status, payload, headers = self.request_with_headers(
                        "GET",
                        f"/api/matters/{matter['id']}/reviewed-pdf",
                    )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], document_rendering.PDF_CONTENT_TYPE)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Reviewed-NDA-redlined.pdf"')
        self.assertEqual(headers["X-PDF-Export-Verified"], "document-to-pdf")
        self.assertEqual(headers["X-PDF-Export-Source-Kind"], "docx")
        self.assertEqual(headers["X-Reviewed-Redline-Count"], "0")
        self.assertEqual(payload, b"%PDF-1.7\nreviewed pdf\n%%EOF\n")

    def test_source_docx_reconstructs_pdf_source_when_converter_available(self):
        from tests.test_pdf_docx_reconstruction import make_valid_docx

        class AvailablePdfDocxConverter:
            name = "fake-pdf2docx"

            def is_available(self):
                return True

            def convert_pdf_to_docx(self, source_path, output_path):
                assert source_path.read_bytes().startswith(b"%PDF-")
                output_path.write_bytes(make_valid_docx("This Agreement shall be governed by the laws of California."))

        source_text = "This Agreement shall be governed by the laws of California."
        source_pdf = make_pdf(source_text)
        review_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_state": {
                "state": "pass",
                "overall_status": "ready_to_sign",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
            "source": {"type": "pdf"},
            "paragraphs": [{"id": "p1", "index": 1, "text": source_text}],
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "pass",
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            "redline_edits": [],
            "extracted_text": source_text,
            "playbook_runtime": self.active_playbook_review_runtime(),
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Reviewed NDA.pdf",
                    document_bytes=source_pdf,
                    extracted_text=source_text,
                    review_result=review_result,
                    triage={
                        "triage_status": "ready_to_sign",
                        "next_action": "Ready to sign",
                        "issue_count": 0,
                        "requirements_passed": 1,
                        "requirements_needs_review": 0,
                        "requirements_failed": 0,
                    },
                )
                with patch.object(
                    server_module.pdf_export_service.pdf_docx_reconstruction,
                    "Pdf2DocxConverter",
                    return_value=AvailablePdfDocxConverter(),
                ):
                    status, payload, headers = self.request_with_headers(
                        "GET",
                        f"/api/matters/{matter['id']}/source-docx",
                    )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Reviewed-NDA.docx"')
        self.assertEqual(headers["X-PDF-DOCX-Reconstruction"], "pdf2docx")
        self.assertEqual(headers["X-PDF-DOCX-Converter"], "fake-pdf2docx")
        self.assertTrue(payload.startswith(b"PK"))

    def test_reviewed_docx_reconstructs_pdf_source_and_runs_redline_package(self):
        from tests.test_pdf_docx_reconstruction import make_valid_docx

        class AvailablePdfDocxConverter:
            name = "fake-pdf2docx"

            def is_available(self):
                return True

            def convert_pdf_to_docx(self, source_path, output_path):
                output_path.write_bytes(make_valid_docx("This Agreement shall be governed by the laws of California."))

        source_text = "This Agreement shall be governed by the laws of California."
        source_pdf = make_pdf(source_text)
        review_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_state": {
                "state": "pass",
                "overall_status": "ready_to_sign",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
            "source": {"type": "pdf"},
            "paragraphs": [{"id": "p1", "index": 1, "text": source_text}],
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "pass",
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            # A genuine reviewer redline so the PDF-source path actually runs the
            # reconstruction (the zero-redline case now serves the original PDF and
            # skips reconstruction). The fake converter writes a body carrying the
            # tracked change so the post-render coverage gate is satisfied.
            "redline_edits": [
                {
                    "id": "r1",
                    "clause_id": "governing_law",
                    "paragraph_id": "p1",
                    "action": "replace_paragraph",
                    "original_text": source_text,
                    "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
                }
            ],
            "extracted_text": source_text,
            "playbook_runtime": self.active_playbook_review_runtime(),
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Reviewed NDA.pdf",
                    document_bytes=source_pdf,
                    extracted_text=source_text,
                    review_result=review_result,
                    triage={
                        "triage_status": "ready_to_sign",
                        "next_action": "Ready to sign",
                        "issue_count": 0,
                        "requirements_passed": 1,
                        "requirements_needs_review": 0,
                        "requirements_failed": 0,
                    },
                )
                # Accept the redlined clause so the reviewed export actually applies the
                # redline (reviewed_docx_payload only includes redlines for accepted
                # clauses); without an accepted change the export now serves the original
                # PDF instead of reconstructing.
                matter_store.set_clause_reviewer_decision(
                    matter["id"],
                    "governing_law",
                    {
                        "action": "accept",
                        "actor": "reviewer",
                        "decided_at": "2026-01-01T00:00:00+00:00",
                    },
                )
                matter_store.update_matter_fields(matter["id"], {"status": "approved"})
                with patch.object(
                    server_module.redline_export_service.pdf_docx_reconstruction,
                    "Pdf2DocxConverter",
                    return_value=AvailablePdfDocxConverter(),
                ):
                    status, payload, headers = self.request_with_headers(
                        "GET",
                        f"/api/matters/{matter['id']}/reviewed-docx",
                    )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Reviewed-NDA-reviewed.docx"')
        self.assertEqual(headers["X-Export-Verified"], "pdf2docx")
        self.assertEqual(headers["X-PDF-DOCX-Reconstruction"], "pdf2docx")
        self.assertEqual(headers["X-PDF-DOCX-Converter"], "fake-pdf2docx")
        self.assertEqual(headers["X-Reviewed-Redline-Count"], "1")
        self.assertTrue(payload.startswith(b"PK"))

    def _ready_redline_matter_fixture(self, *, source_filename, document_bytes, source_type, with_redline=False):
        """Create an approved, ready-to-sign matter that passes the send gate.

        Used by the PDF-reconstruction caveat tests for both PDF and DOCX sources so
        each path reaches the export/send response without tripping human-review.

        ``with_redline`` seeds one accepted reviewer redline. The PDF-source path needs
        it so the export actually runs the reconstruction -- the zero-redline case now
        (correctly) serves the original PDF and skips reconstruction entirely. The
        DOCX-source path is left redline-free (a clean source render), which is the no-op
        case it has always exercised.
        """
        source_text = "This Agreement shall be governed by the laws of California."
        redline_edits = (
            [
                {
                    "id": "r1",
                    "clause_id": "governing_law",
                    "paragraph_id": "p1",
                    "action": "replace_paragraph",
                    "original_text": source_text,
                    "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
                }
            ]
            if with_redline
            else []
        )
        review_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_state": {
                "state": "pass",
                "overall_status": "ready_to_sign",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
            "source": {"type": source_type},
            "paragraphs": [{"id": "p1", "index": 1, "text": source_text}],
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "pass",
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            "redline_edits": redline_edits,
            "extracted_text": source_text,
            "playbook_runtime": self.active_playbook_review_runtime(),
        }
        matter = matter_store.create_matter(
            source_filename=source_filename,
            document_bytes=document_bytes,
            extracted_text=source_text,
            review_result=review_result,
            triage={
                "triage_status": "ready_to_sign",
                "next_action": "Ready to sign",
                "issue_count": 0,
                "requirements_passed": 1,
                "requirements_needs_review": 0,
                "requirements_failed": 0,
            },
        )
        if with_redline:
            matter_store.set_clause_reviewer_decision(
                matter["id"],
                "governing_law",
                {
                    "action": "accept",
                    "actor": "reviewer",
                    "decided_at": "2026-01-01T00:00:00+00:00",
                },
            )
        matter_store.update_matter_fields(matter["id"], {"status": "approved", "human_reviewed": True})
        return matter

    def _pdf_redline_matter_fixture(self):
        return self._ready_redline_matter_fixture(
            source_filename="Reviewed NDA.pdf",
            document_bytes=make_pdf("This Agreement shall be governed by the laws of California."),
            source_type="pdf",
            with_redline=True,
        )

    def _docx_redline_matter_fixture(self):
        return self._ready_redline_matter_fixture(
            source_filename="Reviewed NDA.docx",
            document_bytes=make_docx(["This Agreement shall be governed by the laws of California."]),
            source_type="docx",
        )

    def test_legacy_review_docx_export_surfaces_pdf_reconstruction_caveat(self):
        """Fix 1: the legacy /api/export-review-docx route must merge the PDF
        reconstruction headers (instead of hardcoding the generic verified header)
        so the operator sees the best-effort-from-PDF caveat; DOCX exports keep
        the verified header unchanged."""

        class AvailablePdfDocxConverter:
            name = "fake-pdf2docx"

            def is_available(self):
                return True

            def convert_pdf_to_docx(self, source_path, output_path):
                from tests.test_pdf_docx_reconstruction import make_valid_docx

                output_path.write_bytes(
                    make_valid_docx("This Agreement shall be governed by the laws of California.")
                )

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                # PDF-source matter: the caveat headers must come through.
                pdf_matter = self._pdf_redline_matter_fixture()
                with patch.object(
                    server_module.redline_export_service.pdf_docx_reconstruction,
                    "Pdf2DocxConverter",
                    return_value=AvailablePdfDocxConverter(),
                ):
                    pdf_status, pdf_payload, pdf_headers = self.request_with_headers(
                        "POST",
                        "/api/export-review-docx",
                        {"matter_id": pdf_matter["id"]},
                    )

                # DOCX-source matter on the same route: verified header is unchanged.
                docx_matter = self._docx_redline_matter_fixture()
                docx_status, _docx_payload, docx_headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {"matter_id": docx_matter["id"]},
                )

        # PDF path: reconstruction caveat surfaced (mirrors the reviewed-docx route).
        self.assertEqual(pdf_status, 200)
        self.assertEqual(pdf_headers["X-Export-Verified"], "pdf2docx")
        self.assertEqual(pdf_headers["X-PDF-DOCX-Reconstruction"], "pdf2docx")
        self.assertEqual(pdf_headers["X-PDF-DOCX-Converter"], "fake-pdf2docx")
        self.assertTrue(pdf_payload.startswith(b"PK"))

        # DOCX path: unchanged verified messaging, no reconstruction marker.
        self.assertEqual(docx_status, 200)
        self.assertEqual(docx_headers["X-Export-Verified"], "word-package; track-revisions")
        self.assertNotIn("X-PDF-DOCX-Reconstruction", docx_headers)

    def test_gmail_send_redline_flags_pdf_reconstruction_for_operator(self):
        """Fix 2: /api/gmail/send-redline must report source_reconstructed_from_pdf
        plus the formatting caveat for PDF-source matters, and must NOT set the
        flag for DOCX-source matters."""

        class AvailablePdfDocxConverter:
            name = "fake-pdf2docx"

            def is_available(self):
                return True

            def convert_pdf_to_docx(self, source_path, output_path):
                from tests.test_pdf_docx_reconstruction import make_valid_docx

                output_path.write_bytes(
                    make_valid_docx("This Agreement shall be governed by the laws of California.")
                )

        sent_stub = {
            "message_id": "msg_outbound",
            "outbound_account": "legal@aspora.com",
            "sent_at": "2026-05-31T12:00:00+00:00",
            "subject": "Re: Reviewed NDA",
            "thread_id": "thread_outbound",
            "to": "legal@example.com",
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(server_module.gmail_integration, "validate_outbound_send_ready", return_value={}):
                    with patch.object(server_module.gmail_integration, "send_redline_email", return_value=sent_stub):
                        pdf_matter = self._pdf_redline_matter_fixture()
                        with patch.object(
                            server_module.redline_export_service.pdf_docx_reconstruction,
                            "Pdf2DocxConverter",
                            return_value=AvailablePdfDocxConverter(),
                        ):
                            pdf_send_status, pdf_send_payload = self.request(
                                "POST",
                                "/api/gmail/send-redline",
                                {
                                    "matter_id": pdf_matter["id"],
                                    "confirm_send": True,
                                    "to": "legal@example.com",
                                    "confirm_recipient": "legal@example.com",
                                },
                            )

                        # DOCX-source matter: no reconstruction caveat.
                        docx_matter = self._docx_redline_matter_fixture()
                        docx_send_status, docx_send_payload = self.request(
                            "POST",
                            "/api/gmail/send-redline",
                            {
                                "matter_id": docx_matter["id"],
                                "confirm_send": True,
                                "to": "legal@example.com",
                                "confirm_recipient": "legal@example.com",
                            },
                        )

        # PDF path: flag set + honest caveat string present.
        self.assertEqual(pdf_send_status, 200)
        self.assertTrue(pdf_send_payload["source_reconstructed_from_pdf"])
        self.assertIn("reconstructed from a PDF", pdf_send_payload["source_reconstruction_caveat"])

        # DOCX path: flag false, no caveat string.
        self.assertEqual(docx_send_status, 200)
        self.assertFalse(docx_send_payload["source_reconstructed_from_pdf"])
        self.assertNotIn("source_reconstruction_caveat", docx_send_payload)

    def test_reviewed_docx_reports_unavailable_pdf_reconstruction_engine(self):
        class UnavailablePdfDocxConverter:
            name = "fake-unavailable"

            def is_available(self):
                return False

        source_text = "This Agreement shall be governed by the laws of California."
        source_pdf = make_pdf(source_text)
        review_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_state": {
                "state": "pass",
                "overall_status": "ready_to_sign",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
            "source": {"type": "pdf"},
            "paragraphs": [{"id": "p1", "index": 1, "text": source_text}],
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "pass",
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            # One accepted redline so the export attempts reconstruction (the zero-redline
            # case now serves the original PDF and never reaches the unavailable engine).
            "redline_edits": [
                {
                    "id": "r1",
                    "clause_id": "governing_law",
                    "paragraph_id": "p1",
                    "action": "replace_paragraph",
                    "original_text": source_text,
                    "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
                }
            ],
            "extracted_text": source_text,
            "playbook_runtime": self.active_playbook_review_runtime(),
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Reviewed NDA.pdf",
                    document_bytes=source_pdf,
                    extracted_text=source_text,
                    review_result=review_result,
                    triage={
                        "triage_status": "ready_to_sign",
                        "next_action": "Ready to sign",
                        "issue_count": 0,
                        "requirements_passed": 1,
                        "requirements_needs_review": 0,
                        "requirements_failed": 0,
                    },
                )
                matter_store.set_clause_reviewer_decision(
                    matter["id"],
                    "governing_law",
                    {
                        "action": "accept",
                        "actor": "reviewer",
                        "decided_at": "2026-01-01T00:00:00+00:00",
                    },
                )
                matter_store.update_matter_fields(matter["id"], {"status": "approved"})
                with patch.object(
                    server_module.redline_export_service.pdf_docx_reconstruction,
                    "Pdf2DocxConverter",
                    return_value=UnavailablePdfDocxConverter(),
                ):
                    status, payload = self.request(
                        "GET",
                        f"/api/matters/{matter['id']}/reviewed-docx",
                    )

        self.assertEqual(status, 503)
        self.assertIn("pdf2docx", payload["error"])

    def test_reviewed_pdf_reports_converter_unavailable_for_approved_docx(self):
        class UnavailableConverter:
            name = "test-unavailable"

            def is_available(self):
                return False

        source_text = "This Agreement shall be governed by the laws of California."
        source_docx = make_docx([source_text])
        review_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_state": {
                "state": "pass",
                "overall_status": "ready_to_sign",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
            "paragraphs": [{"id": "p1", "index": 1, "text": source_text}],
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "pass",
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            "redline_edits": [],
            "extracted_text": source_text,
            "playbook_runtime": self.active_playbook_review_runtime(),
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Reviewed NDA.docx",
                    document_bytes=source_docx,
                    extracted_text=source_text,
                    review_result=review_result,
                    triage={
                        "triage_status": "ready_to_sign",
                        "next_action": "Ready to sign",
                        "issue_count": 0,
                        "requirements_passed": 1,
                        "requirements_needs_review": 0,
                        "requirements_failed": 0,
                    },
                )
                matter_store.update_matter_fields(matter["id"], {"status": "approved"})
                with patch.object(document_rendering, "LibreOfficeDocxConverter", return_value=UnavailableConverter()):
                    status, payload, headers = self.request_with_headers(
                        "GET",
                        f"/api/matters/{matter['id']}/reviewed-pdf",
                    )

        self.assertEqual(status, 503)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(payload["document_pdf_export"]["status"], document_rendering.UNAVAILABLE_STATUS)
        self.assertEqual(payload["document_pdf_export"]["error_code"], "converter_unavailable")
        self.assertEqual(payload["document_pdf_export"]["filename"], "Reviewed-NDA-redlined.pdf")
        self.assertIn("LibreOffice/soffice", payload["error"])
        self.assertIn("LibreOffice/soffice", payload["document_pdf_export"]["converter"]["message"])

    def test_matter_render_status_includes_page_manifest_and_streams_page_image(self):
        class FakePdfPageRenderer:
            name = "fake-pdf-pages"

            def is_available(self):
                return True

            def render_pdf_to_page_images(self, pdf_path, output_dir, *, dpi):
                image_path = output_dir / "page-1.png"
                image_path.write_bytes(b"\x89PNG\r\nserver fake page\n")
                return [
                    document_rendering.RenderedPdfPageImage(
                        page_number=1,
                        image_path=image_path,
                        width=1224,
                        height=1584,
                        dpi=dpi,
                        scale=round(dpi / document_rendering.PDF_POINTS_PER_INCH, 4),
                    )
                ]

        source_pdf = b"%PDF-1.7\nsource pdf\n%%EOF\n"

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.object(document_rendering, "PyMuPdfPageRenderer", FakePdfPageRenderer):
                matter = matter_store.create_matter(
                    source_filename="Acme NDA.pdf",
                    document_bytes=source_pdf,
                    extracted_text="PDF text.",
                    review_result={
                        "paragraphs": [{"id": "p1", "index": 1, "page_number": 1, "text": "PDF text."}],
                        "clauses": [{
                            "id": "mutuality",
                            "matched_paragraph_ids": ["p1"],
                            "name": "Mutuality",
                        }],
                        "redline_edits": [],
                    },
                    triage={
                        "triage_status": "ready_to_sign",
                        "next_action": "Ready to sign",
                        "issue_count": 0,
                        "requirements_passed": 1,
                        "requirements_needs_review": 0,
                        "requirements_failed": 0,
                    },
                )
                status, payload, _headers = self.request_with_headers(
                    "GET",
                    f"/api/matters/{matter['id']}/render-status",
                )
                page_status, page_payload, page_headers = self.request_with_headers(
                    "GET",
                    f"/api/matters/{matter['id']}/render-page/1",
                )
                head_status, head_payload, head_headers = self.request_with_headers(
                    "HEAD",
                    f"/api/matters/{matter['id']}/render-page/1",
                )

        self.assertEqual(status, 200)
        render_payload = payload["document_render"]
        self.assertEqual(render_payload["status"], document_rendering.READY_STATUS)
        self.assertEqual(render_payload["pdf_url"], f"/api/matters/{matter['id']}/render-pdf")
        self.assertEqual(render_payload["page_image_status"], document_rendering.READY_STATUS)
        self.assertEqual(render_payload["page_images"]["status"], document_rendering.READY_STATUS)
        self.assertEqual(render_payload["dpi"], document_rendering.DEFAULT_PAGE_IMAGE_DPI)
        self.assertEqual(render_payload["scale"], round(document_rendering.DEFAULT_PAGE_IMAGE_DPI / document_rendering.PDF_POINTS_PER_INCH, 4))
        self.assertEqual(render_payload["pages"], render_payload["page_images"]["pages"])
        self.assertEqual(render_payload["pages"][0]["page_number"], 1)
        self.assertEqual(render_payload["pages"][0]["image_url"], f"/api/matters/{matter['id']}/render-page/1")
        self.assertEqual(render_payload["pages"][0]["width"], 1224)
        self.assertEqual(render_payload["pages"][0]["height"], 1584)
        self.assertEqual(render_payload["document_overlay"]["status"], "partial")
        self.assertEqual(render_payload["document_overlay"]["precision"], "page")
        self.assertEqual(render_payload["document_overlay"]["fallback_mode"], "text_dom_scroll")
        self.assertEqual(render_payload["document_overlay"]["anchors"][0]["clause_id"], "mutuality")
        self.assertEqual(render_payload["document_overlay"]["anchors"][0]["paragraph_id"], "p1")
        self.assertEqual(render_payload["document_overlay"]["anchors"][0]["page_number"], 1)
        self.assertEqual(render_payload["document_overlay"]["anchors"][0]["boxes"], [])
        self.assertEqual(page_status, 200)
        self.assertEqual(page_headers["Content-Type"], document_rendering.PAGE_IMAGE_CONTENT_TYPE)
        self.assertEqual(page_payload, b"\x89PNG\r\nserver fake page\n")
        self.assertEqual(head_status, 200)
        self.assertEqual(head_headers["Content-Type"], document_rendering.PAGE_IMAGE_CONTENT_TYPE)
        self.assertEqual(head_headers["Content-Length"], str(len(page_payload)))
        self.assertEqual(head_payload, b"")

    def test_matter_render_status_reports_page_renderer_unavailable_for_ready_pdf(self):
        class UnavailablePdfPageRenderer:
            name = "fake-page-unavailable"

            def is_available(self):
                return False

            def render_pdf_to_page_images(self, pdf_path, output_dir, *, dpi):
                raise AssertionError("Unavailable page renderer should not be invoked.")

        source_pdf = b"%PDF-1.7\nsource pdf\n%%EOF\n"

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.object(document_rendering, "PyMuPdfPageRenderer", UnavailablePdfPageRenderer):
                matter = matter_store.create_matter(
                    source_filename="Acme NDA.pdf",
                    document_bytes=source_pdf,
                    extracted_text="PDF text.",
                    review_result={"clauses": []},
                    triage={
                        "triage_status": "ready_to_sign",
                        "next_action": "Ready to sign",
                        "issue_count": 0,
                        "requirements_passed": 1,
                        "requirements_needs_review": 0,
                        "requirements_failed": 0,
                    },
                )
                status, payload, _headers = self.request_with_headers(
                    "GET",
                    f"/api/matters/{matter['id']}/render-status",
                )
                page_status, page_payload, _page_headers = self.request_with_headers(
                    "GET",
                    f"/api/matters/{matter['id']}/render-page/1",
                )

        self.assertEqual(status, 200)
        render_payload = payload["document_render"]
        self.assertEqual(render_payload["status"], document_rendering.READY_STATUS)
        self.assertEqual(render_payload["pdf_url"], f"/api/matters/{matter['id']}/render-pdf")
        self.assertEqual(render_payload["pages"], [])
        self.assertEqual(render_payload["page_image_status"], document_rendering.UNAVAILABLE_STATUS)
        self.assertEqual(render_payload["page_image_error_code"], "page_renderer_unavailable")
        self.assertEqual(render_payload["page_images"]["error_code"], "page_renderer_unavailable")
        self.assertEqual(page_status, 409)
        self.assertEqual(page_payload["document_render"]["status"], document_rendering.READY_STATUS)
        self.assertEqual(page_payload["document_render"]["page_image_error_code"], "page_renderer_unavailable")

    def test_matter_render_status_reports_docx_converter_unavailable(self):
        class UnavailableConverter:
            name = "test-unavailable"

            def is_available(self):
                return False

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Acme NDA.docx",
                    document_bytes=b"source docx bytes",
                    extracted_text="DOCX text.",
                    review_result={"clauses": []},
                    triage={
                        "triage_status": "legal_review",
                        "next_action": "Needs legal review",
                        "issue_count": 1,
                        "requirements_passed": 0,
                        "requirements_needs_review": 1,
                        "requirements_failed": 0,
                    },
                )
                with patch.object(document_rendering, "LibreOfficeDocxConverter", return_value=UnavailableConverter()):
                    status, payload, _headers = self.request_with_headers(
                        "GET",
                        f"/api/matters/{matter['id']}/render-status",
                    )
                    pdf_status, pdf_payload, _pdf_headers = self.request_with_headers(
                        "GET",
                        f"/api/matters/{matter['id']}/render-pdf",
                    )

        self.assertEqual(status, 200)
        render_payload = payload["document_render"]
        self.assertEqual(render_payload["status"], document_rendering.UNAVAILABLE_STATUS)
        self.assertEqual(render_payload["source_kind"], "docx")
        self.assertEqual(render_payload["source_label"], "Converted DOCX")
        self.assertEqual(render_payload["error_code"], "converter_unavailable")
        self.assertNotIn("pdf_url", render_payload)
        self.assertEqual(pdf_status, 409)
        self.assertEqual(pdf_payload["document_render"]["status"], document_rendering.UNAVAILABLE_STATUS)

    def test_pdf_export_status_reports_converter_health(self):
        class UnavailableConverter:
            name = "test-unavailable"

            def is_available(self):
                return False

        with patch.object(document_rendering, "LibreOfficeDocxConverter", return_value=UnavailableConverter()):
            status, payload, _headers = self.request_with_headers("GET", "/api/pdf-export/status")

        self.assertEqual(status, 200)
        self.assertEqual(payload["pdf_export"]["converter"], "test-unavailable")
        self.assertFalse(payload["pdf_export"]["available"])
        self.assertIn("LibreOffice/soffice", payload["pdf_export"]["message"])

    def test_render_status_poll_does_not_block_on_slow_rasterization(self):
        # The polled status endpoint must NOT rasterize synchronously on the
        # request thread: with a renderer that blocks past the grace window, the
        # poll returns a non-blocking "rendering" status instead of hanging.
        from nda_automation import matter_render_job

        block_render = threading.Event()

        class BlockingPdfPageRenderer:
            name = "blocking-pdf-pages"

            def is_available(self):
                return True

            def render_pdf_to_page_images(self, pdf_path, output_dir, *, dpi):
                # Hold the background render open past the poll's grace window.
                block_render.wait(timeout=10)
                image_path = output_dir / "page-1.png"
                image_path.write_bytes(b"\x89PNG\r\nlate page\n")
                return [
                    document_rendering.RenderedPdfPageImage(
                        page_number=1, image_path=image_path, width=10, height=10, dpi=dpi, scale=1.0
                    )
                ]

        source_pdf = b"%PDF-1.7\nslow render\n%%EOF\n"
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                patches = self.matter_store_patches(data_dir)
                with patches[0], patches[1], patches[2], \
                        patch.object(document_rendering, "PyMuPdfPageRenderer", BlockingPdfPageRenderer), \
                        patch.object(matter_render_job, "DEFAULT_RENDER_STATUS_POLL_GRACE_SECONDS", 0.1):
                    document_rendering.matter_render_coordinator().reset_for_tests()
                    matter = matter_store.create_matter(
                        source_filename="Slow NDA.pdf",
                        document_bytes=source_pdf,
                        extracted_text="PDF text.",
                        review_result={"clauses": []},
                        triage={"triage_status": "ready_to_sign", "issue_count": 0},
                    )
                    status, payload, _headers = self.request_with_headers(
                        "GET", f"/api/matters/{matter['id']}/render-status"
                    )
                    # The poll returned promptly with a non-terminal rendering
                    # status (it did not block until block_render was set).
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        payload["document_render"]["status"], document_rendering.RENDERING_STATUS
                    )
        finally:
            block_render.set()
            document_rendering.matter_render_coordinator().reset_for_tests()

    def test_matter_upload_defers_ai_review_and_does_not_run_it_at_create(self):
        # Manual upload now mirrors inbound + generation: the AI review is DEFERRED
        # (defer_ai_review=True). The matter is created immediately UN-REVIEWED and
        # the active review engine is NOT invoked synchronously at create -- the
        # on-demand Review/refresh path runs the AI later. This keeps upload off the
        # synchronous review path and prevents a transient AI outage from dropping
        # a manually-uploaded NDA.
        source_docx = make_docx([
            "Each party may disclose Confidential Information to the other party.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with (
                    patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}),
                    patch.object(
                        ingestion_service,
                        "review_nda_with_active_engine",
                    ) as active_review,
                ):
                    status, payload = self.request(
                        "POST",
                        "/api/matters",
                        {
                            "filename": "Acme NDA.docx",
                            "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        },
                    )
                stored_matter = matter_store.get_matter(payload["matter"]["id"])

        self.assertEqual(status, 201)
        # The review engine is never called at create under the deferred contract.
        active_review.assert_not_called()
        # The matter is persisted un-reviewed: no AI review result stored.
        self.assertFalse(stored_matter.get("review_result"))
        # The public view (what the frontend reads) reports ai_review_ran False, so
        # the UI surfaces the "Not reviewed" + Run-AI-review state.
        self.assertIs(payload["matter"].get("ai_review_ran"), False)

    def test_matter_upload_no_longer_fails_closed_when_ai_unavailable(self):
        # Because the AI review is deferred, an unavailable AI reviewer no longer
        # fail-CLOSES the upload with a 502. The matter is created un-reviewed and
        # the operator runs the review on demand later (which surfaces the no-AI
        # notification only at that point). The active engine is not even consulted
        # at create, so a configured-but-broken AI never blocks the import.
        source_docx = make_docx(["Each party may disclose Confidential Information to the other party."])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with (
                    patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}),
                    patch.object(
                        ingestion_service,
                        "review_nda_with_active_engine",
                        side_effect=ActiveReviewEngineError("AI-first review failed: no key"),
                    ),
                ):
                    status, payload = self.request(
                        "POST",
                        "/api/matters",
                        {
                            "filename": "Acme NDA.docx",
                            "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        },
                    )

        self.assertEqual(status, 201)
        self.assertIn("matter", payload)
        self.assertIs(payload["matter"].get("ai_review_ran"), False)

    def test_stale_matter_review_opens_saved_result_without_implicit_refresh(self):
        active_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_mode": "ai_first_compat",
            "overall_status": "meets_requirements",
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 0,
            "review_state": {
                "state": "pass",
                "overall_status": "meets_requirements",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
            "paragraphs": [{"id": "p1", "index": 1, "text": "Mutual NDA text."}],
            "contract_structure": {},
            "reference_resolver": {},
            "concept_classifier": {},
            "clauses": [
                {
                    "id": "mutuality",
                    "decision": "pass",
                    "passes": True,
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            "redline_edits": [],
            "active_review_engine": {
                "selected_engine": "ai_first",
                "executed_engine": "ai_first",
                "engine": "ai_first",
            },
        }
        active_result["playbook_runtime"] = self.active_playbook_review_runtime()

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Acme NDA.docx",
                    document_bytes=b"source docx bytes",
                    extracted_text="Mutual NDA text.",
                    review_result={"clauses": []},
                    triage={
                        "triage_status": "needs_redline",
                        "next_action": "Review redline",
                        "issue_count": 1,
                        "requirements_passed": 0,
                        "requirements_needs_review": 0,
                        "requirements_failed": 1,
                    },
                )
                with (
                    patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}),
                    patch.object(matter_routes, "review_nda_with_active_engine", return_value=deepcopy(active_result)) as active_review,
                ):
                    status, payload = self.request("GET", f"/api/matters/{matter['id']}/review")
                stored_matter = matter_store.get_matter(matter["id"])

        self.assertEqual(status, 200)
        active_review.assert_not_called()
        self.assertEqual(payload["review_result"]["clauses"], [])
        self.assertNotIn("review_mode", payload["review_result"])
        self.assertNotIn("active_review_engine", payload["review_result"])
        self.assertEqual(payload["review_refresh"]["stale"], True)
        self.assertEqual(stored_matter["review_result"], {"clauses": []})
        self.assertEqual(stored_matter["triage_status"], "needs_redline")

    def test_explicit_stale_matter_refresh_uses_active_review_engine_result(self):
        """ASYNC contract: POST /review-refresh returns 202 immediately, enqueues the
        AI review onto the worker pool, and NEVER runs the engine inline. The review
        result (ai_first engine) lands on the stored matter only after the worker
        body runs. The route must not call review_nda_with_active_engine inline.
        """
        active_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_mode": "ai_first_compat",
            "overall_status": "meets_requirements",
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 0,
            "review_state": {
                "state": "pass",
                "overall_status": "meets_requirements",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
            "paragraphs": [{"id": "p1", "index": 1, "text": "Mutual NDA text."}],
            "contract_structure": {},
            "reference_resolver": {},
            "concept_classifier": {},
            "clauses": [
                {
                    "id": "mutuality",
                    "decision": "pass",
                    "passes": True,
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            "redline_edits": [],
            "active_review_engine": {
                "selected_engine": "ai_first",
                "executed_engine": "ai_first",
                "engine": "ai_first",
            },
        }
        active_result["playbook_runtime"] = self.active_playbook_review_runtime()

        # Swap in a fresh, isolated pool so the real enqueue never fires heavy work.
        orig_pool = ingestion_service._INBOUND_REVIEW_POOL
        pool = ingestion_service._InboundReviewWorkerPool()
        pool.configure(lambda mid, owner: None)  # no-op handler -- never runs heavy work
        ingestion_service._INBOUND_REVIEW_POOL = pool
        with ingestion_service._ON_DEMAND_REVIEW_LOCK:
            ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()
        # Force _ai_first_review_enabled() to True so the route sees AI as available.
        orig_ai_enabled = matter_routes._ai_first_review_enabled
        matter_routes._ai_first_review_enabled = lambda: True
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                patches = self.matter_store_patches(data_dir)
                with patches[0], patches[1], patches[2]:
                    matter = matter_store.create_matter(
                        source_filename="Acme NDA.docx",
                        document_bytes=b"source docx bytes",
                        extracted_text="Mutual NDA text.",
                        review_result={"clauses": []},
                        triage={
                            "triage_status": "needs_redline",
                            "next_action": "Review redline",
                            "issue_count": 1,
                            "requirements_passed": 0,
                            "requirements_needs_review": 0,
                            "requirements_failed": 1,
                        },
                    )
                    # Spy: assert the route did NOT call the engine inline.
                    engine_calls: list = []
                    orig_engine = matter_routes.review_nda_with_active_engine
                    matter_routes.review_nda_with_active_engine = lambda *a, **k: engine_calls.append("x") or {}
                    try:
                        with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}):
                            status, payload = self.request("POST", f"/api/matters/{matter['id']}/review-refresh")
                    finally:
                        matter_routes.review_nda_with_active_engine = orig_engine

                    # --- 202 contract assertions ---
                    self.assertEqual(status, 202)
                    self.assertEqual(payload["review_status"], "in_progress")
                    self.assertTrue(payload["job_scheduled"])
                    self.assertEqual(engine_calls, [], "route must NOT run the AI engine inline")
                    # The enqueue stamps in_progress on the stored matter.
                    self.assertEqual(matter_store.get_matter(matter["id"])["review_status"], "in_progress")

                    # --- Land the completed review via the worker body directly ---
                    from nda_automation.matter_repository import DiskMatterRepository as _DiskRepo
                    with patch.dict(os.environ, {"NDA_INBOUND_AI_REVIEW_ENABLED": "true"}):
                        ingestion_service._perform_inbound_ai_review(
                            str(matter["id"]),
                            repository=_DiskRepo(),
                            owner_user_id="",
                            review_engine_func=lambda t, **kw: deepcopy(active_result),
                        )

                    # --- Eventual completed-state assertions (preserve original intent) ---
                    stored_matter = matter_store.get_matter(matter["id"])
                    self.assertEqual(
                        stored_matter["review_result"]["active_review_engine"]["executed_engine"],
                        "ai_first",
                    )
                    self.assertEqual(
                        stored_matter["review_result"]["active_review_engine"]["selected_engine"],
                        "ai_first",
                    )
                    self.assertEqual(stored_matter["review_status"], "completed")
        finally:
            ingestion_service._INBOUND_REVIEW_POOL = orig_pool
            matter_routes._ai_first_review_enabled = orig_ai_enabled
            with ingestion_service._ON_DEMAND_REVIEW_LOCK:
                ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()

    def test_get_review_refresh_query_is_read_only(self):
        active_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_mode": "ai_first_compat",
            "overall_status": "meets_requirements",
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 0,
            "review_state": {
                "state": "pass",
                "overall_status": "meets_requirements",
                "counts": {"pass": 1, "review": 0, "check": 0},
            },
            "paragraphs": [{"id": "p1", "index": 1, "text": "Mutual NDA text."}],
            "contract_structure": {},
            "reference_resolver": {},
            "concept_classifier": {},
            "clauses": [
                {
                    "id": "mutuality",
                    "decision": "pass",
                    "passes": True,
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            "redline_edits": [],
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Acme NDA.docx",
                    document_bytes=b"source docx bytes",
                    extracted_text="Mutual NDA text.",
                    review_result={"clauses": []},
                    triage={
                        "triage_status": "needs_redline",
                        "next_action": "Review redline",
                        "issue_count": 1,
                        "requirements_passed": 0,
                        "requirements_needs_review": 0,
                        "requirements_failed": 1,
                    },
                )
                with patch.object(matter_routes, "review_nda_with_active_engine", return_value=deepcopy(active_result)) as active_review:
                    status, payload = self.request("GET", f"/api/matters/{matter['id']}/review?refresh=1")
                stored_matter = matter_store.get_matter(matter["id"])

        self.assertEqual(status, 200)
        active_review.assert_not_called()
        self.assertEqual(payload["review_refresh"]["stale"], True)
        self.assertEqual(stored_matter["review_result"], {"clauses": []})
        self.assertEqual(stored_matter["triage_status"], "needs_redline")

    def test_stale_matter_refresh_clears_saved_redline_draft_before_export(self):
        """ASYNC contract: POST /review-refresh returns 202 immediately. The stale
        redline_draft is cleared and the fresh review result (with 1 redline_edit)
        lands on the stored matter only after the worker body runs -- not inline in
        the route. The export that follows must see the refreshed review (1 redline).
        """
        source_text = "This Agreement shall be governed by the laws of California."
        source_docx = make_docx([source_text])
        refreshed_redline = {
            "id": "redline-governing-law-new",
            "clause_id": "governing_law",
            "paragraph_id": "p1",
            "action": "replace_paragraph",
            "original_text": source_text,
            "replacement_text": "This Agreement shall be governed by the laws of Delaware.",
            "status": "proposed",
        }
        active_result = {
            "review_engine_version": REVIEW_ENGINE_VERSION,
            "review_mode": "ai_first_compat",
            "overall_status": "redline_required",
            "requirements_passed": 0,
            "requirements_needs_review": 0,
            "requirements_failed": 1,
            "review_state": {
                "state": "check",
                "overall_status": "redline_required",
                "counts": {"pass": 0, "review": 0, "check": 1},
            },
            "paragraphs": [{"id": "p1", "index": 1, "text": source_text}],
            "contract_structure": {},
            "reference_resolver": {},
            "concept_classifier": {},
            "clauses": [
                {
                    "id": "governing_law",
                    "decision": "fail",
                    "passes": False,
                    "structure_context": {},
                    "review_state": {},
                }
            ],
            "redline_edits": [refreshed_redline],
            "active_review_engine": {
                "selected_engine": "ai_first",
                "executed_engine": "ai_first",
                "engine": "ai_first",
            },
        }
        active_result["playbook_runtime"] = self.active_playbook_review_runtime()
        captured_redline_counts = []

        def capture_redline_build(_source_bytes, review_result, **_kwargs):
            captured_redline_counts.append(len(review_result.get("redline_edits") or []))
            return source_docx

        # Swap in a fresh, isolated pool so the real enqueue never fires heavy work.
        orig_pool = ingestion_service._INBOUND_REVIEW_POOL
        pool = ingestion_service._InboundReviewWorkerPool()
        pool.configure(lambda mid, owner: None)  # no-op handler -- never runs heavy work
        ingestion_service._INBOUND_REVIEW_POOL = pool
        with ingestion_service._ON_DEMAND_REVIEW_LOCK:
            ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()
        orig_ai_enabled = matter_routes._ai_first_review_enabled
        matter_routes._ai_first_review_enabled = lambda: True
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                patches = self.matter_store_patches(data_dir)
                with patches[0], patches[1], patches[2]:
                    matter = matter_store.create_matter(
                        source_filename="Draft NDA.docx",
                        document_bytes=source_docx,
                        extracted_text=source_text,
                        review_result={"clauses": []},
                        triage={
                            "triage_status": "needs_redline",
                            "next_action": "Review redline",
                            "issue_count": 1,
                            "requirements_passed": 0,
                            "requirements_needs_review": 0,
                            "requirements_failed": 1,
                        },
                    )
                    matter_store.update_redline_draft(
                        matter["id"],
                        {
                            "redline_decisions": {"redline-governing-law-old": False},
                            "template_selections": {"redline-governing-law-old": "india"},
                            "export_redline_edits": [],
                            "manual_redline_edits": [],
                        },
                    )
                    self.assertIn("redline_draft", matter_store.get_matter(matter["id"]))

                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first"}):
                        review_status, review_payload = self.request("POST", f"/api/matters/{matter['id']}/review-refresh")

                    # --- 202 contract assertions ---
                    self.assertEqual(review_status, 202)
                    self.assertEqual(review_payload["review_status"], "in_progress")
                    self.assertTrue(review_payload["job_scheduled"])

                    # --- Land the completed review via the worker body directly ---
                    from nda_automation.matter_repository import DiskMatterRepository as _DiskRepo
                    with patch.dict(os.environ, {"NDA_INBOUND_AI_REVIEW_ENABLED": "true"}):
                        ingestion_service._perform_inbound_ai_review(
                            str(matter["id"]),
                            repository=_DiskRepo(),
                            owner_user_id="",
                            review_engine_func=lambda t, **kw: deepcopy(active_result),
                        )

                    # --- Eventual completed-state assertions (preserve original intent) ---
                    stored_after_refresh = matter_store.get_matter(matter["id"])
                    # The worker persist path clears stale redline drafts.
                    self.assertNotIn("redline_draft", stored_after_refresh)
                    self.assertEqual(stored_after_refresh["review_status"], "completed")

                    # Export uses the refreshed review (1 redline_edit from active_result).
                    with patch.object(server_module.redline_export_service.docx_package_renderer, "build_source_redline_docx", side_effect=capture_redline_build):
                        with patch.object(server_module.redline_export_service.docx_package_renderer, "validate_docx_open_health", return_value=[]):
                            export_status, _export_payload = self.request(
                                "POST",
                                "/api/export-review-docx",
                                {"matter_id": matter["id"]},
                            )
                    self.assertEqual(export_status, 200)
                    self.assertEqual(captured_redline_counts, [1])
        finally:
            ingestion_service._INBOUND_REVIEW_POOL = orig_pool
            matter_routes._ai_first_review_enabled = orig_ai_enabled
            with ingestion_service._ON_DEMAND_REVIEW_LOCK:
                ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()

    def test_gmail_attachment_import_creates_unreviewed_matter(self):
        # Inbound Gmail import now creates the matter UN-REVIEWED: NO review runs at
        # create (the create-time active-engine call is gone -- it was tied to the
        # OOM/cost storm). The full AI review runs asynchronously off the poll thread.
        source_docx = make_docx(["Each party may disclose Confidential Information to the other party."])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with (
                    # Kill-switch OFF so the async AI review never runs inline here --
                    # this test pins the CREATE-time behaviour (un-reviewed) only.
                    patch.dict(os.environ, {"NDA_INBOUND_AI_REVIEW_ENABLED": "false"}),
                    patch.object(gmail_integration, "_gmail_attachment_already_imported", return_value=False),
                    patch.object(gmail_integration, "_attachment_bytes", return_value=source_docx),
                    patch.object(ingestion_service, "review_nda_with_active_engine") as active_review,
                ):
                    matter, skip = gmail_integration._import_inbound_attachment(
                        object(),
                        "msg_active_ai",
                        {"attachment_id": "att_active_ai", "filename": "Active NDA.docx", "part_id": "1"},
                        {
                            "gmail_account": "legal@example.com",
                            "gmail_message_id": "msg_active_ai",
                            "gmail_thread_id": "thread_active_ai",
                            "reply_to": "counterparty@example.com",
                        },
                    )
                stored_matter = matter_store.get_matter(matter["id"])

        self.assertIsNone(skip)
        # The create-time review engine is NEVER called on the inbound poll path.
        active_review.assert_not_called()
        self.assertEqual(matter["source_type"], "gmail_inbound")
        # The matter is created un-reviewed -- it still carries the extracted text so
        # the async AI review can run later -- and nothing crashes on the null review.
        self.assertIsNone(stored_matter["review_result"])
        self.assertTrue(str(stored_matter.get("extracted_text") or "").strip())

    def test_matter_review_refreshes_stale_clause_decisions(self):
        """ASYNC contract: POST /review-refresh returns 202 immediately. The real AI
        stub (NDA_AI_ASSESSMENT_STUB=1) runs via the worker body directly, producing
        the grounded-downgrade verdict for term_and_survival. Assertions are on the
        STORED matter after the worker body completes -- not the 202 response body.
        """
        text = (
            "TERM AND TERMINATION: This Agreement is effective from the date hereof, "
            "and shall terminate on the earlier of: (i) the date on which a definitive agreement "
            "is executed with respect to the Purpose which includes detailed confidentiality provisions; "
            "or (ii) the expiry of 18 (eighteen) months from the date of this Agreement. "
            "The obligations set out at clauses 2, 3, 4 and 5 of this Agreement shall survive "
            "the expiry or termination of this Agreement for a period of 3 (three) years."
        )
        stale_review = {
            "clauses": [
                {
                    "id": "term_and_survival",
                    "status": "not_present",
                    "passes": False,
                    "issue_type": "missing",
                    "finding": "No fixed term or survival period of up to five years was found.",
                }
            ],
        }

        # Swap in a fresh, isolated pool so the real enqueue never fires heavy work.
        orig_pool = ingestion_service._INBOUND_REVIEW_POOL
        pool = ingestion_service._InboundReviewWorkerPool()
        pool.configure(lambda mid, owner: None)  # no-op handler -- never runs heavy work
        ingestion_service._INBOUND_REVIEW_POOL = pool
        with ingestion_service._ON_DEMAND_REVIEW_LOCK:
            ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()
        orig_ai_enabled = matter_routes._ai_first_review_enabled
        matter_routes._ai_first_review_enabled = lambda: True
        try:
            with tempfile.TemporaryDirectory() as data_dir:
                patches = self.matter_store_patches(data_dir)
                with patches[0], patches[1], patches[2]:
                    matter = matter_store.create_matter(
                        source_filename="Air India NDA.docx",
                        document_bytes=b"source docx bytes",
                        extracted_text=text,
                        review_result=stale_review,
                        triage={
                            "triage_status": "needs_redline",
                            "next_action": "Review redline",
                            "issue_count": 1,
                            "requirements_passed": 0,
                            "requirements_needs_review": 0,
                            "requirements_failed": 1,
                        },
                    )
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        review_status, review_payload = self.request("POST", f"/api/matters/{matter['id']}/review-refresh")

                    # --- 202 contract assertions ---
                    self.assertEqual(review_status, 202)
                    self.assertEqual(review_payload["review_status"], "in_progress")
                    self.assertTrue(review_payload["job_scheduled"])

                    # --- Land the completed review via the worker body using the real AI stub ---
                    from nda_automation.matter_repository import DiskMatterRepository as _DiskRepo
                    with patch.dict(os.environ, {
                        ACTIVE_REVIEW_ENGINE_ENV: "ai_first",
                        "NDA_AI_REVIEW_ENABLED": "true",
                        "NDA_AI_ASSESSMENT_STUB": "1",
                        "NDA_INBOUND_AI_REVIEW_ENABLED": "true",
                    }):
                        ingestion_service._perform_inbound_ai_review(
                            str(matter["id"]),
                            repository=_DiskRepo(),
                            owner_user_id="",
                            review_engine_func=ingestion_service.review_nda_with_active_engine,
                        )

                    # --- Eventual completed-state assertions (preserve original intent) ---
                    stored_matter = matter_store.get_matter(matter["id"])
                    self.assertEqual(stored_matter["review_result"]["review_engine_version"], REVIEW_ENGINE_VERSION)
                    self.assertEqual(stored_matter["review_status"], "completed")

                    term_clause = next(
                        clause
                        for clause in stored_matter["review_result"]["clauses"]
                        if clause["id"] == "term_and_survival"
                    )
                    self.assertEqual(term_clause["status"], "check")
                    self.assertFalse(term_clause["passes"])
                    self.assertEqual(term_clause["decision"], "review")
                    self.assertTrue(term_clause["needs_review"])
                    self.assertEqual(term_clause["reason_code"], "ungrounded_finding")
                    self.assertIn("could not tie this to a specific quote", term_clause["finding"])
        finally:
            ingestion_service._INBOUND_REVIEW_POOL = orig_pool
            matter_routes._ai_first_review_enabled = orig_ai_enabled
            with ingestion_service._ON_DEMAND_REVIEW_LOCK:
                ingestion_service._ON_DEMAND_REVIEW_MATTERS.clear()

    def test_matter_upload_supports_manual_upload_source(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of Delaware.",
            "The Recipient shall keep Confidential Information confidential for five years.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Manual NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "manual_upload",
                        "sender": "counterparty@example.com",
                        "subject": "Uploaded NDA",
                    },
                )

        matter = payload["matter"]
        self.assertEqual(status, 201)
        self.assertEqual(matter["source_type"], "manual_upload")
        self.assertEqual(matter["board_column"], "in_review")
        self.assertEqual(matter["sender"], "counterparty@example.com")
        self.assertEqual(matter["recipient_email"], "counterparty@example.com")
        self.assertEqual(matter["subject"], "Uploaded NDA")

    def test_matter_upload_allows_valid_manual_target_stage(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of Delaware.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                reviewed_status, reviewed_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Reviewed NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "manual_upload",
                        "board_column": "reviewed",
                    },
                )
                sent_status, sent_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Sent NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "manual_upload",
                        "board_column": "sent",
                    },
                )
                invalid_status, invalid_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Invalid NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "manual_upload",
                        "board_column": "redline_ready",
                    },
                )

        self.assertEqual(reviewed_status, 201)
        self.assertEqual(reviewed_payload["matter"]["board_column"], "reviewed")
        self.assertEqual(sent_status, 201)
        self.assertEqual(sent_payload["matter"]["board_column"], "sent")
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_payload["error"], "Unsupported manual upload stage.")

    def test_demo_reset_clears_repository_and_uploaded_documents(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Reset NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "manual_upload",
                    },
                )
                matter = create_payload["matter"]
                stored_matter = matter_store.get_matter(matter["id"])
                stored_path = matter_store.UPLOADS_DIR / stored_matter["stored_filename"]
                reset_status, reset_payload = self.request("POST", "/api/demo/reset")
                stored_path_exists = stored_path.exists()
                list_status, list_payload = self.request("GET", "/api/matters")

        self.assertEqual(create_status, 201)
        self.assertEqual(reset_status, 200)
        self.assertEqual(reset_payload["removed"], 1)
        self.assertEqual(reset_payload["matters"], [])
        self.assertEqual(list_status, 200)
        self.assertEqual(list_payload["matters"], [])
        self.assertFalse(stored_path_exists)

    def test_demo_reset_does_not_delete_documents_when_save_fails(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_store.create_matter(
                    source_filename="Reset Failure NDA.docx",
                    document_bytes=b"source-doc",
                    extracted_text="source doc",
                    review_result={"clauses": []},
                    triage={},
                )
                with (
                    patch.object(matter_store, "_delete_matter_record", side_effect=matter_store.MatterStoreError("save failed")),
                    patch.object(matter_store, "_delete_stored_document") as delete_stored_document,
                ):
                    with self.assertRaisesRegex(matter_store.MatterStoreError, "save failed"):
                        matter_store.reset_demo_repository()

        delete_stored_document.assert_not_called()

    def test_matter_delete_removes_repository_item_and_uploaded_document(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Delete Me.docx",
                    document_bytes=b"delete-me",
                    extracted_text="delete me",
                    review_result={"clauses": []},
                    triage={"triage_status": "needs_redline", "issue_count": 1},
                )
                stored_path = matter_store.UPLOADS_DIR / matter["stored_filename"]
                delete_status, delete_payload = self.request("DELETE", f"/api/matters/{matter['id']}")
                fetch_status, fetch_payload = self.request("GET", f"/api/matters/{matter['id']}")
                list_status, list_payload = self.request("GET", "/api/matters")
                missing_delete_status, missing_delete_payload = self.request("DELETE", "/api/matters/matter_missing")
                stored_path_exists = stored_path.exists()

        self.assertEqual(delete_status, 200)
        self.assertEqual(delete_payload["deleted"]["id"], matter["id"])
        self.assertNotIn("stored_filename", delete_payload["deleted"])
        self.assertEqual(fetch_status, 404)
        self.assertEqual(fetch_payload["error"], "NDA not found.")
        self.assertEqual(list_status, 200)
        self.assertEqual(list_payload["matters"], [])
        self.assertEqual(missing_delete_status, 404)
        self.assertEqual(missing_delete_payload["error"], "NDA not found.")
        self.assertFalse(stored_path_exists)

    def test_matter_delete_purges_render_cache(self):
        # Rendering populates a per-matter cache entry under DATA_DIR/cache;
        # deleting the matter must purge that entry so rendered artifacts do not
        # outlive the matter.
        source_pdf = b"%PDF-1.7\npurge-me\n%%EOF\n"
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename="Purge NDA.pdf",
                    document_bytes=source_pdf,
                    extracted_text="PDF text.",
                    review_result={"clauses": []},
                    triage={"triage_status": "ready_to_sign", "issue_count": 0},
                )
                # Populate the render cache via the status endpoint.
                render_status, _payload, _headers = self.request_with_headers(
                    "GET", f"/api/matters/{matter['id']}/render-status"
                )
                cache_root = document_rendering.document_render_cache_dir()
                entries_before = sorted(p.name for p in cache_root.iterdir() if p.is_dir()) if cache_root.is_dir() else []

                delete_status, _delete_payload = self.request("DELETE", f"/api/matters/{matter['id']}")
                entries_after = sorted(p.name for p in cache_root.iterdir() if p.is_dir()) if cache_root.is_dir() else []

        self.assertEqual(render_status, 200)
        self.assertTrue(entries_before, "render did not populate a cache entry")
        self.assertEqual(delete_status, 200)
        self.assertEqual(entries_after, [], "render cache entry survived matter deletion")

    def test_public_matter_uses_explicit_allowlist(self):
        public = matter_view.public_matter({
            "id": "matter_1",
            "sender": "Sender <sender@example.com>",
            "subject": "NDA",
            "stored_filename": "internal.docx",
            "gmail_message_id": "msg_123",
            "gmail_attachment_id": "att_123",
            "review_result": {"clauses": [{"id": "mutuality", "decision": "pass"}]},
            "extracted_text": "Text",
            "redline_draft": {"manual_redline_edits": []},
        })

        self.assertEqual(public["id"], "matter_1")
        self.assertEqual(public["recipient_email"], "sender@example.com")
        self.assertEqual(public["can_send_redline"], True)
        self.assertNotIn("stored_filename", public)
        self.assertNotIn("gmail_message_id", public)
        self.assertNotIn("gmail_attachment_id", public)
        self.assertNotIn("review_result", public)
        self.assertNotIn("extracted_text", public)
        self.assertNotIn("redline_draft", public)
        self.assertIn("document_downloads", public)
        self.assertNotIn("internal.docx", str(public["document_downloads"]))
        self.assertEqual(public["has_redline_draft"], True)

    def test_public_matters_list_omits_heavy_detail_fields(self):
        public = matter_view.public_matters([{
            "id": "matter_1",
            "sender": "Sender <sender@example.com>",
            "subject": "NDA",
            "extracted_text": "Large extracted document text",
            "redline_draft": {"manual_redline_edits": []},
            "review_result": {"clauses": []},
        }])[0]

        self.assertEqual(public["id"], "matter_1")
        self.assertEqual(public["recipient_email"], "sender@example.com")
        self.assertNotIn("extracted_text", public)
        self.assertNotIn("redline_draft", public)
        self.assertNotIn("review_result", public)

    def test_public_matter_rejects_sender_display_name_email_spoof(self):
        public = matter_view.public_matter({
            "id": "matter_1",
            "sender": '"jane@x.com" <attacker@evil.com>',
            "subject": "NDA",
        })

        self.assertEqual(public["recipient_email"], "")
        self.assertEqual(public["can_send_redline"], False)

    def test_public_matter_prefers_reply_to_recipient(self):
        public = matter_view.public_matter({
            "id": "matter_1",
            "reply_to": "Counsel <reply@example.com>",
            "sender": "Noreply <noreply@example.com>",
            "subject": "NDA",
        })

        self.assertEqual(public["recipient_email"], "reply@example.com")
        self.assertEqual(public["can_send_redline"], True)

    def test_public_matter_blocks_send_when_review_is_needed(self):
        # AI-reviewed matter that needs review: SEND AUTHORITY blocks AND the surfaced
        # review_state shows the "review" verdict (the AI-reviewed path is unchanged).
        public = matter_view.public_matter({
            "id": "matter_1",
            "sender": "Sender <sender@example.com>",
            "subject": "NDA",
            "requirements_needs_review": 1,
            "review_result": {
                "active_review_engine": {"executed_engine": "ai_first"},
                "overall_status": "needs_review",
                "requirements_needs_review": 1,
            },
        })

        self.assertEqual(public["recipient_email"], "sender@example.com")
        self.assertEqual(public["can_send_redline"], False)
        self.assertEqual(public["review_state"]["state"], "review")
        self.assertTrue(public["review_state"]["blocks_send"])
        self.assertIn("human review", public["send_block_reason"])

    def test_deterministic_only_review_blocks_send_but_surfaces_no_verdict(self):
        # DETERMINISTIC-GHOST DEMOTION at the source: a NON-AI matter (no ai_first
        # active-engine marker) that the deterministic engine flagged needs-review
        # must STILL block send (send authority derives from the raw review_result),
        # but its surfaced review_state must NOT show a "review" verdict -- it is
        # demoted to PENDING and the raw requirements_* integers are dropped, so no
        # consumer reads a deterministic verdict.
        public = matter_view.public_matter({
            "id": "matter_1",
            "sender": "Sender <sender@example.com>",
            "subject": "NDA",
            "requirements_needs_review": 1,
            "review_result": {
                "overall_status": "needs_review",
                "requirements_needs_review": 1,
            },
        })

        # Send authority UNCHANGED -- still blocks.
        self.assertEqual(public["can_send_redline"], False)
        self.assertTrue(public["needs_human_review"])
        self.assertTrue(public["blocks_send"])
        self.assertIn("human review", public["send_block_reason"])
        # Display state DEMOTED -- no deterministic verdict surfaced.
        self.assertFalse(public["ai_review_ran"])
        self.assertEqual(public["review_state"]["state"], "pending")
        self.assertFalse(public["review_state"]["blocks_send"])
        self.assertNotIn("requirements_needs_review", public)

    def test_public_matter_allows_redline_send_for_pdf_source(self):
        public = matter_view.public_matter({
            "id": "matter_1",
            "sender": "Sender <sender@example.com>",
            "source_filename": "Uploaded NDA.pdf",
            "source_type": "pdf",
            "subject": "NDA",
        })

        self.assertEqual(public["recipient_email"], "sender@example.com")
        self.assertEqual(public["can_send_redline"], True)
        self.assertNotIn("send_block_reason", public)

    def test_public_matter_blocks_connected_account_sender(self):
        public = matter_view.public_matter({
            "id": "matter_1",
            "gmail_account": "daniyal.ahmad@aspora.com",
            "sender": "Daniyal Ahmad <daniyal.ahmad@aspora.com>",
            "subject": "Re: NDA",
        })

        self.assertEqual(public["recipient_email"], "daniyal.ahmad@aspora.com")
        self.assertEqual(public["can_send_redline"], False)
        self.assertIn("self-sent Gmail message", public["send_block_reason"])

    def test_public_matter_surfaces_missing_reply_recipient_block(self):
        public = matter_view.public_matter({
            "id": "matter_1",
            "human_reviewed": True,
            "sender": "Manual upload",
            "subject": "Uploaded NDA",
            "review_result": {
                "overall_status": "needs_redline",
                "requirements_needs_review": 0,
            },
        })

        self.assertEqual(public["recipient_email"], "")
        self.assertEqual(public["can_send_redline"], False)
        self.assertIn("valid reply recipient", public["send_block_reason"])

    def test_matter_retention_prunes_old_closed_uploads(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patch.dict(os.environ, {"NDA_MATTER_RETENTION_LIMIT": "2"}):
                with patches[0], patches[1], patches[2]:
                    first = matter_store.create_matter(
                        source_filename="first.docx",
                        document_bytes=b"first",
                        extracted_text="first",
                        review_result={"clauses": []},
                        triage={},
                    )
                    first_path = matter_store.UPLOADS_DIR / first["stored_filename"]
                    matter_store.update_matter_stage(first["id"], "signed_closed")
                    second = matter_store.create_matter(
                        source_filename="second.docx",
                        document_bytes=b"second",
                        extracted_text="second",
                        review_result={"clauses": []},
                        triage={},
                    )
                    third = matter_store.create_matter(
                        source_filename="third.docx",
                        document_bytes=b"third",
                        extracted_text="third",
                        review_result={"clauses": []},
                        triage={},
                    )
                    matters = matter_store.list_matters()
                    first_path_exists = first_path.exists()

        self.assertEqual({matter["id"] for matter in matters}, {second["id"], third["id"]})
        self.assertFalse(first_path_exists)

    def test_matter_retention_keeps_active_uploads_over_limit(self):
        telemetry.reset()
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patch.dict(os.environ, {"NDA_MATTER_RETENTION_LIMIT": "2"}):
                with patches[0], patches[1], patches[2]:
                    first = matter_store.create_matter(
                        source_filename="first.docx",
                        document_bytes=b"first",
                        extracted_text="first",
                        review_result={"clauses": []},
                        triage={},
                    )
                    first_path = matter_store.UPLOADS_DIR / first["stored_filename"]
                    second = matter_store.create_matter(
                        source_filename="second.docx",
                        document_bytes=b"second",
                        extracted_text="second",
                        review_result={"clauses": []},
                        triage={},
                    )
                    third = matter_store.create_matter(
                        source_filename="third.docx",
                        document_bytes=b"third",
                        extracted_text="third",
                        review_result={"clauses": []},
                        triage={},
                    )
                    matters = matter_store.list_matters()
                    first_path_exists = first_path.exists()

        self.assertEqual({matter["id"] for matter in matters}, {first["id"], second["id"], third["id"]})
        self.assertTrue(first_path_exists)
        self.assertEqual(telemetry.snapshot()["counters"]["matter_retention_over_cap_without_prune"], 1)

    def test_matter_retention_keeps_pruned_matter_when_archive_fails(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patch.dict(os.environ, {"NDA_MATTER_RETENTION_LIMIT": "1"}):
                with patches[0], patches[1], patches[2]:
                    first = matter_store.create_matter(
                        source_filename="first.docx",
                        document_bytes=b"first",
                        extracted_text="first",
                        review_result={"clauses": []},
                        triage={},
                    )
                    first_path = matter_store.UPLOADS_DIR / first["stored_filename"]
                    matter_store.update_matter_stage(first["id"], "signed_closed")
                    with patch.object(matter_store, "_archive_pruned_matters", return_value=False):
                        second = matter_store.create_matter(
                            source_filename="second.docx",
                            document_bytes=b"second",
                            extracted_text="second",
                            review_result={"clauses": []},
                            triage={},
                        )
                    matters = matter_store.list_matters()
                    first_path_exists = first_path.exists()

        self.assertEqual({matter["id"] for matter in matters}, {first["id"], second["id"]})
        self.assertTrue(first_path_exists)

    def test_matter_create_removes_upload_when_save_fails(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(matter_store, "_save_matter_record", side_effect=matter_store.MatterStoreError("boom")):
                    with self.assertRaises(matter_store.MatterStoreError):
                        matter_store.create_matter(
                            source_filename="orphan.docx",
                            document_bytes=b"orphan",
                            extracted_text="orphan",
                            review_result={"clauses": []},
                            triage={},
                        )
                uploaded_files = list(matter_store.UPLOADS_DIR.glob("*"))

        self.assertEqual(uploaded_files, [])

    def test_matter_create_prunes_uploads_after_successful_save(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patch.dict(os.environ, {"NDA_MATTER_RETENTION_LIMIT": "1"}):
                with patches[0], patches[1], patches[2]:
                    first = matter_store.create_matter(
                        source_filename="first.docx",
                        document_bytes=b"first",
                        extracted_text="first",
                        review_result={"clauses": []},
                        triage={},
                    )
                    first_path = matter_store.UPLOADS_DIR / first["stored_filename"]
                    with patch.object(matter_store, "_save_matter_record", side_effect=matter_store.MatterStoreError("boom")):
                        with self.assertRaises(matter_store.MatterStoreError):
                            matter_store.create_matter(
                                source_filename="second.docx",
                                document_bytes=b"second",
                                extracted_text="second",
                                review_result={"clauses": []},
                                triage={},
                            )
                    uploaded_files = list(matter_store.UPLOADS_DIR.glob("*"))
                    self.assertTrue(first_path.exists())
                    self.assertEqual(uploaded_files, [first_path])

    def test_matter_create_caps_source_filename_length(self):
        long_filename = f"{'a' * 400}.docx"
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = matter_store.create_matter(
                    source_filename=long_filename,
                    document_bytes=b"docx",
                    extracted_text="docx",
                    review_result={"clauses": []},
                    triage={},
                )

        self.assertLessEqual(len(matter["source_filename"]), matter_store.MAX_SOURCE_FILENAME_LENGTH)
        self.assertLessEqual(len(matter["stored_filename"]), matter_store.MAX_SOURCE_FILENAME_LENGTH + len("matter_000000000000-"))
        self.assertTrue(matter["stored_filename"].endswith(".docx"))

    def test_matter_retention_prunes_by_entry_not_duplicate_id(self):
        matters = [
            {"id": "matter_old", "updated_at": "2026-01-01T00:00:00+00:00", "board_column": "signed_closed"},
            {"id": "matter_old", "updated_at": "2026-01-02T00:00:00+00:00", "board_column": "in_review"},
            {"id": "matter_new", "updated_at": "2026-01-03T00:00:00+00:00", "board_column": "in_review"},
        ]

        with patch.dict(os.environ, {"NDA_MATTER_RETENTION_LIMIT": "2"}):
            kept, pruned = matter_store._prune_stored_matters(matters, protected_matter_id="matter_new")

        self.assertEqual(kept, [matters[1], matters[2]])
        self.assertEqual(pruned, [matters[0]])

    def test_gmail_attachment_dedupe_keeps_distinct_docs_sharing_message_filename(self):
        # Two genuinely DIFFERENT documents arrive under the same gmail message +
        # same filename (gmail's attachment ids are unstable). A shared filename is
        # not a content identity, so both must be preserved — not merged away.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                first = matter_store.create_matter(
                    source_filename="Counterparty NDA.docx",
                    document_bytes=b"first",
                    extracted_text="first",
                    review_result={"clauses": []},
                    triage={},
                    source_type="gmail_inbound",
                    intake_metadata={
                        "attachment_filename": "Counterparty NDA.docx",
                        "gmail_attachment_id": "unstable_att_1",
                        "gmail_message_id": "msg_123",
                    },
                )
                first_path = matter_store.UPLOADS_DIR / first["stored_filename"]
                second = matter_store.create_matter(
                    source_filename="Counterparty NDA.docx",
                    document_bytes=b"second",
                    extracted_text="second",
                    review_result={"clauses": []},
                    triage={},
                    source_type="gmail_inbound",
                    intake_metadata={
                        "attachment_filename": "Counterparty NDA.docx",
                        "gmail_attachment_id": "unstable_att_2",
                        "gmail_message_id": "msg_123",
                    },
                )
                second_path = matter_store.UPLOADS_DIR / second["stored_filename"]
                # A lookup keyed only on message + filename (no content hash) cannot
                # confirm either stored doc is the same one, so it is not a duplicate.
                duplicate = matter_store.find_gmail_attachment(
                    "msg_123",
                    "unstable_att_3",
                    attachment_filename="Counterparty NDA.docx",
                )
                removed = matter_store.deduplicate_gmail_matters()
                matters = matter_store.list_matters()
                first_path_exists = first_path.exists()
                second_path_exists = second_path.exists()

        self.assertIsNone(duplicate)
        self.assertEqual(removed, 0)
        self.assertEqual({matter["id"] for matter in matters}, {first["id"], second["id"]})
        self.assertTrue(first_path_exists)
        self.assertTrue(second_path_exists)

    def test_gmail_attachment_dedupe_cleanup_uses_live_lookup_keys(self):
        # A legacy matter (no content hash) and a hashed matter share a message +
        # filename but hold DIFFERENT documents. A content-hash lookup still finds
        # the matching hashed doc, but the sweep must not merge the distinct legacy
        # doc away on the filename alone.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                legacy = matter_store.create_matter(
                    source_filename="Counterparty NDA.docx",
                    document_bytes=b"legacy",
                    extracted_text="legacy",
                    review_result={"clauses": []},
                    triage={},
                    source_type="gmail_inbound",
                    intake_metadata={
                        "attachment_filename": "Counterparty NDA.docx",
                        "gmail_attachment_id": "unstable_att_1",
                        "gmail_message_id": "msg_123",
                    },
                )
                hashed = matter_store.create_matter(
                    source_filename="Counterparty NDA.docx",
                    document_bytes=b"hashed",
                    extracted_text="hashed",
                    review_result={"clauses": []},
                    triage={},
                    source_type="gmail_inbound",
                    intake_metadata={
                        "attachment_filename": "Counterparty NDA.docx",
                        "gmail_attachment_id": "unstable_att_2",
                        "gmail_attachment_sha256": "hash_a",
                        "gmail_message_id": "msg_123",
                    },
                )
                # The lookup carries the hashed doc's content hash, so it matches the
                # hashed matter on the content (sha256) key — a true content match.
                duplicate = matter_store.find_gmail_attachment(
                    "msg_123",
                    "unstable_att_3",
                    attachment_filename="Counterparty NDA.docx",
                    attachment_sha256="hash_a",
                )
                removed = matter_store.deduplicate_gmail_matters()
                matters = matter_store.list_matters()

        self.assertEqual(duplicate["id"], hashed["id"])
        self.assertEqual(removed, 0)
        self.assertEqual({matter["id"] for matter in matters}, {legacy["id"], hashed["id"]})

    def test_gmail_attachment_dedupe_uses_key_index_without_pairwise_matching(self):
        # The sweep groups by key without per-pair matcher calls. Two DIFFERENT
        # documents with the same content hash are a true duplicate and collapse to
        # one; the sweep must reach that verdict from the content-hash grouping alone,
        # never invoking the pairwise matcher.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                for attachment_id in ["unstable_att_1", "unstable_att_2"]:
                    matter_store.create_matter(
                        source_filename="Counterparty NDA.docx",
                        document_bytes=b"identical attachment bytes",
                        extracted_text=attachment_id,
                        review_result={"clauses": []},
                        triage={},
                        source_type="gmail_inbound",
                        intake_metadata={
                            "attachment_filename": "Counterparty NDA.docx",
                            "gmail_attachment_id": attachment_id,
                            "gmail_attachment_sha256": "shared_hash",
                            "gmail_message_id": "msg_123",
                        },
                    )
                with patch.object(
                    matter_store,
                    "_gmail_attachments_match",
                    side_effect=AssertionError("pairwise matcher called"),
                ):
                    removed = matter_store.deduplicate_gmail_matters()
                matters = matter_store.list_matters()

        self.assertEqual(removed, 1)
        self.assertEqual(len(matters), 1)

    def test_gmail_attachment_dedupe_keeps_same_filename_when_hashes_conflict(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                for attachment_id, attachment_sha256 in [("unstable_att_1", "hash_a"), ("unstable_att_2", "hash_b")]:
                    matter_store.create_matter(
                        source_filename="Counterparty NDA.docx",
                        document_bytes=attachment_id.encode("utf-8"),
                        extracted_text=attachment_id,
                        review_result={"clauses": []},
                        triage={},
                        source_type="gmail_inbound",
                        intake_metadata={
                            "attachment_filename": "Counterparty NDA.docx",
                            "gmail_attachment_id": attachment_id,
                            "gmail_attachment_sha256": attachment_sha256,
                            "gmail_message_id": "msg_123",
                        },
                    )
                removed = matter_store.deduplicate_gmail_matters()
                matters = matter_store.list_matters()

        self.assertEqual(removed, 0)
        self.assertEqual(len(matters), 2)

    def test_gmail_matter_create_is_idempotent_by_attachment_hash(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                first = matter_store.create_matter(
                    source_filename="Counterparty NDA.docx",
                    document_bytes=b"same attachment",
                    extracted_text="same attachment",
                    review_result={"clauses": []},
                    triage={},
                    source_type="gmail_inbound",
                    intake_metadata={
                        "attachment_filename": "Counterparty NDA.docx",
                        "gmail_attachment_sha256": "hash_a",
                        "gmail_message_id": "msg_123",
                    },
                    dedupe_gmail=True,
                )
                duplicate = matter_store.create_matter(
                    source_filename="Counterparty NDA.docx",
                    document_bytes=b"same attachment",
                    extracted_text="same attachment",
                    review_result={"clauses": []},
                    triage={},
                    source_type="gmail_inbound",
                    intake_metadata={
                        "attachment_filename": "Counterparty NDA.docx",
                        "gmail_attachment_sha256": "hash_a",
                        "gmail_message_id": "msg_123",
                    },
                    dedupe_gmail=True,
                )
                different_attachment = matter_store.create_matter(
                    source_filename="Counterparty NDA.docx",
                    document_bytes=b"different attachment",
                    extracted_text="different attachment",
                    review_result={"clauses": []},
                    triage={},
                    source_type="gmail_inbound",
                    intake_metadata={
                        "attachment_filename": "Counterparty NDA.docx",
                        "gmail_attachment_sha256": "hash_b",
                        "gmail_message_id": "msg_123",
                    },
                    dedupe_gmail=True,
                )
                removed = matter_store.deduplicate_gmail_matters()
                matters = matter_store.list_matters()
                uploaded_files = list(matter_store.UPLOADS_DIR.glob("*"))

        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(duplicate["_existing_gmail_duplicate"], True)
        self.assertNotEqual(different_attachment["id"], first["id"])
        self.assertEqual(removed, 0)
        self.assertEqual({matter["id"] for matter in matters}, {first["id"], different_attachment["id"]})
        self.assertEqual(len(uploaded_files), 2)

    def test_triage_fails_closed_when_clauses_is_not_list(self):
        triage = triage_review_result({"clauses": {"passes": True}})

        self.assertEqual(triage["triage_status"], "needs_redline")
        self.assertEqual(triage["requirements_failed"], 1)
        self.assertEqual(triage["requirements_needs_review"], 0)
        self.assertEqual(triage["issue_count"], 1)

    def test_gmail_status_requires_token_paths_or_local_tokens(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {
                    gmail_integration.ROLE_TOKEN_ENV["inbound"]: "",
                    gmail_integration.ROLE_TOKEN_ENV["outbound"]: "",
                }, clear=False):
                    status = gmail_integration.gmail_status()

        self.assertEqual(status["inbound"]["configured"], False)
        self.assertEqual(status["outbound"]["configured"], False)
        self.assertEqual(status["inbound"]["enabled"], True)
        self.assertEqual(status["outbound"]["enabled"], True)
        self.assertEqual(status["inbound"]["query"], gmail_integration.DEFAULT_INBOUND_QUERY)
        self.assertIn("plain text email body", status["inbound"]["parsing"]["fields"])
        self.assertIn("HTML email body", status["inbound"]["parsing"]["fields"])
        self.assertIn("NDA", status["inbound"]["parsing"]["terms"])
        self.assertIn("MNDA", status["inbound"]["parsing"]["terms"])
        self.assertIn("mutual non-disclosure agreement", status["inbound"]["parsing"]["terms"])
        self.assertIn("mutual non disclosure agreement", status["inbound"]["parsing"]["terms"])
        self.assertIn("data processing agreement", status["inbound"]["parsing"]["terms"])
        # The keyword group is no longer a fetch gate: the inbound query is the
        # structural envelope only (no NDA terms). The keyword vocabulary lives
        # on as the deterministic scoring/ranking hint, surfaced in parsing.terms.
        self.assertNotIn("mutual NDA", status["inbound"]["query"])
        self.assertNotIn("non-disclosure", status["inbound"]["query"])
        self.assertIn("newer_than:90d", status["inbound"]["query"])
        self.assertIn("has:attachment", status["inbound"]["query"])
        self.assertIn(gmail_integration.ROLE_TOKEN_ENV["inbound"], status["inbound"]["error"])
        self.assertIn("data/google/inbound-token.json", status["inbound"]["error"])
        self.assertFalse(status["inbound"]["token"]["configured"])
        self.assertEqual(
            status["inbound"]["token"]["label"],
            "NDA_GMAIL_INBOUND_TOKEN_PATH or data/google/inbound-token.json",
        )
        self.assertEqual(status["inbound"]["token"]["source"], "missing")
        self.assertEqual(status["inbound"]["token"]["scope_status"]["ok"], False)
        self.assertEqual(status["inbound"]["recovery"]["state"], "missing_oauth_config")

    def test_user_gmail_oauth_connect_status_and_disconnect_are_owner_scoped(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeUsers:
            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "alice@example.com"})

        class FakeGmailService:
            def users(self):
                return FakeUsers()

        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
            "NDA_GMAIL_OAUTH_REDIRECT_URI": "https://nda.example.com/auth/gmail/callback",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.dict(os.environ, auth_env):
                session_headers, user = self.google_session_headers()
                start_status, start_payload, start_headers = self.request_with_headers(
                    "GET",
                    "/auth/gmail/start?role=all&next=/api/gmail/status",
                    headers=session_headers,
                )
                start_location = start_headers["Location"]
                parsed_start = urlparse(start_location)
                state = parse_qs(parsed_start.query)["state"][0]
                with patch(
                    "nda_automation.routes.gmail.google_connection.exchange_oauth_code",
                    return_value={"access_token": "access-token", "refresh_token": "refresh-token"},
                ) as exchange_code:
                    with patch.object(gmail_integration, "_clear_profile_cache_for_owner") as clear_profile_cache:
                        callback_status, callback_payload, callback_headers = self.request_with_headers(
                            "GET",
                            f"/auth/gmail/callback?code=gmail-code&state={state}",
                            headers=session_headers,
                        )
                token_root = matter_store.DATA_DIR / "users" / "google" / user["id"]
                inbound_token = token_root / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["inbound"]
                outbound_token = token_root / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["outbound"]
                self.assertEqual(callback_status, 302, callback_payload)
                inbound_token_exists_after_connect = inbound_token.is_file()
                outbound_token_exists_after_connect = outbound_token.is_file()
                legacy_gmail_dir_exists_after_connect = (matter_store.DATA_DIR / "gmail").exists()
                token_payload = json.loads(inbound_token.read_text(encoding="utf-8"))
                with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService()):
                    status_status, status_payload = self.request(
                        "GET",
                        "/api/gmail/status",
                        headers=session_headers,
                    )
                with patch.object(gmail_integration, "_clear_profile_cache_for_owner") as disconnect_clear_profile_cache:
                    disconnect_status, disconnect_payload = self.request(
                        "POST",
                        "/api/gmail/disconnect",
                        {"role": "inbound"},
                        headers=session_headers,
                    )
                inbound_token_exists_after_disconnect = inbound_token.exists()
                outbound_token_exists_after_disconnect = outbound_token.exists()

        self.assertEqual(start_status, 302)
        self.assertEqual(start_payload, b"")
        self.assertEqual(parsed_start.scheme, "https")
        self.assertEqual(parsed_start.netloc, "accounts.google.com")
        start_query = parse_qs(parsed_start.query)
        self.assertEqual(start_query["client_id"], ["google-client"])
        self.assertEqual(start_query["redirect_uri"], ["https://nda.example.com/auth/gmail/callback"])
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", start_query["scope"][0])
        self.assertIn("https://www.googleapis.com/auth/gmail.send", start_query["scope"][0])
        self.assertEqual(callback_status, 302)
        self.assertEqual(callback_payload, b"")
        self.assertEqual(callback_headers["Location"], "/api/gmail/status")
        self.assertEqual(callback_headers["X-Gmail-Connected-Roles"], "inbound,outbound,drive")
        exchange_code.assert_called_once_with(
            "gmail-code",
            redirect_uri="https://nda.example.com/auth/gmail/callback",
        )
        clear_profile_cache.assert_called_once_with(user["id"])
        self.assertTrue(inbound_token_exists_after_connect)
        self.assertTrue(outbound_token_exists_after_connect)
        self.assertFalse(legacy_gmail_dir_exists_after_connect)
        self.assertEqual(token_payload["client_id"], "google-client")
        self.assertEqual(token_payload["refresh_token"], "refresh-token")
        self.assertNotIn("access-token", json.dumps(status_payload))
        self.assertEqual(status_status, 200)
        self.assertTrue(status_payload["gmail"]["user_scoped"])
        self.assertEqual(status_payload["gmail"]["setup"]["state"], "ready_to_connect")
        self.assertEqual(status_payload["gmail"]["inbound"]["token"]["source"], "user_data")
        self.assertEqual(status_payload["gmail"]["outbound"]["token"]["source"], "user_data")
        self.assertEqual(status_payload["gmail"]["inbound"]["token"]["scope_status"]["missing"], [])
        self.assertEqual(status_payload["gmail"]["outbound"]["token"]["scope_status"]["missing"], [])
        self.assertEqual(status_payload["gmail"]["inbound"]["recovery"]["state"], "ready")
        self.assertEqual(status_payload["gmail"]["outbound"]["recovery"]["state"], "ready")
        self.assertTrue(status_payload["gmail"]["inbound"]["ready"])
        self.assertEqual(disconnect_status, 200)
        self.assertEqual(disconnect_payload["disconnected"], 1)
        disconnect_clear_profile_cache.assert_called_once_with(user["id"])
        self.assertFalse(inbound_token_exists_after_disconnect)
        self.assertTrue(outbound_token_exists_after_disconnect)
        self.assertEqual(disconnect_payload["gmail"]["inbound"]["token"]["source"], "missing")
        self.assertEqual(disconnect_payload["gmail"]["inbound"]["recovery"]["state"], "missing_token")
        self.assertEqual(disconnect_payload["gmail"]["outbound"]["token"]["source"], "user_data")

    def test_gmail_settings_updates_inbound_search_terms(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {
                        "inbound_search_terms": [
                            "NDA",
                            "mutual NDA",
                            "confidentiality deed",
                            "data processing agreement",
                        ],
                    },
                )

        self.assertEqual(status, 200)
        self.assertEqual(payload["gmail_settings"]["inbound_search_terms"], [
            "NDA",
            "mutual NDA",
            "confidentiality deed",
            "data processing agreement",
        ])
        # Search terms are now the deterministic scoring/ranking vocabulary, NOT
        # a fetch gate. Updating them must NOT change the structural fetch query
        # (it stays the keyword-free envelope); the new vocabulary surfaces in
        # parsing.terms instead.
        query = payload["gmail"]["inbound"]["query"]
        self.assertEqual(query, gmail_integration.DEFAULT_INBOUND_QUERY)
        self.assertNotIn("mutual NDA", query)
        self.assertNotIn("confidentiality deed", query)
        self.assertEqual(payload["gmail"]["inbound"]["parsing"]["terms"], [
            "NDA",
            "mutual NDA",
            "confidentiality deed",
            "data processing agreement",
        ])

    def test_gmail_settings_rejects_empty_inbound_search_terms(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                # Seed a non-default baseline so we can prove the rejected empty
                # submission does NOT silently revert to the built-in defaults.
                self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"inbound_search_terms": ["custom term"]},
                )
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"inbound_search_terms": ["", "  "]},
                )
                settings_after = app_settings.gmail_settings()

        self.assertEqual(status, 400)
        # Honest, consistent message; the save did NOT take and was NOT silently
        # defaulted -- the previously-saved custom term is still in force.
        self.assertEqual(payload["error"], "Add at least one Gmail search term — it can't be empty.")
        self.assertEqual(settings_after["inbound_search_terms"], ["custom term"])
        self.assertNotEqual(
            settings_after["inbound_search_terms"],
            list(app_settings.DEFAULT_GMAIL_INBOUND_SEARCH_TERMS),
        )

    def test_gmail_settings_helper_does_not_silently_default_empty_terms(self):
        # The backend write helper must be HONEST + consistent with the route: an
        # empty list submitted directly to update_gmail_settings must NOT revive
        # the defaults; the empty value is dropped and the prior value preserved.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                app_settings.update_gmail_settings({"inbound_search_terms": ["seed term"]})
                app_settings.update_gmail_settings({"inbound_search_terms": []})
                settings_after = app_settings.gmail_settings()

        self.assertEqual(settings_after["inbound_search_terms"], ["seed term"])

    def test_gmail_settings_saves_valid_inbound_search_terms(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"inbound_search_terms": ["alpha", "beta"]},
                )

        self.assertEqual(status, 200)
        self.assertEqual(payload["gmail_settings"]["inbound_search_terms"], ["alpha", "beta"])
        self.assertNotIn("warning", payload)

    def test_gmail_status_uses_local_data_tokens_when_env_paths_are_missing(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeUsers:
            def __init__(self, email):
                self.email = email

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": self.email})

        class FakeGmailService:
            def __init__(self, email):
                self.users_api = FakeUsers(email)

            def users(self):
                return self.users_api

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                token_dir = matter_store.DATA_DIR / "gmail"
                token_dir.mkdir(parents=True)
                (token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["inbound"]).write_text("{}", encoding="utf-8")
                (token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["outbound"]).write_text("{}", encoding="utf-8")
                with patch.dict(os.environ, {
                    gmail_integration.ROLE_TOKEN_ENV["inbound"]: "",
                    gmail_integration.ROLE_TOKEN_ENV["outbound"]: "",
                }, clear=False):
                    with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService("legal@aspora.com")):
                        status = gmail_integration.gmail_status()

        self.assertEqual(status["account_match"], True)
        self.assertEqual(status["inbound"]["configured"], True)
        self.assertEqual(status["outbound"]["configured"], True)
        # The local-data tokens here carry no scopes, so readiness must reflect
        # reality: the profile fetch succeeding does NOT make the connection ready
        # when the required import scope is missing.
        self.assertEqual(status["inbound"]["ready"], False)
        self.assertEqual(status["outbound"]["ready"], False)
        self.assertIn("missing permission", status["inbound"]["reason"])
        self.assertIn("gmail.readonly", status["inbound"]["reason"])
        self.assertEqual(status["inbound"]["error"], status["inbound"]["reason"])
        self.assertEqual(status["inbound"]["email"], "legal@aspora.com")
        self.assertEqual(status["inbound"]["token"]["configured"], True)
        self.assertEqual(status["inbound"]["token"]["label"], "data/gmail/inbound-token.json")
        self.assertEqual(status["inbound"]["token"]["source"], "local_data")
        self.assertEqual(status["inbound"]["token"]["scope_status"]["ok"], False)
        self.assertIn(
            "https://www.googleapis.com/auth/gmail.readonly",
            status["inbound"]["token"]["scope_status"]["missing"],
        )

    def _gmail_readiness_fake_service(self, email="legal@aspora.com"):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeUsers:
            def __init__(self, addr):
                self.addr = addr

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": self.addr})

        class FakeGmailService:
            def __init__(self, addr):
                self.users_api = FakeUsers(addr)

            def users(self):
                return self.users_api

        return FakeGmailService(email)

    def _write_role_tokens(self, *, inbound_payload, outbound_payload):
        token_dir = matter_store.DATA_DIR / "gmail"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["inbound"]).write_text(
            json.dumps(inbound_payload), encoding="utf-8"
        )
        (token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["outbound"]).write_text(
            json.dumps(outbound_payload), encoding="utf-8"
        )

    def test_gmail_status_missing_scope_reports_not_ready_with_reason(self):
        # A token that fetched the profile fine but is missing the required import
        # scope must NOT read as ready — the dashboard "ready" lie.
        no_scope = {
            "client_id": "google-client",
            "client_secret": "secret",
            "refresh_token": "refresh-token",
            "scopes": [],
            "token": "access-token",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                self._write_role_tokens(inbound_payload=no_scope, outbound_payload=no_scope)
                with patch.dict(os.environ, {
                    gmail_integration.ROLE_TOKEN_ENV["inbound"]: "",
                    gmail_integration.ROLE_TOKEN_ENV["outbound"]: "",
                }, clear=False):
                    with patch.object(
                        gmail_integration,
                        "_gmail_service",
                        return_value=self._gmail_readiness_fake_service(),
                    ):
                        status = gmail_integration.gmail_status()

        self.assertEqual(status["inbound"]["ready"], False)
        self.assertIn("missing permission", status["inbound"]["reason"])
        self.assertIn("gmail.readonly", status["inbound"]["reason"])
        self.assertEqual(status["inbound"]["error"], status["inbound"]["reason"])

    def test_gmail_status_expired_unrefreshable_token_reports_not_ready(self):
        # An expired token with no refresh_token can never re-authenticate the poll,
        # so it must read as not-ready with a reconnect reason — even though the
        # cached/basic profile call "works".
        unrefreshable = {
            "client_id": "google-client",
            "client_secret": "secret",
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "token": "access-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            # No refresh_token => once the access token lapses it can never recover.
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                self._write_role_tokens(inbound_payload=unrefreshable, outbound_payload=unrefreshable)
                with patch.dict(os.environ, {
                    gmail_integration.ROLE_TOKEN_ENV["inbound"]: "",
                    gmail_integration.ROLE_TOKEN_ENV["outbound"]: "",
                }, clear=False):
                    with patch.object(
                        gmail_integration,
                        "_gmail_service",
                        return_value=self._gmail_readiness_fake_service(),
                    ):
                        status = gmail_integration.gmail_status()

        self.assertEqual(status["inbound"]["ready"], False)
        self.assertIn("reconnect", status["inbound"]["reason"].lower())
        self.assertIn("expired or revoked", status["inbound"]["reason"])
        self.assertEqual(status["inbound"]["error"], status["inbound"]["reason"])

    def test_gmail_status_healthy_token_stays_ready(self):
        # Full scopes + a refresh_token = a connection the poller can keep using.
        # Readiness must be unchanged (True) and carry no block reason.
        inbound_ok = {
            "client_id": "google-client",
            "client_secret": "secret",
            "refresh_token": "refresh-token",
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "token": "access-token",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        outbound_ok = {
            "client_id": "google-client",
            "client_secret": "secret",
            "refresh_token": "refresh-token",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.metadata",
            ],
            "token": "access-token",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                self._write_role_tokens(inbound_payload=inbound_ok, outbound_payload=outbound_ok)
                with patch.dict(os.environ, {
                    gmail_integration.ROLE_TOKEN_ENV["inbound"]: "",
                    gmail_integration.ROLE_TOKEN_ENV["outbound"]: "",
                }, clear=False):
                    with patch.object(
                        gmail_integration,
                        "_gmail_service",
                        return_value=self._gmail_readiness_fake_service(),
                    ):
                        status = gmail_integration.gmail_status()

        self.assertEqual(status["inbound"]["ready"], True)
        self.assertEqual(status["outbound"]["ready"], True)
        self.assertNotIn("reason", status["inbound"])
        self.assertNotIn("error", status["inbound"])

    def test_gmail_status_surfaces_and_caches_profile_rate_limit(self):
        class FakeRateLimitError(Exception):
            resp = type("Resp", (), {"status": 429})()
            content = json.dumps({
                "error": {
                    "message": "User-rate limit exceeded. Retry after 2099-06-04T14:06:26.379Z",
                    "errors": [{"reason": "rateLimitExceeded"}],
                    "status": "RESOURCE_EXHAUSTED",
                }
            }).encode("utf-8")

        class FakeExecutable:
            calls = 0

            def execute(self):
                FakeExecutable.calls += 1
                raise FakeRateLimitError()

        class FakeUsers:
            def getProfile(self, userId):
                return FakeExecutable()

        class FakeGmailService:
            def users(self):
                return FakeUsers()

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                token_dir = matter_store.DATA_DIR / "gmail"
                token_dir.mkdir(parents=True)
                (token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["inbound"]).write_text("{}", encoding="utf-8")
                (token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["outbound"]).write_text("{}", encoding="utf-8")
                with patch.dict(os.environ, {
                    gmail_integration.ROLE_TOKEN_ENV["inbound"]: "",
                    gmail_integration.ROLE_TOKEN_ENV["outbound"]: "",
                }, clear=False):
                    with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService()):
                        first_status = gmail_integration.gmail_status()
                        second_status = gmail_integration.gmail_status()

        self.assertEqual(FakeExecutable.calls, 2)
        self.assertEqual(first_status["inbound"]["ready"], False)
        self.assertIn("Gmail API rate limit exceeded", first_status["inbound"]["error"])
        self.assertIn("2099-06-04T14:06:26.379Z", first_status["inbound"]["error"])
        self.assertEqual(second_status["inbound"]["error"], first_status["inbound"]["error"])

    def test_gmail_status_blocks_outbound_when_accounts_do_not_match(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeUsers:
            def __init__(self, email):
                self.email = email

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": self.email})

        class FakeGmailService:
            def __init__(self, email):
                self.users_api = FakeUsers(email)

            def users(self):
                return self.users_api

        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as token_dir:
            token_path = server_module.Path(token_dir)
            inbound_token = token_path / "inbound.json"
            outbound_token = token_path / "outbound.json"
            # Fully-scoped, refreshable tokens so this test isolates the account
            # mismatch: inbound is otherwise ready, and outbound is blocked purely
            # because its resolved account differs from inbound's.
            inbound_token.write_text(json.dumps({
                "client_id": "google-client",
                "client_secret": "secret",
                "refresh_token": "refresh-token",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "token": "access-token",
                "token_uri": "https://oauth2.googleapis.com/token",
            }), encoding="utf-8")
            outbound_token.write_text(json.dumps({
                "client_id": "google-client",
                "client_secret": "secret",
                "refresh_token": "refresh-token",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.send",
                    "https://www.googleapis.com/auth/gmail.metadata",
                ],
                "token": "access-token",
                "token_uri": "https://oauth2.googleapis.com/token",
            }), encoding="utf-8")
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {
                    gmail_integration.ROLE_TOKEN_ENV["inbound"]: str(inbound_token),
                    gmail_integration.ROLE_TOKEN_ENV["outbound"]: str(outbound_token),
                }, clear=False):
                    with patch.object(gmail_integration, "_gmail_service", side_effect=lambda role: FakeGmailService(
                        "inbound@aspora.com" if role == "inbound" else "outbound@aspora.com"
                    )):
                        status = gmail_integration.gmail_status()

        self.assertEqual(status["account_match"], False)
        self.assertEqual(status["inbound"]["ready"], True)
        self.assertEqual(status["outbound"]["ready"], False)
        self.assertIn("does not match inbound Gmail account inbound@aspora.com", status["outbound"]["error"])
        self.assertEqual(status["inbound"]["token"]["configured"], True)
        self.assertEqual(status["inbound"]["token"]["label"], gmail_integration.ROLE_TOKEN_ENV["inbound"])
        self.assertEqual(status["inbound"]["token"]["source"], "environment")
        # Inbound's token is fully scoped now, so scope is satisfied and inbound is
        # ready; outbound is blocked solely by the account mismatch.
        self.assertEqual(status["inbound"]["token"]["scope_status"]["ok"], True)
        self.assertEqual(status["inbound"]["token"]["scope_status"]["missing"], [])
        self.assertNotIn(str(token_path), json.dumps(status))

    def test_gmail_settings_endpoint_persists_toggles(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"inbound_enabled": False, "outbound_enabled": True, "sync_frequency": "30_minutes"},
                )
                invalid_status, invalid_payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"inbound_enabled": "off"},
                )
                invalid_frequency_status, invalid_frequency_payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"sync_frequency": "3_minutes"},
                )
                legacy_cadence_status, legacy_cadence_payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"sync_cadence": "30_minutes"},
                )
                settings = app_settings.gmail_settings()

        self.assertEqual(status, 200)
        self.assertEqual(payload["gmail_settings"]["inbound_enabled"], False)
        self.assertEqual(payload["gmail_settings"]["outbound_enabled"], True)
        self.assertEqual(payload["gmail_settings"]["sync_frequency"], "30_minutes")
        self.assertEqual(payload["gmail"]["inbound"]["enabled"], False)
        self.assertEqual(payload["gmail"]["outbound"]["enabled"], True)
        self.assertEqual(payload["gmail"]["settings"]["sync_frequency"], "30_minutes")
        self.assertEqual(settings["inbound_enabled"], False)
        self.assertEqual(settings["outbound_enabled"], True)
        self.assertEqual(settings["sync_frequency"], "30_minutes")
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_payload["error"], "Gmail enabled settings must be true or false.")
        self.assertEqual(invalid_frequency_status, 400)
        self.assertEqual(invalid_frequency_payload["error"], "Unsupported Gmail sync frequency.")
        self.assertEqual(legacy_cadence_status, 400)
        self.assertEqual(legacy_cadence_payload["error"], "Use sync_frequency for Gmail sync frequency.")

    def test_ai_settings_endpoint_persists_toggle(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(
                    os.environ,
                    {
                        "OPENROUTER_API_KEY": "server-only-secret",
                        "NDA_AI_REVIEW_ENABLED": "",
                        ACTIVE_REVIEW_ENGINE_ENV: "ai_first",
                    },
                    clear=False,
                ):
                    initial_status, initial_payload = self.request("GET", "/api/ai/settings")
                    on_status, on_payload = self.request("POST", "/api/ai/settings", {"enabled": True})
                    off_status, off_payload = self.request("POST", "/api/ai/settings", {"enabled": False})
                    invalid_status, invalid_payload = self.request("POST", "/api/ai/settings", {"enabled": "yes"})
                    missing_status, missing_payload = self.request("POST", "/api/ai/settings", {})
                    settings = app_settings.ai_settings()

        self.assertEqual(initial_status, 200)
        self.assertEqual(initial_payload["ai_review"]["enabled"], False)
        self.assertEqual(initial_payload["ai_review"]["stored_enabled"], None)
        self.assertEqual(initial_payload["ai_review"]["environment_enabled"], False)
        self.assertEqual(initial_payload["ai_review"]["api_key_configured"], True)
        # Default-on: with the AI-first engine active AND a key configured, an UNSET
        # NDA_AI_VERIFIER now arms the real verifier (the polarity-fix). active_kind is
        # surfaced as "ai" so the armed pass is observable, not silently assumed off.
        self.assertEqual(initial_payload["ai_verifier"]["enabled"], True)
        self.assertEqual(initial_payload["ai_verifier"]["active_kind"], "ai")
        self.assertEqual(initial_payload["ai_verifier"]["api_key_source"], "environment")
        self.assertEqual(initial_payload["active_review_engine"]["active_engine"], "ai_first")
        self.assertNotIn("server-only-secret", json.dumps(initial_payload))
        self.assertEqual(on_status, 200)
        self.assertEqual(on_payload["ai_review"]["enabled"], True)
        self.assertEqual(on_payload["ai_review"]["stored_enabled"], True)
        self.assertEqual(off_status, 200)
        self.assertEqual(off_payload["ai_review"]["enabled"], False)
        self.assertEqual(off_payload["ai_review"]["stored_enabled"], False)
        self.assertEqual(settings["enabled"], False)
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_payload["error"], "AI enabled setting must be true or false.")
        self.assertEqual(missing_status, 400)
        self.assertEqual(missing_payload["error"], "Provide an AI or runtime review setting to update.")

    def test_ai_settings_enable_without_key_is_rejected_and_not_persisted(self):
        # Enable-requires-key: turning AI on with no configured key must be rejected
        # (409) and must NOT persist an "on-but-broken" state.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(
                    os.environ,
                    {"OPENROUTER_API_KEY": "", "NDA_AI_REVIEW_ENABLED": ""},
                    clear=False,
                ):
                    status, payload = self.request("POST", "/api/ai/settings", {"enabled": True})
                    persisted = app_settings.ai_settings()

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "Add a working OpenRouter API key before turning AI on.")
        # The rejected toggle must not persist an enabled=True ("on-but-broken") state.
        self.assertNotEqual(persisted["enabled"], True)

    def test_ai_settings_enable_with_key_is_accepted(self):
        # With a configured key (env), enabling AI succeeds and persists.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(
                    os.environ,
                    {"OPENROUTER_API_KEY": "sk-or-test-key", "NDA_AI_REVIEW_ENABLED": ""},
                    clear=False,
                ):
                    status, payload = self.request("POST", "/api/ai/settings", {"enabled": True})
                    persisted = app_settings.ai_settings()

        self.assertEqual(status, 200)
        self.assertEqual(payload["ai_review"]["enabled"], True)
        self.assertEqual(persisted["enabled"], True)
        self.assertNotIn("sk-or-test-key", json.dumps(payload))

    def test_ai_settings_disable_without_key_is_allowed(self):
        # Turning AI OFF must never be blocked by the enable-requires-key gate.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(
                    os.environ,
                    {"OPENROUTER_API_KEY": "", "NDA_AI_REVIEW_ENABLED": ""},
                    clear=False,
                ):
                    status, payload = self.request("POST", "/api/ai/settings", {"enabled": False})

        self.assertEqual(status, 200)
        self.assertEqual(payload["ai_review"]["enabled"], False)

    def test_ai_settings_valid_model_slug_is_persisted(self):
        ai_review._reset_model_catalog_cache_for_tests()
        self.addCleanup(ai_review._reset_model_catalog_cache_for_tests)
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(
                    ai_review,
                    "_fetch_openrouter_model_slugs",
                    return_value=frozenset({"anthropic/claude-opus-4.8", "deepseek/deepseek-chat"}),
                ):
                    status, payload = self.request(
                        "POST", "/api/ai/settings", {"model": "deepseek/deepseek-chat"}
                    )
                    persisted = app_settings.ai_settings()

        self.assertEqual(status, 200)
        self.assertEqual(payload["ai_review"]["model"], "deepseek/deepseek-chat")
        self.assertEqual(persisted["model"], "deepseek/deepseek-chat")

    def test_ai_settings_unknown_model_slug_is_rejected_and_not_persisted(self):
        ai_review._reset_model_catalog_cache_for_tests()
        self.addCleanup(ai_review._reset_model_catalog_cache_for_tests)
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                before = app_settings.ai_settings()["model"]
                with patch.object(
                    ai_review,
                    "_fetch_openrouter_model_slugs",
                    return_value=frozenset({"anthropic/claude-opus-4.8"}),
                ):
                    status, payload = self.request(
                        "POST", "/api/ai/settings", {"model": "totally/made-up-model"}
                    )
                    persisted = app_settings.ai_settings()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Unknown model 'totally/made-up-model' -- check the model id.")
        self.assertEqual(persisted["model"], before)

    def test_ai_settings_model_persisted_with_warning_when_catalog_unreachable(self):
        # Catalog fetch failure must NOT hard-block: persist with an explicit
        # unverified-model warning rather than a false success or a false rejection.
        ai_review._reset_model_catalog_cache_for_tests()
        self.addCleanup(ai_review._reset_model_catalog_cache_for_tests)
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(
                    ai_review,
                    "_fetch_openrouter_model_slugs",
                    side_effect=urllib.error.URLError("no network"),
                ):
                    status, payload = self.request(
                        "POST", "/api/ai/settings", {"model": "anthropic/claude-opus-4.8"}
                    )
                    persisted = app_settings.ai_settings()

        self.assertEqual(status, 200)
        self.assertEqual(persisted["model"], "anthropic/claude-opus-4.8")
        warning_codes = [w.get("code") for w in payload.get("operational_warnings", [])]
        self.assertIn("ai_model_unverified", warning_codes)

    def test_validate_model_slug_parses_catalog_and_caches(self):
        # Exercises the real fetch+parse+cache path (urlopen mocked, no key sent).
        ai_review._reset_model_catalog_cache_for_tests()
        self.addCleanup(ai_review._reset_model_catalog_cache_for_tests)
        catalog_body = json.dumps(
            {"data": [{"id": "anthropic/claude-opus-4.8"}, {"id": "deepseek/deepseek-chat"}]}
        ).encode("utf-8")
        with patch.object(
            ai_review.urllib.request,
            "urlopen",
            return_value=_FakeUrlopen(catalog_body),
        ) as mocked_urlopen:
            self.assertEqual(ai_review.validate_model_slug("deepseek/deepseek-chat"), ("valid", ""))
            not_found_status, not_found_message = ai_review.validate_model_slug("nope/nope")
        self.assertEqual(not_found_status, "not_found")
        self.assertEqual(not_found_message, "Unknown model 'nope/nope' -- check the model id.")
        # Second slug check is served from cache: urlopen called exactly once.
        self.assertEqual(mocked_urlopen.call_count, 1)
        # The catalog request carries no Authorization/API-key header.
        request_arg = mocked_urlopen.call_args.args[0]
        self.assertNotIn("Authorization", request_arg.headers)
        self.assertEqual(request_arg.full_url, ai_review.OPENROUTER_MODELS_ENDPOINT)

    def test_ai_settings_endpoint_warns_when_enabled_verifier_is_inactive_without_key(self):
        # AI verifier enabled but unkeyed: it is INACTIVE (a no-op that changes no
        # verdicts) -- it does NOT fall back to the offline regex engine. The operator
        # is warned so they configure a key for the DeepSeek verifier.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(
                    os.environ,
                    {
                        "NDA_AI_VERIFIER": "true",
                        "OPENROUTER_API_KEY": "",
                    },
                    clear=False,
                ):
                    status, payload = self.request("GET", "/api/ai/settings")

        self.assertEqual(status, 200)
        self.assertEqual(payload["ai_verifier"]["enabled"], True)
        self.assertEqual(payload["ai_verifier"]["active_kind"], "noop")
        self.assertEqual(payload["ai_verifier"]["fallback_reason"], "missing_openrouter_api_key")
        self.assertIn("ai_verifier_inactive_no_key", [warning["code"] for warning in payload["operational_warnings"]])
        self.assertNotIn("OPENROUTER_API_KEY", json.dumps(payload))

    def test_personalisation_settings_endpoint_persists_text(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                initial_status, initial_payload = self.request("GET", "/api/admin/personalisation-settings")
                update_status, update_payload = self.request(
                    "POST",
                    "/api/admin/personalisation-settings",
                    {
                        "sign_off": "  Warm regards,  ",
                        "signature": "  Daniyal Ahmad  ",
                        "signature_block": "Warm regards,\r\n\r\nDaniyal Ahmad\nAspora Legal",
                    },
                )
                persisted = app_settings.personalisation_settings()

        self.assertEqual(initial_status, 200)
        self.assertEqual(initial_payload["personalisation"], app_settings.DEFAULT_PERSONALISATION_SETTINGS)
        self.assertEqual(initial_payload["defaults"], app_settings.DEFAULT_PERSONALISATION_SETTINGS)
        self.assertEqual(update_status, 200)
        # The blank SEPARATOR line between the sign-off and the name MUST survive
        # the round trip -- this block is used verbatim on every outbound NDA
        # email, and admins author normal multi-block signatures. (Regression:
        # the cleaner used to drop every empty line and flatten the spacing.)
        self.assertEqual(update_payload["personalisation"], {
            "sign_off": "Warm regards,",
            "signature": "Daniyal Ahmad",
            "signature_block": "Warm regards,\n\nDaniyal Ahmad\nAspora Legal",
        })
        self.assertEqual(update_payload["defaults"], app_settings.DEFAULT_PERSONALISATION_SETTINGS)
        self.assertEqual(persisted, update_payload["personalisation"])
        self.assertEqual(update_payload["settings_audit"][0]["action"], "personalisation_settings_update")
        self.assertEqual(
            [change["setting"] for change in update_payload["settings_audit"][0]["changes"]],
            [
                "personalisation.sign_off",
                "personalisation.signature",
                "personalisation.signature_block",
            ],
        )

    def test_signature_block_blank_separator_line_round_trips(self):
        # P0 regression: a normal multi-block email signature has a blank line
        # between the sign-off and the name. That blank separator must survive a
        # full save -> reload, because the stored block is pasted verbatim into
        # every outbound NDA email. The cleaner used to drop every empty line.
        canonical = "Best,\n\nDaniyal Ahmad\nAspora"
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                save_status, save_payload = self.request(
                    "POST",
                    "/api/admin/personalisation-settings",
                    {"signature_block": canonical},
                )
                reload_status, reload_payload = self.request(
                    "GET", "/api/admin/personalisation-settings"
                )
                persisted = app_settings.personalisation_settings()

        self.assertEqual(save_status, 200)
        self.assertEqual(save_payload["personalisation"]["signature_block"], canonical)
        self.assertIn("\n\n", save_payload["personalisation"]["signature_block"])
        # Reload from the store returns the same block, blank line intact.
        self.assertEqual(reload_status, 200)
        self.assertEqual(reload_payload["personalisation"]["signature_block"], canonical)
        self.assertEqual(persisted["signature_block"], canonical)

    def test_clean_signature_block_preserves_structure_and_tidies(self):
        # Direct cleaner unit coverage: keep single separators, trim leading and
        # trailing blank lines, collapse 3+ blank-line runs to one, collapse
        # intra-line whitespace and strip trailing spaces -- single-line fields
        # stay fully flattened.
        clean = app_settings._clean_personalisation_setting
        self.assertEqual(
            clean("signature_block", "Best,\n\nDaniyal Ahmad\nAspora"),
            "Best,\n\nDaniyal Ahmad\nAspora",
        )
        self.assertEqual(
            clean("signature_block", "\n\nBest,\n\n\n\nDaniyal\n\n"),
            "Best,\n\nDaniyal",
        )
        self.assertEqual(
            clean("signature_block", "Best,   sir   \n\nDaniyal\t\tAhmad   "),
            "Best, sir\n\nDaniyal Ahmad",
        )
        # Single-line fields remain whitespace-collapsed (unchanged behaviour).
        self.assertEqual(clean("sign_off", "  Best,   regards  "), "Best, regards")
        self.assertEqual(clean("signature", "Daniyal\n\nAhmad"), "Daniyal Ahmad")

    def test_personalisation_settings_endpoint_rejects_invalid_payloads(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                unsupported_status, unsupported_payload = self.request(
                    "POST",
                    "/api/admin/personalisation-settings",
                    {"display_name": "Daniyal"},
                )
                missing_status, missing_payload = self.request("POST", "/api/admin/personalisation-settings", {})
                non_text_status, non_text_payload = self.request(
                    "POST",
                    "/api/admin/personalisation-settings",
                    {"signature": 123},
                )

        self.assertEqual(unsupported_status, 400)
        self.assertEqual(unsupported_payload["error"], "Unsupported personalisation setting: display_name.")
        self.assertEqual(missing_status, 400)
        self.assertEqual(
            missing_payload["error"],
            "Provide a sign_off, signature, or signature_block setting to update.",
        )
        self.assertEqual(non_text_status, 400)
        self.assertEqual(non_text_payload["error"], "Personalisation settings must be text values.")

    # --- Per-user (non-admin) personalisation self-serve --------------------
    # A non-admin must be able to set THEIR OWN signature, read it back, and have
    # the outbound email body use it -- with strict per-owner isolation and a
    # never-hard-block default when they have saved nothing.

    def test_non_admin_can_save_and_read_back_own_personalisation(self):
        # NDA_REQUIRE_AUTH + empty NDA_ADMIN_USERS + Google session => the caller is
        # authenticated but NOT an admin. They must still reach /api/me/... .
        auth_env = self._google_oauth_auth_env(NDA_ADMIN_USERS="")
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                session_headers, user = self.google_session_headers(
                    subject="user-a-sub", email="usera@example.com"
                )
                with patch.dict(os.environ, auth_env):
                    get_before_status, get_before_payload = self.request(
                        "GET", "/api/me/personalisation-settings", headers=session_headers
                    )
                    post_status, post_payload = self.request(
                        "POST",
                        "/api/me/personalisation-settings",
                        {
                            "sign_off": "  Kind regards,  ",
                            "signature": "User A",
                            "signature_block": "Kind regards,\r\n\r\nUser A\nLegal",
                        },
                        headers=session_headers,
                    )
                    get_after_status, get_after_payload = self.request(
                        "GET", "/api/me/personalisation-settings", headers=session_headers
                    )
                stored = app_settings.user_personalisation_settings(user["id"])

        # Before saving: inherits the global/built-in default, flagged not-custom.
        self.assertEqual(get_before_status, 200)
        self.assertFalse(get_before_payload["is_custom"])
        self.assertEqual(
            get_before_payload["personalisation"], app_settings.DEFAULT_PERSONALISATION_SETTINGS
        )
        # Save: 200, cleaned/normalised, flagged custom.
        self.assertEqual(post_status, 200)
        self.assertTrue(post_payload["is_custom"])
        self.assertEqual(
            post_payload["personalisation"],
            {
                "sign_off": "Kind regards,",
                "signature": "User A",
                "signature_block": "Kind regards,\n\nUser A\nLegal",
            },
        )
        # Read back: their own saved override, persisted to disk under their id.
        self.assertEqual(get_after_status, 200)
        self.assertTrue(get_after_payload["is_custom"])
        self.assertEqual(get_after_payload["personalisation"], post_payload["personalisation"])
        self.assertEqual(stored, post_payload["personalisation"])

    def test_non_admin_personalisation_is_per_owner_isolated(self):
        # User A saves a signature; User B must NOT see it, and B's own save must
        # NOT overwrite A's slot (strict per-owner isolation / no cross-tenant leak).
        auth_env = self._google_oauth_auth_env(NDA_ADMIN_USERS="")
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                headers_a, user_a = self.google_session_headers(
                    subject="iso-user-a", email="isoa@example.com"
                )
                headers_b, user_b = self.google_session_headers(
                    subject="iso-user-b", email="isob@example.com"
                )
                self.assertNotEqual(user_a["id"], user_b["id"])
                with patch.dict(os.environ, auth_env):
                    self.request(
                        "POST",
                        "/api/me/personalisation-settings",
                        {"signature": "Alice Only", "signature_block": "Best,\nAlice Only"},
                        headers=headers_a,
                    )
                    # B reads BEFORE saving: must see the default, never A's value.
                    b_get_status, b_get_payload = self.request(
                        "GET", "/api/me/personalisation-settings", headers=headers_b
                    )
                    # B saves their own.
                    self.request(
                        "POST",
                        "/api/me/personalisation-settings",
                        {"signature": "Bob Only", "signature_block": "Regards,\nBob Only"},
                        headers=headers_b,
                    )
                    # A re-reads: must still be A's value, untouched by B's write.
                    a_get_status, a_get_payload = self.request(
                        "GET", "/api/me/personalisation-settings", headers=headers_a
                    )
                stored_a = app_settings.user_personalisation_settings(user_a["id"])
                stored_b = app_settings.user_personalisation_settings(user_b["id"])

        self.assertEqual(b_get_status, 200)
        self.assertFalse(b_get_payload["is_custom"])
        self.assertNotIn("Alice Only", b_get_payload["personalisation"]["signature_block"])
        self.assertEqual(a_get_status, 200)
        self.assertEqual(a_get_payload["personalisation"]["signature"], "Alice Only")
        self.assertEqual(stored_a["signature"], "Alice Only")
        self.assertEqual(stored_b["signature"], "Bob Only")
        self.assertEqual(stored_a["signature_block"], "Best,\nAlice Only")
        self.assertEqual(stored_b["signature_block"], "Regards,\nBob Only")

    def test_non_admin_personalisation_write_requires_signed_in_user(self):
        # An unauthenticated caller (no owner id) must be refused the per-user write
        # so there is no anonymous/shared slot to clobber.
        auth_env = self._google_oauth_auth_env(NDA_ADMIN_USERS="")
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, auth_env):
                    status, payload = self.request(
                        "POST",
                        "/api/me/personalisation-settings",
                        {"signature": "Nobody"},
                    )
        self.assertIn(status, (401, 403))

    def test_outbound_body_uses_per_user_signature_then_falls_back(self):
        # The OUTBOUND email body (default_outbound_body) is where personalisation
        # actually reaches the generated/sent document. Prove resolution order:
        #   1. a user's OWN override wins;
        #   2. a user with NO override still gets a working body via the global/
        #      built-in default (never a hard block).
        matter = {"subject": "Acme MNDA"}
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                app_settings.update_user_personalisation_settings(
                    "owner-with-sig",
                    {"signature_block": "Cheers,\nThe Owner"},
                )
                own_body = gmail_matter_outbox.default_outbound_body(
                    matter, owner_user_id="owner-with-sig"
                )
                # A different, never-configured user falls through to the default.
                default_body = gmail_matter_outbox.default_outbound_body(
                    matter, owner_user_id="owner-without-sig"
                )
                # Strict isolation: the no-override user must NOT see the other's sig.
                isolated = "The Owner" not in default_body

        self.assertIn("Cheers,\nThe Owner", own_body)
        self.assertIn("Acme MNDA", own_body)
        # Default body still produces a valid signature (built-in fallback).
        self.assertIn("Aspora Legal", default_body)
        self.assertTrue(isolated)

    def test_ai_settings_endpoint_updates_runtime_review_engine(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: ""}, clear=False):
                    initial_status, initial_payload = self.request("GET", "/api/ai/settings")
                    runtime_status, runtime_payload = self.request(
                        "POST",
                        "/api/ai/settings",
                        {
                            "active_review_engine": "ai_first",
                        },
                    )
                    invalid_engine_status, invalid_engine_payload = self.request(
                        "POST",
                        "/api/ai/settings",
                        {"active_review_engine": "random"},
                    )
                    deterministic_status, deterministic_payload = self.request(
                        "POST",
                        "/api/ai/settings",
                        {"active_review_engine": "deterministic"},
                    )
                    runtime_settings = app_settings.review_runtime_settings()
                    telemetry_counters = telemetry.snapshot()["counters"]

        self.assertEqual(initial_status, 200)
        self.assertEqual(initial_payload["active_review_engine"]["active_engine"], "ai_first")
        self.assertEqual(initial_payload["active_review_engine"]["engine_source"], "default")
        # Membership (not index 0): under the ephemeral test data dir the
        # storage_not_durable warning also fires, so assert presence robustly.
        self.assertIn("ai_first_without_key", [w["code"] for w in initial_payload["operational_warnings"]])
        self.assertEqual(initial_payload["settings_audit"], [])
        self.assertEqual(runtime_status, 200)
        self.assertEqual(runtime_payload["active_review_engine"]["active_engine"], "ai_first")
        self.assertEqual(runtime_payload["active_review_engine"]["engine_source"], "runtime_settings")
        self.assertIn("ai_first_without_key", [w["code"] for w in runtime_payload["operational_warnings"]])
        self.assertEqual(runtime_payload["settings_audit"][0]["action"], "admin_settings_update")
        self.assertEqual(
            [change["setting"] for change in runtime_payload["settings_audit"][0]["changes"]],
            ["review_runtime.active_review_engine"],
        )
        self.assertEqual(runtime_settings["active_review_engine"], "ai_first")
        self.assertEqual(telemetry_counters["review_runtime_settings_updates"], 1)
        self.assertEqual(telemetry_counters["settings_audit_events"], 1)
        self.assertEqual(invalid_engine_status, 400)
        self.assertEqual(invalid_engine_payload["error"], "Active review engine must be ai_first.")
        self.assertEqual(deterministic_status, 400)
        self.assertEqual(deterministic_payload["error"], "Active review engine must be ai_first.")

    def test_ai_settings_endpoint_treats_environment_pin_as_noop(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(
                    os.environ,
                    {
                        ACTIVE_REVIEW_ENGINE_ENV: "ai_first",
                    },
                    clear=False,
                ):
                    status, payload = self.request("GET", "/api/ai/settings")
                    engine_status, engine_payload = self.request(
                        "POST",
                        "/api/ai/settings",
                        {"active_review_engine": "ai_first"},
                    )
                    runtime_settings = app_settings.review_runtime_settings()
                    telemetry_counters = telemetry.snapshot()["counters"]

        self.assertEqual(status, 200)
        self.assertEqual(payload["active_review_engine"]["engine_source"], "environment")
        self.assertIn("active_engine_environment_pinned", [warning["code"] for warning in payload["operational_warnings"]])
        self.assertEqual(engine_status, 200)
        self.assertEqual(engine_payload["active_review_engine"]["active_engine"], "ai_first")
        self.assertIsNone(runtime_settings["active_review_engine"])
        self.assertNotIn("review_runtime_update_blocked_environment", telemetry_counters)

    def test_ai_api_key_endpoint_saves_local_key_and_enables_ai(self):
        from nda_automation import ai_review

        valid_result = ai_review.ApiKeyValidationResult("valid", "OpenRouter key verified.")
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "NDA_AI_REVIEW_ENABLED": ""}, clear=False):
                    # The save path now round-trips the key against OpenRouter before
                    # persisting; mock that probe so the test never hits the network.
                    with patch.object(server_module.admin_routes.ai_review, "validate_api_key", return_value=valid_result) as validate_mock:
                        initial_status, initial_payload = self.request("GET", "/api/ai/settings")
                        save_status, save_payload = self.request("POST", "/api/ai/api-key", {"api_key": "local-secret-key"})
                        saved_key = app_settings.stored_ai_api_key()
                        invalid_status, invalid_payload = self.request("POST", "/api/ai/api-key", {"api_key": ""})
                        clear_status, clear_payload = self.request("DELETE", "/api/ai/api-key")
                        cleared_key = app_settings.stored_ai_api_key()
                        settings = app_settings.ai_settings()
                    # The empty-key request short-circuits before validation; the one
                    # real key save is the only probe.
                    validate_mock.assert_called_once_with("local-secret-key")

        self.assertEqual(initial_status, 200)
        self.assertEqual(initial_payload["ai_review"]["api_key_configured"], False)
        self.assertEqual(initial_payload["ai_review"]["api_key_source"], "")
        self.assertEqual(save_status, 200)
        self.assertEqual(save_payload["ai_review"]["enabled"], True)
        self.assertEqual(save_payload["ai_review"]["stored_enabled"], True)
        self.assertEqual(save_payload["ai_review"]["provider"], "openrouter")
        self.assertEqual(save_payload["ai_review"]["model"], "anthropic/claude-opus-4.8-fast")
        self.assertEqual(save_payload["ai_review"]["api_key_configured"], True)
        self.assertEqual(save_payload["ai_review"]["api_key_source"], "local_settings")
        self.assertEqual(save_payload["settings_audit"][0]["action"], "ai_api_key_saved")
        self.assertIn("ai_review.api_key", [change["setting"] for change in save_payload["settings_audit"][0]["changes"]])
        self.assertNotIn("local-secret-key", json.dumps(save_payload))
        self.assertEqual(saved_key, "local-secret-key")
        self.assertEqual(settings["enabled"], True)
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_payload["error"], "Provide an AI API key to save.")
        self.assertEqual(clear_status, 200)
        self.assertEqual(clear_payload["ai_review"]["api_key_configured"], False)
        self.assertEqual(clear_payload["ai_review"]["api_key_source"], "")
        self.assertEqual(cleared_key, "")

    def test_ai_api_key_rejected_key_is_not_persisted_and_ai_not_enabled(self):
        from nda_automation import ai_review

        rejected = ai_review.ApiKeyValidationResult(
            "rejected",
            "This OpenRouter key was rejected — check it's correct and not expired.",
        )
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "NDA_AI_REVIEW_ENABLED": ""}, clear=False):
                    # Seed a known-good prior state so we can prove it stays untouched.
                    with patch.object(server_module.admin_routes.ai_review, "validate_api_key", return_value=ai_review.ApiKeyValidationResult("valid", "ok")):
                        self.request("POST", "/api/ai/api-key", {"api_key": "good-key-prior"})
                    prior_key = app_settings.stored_ai_api_key()
                    prior_enabled = app_settings.ai_settings()["enabled"]

                    with patch.object(server_module.admin_routes.ai_review, "validate_api_key", return_value=rejected):
                        status, payload = self.request("POST", "/api/ai/api-key", {"api_key": "bad-key"})

                    key_after = app_settings.stored_ai_api_key()
                    enabled_after = app_settings.ai_settings()["enabled"]

        self.assertEqual(prior_key, "good-key-prior")
        self.assertTrue(prior_enabled)
        # Rejected: HTTP 400, clear message, NOT persisted, AI still on the PRIOR key.
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "This OpenRouter key was rejected — check it's correct and not expired.")
        self.assertNotIn("bad-key", json.dumps(payload))
        self.assertEqual(key_after, "good-key-prior")
        self.assertEqual(enabled_after, prior_enabled)

    def test_ai_api_key_rejected_key_never_enables_ai_from_clean_state(self):
        from nda_automation import ai_review

        rejected = ai_review.ApiKeyValidationResult(
            "rejected",
            "This OpenRouter key was rejected — check it's correct and not expired.",
        )
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "NDA_AI_REVIEW_ENABLED": ""}, clear=False):
                    with patch.object(server_module.admin_routes.ai_review, "validate_api_key", return_value=rejected):
                        status, payload = self.request("POST", "/api/ai/api-key", {"api_key": "bad-key"})
                    key_after = app_settings.stored_ai_api_key()
                    enabled_after = app_settings.ai_settings()["enabled"]

        # Closely-related guard: a rejected key from a clean state must never flip
        # AI to enabled and must not persist.
        self.assertEqual(status, 400)
        self.assertEqual(key_after, "")
        self.assertFalse(enabled_after)
        self.assertFalse(payload["ai_review"]["enabled"] if "ai_review" in payload else False)

    def test_ai_api_key_unreachable_does_not_persist_or_enable(self):
        from nda_automation import ai_review

        unreachable = ai_review.ApiKeyValidationResult(
            "unreachable",
            "Couldn't verify the key right now (OpenRouter unreachable) — try again.",
        )
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "NDA_AI_REVIEW_ENABLED": ""}, clear=False):
                    with patch.object(server_module.admin_routes.ai_review, "validate_api_key", return_value=unreachable):
                        status, payload = self.request("POST", "/api/ai/api-key", {"api_key": "maybe-good-key"})
                    key_after = app_settings.stored_ai_api_key()
                    enabled_after = app_settings.ai_settings()["enabled"]

        # Unreachable (transient): we decline to persist rather than pretend it's
        # verified — 503 + clear retry message, no key saved, AI not enabled. This
        # guarantees no false "verified / AI on" state from an unverified key.
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "Couldn't verify the key right now (OpenRouter unreachable) — try again.")
        self.assertEqual(key_after, "")
        self.assertFalse(enabled_after)

    def test_gmail_sync_history_records_recent_counts_and_errors(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                app_settings.record_gmail_sync(
                    {
                        "deduplicated_count": 2,
                        "imported": [{"id": "matter_1"}],
                        "query": "query one",
                        "skipped": [
                            {"reason": "duplicate_attachment"},
                            {"reason": "review_failed"},
                        ],
                    },
                    started_at="2026-06-01T00:00:00+00:00",
                    synced_at="2026-06-01T00:00:02+00:00",
                    finished_at="2026-06-01T00:00:02+00:00",
                )
                app_settings.record_gmail_sync_error(
                    "token missing",
                    started_at="2026-06-01T00:01:00+00:00",
                    finished_at="2026-06-01T00:01:01+00:00",
                    query="query two",
                )
                settings = app_settings.gmail_settings()

        self.assertEqual(settings["last_sync_at"], "2026-06-01T00:01:01+00:00")
        self.assertEqual(settings["last_sync_imported_count"], 0)
        self.assertEqual(settings["last_sync_skipped_count"], 0)
        self.assertEqual(len(settings["sync_history"]), 2)
        self.assertEqual(settings["sync_history"][0]["status"], "error")
        self.assertEqual(settings["sync_history"][0]["error"], "token missing")
        self.assertEqual(settings["sync_history"][1]["query"], "query one")
        self.assertEqual(settings["sync_history"][1]["imported_count"], 1)
        self.assertEqual(settings["sync_history"][1]["skipped_count"], 2)
        self.assertEqual(settings["sync_history"][1]["duplicate_count"], 1)
        self.assertEqual(settings["sync_history"][1]["deduplicated_count"], 2)
        self.assertEqual(settings["sync_history"][1]["review_failed_count"], 1)

    def test_scheduled_gmail_sync_uses_full_import_window(self):
        result = {
            "account": "inbound@aspora.com",
            "imported": [{"id": "matter_1"}],
            "query": gmail_integration.DEFAULT_INBOUND_QUERY,
            "skipped": [],
        }
        with patch.dict(os.environ, {"NDA_GMAIL_SERVER_INBOUND": "1"}, clear=False):
            with patch.object(server_module.gmail_integration, "import_inbound_matters", return_value=result) as import_inbound:
                with patch.object(server_module.matter_store, "deduplicate_gmail_matters", return_value=2) as deduplicate:
                    with patch.object(server_module.app_settings, "record_gmail_sync") as record_sync:
                        server_module._run_scheduled_gmail_sync()

        import_inbound.assert_called_once_with(limit=gmail_integration.MAX_GMAIL_IMPORT_LIMIT)
        deduplicate.assert_called_once_with()
        record_sync.assert_called_once()
        self.assertEqual(record_sync.call_args.args[0], {**result, "deduplicated_count": 2})

    def test_gmail_sync_owner_user_ids_only_include_connected_inbound_users(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                first_user = user_store.upsert_google_user({
                    "sub": "google-user-a",
                    "email": "a@example.com",
                    "name": "A",
                    "picture": "",
                })
                second_user = user_store.upsert_google_user({
                    "sub": "google-user-b",
                    "email": "b@example.com",
                    "name": "B",
                    "picture": "",
                })
                first_token_dir = matter_store.DATA_DIR / "users" / "gmail" / first_user["id"]
                first_token_dir.mkdir(parents=True, exist_ok=True)
                (first_token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["inbound"]).write_text(
                    "{}\n",
                    encoding="utf-8",
                )
                second_token_dir = matter_store.DATA_DIR / "users" / "gmail" / second_user["id"]
                second_token_dir.mkdir(parents=True, exist_ok=True)
                (second_token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["outbound"]).write_text(
                    "{}\n",
                    encoding="utf-8",
                )

                owner_user_ids = gmail_integration.gmail_sync_owner_user_ids()

        self.assertEqual(owner_user_ids, [first_user["id"]])

    def test_scheduled_gmail_sync_runs_for_each_connected_google_user(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                first_user = user_store.upsert_google_user({
                    "sub": "google-user-a",
                    "email": "a@example.com",
                    "name": "A",
                    "picture": "",
                })
                second_user = user_store.upsert_google_user({
                    "sub": "google-user-b",
                    "email": "b@example.com",
                    "name": "B",
                    "picture": "",
                })
                for user in (first_user, second_user):
                    token_dir = matter_store.DATA_DIR / "users" / "gmail" / user["id"]
                    token_dir.mkdir(parents=True, exist_ok=True)
                    (token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["inbound"]).write_text(
                        "{}\n",
                        encoding="utf-8",
                    )

                def import_side_effect(*, limit, query=None, owner_user_id=""):
                    self.assertEqual(limit, gmail_integration.MAX_GMAIL_IMPORT_LIMIT)
                    self.assertIsNone(query)
                    return {
                        "account": f"{owner_user_id}@example.com",
                        "imported": [{"id": f"matter-{owner_user_id}"}],
                        "query": "in:inbox has:attachment",
                        "skipped": [{"message_id": f"msg-{owner_user_id}", "reason": "duplicate_attachment"}],
                    }

                def deduplicate_side_effect(*, owner_user_id=""):
                    return 1 if owner_user_id == first_user["id"] else 2

                with patch.object(
                    server_module.gmail_integration,
                    "import_inbound_matters",
                    side_effect=import_side_effect,
                ) as import_inbound:
                    with patch.object(
                        server_module.matter_store,
                        "deduplicate_gmail_matters",
                        side_effect=deduplicate_side_effect,
                    ) as deduplicate:
                        with patch.object(server_module.app_settings, "record_gmail_sync") as record_sync:
                            server_module._run_scheduled_gmail_sync()
                first_sync = user_store.gmail_sync_status(first_user["id"])
                second_sync = user_store.gmail_sync_status(second_user["id"])

        import_inbound.assert_has_calls([
            call(limit=gmail_integration.MAX_GMAIL_IMPORT_LIMIT, owner_user_id=first_user["id"]),
            call(limit=gmail_integration.MAX_GMAIL_IMPORT_LIMIT, owner_user_id=second_user["id"]),
        ])
        deduplicate.assert_has_calls([
            call(owner_user_id=first_user["id"]),
            call(owner_user_id=second_user["id"]),
        ])
        record_sync.assert_called_once()
        recorded_result = record_sync.call_args.args[0]
        self.assertEqual(len(recorded_result["imported"]), 2)
        self.assertEqual(len(recorded_result["skipped"]), 2)
        self.assertEqual(recorded_result["deduplicated_count"], 3)
        self.assertEqual(recorded_result["query"], "in:inbox has:attachment")
        self.assertEqual(
            [entry["owner_user_id"] for entry in recorded_result["per_user"]],
            [first_user["id"], second_user["id"]],
        )
        self.assertEqual(first_sync["last_sync_imported_count"], 1)
        self.assertEqual(first_sync["last_sync_skipped_count"], 1)
        self.assertEqual(first_sync["sync_history"][0]["deduplicated_count"], 1)
        self.assertEqual(second_sync["last_sync_imported_count"], 1)
        self.assertEqual(second_sync["last_sync_skipped_count"], 1)
        self.assertEqual(second_sync["sync_history"][0]["deduplicated_count"], 2)

    def test_gmail_sync_scheduler_step_idles_when_interval_has_not_elapsed(self):
        with patch.object(
            server_module.app_settings,
            "gmail_settings",
            return_value={"inbound_enabled": True, "sync_frequency": "10_minutes"},
        ):
            with patch.object(server_module.app_settings, "gmail_sync_interval_seconds", return_value=600):
                with patch.object(server_module.time, "monotonic", return_value=100.0):
                    with patch.object(server_module, "_run_scheduled_gmail_sync") as run_sync:
                        last_run, last_frequency, sleep_seconds = server_module._gmail_sync_scheduler_step(
                            99.0,
                            "10_minutes",
                        )

        self.assertEqual(last_run, 99.0)
        self.assertEqual(last_frequency, "10_minutes")
        self.assertEqual(sleep_seconds, 599)
        run_sync.assert_not_called()

    def test_gmail_sync_scheduler_step_skips_when_inbound_setup_is_missing(self):
        with patch.object(
            server_module.app_settings,
            "gmail_settings",
            return_value={"inbound_enabled": True, "sync_frequency": "always_on"},
        ):
            with patch.object(server_module.app_settings, "gmail_sync_interval_seconds", return_value=60):
                with patch.object(server_module.gmail_integration, "gmail_role_setup_error", return_value="Set token"):
                    with patch.object(server_module.time, "monotonic", return_value=120.0):
                        with patch.object(server_module, "_run_scheduled_gmail_sync") as run_sync:
                            last_run, last_frequency, sleep_seconds = server_module._gmail_sync_scheduler_step(
                                0.0,
                                "always_on",
                            )

        self.assertEqual(last_run, 120.0)
        self.assertEqual(last_frequency, "always_on")
        self.assertEqual(sleep_seconds, 60)
        run_sync.assert_not_called()

    def test_gmail_sync_scheduler_step_runs_when_inbound_setup_is_ready(self):
        # No connected user here: this is the server-token path, which is now
        # gated behind the explicit NDA_GMAIL_SERVER_INBOUND opt-in.
        with patch.dict(os.environ, {"NDA_GMAIL_SERVER_INBOUND": "1"}, clear=False):
            with patch.object(
                server_module.app_settings,
                "gmail_settings",
                return_value={"inbound_enabled": True, "sync_frequency": "always_on"},
            ):
                with patch.object(server_module.app_settings, "gmail_sync_interval_seconds", return_value=60):
                    with patch.object(server_module.gmail_integration, "gmail_role_setup_error", return_value=""):
                        with patch.object(server_module.time, "monotonic", return_value=120.0):
                            with patch.object(server_module, "_gmail_sync_process_lock", self.acquired_gmail_sync_lock):
                                with patch.object(server_module, "_run_scheduled_gmail_sync") as run_sync:
                                    last_run, last_frequency, sleep_seconds = server_module._gmail_sync_scheduler_step(
                                        0.0,
                                        "always_on",
                                    )

        self.assertEqual(last_run, 120.0)
        self.assertEqual(last_frequency, "always_on")
        self.assertEqual(sleep_seconds, 60)
        run_sync.assert_called_once_with()

    def test_gmail_sync_scheduler_step_skips_server_token_when_no_user_and_no_opt_in(self):
        # Regression: after the last user disconnects (no connected owners), a
        # scheduled poll must NOT fall back to a leftover server/env token.
        # Disconnecting Gmail should actually stop the inbound sync.
        with patch.dict(os.environ, {"NDA_GMAIL_SERVER_INBOUND": ""}, clear=False):
            with patch.object(
                server_module.app_settings,
                "gmail_settings",
                return_value={"inbound_enabled": True, "sync_frequency": "always_on"},
            ):
                with patch.object(server_module.app_settings, "gmail_sync_interval_seconds", return_value=60):
                    with patch.object(
                        server_module.gmail_integration, "gmail_sync_owner_user_ids", return_value=[]
                    ):
                        # A leftover server token would otherwise satisfy this gate.
                        with patch.object(
                            server_module.gmail_integration, "gmail_role_setup_error", return_value=""
                        ):
                            with patch.object(server_module.time, "monotonic", return_value=120.0):
                                with patch.object(server_module, "_run_scheduled_gmail_sync") as run_sync:
                                    last_run, last_frequency, sleep_seconds = (
                                        server_module._gmail_sync_scheduler_step("", "always_on")
                                    )

        self.assertEqual(last_run, 120.0)
        self.assertEqual(last_frequency, "always_on")
        self.assertEqual(sleep_seconds, 60)
        run_sync.assert_not_called()

    def test_gmail_sync_scheduler_step_runs_server_token_when_opted_in(self):
        # The explicit opt-in keeps a legitimately-configured server-token inbound
        # deployment working.
        with patch.dict(os.environ, {"NDA_GMAIL_SERVER_INBOUND": "1"}, clear=False):
            with patch.object(
                server_module.app_settings,
                "gmail_settings",
                return_value={"inbound_enabled": True, "sync_frequency": "always_on"},
            ):
                with patch.object(server_module.app_settings, "gmail_sync_interval_seconds", return_value=60):
                    with patch.object(
                        server_module.gmail_integration, "gmail_sync_owner_user_ids", return_value=[]
                    ):
                        with patch.object(
                            server_module.gmail_integration, "gmail_role_setup_error", return_value=""
                        ):
                            with patch.object(server_module.time, "monotonic", return_value=120.0):
                                with patch.object(
                                    server_module, "_gmail_sync_process_lock", self.acquired_gmail_sync_lock
                                ):
                                    with patch.object(server_module, "_run_scheduled_gmail_sync") as run_sync:
                                        last_run, last_frequency, sleep_seconds = (
                                            server_module._gmail_sync_scheduler_step("", "always_on")
                                        )

        self.assertEqual(last_run, 120.0)
        self.assertEqual(last_frequency, "always_on")
        self.assertEqual(sleep_seconds, 60)
        run_sync.assert_called_once_with()

    def test_run_scheduled_gmail_sync_no_ops_without_user_or_opt_in(self):
        # Defense-in-depth: the run function itself makes ZERO import calls when
        # there is no connected user and no explicit server-inbound opt-in.
        with patch.dict(os.environ, {"NDA_GMAIL_SERVER_INBOUND": ""}, clear=False):
            with patch.object(
                server_module.gmail_integration, "gmail_sync_owner_user_ids", return_value=[]
            ):
                with patch.object(
                    server_module.gmail_integration, "import_inbound_matters"
                ) as import_inbound:
                    with patch.object(server_module.app_settings, "record_gmail_sync") as record_sync:
                        server_module._run_scheduled_gmail_sync()

        import_inbound.assert_not_called()
        record_sync.assert_not_called()
        self.assertEqual(
            telemetry.snapshot()["counters"].get("gmail_sync_skipped_no_connected_user"), 1
        )

    def test_run_scheduled_gmail_sync_uses_server_token_when_opted_in(self):
        with patch.dict(os.environ, {"NDA_GMAIL_SERVER_INBOUND": "1"}, clear=False):
            with patch.object(
                server_module.gmail_integration, "gmail_sync_owner_user_ids", return_value=[]
            ):
                with patch.object(
                    server_module.gmail_integration,
                    "import_inbound_matters",
                    return_value={"imported": [], "skipped": []},
                ) as import_inbound:
                    with patch.object(
                        server_module.DiskMatterRepository,
                        "deduplicate_gmail_matters",
                        return_value=0,
                    ):
                        with patch.object(server_module.app_settings, "record_gmail_sync") as record_sync:
                            server_module._run_scheduled_gmail_sync()

        import_inbound.assert_called_once()
        record_sync.assert_called_once()

    def test_scheduled_gmail_sync_backs_off_after_gmail_rate_limit(self):
        error = gmail_integration.GmailRateLimitError(
            "Gmail API rate limit exceeded. Retry after 2026-06-04T14:06:26.379Z.",
            retry_after_epoch=1000.0,
        )
        with patch.dict(os.environ, {"NDA_GMAIL_SERVER_INBOUND": "1"}, clear=False):
            with patch.object(server_module.gmail_integration, "import_inbound_matters", side_effect=error):
                with patch.object(server_module.app_settings, "record_gmail_sync_error") as record_error:
                    with patch.object(server_module, "_log_background_error"):
                        server_module._run_scheduled_gmail_sync()

        record_error.assert_called_once()
        self.assertEqual(telemetry.snapshot()["counters"]["gmail_sync_rate_limit_failures"], 1)

        with patch.object(
            server_module.app_settings,
            "gmail_settings",
            return_value={"inbound_enabled": True, "sync_frequency": "always_on"},
        ):
            with patch.object(server_module.app_settings, "gmail_sync_interval_seconds", return_value=60):
                with patch.object(server_module.gmail_integration, "gmail_role_setup_error", return_value=""):
                    with patch.object(server_module.time, "time", return_value=500.0):
                        with patch.object(server_module.time, "monotonic", return_value=500.0):
                            with patch.object(server_module, "_run_scheduled_gmail_sync") as run_sync:
                                last_run, last_frequency, sleep_seconds = server_module._gmail_sync_scheduler_step(
                                    0.0,
                                    "always_on",
                                )

        self.assertEqual(last_run, 0.0)
        self.assertEqual(last_frequency, "always_on")
        self.assertEqual(sleep_seconds, 60)
        run_sync.assert_not_called()

    def test_gmail_sync_scheduler_sleep_seconds_uses_configured_interval(self):
        self.assertEqual(server_module._gmail_sync_scheduler_sleep_seconds(0), 1)
        self.assertEqual(server_module._gmail_sync_scheduler_sleep_seconds(10), 10)
        self.assertEqual(server_module._gmail_sync_scheduler_sleep_seconds(600), 600)

    def test_gmail_sync_scheduler_loop_sleeps_after_step_errors(self):
        with patch.object(server_module, "_gmail_sync_scheduler_step", side_effect=RuntimeError("settings failed")):
            with patch.object(server_module, "_log_background_error") as log_error:
                with patch.object(server_module.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
                    with self.assertRaises(KeyboardInterrupt):
                        server_module._gmail_sync_scheduler_loop()

        log_error.assert_called_once()
        sleep.assert_called_once_with(server_module.MAX_GMAIL_SYNC_IDLE_SECONDS)

    def test_gmail_import_skips_duplicate_and_imports_new_attachment(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults):
                return FakeExecutable({"messages": [{"id": message_id} for message_id in self.messages][:maxResults]})

            def attachments(self):
                return self

            def get(self, userId=None, messageId=None, id=None, format=None):
                if messageId:
                    return FakeExecutable({"data": ""})
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "daniyal.ahmad@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        docx_bytes = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each party may disclose Confidential Information to the other party.",
            "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])
        inline_data = base64.urlsafe_b64encode(docx_bytes).decode("ascii").rstrip("=")
        unsafe_inline_data = base64.urlsafe_b64encode(make_unsafe_docx()).decode("ascii").rstrip("=")
        messages = {
            "msg_duplicate": {
                "id": "msg_duplicate",
                "threadId": "thr_duplicate",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [{"name": "From", "value": "Legal <legal@example.com>"}, {"name": "Subject", "value": "NDA"}],
                    "parts": [{"partId": "1", "filename": "Duplicate NDA.docx", "body": {"attachmentId": "att_duplicate"}}],
                },
            },
            "msg_self": {
                "id": "msg_self",
                "threadId": "thr_self",
                "labelIds": ["SENT"],
                "snippet": "Please find attached the redlined version.",
                "payload": {
                    "headers": [{"name": "From", "value": "Daniyal Ahmad <daniyal.ahmad@aspora.com>"}, {"name": "Subject", "value": "Re: NDA"}],
                    "parts": [{"partId": "1", "filename": "New NDA-redlined.docx", "body": {"data": inline_data}}],
                },
            },
            "msg_new": {
                "id": "msg_new",
                "threadId": "thr_new",
                "labelIds": ["INBOX"],
                "snippet": "Please review.",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Noreply <noreply@example.com>"},
                        {"name": "Reply-To", "value": "Legal <legal@example.com>"},
                        {"name": "Subject", "value": "NDA"},
                    ],
                    "parts": [{"partId": "1", "filename": "New NDA.docx", "body": {"data": inline_data}}],
                },
            },
            "msg_unsafe": {
                "id": "msg_unsafe",
                "threadId": "thr_unsafe",
                "labelIds": ["INBOX"],
                "snippet": "Please review.",
                "payload": {
                    "headers": [{"name": "From", "value": "Legal <legal@example.com>"}, {"name": "Subject", "value": "NDA"}],
                    "parts": [{"partId": "1", "filename": "Unsafe NDA.docx", "body": {"data": unsafe_inline_data}}],
                },
            },
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_store.create_matter(
                    source_filename="Duplicate NDA.docx",
                    document_bytes=docx_bytes,
                    extracted_text="Duplicate",
                    review_result={"clauses": []},
                    triage={},
                    source_type="gmail_inbound",
                    intake_metadata={
                        "gmail_attachment_id": "att_duplicate",
                        "gmail_message_id": "msg_duplicate",
                        "gmail_part_id": "1",
                    },
                    dedupe_gmail=True,
                )
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                            result = gmail_integration.import_inbound_matters(limit=25)
                stored = matter_store.list_matters()

        self.assertEqual(result["account"], "daniyal.ahmad@aspora.com")
        # msg_duplicate's attachment is already imported, so it is short-circuited at
        # the message level ("already_imported") BEFORE any download/extract, rather
        # than the per-attachment "duplicate_attachment". msg_self / msg_unsafe are
        # unchanged.
        self.assertEqual([item["reason"] for item in result["skipped"]], ["already_imported", "self_sent_or_outbound", "review_failed"])
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(result["imported"][0]["gmail_message_id"], "msg_new")
        self.assertEqual(result["imported"][0]["reply_to"], "Legal <legal@example.com>")
        self.assertEqual(result["imported"][0]["gmail_detection_sources"], "subject, attachment_filename, attachment_content")
        self.assertIn("NDA", result["imported"][0]["gmail_detection_terms"])
        self.assertIn("confidential information", result["imported"][0]["gmail_detection_terms"])
        self.assertEqual(matter_view.public_matter(result["imported"][0])["recipient_email"], "legal@example.com")
        self.assertEqual(len(stored), 2)

    def test_gmail_inbound_import_defers_ai_review_off_the_poll_thread(self):
        # The inbound poll must create the matter UN-REVIEWED (no review at create)
        # and NEVER call the AI assessor/verifier in the poll thread (that
        # Opus+Pro storm + the PDF/extract memory spike is what OOM-crash-looped the
        # single prod worker). Pin the active engine to ai_first -- WITHOUT the
        # defer fix the poll would call assess_nda_with_ai once per attachment -- and
        # assert the assessor is never invoked while the matter still imports.
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults):
                return FakeExecutable({"messages": [{"id": mid} for mid in self.messages][:maxResults]})

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        docx_bytes = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each party may disclose Confidential Information to the other party.",
            "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
            "This Agreement shall be governed by the laws of California.",
        ])
        inline_data = base64.urlsafe_b64encode(docx_bytes).decode("ascii").rstrip("=")
        messages = {
            "msg_new": {
                "id": "msg_new",
                "threadId": "thr_new",
                "labelIds": ["INBOX"],
                "snippet": "Please review.",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Legal <legal@example.com>"},
                        {"name": "Subject", "value": "NDA"},
                    ],
                    "parts": [{"partId": "1", "filename": "New NDA.docx", "body": {"attachmentId": "att_new", "data": inline_data}}],
                },
            },
        }

        assessor = MagicMock(name="assess_nda_with_ai")
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    # ai_first is the active engine; the assessor is a strict mock so
                    # ANY poll-path AI call would be recorded (and break the contract).
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true"}):
                        with patch("nda_automation.ai_assessor.assess_nda_with_ai", assessor):
                            with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                                result = gmail_integration.import_inbound_matters(limit=25)
                imported = result["imported"]
                stored_matter = matter_store.get_matter(imported[0]["id"]) if imported else None

        # The poll imported the matter but ran ZERO assessor/verifier calls.
        assessor.assert_not_called()
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["gmail_message_id"], "msg_new")
        # The matter is created UN-REVIEWED on the poll thread: no review runs at
        # create (no deterministic first-pass), and the AI review is fully deferred.
        self.assertIsNone(stored_matter["review_result"])

    def test_gmail_inbound_skips_already_imported_message_before_content_scan(self):
        # An already-imported message (re-surfaced by the inbox query every poll)
        # must be short-circuited BEFORE any download/extract: neither the content
        # scan (attachment_nda_detection) nor the attachment download may run for it.
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults):
                return FakeExecutable({"messages": [{"id": mid} for mid in self.messages][:maxResults]})

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        # A NEUTRAL subject/body so message_nda_detection does NOT match -- without
        # the dedup short-circuit the loop would fall through to the content scan
        # (attachment_nda_detection), which downloads + extracts the attachment.
        docx_bytes = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
        ])
        messages = {
            "msg_seen": {
                "id": "msg_seen",
                "threadId": "thr_seen",
                "labelIds": ["INBOX"],
                "snippet": "please take a look",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Ops <ops@example.com>"},
                        {"name": "Subject", "value": "Fwd: a document"},
                    ],
                    "parts": [
                        {"partId": "doc", "filename": "Document.docx", "body": {"attachmentId": "att_seen"}},
                    ],
                },
            },
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                # Pre-seed a matter carrying this message+attachment's identity keys
                # so the message is genuinely "already imported".
                matter_store.create_matter(
                    source_filename="Document.docx",
                    document_bytes=docx_bytes,
                    extracted_text="Seen",
                    review_result={"clauses": []},
                    triage={},
                    source_type="gmail_inbound",
                    intake_metadata={
                        "gmail_attachment_id": "att_seen",
                        "gmail_message_id": "msg_seen",
                        "gmail_part_id": "doc",
                    },
                    dedupe_gmail=True,
                )
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                            # The content scan and the attachment download MUST NOT
                            # run for an already-imported message.
                            with (
                                patch.object(
                                    gmail_integration,
                                    "_attachment_nda_detection",
                                    side_effect=AssertionError("content scan ran for an already-imported message"),
                                ) as content_scan,
                                patch.object(
                                    gmail_integration,
                                    "_attachment_bytes",
                                    side_effect=AssertionError("attachment downloaded for an already-imported message"),
                                ),
                            ):
                                result = gmail_integration.import_inbound_matters(limit=25)
                matters = matter_store.list_matters()

        content_scan.assert_not_called()
        self.assertEqual(result["imported"], [])
        self.assertEqual([item["reason"] for item in result["skipped"]], ["already_imported"])
        # No duplicate matter was created; the pre-seeded one is untouched.
        self.assertEqual(len(matters), 1)

    def test_gmail_import_parses_plain_text_and_html_message_bodies_for_nda_signals(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults):
                return FakeExecutable({"messages": [{"id": message_id} for message_id in self.messages][:maxResults]})

            def attachments(self):
                return self

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        nda_docx_bytes = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each party may disclose Confidential Information to the other party.",
            "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])
        non_nda_docx_bytes = make_docx([
            "STATEMENT OF WORK",
            "This document describes implementation milestones and support responsibilities.",
        ])
        docx_data = inline(nda_docx_bytes)
        non_nda_docx_data = inline(non_nda_docx_bytes)
        plain_body = inline(b"Please review the attached non-disclosure agreement.")
        html_body = inline(b"<html><body><p>Please review the attached confidentiality agreement.</p></body></html>")
        irrelevant_body = inline(b"Please review the attached statement of work.")
        messages = {
            "msg_plain_body": {
                "id": "msg_plain_body",
                "threadId": "thr_plain",
                "labelIds": ["INBOX"],
                "snippet": "Please review.",
                "payload": {
                    "headers": [{"name": "From", "value": "Founder <founder@example.com>"}, {"name": "Subject", "value": "Documents for review"}],
                    "parts": [
                        {"partId": "body", "mimeType": "text/plain", "body": {"data": plain_body}},
                        {"partId": "doc", "filename": "Agreement.docx", "body": {"data": docx_data}},
                    ],
                },
            },
            "msg_html_body": {
                "id": "msg_html_body",
                "threadId": "thr_html",
                "labelIds": ["INBOX"],
                "snippet": "Please review.",
                "payload": {
                    "headers": [{"name": "From", "value": "Legal <legal@example.com>"}, {"name": "Subject", "value": "Attached contract"}],
                    "parts": [
                        {"partId": "body", "mimeType": "text/html", "body": {"data": html_body}},
                        {"partId": "doc", "filename": "Contract.docx", "body": {"data": docx_data}},
                    ],
                },
            },
            "msg_irrelevant": {
                "id": "msg_irrelevant",
                "threadId": "thr_irrelevant",
                "labelIds": ["INBOX"],
                "snippet": "Please review.",
                "payload": {
                    "headers": [{"name": "From", "value": "Ops <ops@example.com>"}, {"name": "Subject", "value": "Attached contract"}],
                    "parts": [
                        {"partId": "body", "mimeType": "text/plain", "body": {"data": irrelevant_body}},
                        {"partId": "doc", "filename": "Contract.docx", "body": {"data": non_nda_docx_data}},
                    ],
                },
            },
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                            result = gmail_integration.import_inbound_matters(limit=25)

        # The message-level no_nda_signal early-drop is gone: the irrelevant SOW
        # document is now judged by content at the attachment level and skipped as
        # non_nda_attachment (score 0, no content basis), not silently dropped
        # before its content is read.
        self.assertEqual([item["reason"] for item in result["skipped"]], ["non_nda_attachment"])
        self.assertEqual(len(result["imported"]), 2)
        imported_by_id = {item["gmail_message_id"]: item for item in result["imported"]}
        self.assertEqual(imported_by_id["msg_plain_body"]["gmail_detection_sources"], "body, attachment_content")
        self.assertIn("non-disclosure agreement", imported_by_id["msg_plain_body"]["gmail_detection_terms"])
        self.assertIn("confidential information", imported_by_id["msg_plain_body"]["gmail_detection_terms"])
        self.assertEqual(imported_by_id["msg_html_body"]["gmail_detection_sources"], "body, attachment_content")
        self.assertIn("confidentiality agreement", imported_by_id["msg_html_body"]["gmail_detection_terms"])
        self.assertIn("confidential information", imported_by_id["msg_html_body"]["gmail_detection_terms"])

    def test_gmail_import_detects_nda_signal_inside_attachment_content(self):
        # Regression: an e-signature forward (Juro/DocuSign) whose subject, body,
        # snippet, and filename carry no NDA wording -- the signal lives only
        # inside the attached .docx. Gmail's query surfaces it via attachment
        # text, so local detection must read the document content instead of
        # dropping it as no_nda_signal.
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults):
                return FakeExecutable({"messages": [{"id": message_id} for message_id in self.messages][:maxResults]})

            def attachments(self):
                return self

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "daniyal.ahmad@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        docx_bytes = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "The parties shall keep all Confidential Information confidential.",
        ])
        esign_body = inline(b"Elliott has shared a document with you. View the document to review and sign.")
        messages = {
            "msg_esign": {
                "id": "msg_esign",
                "threadId": "thr_esign",
                "labelIds": ["INBOX"],
                "snippet": "Invitation to review the Aspora document",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Neha Chawla <neha.chawla@aspora.com>"},
                        {"name": "Subject", "value": "Fwd: Invitation to review the 'Aspora' document | Juro"},
                    ],
                    "parts": [
                        {"partId": "body", "mimeType": "text/plain", "body": {"data": esign_body}},
                        {"partId": "doc", "filename": "Aspora + Monavate Ltd document.docx", "body": {"data": inline(docx_bytes)}},
                    ],
                },
            },
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                            result = gmail_integration.import_inbound_matters(limit=25)

        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(result["imported"][0]["gmail_message_id"], "msg_esign")
        self.assertEqual(result["imported"][0]["gmail_detection_sources"], "attachment_content")
        self.assertIn("non-disclosure agreement", result["imported"][0]["gmail_detection_terms"])

    def test_gmail_import_ingests_attachment_only_nda_with_neutral_subject(self):
        # T1 -- the core fix. An attachment-only NDA with a fully neutral subject,
        # body, snippet, and filename used to be invisible: the keyword group was
        # AND-appended to the fetch query, so Gmail never listed it. The fetch
        # query is now the structural envelope only (no keyword group), so it is
        # listed, and the content detector reads the NDA wording inside the doc.
        captured = {}

        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults):
                captured["query"] = q
                return FakeExecutable({"messages": [{"id": message_id} for message_id in self.messages][:maxResults]})

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        docx_bytes = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each Disclosing Party may disclose Confidential Information to a Receiving Party.",
            "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
        ])
        messages = {
            "msg_neutral": {
                "id": "msg_neutral",
                "threadId": "thr_neutral",
                "labelIds": ["INBOX"],
                "snippet": "please review the attached document",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Ops <ops@example.com>"},
                        {"name": "Subject", "value": "Fwd: shared with you"},
                    ],
                    "parts": [
                        {"partId": "body", "mimeType": "text/plain", "body": {"data": inline(b"please review the attached document")}},
                        {"partId": "doc", "filename": "Document.docx", "body": {"data": inline(docx_bytes)}},
                    ],
                },
            },
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                            result = gmail_integration.import_inbound_matters(limit=25)

        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(result["imported"][0]["gmail_detection_sources"], "attachment_content")
        # The fetch query proves the keyword gate is gone.
        self.assertEqual(captured["query"], gmail_integration.DEFAULT_INBOUND_QUERY)
        self.assertNotIn("non-disclosure", captured["query"])
        self.assertIn("newer_than:90d", captured["query"])

    def test_gmail_import_flags_borderline_attachment_as_needs_triage(self):
        # T3 -- an attachment whose content scores in [40, 70) with a weak basis is
        # imported AND flagged needs_triage, never silently dropped.
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults):
                return FakeExecutable({"messages": [{"id": message_id} for message_id in self.messages][:maxResults]})

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        borderline_docx = make_docx([
            "This document is shared in connection with our discussions.",
            "The Receiving Party will handle the Confidential Information appropriately.",
        ])
        messages = {
            "msg_borderline": {
                "id": "msg_borderline",
                "threadId": "thr_borderline",
                "labelIds": ["INBOX"],
                "snippet": "please take a look",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Ops <ops@example.com>"},
                        {"name": "Subject", "value": "Fwd: a document"},
                    ],
                    "parts": [
                        {"partId": "body", "mimeType": "text/plain", "body": {"data": inline(b"please take a look at the attached")}},
                        {"partId": "doc", "filename": "Document.docx", "body": {"data": inline(borderline_docx)}},
                    ],
                },
            },
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                            result = gmail_integration.import_inbound_matters(limit=25)

        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["imported"]), 1)
        imported = result["imported"][0]
        self.assertEqual(imported["needs_triage"], "true")
        self.assertEqual(imported["triage_reason"], "low_confidence_nda_content")
        self.assertEqual(imported["triage_confidence"], imported["gmail_attachment_score"])
        self.assertTrue(40 <= int(imported["triage_confidence"]) < 70)
        # public_matter must expose the triage flag (guards the PUBLIC_MATTER_FIELDS
        # + GMAIL_METADATA_FIELDS allowlist wiring -- the easy-to-miss step).
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                            gmail_integration.import_inbound_matters(limit=25)
                stored = matter_store.list_matters()
                public = matter_view.public_matter(stored[0])
        self.assertEqual(public["needs_triage"], "true")
        self.assertEqual(public["triage_reason"], "low_confidence_nda_content")

    def test_classify_attachment_lane_bands(self):
        # Unit test for the band classifier at scores 75 / 55 / 20.
        from nda_automation import gmail_matter_inbox

        confident = {"accepted": True, "has_content_basis": True, "score": 75}
        triage = {"accepted": False, "has_content_basis": False, "score": 55}
        skip = {"accepted": False, "has_content_basis": False, "score": 20}

        self.assertEqual(
            gmail_matter_inbox.classify_attachment_lane(
                confident, selector_selected=False, selector_configured=False
            ),
            ("confident", ""),
        )
        self.assertEqual(
            gmail_matter_inbox.classify_attachment_lane(
                triage, selector_selected=False, selector_configured=False
            ),
            ("triage", "low_confidence_nda_content"),
        )
        self.assertEqual(
            gmail_matter_inbox.classify_attachment_lane(
                skip, selector_selected=False, selector_configured=False
            ),
            ("skip", ""),
        )
        # Selector authority: selected promotes to confident even below the bar;
        # non-selected with content basis triages; non-selected without basis skips.
        self.assertEqual(
            gmail_matter_inbox.classify_attachment_lane(
                skip, selector_selected=True, selector_configured=True
            ),
            ("confident", ""),
        )
        self.assertEqual(
            gmail_matter_inbox.classify_attachment_lane(
                {"accepted": True, "has_content_basis": True, "score": 75},
                selector_selected=False,
                selector_configured=True,
            ),
            ("triage", "ai_selector_not_selected"),
        )
        self.assertEqual(
            gmail_matter_inbox.classify_attachment_lane(
                skip, selector_selected=False, selector_configured=True
            ),
            ("skip", ""),
        )

    def _intake_attachments(self, attachments, metadata, *, verdicts):
        # Drive _import_inbound_attachments with the AI intake classifier configured
        # and a stub that returns the next verdict from ``verdicts`` per call. The
        # AI selector stays unconfigured so the classifier overlays the pure
        # deterministic band lane (not the selector lane).
        from nda_automation import gmail_intake_classifier

        verdict_iter = iter(verdicts)

        def _stub_classify(_message_metadata, _candidate, _playbook):
            verdict, confidence, reason = next(verdict_iter)
            return {
                "verdict": verdict,
                "confidence": confidence,
                "reason": reason,
                "model": "deepseek/deepseek-v4-flash",
                "status": "ok",
            }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                    with patch.object(gmail_integration.gmail_attachment_selector, "selector_configured", return_value=False):
                        with patch.object(gmail_intake_classifier, "classifier_configured", return_value=True):
                            with patch.object(gmail_intake_classifier, "classify_intake_attachment", side_effect=_stub_classify) as classify:
                                result = gmail_integration._import_inbound_attachments(
                                    None,
                                    "msg_intake",
                                    attachments,
                                    metadata,
                                )
        return result, classify

    def _intake_attachment_payload(self, paragraphs, filename):
        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        return {
            "attachment_id": f"att_{filename}",
            "data": inline(make_docx(paragraphs)),
            "filename": filename,
            "part_id": "1",
        }

    def test_intake_classifier_nda_imports_without_triage(self):
        # C12 -- attachment-only NDA, neutral subject, AI verdict NDA -> imported, no
        # needs_triage flag (deterministic confident + AI NDA agree -> confident).
        attachments = [self._intake_attachment_payload([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each Disclosing Party may disclose Confidential Information to a Receiving Party.",
            "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
        ], "Mutual NDA.docx")]
        metadata = {
            "gmail_account": "legal@aspora.com",
            "gmail_message_id": "msg_intake",
            "subject": "Fwd: a document",
            "sender": "ops@example.com",
        }
        result, classify = self._intake_attachments(
            attachments, metadata, verdicts=[("NDA", 0.95, "mutual nda")]
        )
        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["imported"]), 1)
        self.assertNotIn("needs_triage", result["imported"][0])
        classify.assert_called_once()
        # A healthy ok call is tallied as one ai_call with no errors/timeouts.
        self.assertEqual(
            result["ai_intake"],
            {"ai_calls": 1, "ai_errors": 0, "ai_timeouts": 0, "ai_skipped_cap": 0},
        )

    def test_intake_classifier_uncertain_imports_with_ai_triage(self):
        # C13 -- AI UNCERTAIN on a borderline doc -> imported with needs_triage,
        # triage_reason ai_intake_uncertain, triage_confidence from the AI confidence.
        attachments = [self._intake_attachment_payload([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each Disclosing Party may disclose Confidential Information to a Receiving Party.",
            "The Receiving Party shall not disclose Confidential Information.",
        ], "Maybe NDA.docx")]
        metadata = {
            "gmail_account": "legal@aspora.com",
            "gmail_message_id": "msg_intake",
            "subject": "Fwd: a document",
            "sender": "ops@example.com",
        }
        result, _classify = self._intake_attachments(
            attachments, metadata, verdicts=[("UNCERTAIN", 0.42, "ambiguous")]
        )
        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["imported"]), 1)
        imported = result["imported"][0]
        self.assertEqual(imported["needs_triage"], "true")
        self.assertEqual(imported["triage_reason"], "ai_intake_uncertain")
        # AI confidence 0.42 -> "42" (the model's number overrides the deterministic score).
        self.assertEqual(imported["triage_confidence"], "42")

    def test_intake_classifier_not_nda_skips_with_ai_detail(self):
        # C14 -- AI NOT_NDA on a doc with no deterministic NDA basis -> skipped with
        # non_nda_attachment carrying the AI reason/model; no matter created.
        attachments = [self._intake_attachment_payload([
            "MASTER SERVICES AGREEMENT",
            "The Supplier shall provide the services described in each statement of work.",
            "Fees are payable within thirty days of invoice.",
        ], "MSA.docx")]
        metadata = {
            "gmail_account": "legal@aspora.com",
            "gmail_message_id": "msg_intake",
            "subject": "Fwd: a document",
            "sender": "ops@example.com",
        }
        result, _classify = self._intake_attachments(
            attachments, metadata, verdicts=[("NOT_NDA", 0.9, "this is an MSA not an NDA")]
        )
        self.assertEqual(result["imported"], [])
        self.assertEqual(len(result["skipped"]), 1)
        skip = result["skipped"][0]
        self.assertEqual(skip["reason"], "non_nda_attachment")
        self.assertEqual(skip["model"], "deepseek/deepseek-v4-flash")
        self.assertEqual(skip["detail"], "this is an MSA not an NDA")

    def test_intake_classifier_unconfigured_is_byte_identical_to_deterministic(self):
        # C15 -- with the classifier unconfigured, the inbox flow is identical to the
        # deterministic classify_attachment_lane path (regression lock). The same
        # borderline doc lands as a low_confidence_nda_content triage, exactly as the
        # deterministic-only T3 test asserts.
        from nda_automation import gmail_intake_classifier

        attachments = [self._intake_attachment_payload([
            "This document is shared in connection with our discussions.",
            "The Receiving Party will handle the Confidential Information appropriately.",
        ], "Document.docx")]
        metadata = {
            "gmail_account": "legal@aspora.com",
            "gmail_message_id": "msg_intake",
            "subject": "Fwd: a document",
            "sender": "ops@example.com",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                    with patch.object(gmail_integration.gmail_attachment_selector, "selector_configured", return_value=False):
                        with patch.object(gmail_intake_classifier, "classifier_configured", return_value=False):
                            with patch.object(gmail_intake_classifier, "classify_intake_attachment") as classify:
                                result = gmail_integration._import_inbound_attachments(
                                    None,
                                    "msg_intake",
                                    attachments,
                                    metadata,
                                )
        # The classifier is never called when unconfigured.
        classify.assert_not_called()
        self.assertEqual(result["skipped"], [])
        self.assertEqual(len(result["imported"]), 1)
        imported = result["imported"][0]
        self.assertEqual(imported["needs_triage"], "true")
        self.assertEqual(imported["triage_reason"], "low_confidence_nda_content")
        # Deterministic score-derived confidence (NOT an AI override).
        self.assertEqual(imported["triage_confidence"], imported["gmail_attachment_score"])

    def test_intake_classifier_per_sync_cap_falls_back_to_deterministic(self):
        # C16 -- with the per-sync cap forced low, only the first N attachments hit
        # the AI; the overflow takes the deterministic lane (skipped_cap) and the AI
        # is called at most the cap number of times.
        from nda_automation import gmail_intake_classifier, gmail_matter_inbox

        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults, pageToken=""):
                return FakeExecutable({"messages": [{"id": message_id} for message_id in self.messages][:maxResults]})

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        nda_docx = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each Disclosing Party may disclose Confidential Information to a Receiving Party.",
            "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
        ])
        # Three messages, each a single NDA attachment; cap forced to 2.
        message_ids = ["msg_a", "msg_b", "msg_c"]
        messages = {}
        for message_id in message_ids:
            messages[message_id] = {
                "id": message_id,
                "threadId": f"thr_{message_id}",
                "labelIds": ["INBOX"],
                "snippet": "please review",
                "payload": {
                    "headers": [
                        {"name": "From", "value": f"{message_id} <{message_id}@example.com>"},
                        {"name": "Subject", "value": "Fwd: document"},
                    ],
                    "parts": [
                        {"partId": "body", "mimeType": "text/plain", "body": {"data": inline(b"please review the attached document")}},
                        {"partId": "doc", "filename": f"{message_id}.docx", "body": {"data": inline(nda_docx)}},
                    ],
                },
            }

        def _stub_classify(_message_metadata, _candidate, _playbook):
            return {
                "verdict": "NDA",
                "confidence": 0.95,
                "reason": "nda",
                "model": "deepseek/deepseek-v4-flash",
                "status": "ok",
            }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_matter_inbox, "MAX_INTAKE_CALLS_PER_SYNC", 2):
                            with patch.object(gmail_integration.gmail_attachment_selector, "selector_configured", return_value=False):
                                with patch.object(gmail_intake_classifier, "classifier_configured", return_value=True):
                                    with patch.object(gmail_intake_classifier, "classify_intake_attachment", side_effect=_stub_classify) as classify:
                                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                                            result = gmail_integration.import_inbound_matters(limit=25)

        # All three NDAs are imported (overflow falls back to the deterministic
        # confident lane, which also imports the NDA), but the AI was called at most
        # the cap number of times.
        self.assertEqual(len(result["imported"]), 3)
        self.assertLessEqual(classify.call_count, 2)
        self.assertEqual(classify.call_count, 2)
        # The per-sync ai_intake tallies are aggregated across all three messages:
        # two ok calls and one candidate that took the deterministic lane because
        # the per-sync cap was exhausted.
        self.assertEqual(
            result["ai_intake"],
            {"ai_calls": 2, "ai_errors": 0, "ai_timeouts": 0, "ai_skipped_cap": 1},
        )

    def test_intake_classifier_degraded_calls_are_tallied_and_warned(self):
        # The classifier is configured but every call fails (error/timeout). The
        # per-sync tallies must count the failures (so a fully-degraded classifier
        # is distinguishable from a healthy one) and a warn-log must fire because
        # the failure fraction is high.
        from nda_automation import gmail_intake_classifier, gmail_matter_inbox

        statuses = iter(["error", "timeout"])

        def _stub_classify(_message_metadata, _candidate, _playbook):
            return {
                "verdict": "",
                "confidence": 0.0,
                "reason": "",
                "model": "deepseek/deepseek-v4-flash",
                "status": next(statuses),
            }

        attachments = [
            self._intake_attachment_payload([
                "MUTUAL NON-DISCLOSURE AGREEMENT",
                "Each Disclosing Party may disclose Confidential Information to a Receiving Party.",
                "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
            ], "NDA-1.docx"),
            self._intake_attachment_payload([
                "MUTUAL NON-DISCLOSURE AGREEMENT",
                "Each Disclosing Party may disclose Confidential Information to a Receiving Party.",
                "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
            ], "NDA-2.docx"),
        ]
        metadata = {
            "gmail_account": "legal@aspora.com",
            "gmail_message_id": "msg_intake",
            "subject": "Fwd: a document",
            "sender": "ops@example.com",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                    with patch.object(gmail_integration.gmail_attachment_selector, "selector_configured", return_value=False):
                        with patch.object(gmail_intake_classifier, "classifier_configured", return_value=True):
                            with patch.object(gmail_intake_classifier, "classify_intake_attachment", side_effect=_stub_classify):
                                with self.assertLogs(gmail_matter_inbox.LOGGER, level="WARNING") as logs:
                                    result = gmail_integration._import_inbound_attachments(
                                        None,
                                        "msg_intake",
                                        attachments,
                                        metadata,
                                    )
        # Both degraded calls counted; deterministic fallback still imported them.
        self.assertEqual(
            result["ai_intake"],
            {"ai_calls": 2, "ai_errors": 1, "ai_timeouts": 1, "ai_skipped_cap": 0},
        )
        self.assertTrue(any("degraded" in line for line in logs.output))

    def test_intake_playbook_round_trips_through_settings(self):
        # D17 -- intake_playbook round-trips, clamps over 8000, empty -> default,
        # non-empty -> verbatim.
        from nda_automation import gmail_intake_classifier

        empty = app_settings.gmail_settings_from_payload({"intake_playbook": ""})
        self.assertEqual(empty["intake_playbook"], "")

        verbatim = app_settings.gmail_settings_from_payload({"intake_playbook": "only deeds count"})
        self.assertEqual(verbatim["intake_playbook"], "only deeds count")

        long_value = "x" * 9000
        clamped = app_settings.gmail_settings_from_payload({"intake_playbook": long_value})
        self.assertEqual(len(clamped["intake_playbook"]), 8000)

        # gmail_intake_playbook(): empty -> default, non-empty -> verbatim.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_settings", return_value={"intake_playbook": ""}):
                    self.assertEqual(
                        gmail_intake_classifier.gmail_intake_playbook(),
                        gmail_intake_classifier.DEFAULT_INTAKE_PLAYBOOK,
                    )
                with patch.object(app_settings, "gmail_settings", return_value={"intake_playbook": "custom criteria"}):
                    self.assertEqual(
                        gmail_intake_classifier.gmail_intake_playbook(),
                        "custom criteria",
                    )

    def test_gmail_settings_patch_accepts_and_rejects_intake_playbook(self):
        # D18 -- PATCH /api/gmail/settings with valid intake_playbook -> persisted;
        # non-str / over-8000 -> 400; unchanged keys untouched.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                # Valid value persists.
                ok = self._patch_gmail_settings({"intake_playbook": "deeds of confidentiality only"})
                self.assertEqual(ok.status, 200)
                body = json.loads(ok.read().decode("utf-8"))
                self.assertEqual(body["gmail_settings"]["intake_playbook"], "deeds of confidentiality only")

                # Over-length is rejected and the stored value is untouched.
                too_long = self._patch_gmail_settings({"intake_playbook": "y" * 9000})
                self.assertEqual(too_long.status, 400)
                self.assertIn("error", json.loads(too_long.read().decode("utf-8")))
                self.assertEqual(
                    app_settings.gmail_settings()["intake_playbook"],
                    "deeds of confidentiality only",
                )

                # Non-string is rejected.
                non_str = self._patch_gmail_settings({"intake_playbook": 123})
                self.assertEqual(non_str.status, 400)

    def test_gmail_settings_patch_accepts_and_rejects_inbound_window_days(self):
        # POST /api/gmail/settings with a valid in-band window persists, drives the
        # effective inbound query's newer_than:{N}d clause, and is audited; out-of-band
        # / non-numeric input returns a 400 and leaves the stored value untouched.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                # Valid value persists.
                ok = self._patch_gmail_settings({"inbound_window_days": 30})
                self.assertEqual(ok.status, 200)
                body = json.loads(ok.read().decode("utf-8"))
                self.assertEqual(body["gmail_settings"]["inbound_window_days"], 30)
                self.assertEqual(app_settings.gmail_settings()["inbound_window_days"], 30)

                # The EFFECTIVE inbound query now carries the configured window.
                self.assertIn("newer_than:30d", gmail_integration._default_inbound_query())
                self.assertIn(
                    "newer_than:30d",
                    body["gmail"]["inbound"]["query"],
                    "the status payload's inbound query reflects the saved window",
                )
                self.assertEqual(body["gmail"]["inbound_window_days"], 30)
                self.assertEqual(body["gmail"]["inbound_window_days_default"], 90)

                # The change is audited.
                audit = app_settings.settings_audit_history()
                self.assertTrue(audit, "a settings audit event is recorded")
                self.assertEqual(audit[0]["action"], "gmail_settings_update")
                self.assertIn(
                    "gmail.inbound_window_days",
                    [change["setting"] for change in audit[0]["changes"]],
                )

                # The widened ceiling (~10 years) is accepted and drives the query.
                wide = self._patch_gmail_settings({"inbound_window_days": 3650})
                self.assertEqual(wide.status, 200)
                self.assertEqual(
                    json.loads(wide.read().decode("utf-8"))["gmail_settings"]["inbound_window_days"],
                    3650,
                )
                self.assertIn("newer_than:3650d", gmail_integration._default_inbound_query())
                # Reset to 30 so the rejected-save assertion below has a stable baseline.
                self._patch_gmail_settings({"inbound_window_days": 30})

                # Out-of-band / bad input is rejected with a 400; stored value holds.
                # 3651 is one past the new ceiling; 99999 is far past it.
                for bad in (0, -5, 3651, 99999, "abc", True):
                    rejected = self._patch_gmail_settings({"inbound_window_days": bad})
                    self.assertEqual(rejected.status, 400, f"window={bad!r} should 400")
                    self.assertIn("error", json.loads(rejected.read().decode("utf-8")))
                self.assertEqual(
                    app_settings.gmail_settings()["inbound_window_days"],
                    30,
                    "a rejected save never mutates the stored window",
                )

    def _patch_gmail_settings(self, payload):
        from nda_automation.routes import gmail as gmail_routes

        class _FakeServer:
            # The admin gate (require_admin) reads handler.server.server_address[0]
            # to detect a loopback host; on loopback with auth not required the local
            # caller is trusted, so this validation-focused test clears the gate
            # without modelling a full authenticated session.
            server_address = ("127.0.0.1", 0)

        class _FakeHandler:
            def __init__(self, payload):
                self._payload = payload
                self.status = None
                self._chunks = []
                self.server = _FakeServer()
                self.current_user = None

            def _read_json_payload(self):
                return self._payload

            def _send_json(self, body, status=200, send_body=True):
                self.status = status
                self._body = json.dumps(body).encode("utf-8")

            def read(self):
                return getattr(self, "_body", b"{}")

        handler = _FakeHandler(payload)
        gmail_routes.handle_gmail_settings_update(handler)
        return handler

    # --- Gmail sync controls: sync_enabled master gate + import_limit -----------

    def test_gmail_scheduler_step_skips_whole_step_when_sync_disabled(self):
        # The master pause gate: with sync_enabled False the scheduler must skip the
        # ENTIRE step (no poll) and bump the skip telemetry, even though
        # inbound_enabled is still True. It must also be re-enable-safe: reset
        # last_run and idle SHORT (not a full cadence) so resuming polls promptly.
        telemetry.reset()
        with patch.object(
            server_module.app_settings,
            "gmail_settings",
            return_value={
                "sync_enabled": False,
                "inbound_enabled": True,
                "sync_frequency": "2_hours",
            },
        ):
            with patch.object(server_module.app_settings, "gmail_sync_interval_seconds", return_value=7200):
                with patch.object(server_module.time, "monotonic", return_value=500.0):
                    with patch.object(server_module, "_run_scheduled_gmail_sync") as run_sync:
                        last_run, last_frequency, sleep_seconds = server_module._gmail_sync_scheduler_step(
                            480.0,
                            "2_hours",
                        )

        run_sync.assert_not_called()
        # last_run reset + short idle => re-enabling the toggle polls within ~30s,
        # not after a 2-hour cadence.
        self.assertEqual(last_run, 0.0)
        self.assertEqual(last_frequency, "2_hours")
        self.assertEqual(sleep_seconds, server_module.MAX_GMAIL_SYNC_IDLE_SECONDS)
        self.assertEqual(
            telemetry.snapshot()["counters"].get("gmail_sync_skipped_disabled"),
            1,
        )

    def test_gmail_scheduler_step_env_kill_switch_stops_before_settings(self):
        # The NDA_GMAIL_SYNC_ENABLED env kill switch is the absolute override: it
        # must short-circuit BEFORE settings are read (works even if settings are
        # unreachable) and be re-enable-safe.
        telemetry.reset()
        with patch.dict(os.environ, {"NDA_GMAIL_SYNC_ENABLED": "false"}, clear=False):
            with patch.object(
                server_module.app_settings, "gmail_settings", side_effect=AssertionError("settings read")
            ):
                with patch.object(server_module, "_run_scheduled_gmail_sync") as run_sync:
                    last_run, last_frequency, sleep_seconds = server_module._gmail_sync_scheduler_step(
                        999.0,
                        "10_minutes",
                    )

        run_sync.assert_not_called()
        self.assertEqual(last_run, 0.0)
        self.assertEqual(last_frequency, "10_minutes")
        self.assertEqual(sleep_seconds, server_module.MAX_GMAIL_SYNC_IDLE_SECONDS)
        self.assertEqual(
            telemetry.snapshot()["counters"].get("gmail_sync_skipped_disabled"),
            1,
        )

    def test_gmail_sync_enabled_env_parses_falsey_values(self):
        for raw in ("false", "0", "no", "off", "FALSE", " Off "):
            with patch.dict(os.environ, {"NDA_GMAIL_SYNC_ENABLED": raw}, clear=False):
                self.assertFalse(server_module._gmail_sync_enabled(), raw)
        for raw in ("", "true", "1", "yes", "on", "anything"):
            with patch.dict(os.environ, {"NDA_GMAIL_SYNC_ENABLED": raw}, clear=False):
                self.assertTrue(server_module._gmail_sync_enabled(), raw)

    def test_gmail_scheduler_step_runs_when_sync_enabled_default_absent(self):
        # Backward-compat: a settings blob with no sync_enabled key (written before
        # this control existed) must keep polling -- the gate defaults to True.
        with patch.dict(os.environ, {"NDA_GMAIL_SERVER_INBOUND": "1"}, clear=False):
            with patch.object(
                server_module.app_settings,
                "gmail_settings",
                return_value={"inbound_enabled": True, "sync_frequency": "always_on"},
            ):
                with patch.object(server_module.app_settings, "gmail_sync_interval_seconds", return_value=60):
                    with patch.object(server_module.gmail_integration, "gmail_role_setup_error", return_value=""):
                        with patch.object(server_module.time, "monotonic", return_value=120.0):
                            with patch.object(server_module, "_gmail_sync_process_lock", self.acquired_gmail_sync_lock):
                                with patch.object(server_module, "_run_scheduled_gmail_sync") as run_sync:
                                    server_module._gmail_sync_scheduler_step(0.0, "always_on")

        run_sync.assert_called_once_with()

    def test_scheduled_gmail_sync_uses_configured_import_limit(self):
        # The scheduler must use the admin-configured import_limit (clamped), NOT a
        # hardcoded value. Set a custom limit and assert it flows into the import.
        result = {
            "account": "inbound@aspora.com",
            "imported": [{"id": "matter_1"}],
            "query": gmail_integration.DEFAULT_INBOUND_QUERY,
            "skipped": [],
        }
        with patch.dict(os.environ, {"NDA_GMAIL_SERVER_INBOUND": "1"}, clear=False):
            with patch.object(
                server_module.app_settings, "gmail_import_limit", return_value=33
            ) as import_limit:
                with patch.object(
                    server_module.gmail_integration, "import_inbound_matters", return_value=result
                ) as import_inbound:
                    with patch.object(
                        server_module.matter_store, "deduplicate_gmail_matters", return_value=0
                    ):
                        with patch.object(server_module.app_settings, "record_gmail_sync"):
                            server_module._run_scheduled_gmail_sync()

        import_limit.assert_called_once_with()
        import_inbound.assert_called_once_with(limit=33)

    def test_scheduled_user_gmail_sync_uses_configured_import_limit(self):
        # The per-connected-user path must also honour the configured limit.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                user = user_store.upsert_google_user({
                    "sub": "google-user-limit",
                    "email": "limit@example.com",
                    "name": "Limit",
                    "picture": "",
                })
                token_dir = matter_store.DATA_DIR / "users" / "gmail" / user["id"]
                token_dir.mkdir(parents=True, exist_ok=True)
                (token_dir / gmail_integration.ROLE_LOCAL_TOKEN_FILENAME["inbound"]).write_text(
                    "{}\n",
                    encoding="utf-8",
                )

                captured: dict[str, object] = {}

                def import_side_effect(*, limit, query=None, owner_user_id=""):
                    captured["limit"] = limit
                    return {
                        "account": f"{owner_user_id}@example.com",
                        "imported": [],
                        "query": "in:inbox has:attachment",
                        "skipped": [],
                    }

                with patch.object(
                    server_module.app_settings, "gmail_import_limit", return_value=37
                ):
                    with patch.object(
                        server_module.gmail_integration,
                        "import_inbound_matters",
                        side_effect=import_side_effect,
                    ):
                        with patch.object(
                            server_module.matter_store, "deduplicate_gmail_matters", return_value=0
                        ):
                            with patch.object(server_module.app_settings, "record_gmail_sync"):
                                server_module._run_scheduled_gmail_sync()

        self.assertEqual(captured["limit"], 37)

    def test_gmail_settings_endpoint_accepts_sync_enabled_and_import_limit(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"sync_enabled": False, "import_limit": 25},
                )
                settings = app_settings.gmail_settings()

        self.assertEqual(status, 200)
        self.assertEqual(payload["gmail_settings"]["sync_enabled"], False)
        self.assertEqual(payload["gmail_settings"]["import_limit"], 25)
        self.assertEqual(payload["gmail"]["settings"]["sync_enabled"], False)
        self.assertEqual(payload["gmail"]["settings"]["import_limit"], 25)
        self.assertEqual(settings["sync_enabled"], False)
        self.assertEqual(settings["import_limit"], 25)

    def test_gmail_settings_endpoint_clamps_import_limit_to_ceiling(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"import_limit": 1000},
                )

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["gmail_settings"]["import_limit"],
            app_settings.MAX_GMAIL_IMPORT_LIMIT_CLAMP,
        )
        self.assertEqual(app_settings.MAX_GMAIL_IMPORT_LIMIT_CLAMP, 40)
        # Honesty: a clamped-down request must carry a warning naming the cap, and
        # the effective (capped) value must be the one returned for display.
        self.assertEqual(
            payload["warning"],
            "Import limit capped at 40 (max safe per-poll value).",
        )

    def test_gmail_settings_endpoint_within_cap_import_limit_has_no_warning(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"import_limit": 30},
                )

        self.assertEqual(status, 200)
        self.assertEqual(payload["gmail_settings"]["import_limit"], 30)
        # A within-cap value is honoured verbatim -- no honesty warning.
        self.assertNotIn("warning", payload)

    def test_gmail_settings_endpoint_at_cap_import_limit_has_no_warning(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"import_limit": app_settings.MAX_GMAIL_IMPORT_LIMIT_CLAMP},
                )

        self.assertEqual(status, 200)
        self.assertEqual(payload["gmail_settings"]["import_limit"], 40)
        # Exactly at the cap is not a reduction, so no warning.
        self.assertNotIn("warning", payload)

    def test_gmail_settings_endpoint_rejects_non_bool_sync_enabled(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"sync_enabled": "off"},
                )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Gmail enabled settings must be true or false.")

    def test_gmail_settings_endpoint_rejects_non_numeric_import_limit(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"import_limit": "lots"},
                )
                bool_status, _ = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"import_limit": True},
                )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Gmail import limit must be a whole number.")
        self.assertEqual(bool_status, 400)

    def test_gmail_settings_partial_update_preserves_other_settings(self):
        # A partial update must touch ONLY the supplied keys and leave the rest
        # (sync_frequency, inbound_enabled, import_limit, search terms) intact.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                # Seed a non-default baseline.
                self.request(
                    "POST",
                    "/api/gmail/settings",
                    {
                        "sync_frequency": "30_minutes",
                        "inbound_enabled": False,
                        "import_limit": 15,
                        "inbound_search_terms": ["custom term"],
                    },
                )
                # Now flip ONLY the master gate.
                status, payload = self.request(
                    "POST",
                    "/api/gmail/settings",
                    {"sync_enabled": False},
                )
                settings = app_settings.gmail_settings()

        self.assertEqual(status, 200)
        self.assertEqual(settings["sync_enabled"], False)
        # Everything else from the baseline survives untouched.
        self.assertEqual(settings["sync_frequency"], "30_minutes")
        self.assertEqual(settings["inbound_enabled"], False)
        self.assertEqual(settings["import_limit"], 15)
        self.assertEqual(settings["inbound_search_terms"], ["custom term"])

    def test_gmail_import_limit_helper_setting_wins_over_env(self):
        # The stored setting is the source of truth; the env is only the fallback.
        with patch.dict(os.environ, {"NDA_GMAIL_IMPORT_LIMIT": "20"}, clear=False):
            with patch.object(gmail_integration, "MAX_GMAIL_IMPORT_LIMIT", 20):
                # Stored positive value wins (and is clamped).
                self.assertEqual(app_settings.gmail_import_limit({"import_limit": 12}), 12)
                self.assertEqual(app_settings.gmail_import_limit({"import_limit": 999}), 40)
                # Unset (0) falls back to the env default.
                self.assertEqual(app_settings.gmail_import_limit({"import_limit": 0}), 20)
                # Missing key also falls back.
                self.assertEqual(app_settings.gmail_import_limit({}), 20)

    def test_gmail_import_limit_from_payload_parses_and_clamps(self):
        self.assertEqual(app_settings.gmail_import_limit_from_payload(10), 10)
        self.assertEqual(app_settings.gmail_import_limit_from_payload("8"), 8)
        self.assertEqual(app_settings.gmail_import_limit_from_payload(500), 40)
        # Unset / invalid -> the unset sentinel (env fallback applies downstream).
        self.assertEqual(app_settings.gmail_import_limit_from_payload(None), 0)
        self.assertEqual(app_settings.gmail_import_limit_from_payload(""), 0)
        self.assertEqual(app_settings.gmail_import_limit_from_payload("nope"), 0)
        self.assertEqual(app_settings.gmail_import_limit_from_payload(0), 0)
        self.assertEqual(app_settings.gmail_import_limit_from_payload(-5), 0)

    def test_gmail_settings_from_payload_parses_sync_enabled_and_import_limit(self):
        # Defaults when absent.
        defaults = app_settings.gmail_settings_from_payload({})
        self.assertEqual(defaults["sync_enabled"], True)
        self.assertEqual(defaults["import_limit"], 0)
        # Stored False survives; import_limit is clamped on read.
        parsed = app_settings.gmail_settings_from_payload(
            {"sync_enabled": False, "import_limit": 250}
        )
        self.assertEqual(parsed["sync_enabled"], False)
        self.assertEqual(parsed["import_limit"], 40)

    def test_gmail_scheduler_enabled_defaults_true(self):
        self.assertTrue(app_settings.gmail_scheduler_enabled({}))
        self.assertTrue(app_settings.gmail_scheduler_enabled({"sync_enabled": True}))
        self.assertFalse(app_settings.gmail_scheduler_enabled({"sync_enabled": False}))

    def test_gmail_import_paginates_beyond_single_page(self):
        # T4 -- the listing follows nextPageToken and accumulates up to import_limit.
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        nda_docx = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each Disclosing Party may disclose Confidential Information to a Receiving Party.",
            "The Receiving Party shall not disclose the Disclosing Party's Confidential Information.",
        ])
        message_ids = [f"msg_{i:03d}" for i in range(100)]
        messages = {}
        for message_id in message_ids:
            messages[message_id] = {
                "id": message_id,
                "threadId": f"thr_{message_id}",
                "labelIds": ["INBOX"],
                "snippet": "please review",
                "payload": {
                    "headers": [
                        {"name": "From", "value": f"{message_id} <{message_id}@example.com>"},
                        {"name": "Subject", "value": "Fwd: document"},
                    ],
                    "parts": [
                        {"partId": "body", "mimeType": "text/plain", "body": {"data": inline(b"please review the attached document")}},
                        {"partId": "doc", "filename": f"{message_id}.docx", "body": {"data": inline(nda_docx)}},
                    ],
                },
            }

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages
                self.list_calls = []

            def list(self, userId, q, maxResults, pageToken=""):
                self.list_calls.append({"maxResults": maxResults, "pageToken": pageToken})
                if not pageToken:
                    page = [{"id": message_id} for message_id in message_ids[:50]]
                    return FakeExecutable({"messages": page, "nextPageToken": "p2"})
                page = [{"id": message_id} for message_id in message_ids[50:100]]
                return FakeExecutable({"messages": page})

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        service = FakeGmailService(messages)

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    # Raise the per-poll catch-up cap to 100 so this pagination
                    # assertion spans BOTH 50-stub pages; the default (20) would be
                    # satisfied within the first page and never follow nextPageToken.
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1", gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV: "100"}):
                        with patch.object(gmail_integration, "MAX_GMAIL_IMPORT_LIMIT", 100):
                            with patch.object(gmail_integration, "_gmail_service", return_value=service):
                                result = gmail_integration.import_inbound_matters(limit=100)

        self.assertEqual(len(result["imported"]), 100)
        list_calls = service.users_api.messages_api.list_calls
        self.assertEqual(len(list_calls), 2)
        self.assertEqual(list_calls[0]["pageToken"], "")
        self.assertEqual(list_calls[1]["pageToken"], "p2")

    def test_gmail_import_limit_defaults_low_and_is_env_configurable(self):
        # The gentle catch-up knob: the default is deliberately modest so
        # re-enabling Gmail cannot burst the single 2 GB worker, and an operator can
        # raise it via NDA_GMAIL_IMPORT_LIMIT to drain the backlog faster.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, None)
            self.assertEqual(gmail_integration._gmail_import_limit_from_env(), 20)
            os.environ[gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV] = "40"
            self.assertEqual(gmail_integration._gmail_import_limit_from_env(), 40)
            # Garbage / non-positive overrides fall back to the safe default.
            for bad in ("", "nope", "0", "-1"):
                os.environ[gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV] = bad
                self.assertEqual(gmail_integration._gmail_import_limit_from_env(), 20)
            # The module constant was resolved from the env at import time; with the
            # knob unset it is the modest default.
            os.environ.pop(gmail_integration.NDA_GMAIL_IMPORT_LIMIT_ENV, None)
            self.assertEqual(
                gmail_integration.MAX_GMAIL_IMPORT_LIMIT,
                gmail_integration._gmail_import_limit_from_env(),
            )

    def test_triage_matter_still_dedupes_by_hash(self):
        # T5 -- a triage matter goes through the same dedupe-on-create critical
        # section as a confident one: importing the same borderline attachment
        # twice yields a single matter and a duplicate_attachment skip.
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults):
                return FakeExecutable({"messages": [{"id": message_id} for message_id in self.messages][:maxResults]})

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        borderline_docx = make_docx([
            "This document is shared in connection with our discussions.",
            "The Receiving Party will handle the Confidential Information appropriately.",
        ])
        messages = {
            "msg_borderline": {
                "id": "msg_borderline",
                "threadId": "thr_borderline",
                "labelIds": ["INBOX"],
                "snippet": "please take a look",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Ops <ops@example.com>"},
                        {"name": "Subject", "value": "Fwd: a document"},
                    ],
                    "parts": [
                        {"partId": "doc", "filename": "Document.docx", "body": {"attachmentId": "att_borderline", "data": inline(borderline_docx)}},
                    ],
                },
            },
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                            first = gmail_integration.import_inbound_matters(limit=25)
                            second = gmail_integration.import_inbound_matters(limit=25)
                matters = matter_store.list_matters()

        self.assertEqual(len(first["imported"]), 1)
        self.assertEqual(first["imported"][0]["needs_triage"], "true")
        self.assertEqual(second["imported"], [])
        # The second poll short-circuits the message BEFORE any re-download/re-extract.
        # The processed-message ledger fires one step EARLIER than the attachment
        # dedup gate (it marked this fully-imported message on poll 1), so the skip
        # reason is now the message-level "processed_message"; absent the ledger it
        # would be "already_imported". Either short-circuit means the matter is
        # deduped (one matter, nothing re-imported, no re-download).
        self.assertEqual(
            [item["reason"] for item in second["skipped"]],
            ["processed_message"],
        )
        self.assertEqual(len(matters), 1)

    def test_selector_not_selected_attachment_becomes_triage_not_silently_imported(self):
        # T6 -- with the selector configured, a non-selected attachment that still
        # has a content basis becomes a needs_triage matter (reason
        # ai_selector_not_selected), never the old silent ai_not_selected drop;
        # a non-selected attachment with NO content basis is still skipped. The
        # candidate-id intersection injection defense (test_gmail_attachment_selector)
        # is structurally unchanged.
        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        selected_nda = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each Disclosing Party may disclose Confidential Information to a Receiving Party.",
            "The Receiving Party shall not disclose Confidential Information.",
        ])
        unselected_nda = make_docx([
            "Each Disclosing Party may share Confidential Information with the Receiving Party.",
            "The Receiving Party shall keep the Confidential Information confidential.",
        ])
        unselected_collateral = make_docx([
            "Project Proposal Form",
            "Pricing and statement of work (SOW) overview for the engagement.",
        ])
        attachments = [
            {"attachment_id": "att_nda", "data": inline(selected_nda), "filename": "Mutual NDA.docx", "part_id": "1"},
            {"attachment_id": "att_other_nda", "data": inline(unselected_nda), "filename": "Side Agreement.docx", "part_id": "2"},
            {"attachment_id": "att_proposal", "data": inline(unselected_collateral), "filename": "Proposal Form.docx", "part_id": "3"},
        ]
        metadata = {
            "gmail_account": "legal@aspora.com",
            "gmail_message_id": "msg_multi",
            "message_snippet": "please review",
            "sender": "Neha <neha@example.com>",
            "subject": "Fwd: documents",
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                    with patch.object(gmail_integration.gmail_attachment_selector, "selector_configured", return_value=True):
                        with patch.object(
                            gmail_integration.gmail_attachment_selector,
                            "select_nda_attachments",
                            return_value={
                                "status": "selected",
                                # Includes a fabricated id the intersection must drop.
                                "selected_attachment_ids": ["att_nda", "att_injected"],
                                "confidence": 0.9,
                                "reason": "The mutual NDA is the legal review document.",
                                "model": "google/gemini-3.5-flash",
                            },
                        ):
                            result = gmail_integration._import_inbound_attachments(
                                None,
                                "msg_multi",
                                attachments,
                                metadata,
                            )

        imported_by_filename = {item["source_filename"]: item for item in result["imported"]}
        self.assertEqual(
            sorted(imported_by_filename),
            ["Mutual NDA.docx", "Side Agreement.docx"],
        )
        self.assertNotIn("needs_triage", imported_by_filename["Mutual NDA.docx"])
        self.assertEqual(imported_by_filename["Side Agreement.docx"]["needs_triage"], "true")
        self.assertEqual(imported_by_filename["Side Agreement.docx"]["triage_reason"], "ai_selector_not_selected")
        self.assertEqual([item["attachment_filename"] for item in result["skipped"]], ["Proposal Form.docx"])
        self.assertEqual(result["skipped"][0]["reason"], "ai_not_selected_attachment")

    @requires_pypdf
    def test_gmail_import_filters_non_nda_collateral_after_message_match(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages

            def list(self, userId, q, maxResults):
                return FakeExecutable({"messages": [{"id": message_id} for message_id in self.messages][:maxResults]})

            def attachments(self):
                return self

            def get(self, userId=None, messageId=None, id=None, format=None):
                if messageId:
                    message = self.messages["msg_moorwand"]
                    for part in message["payload"]["parts"]:
                        body = part.get("body") or {}
                        if body.get("attachmentId") == id:
                            return FakeExecutable({"data": body["data"]})
                    return FakeExecutable({"data": ""})
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        programme_manager_pdf = make_pdf(
            "Moorwand expectations of a Programme Manager. Brief summary of programme resources and capabilities."
        )
        mutual_nda_pdf = make_pdf(
            "MUTUAL CONFIDENTIALITY AND NON-DISCLOSURE AGREEMENT. "
            "Each Disclosing Party may disclose Confidential Information to a Receiving Party. "
            "The Receiving Party shall not disclose Confidential Information."
        )
        proposal_docx = make_docx([
            "Project Proposal Form",
            "Client Contact Details",
            "Questions Answers Details",
            "Programme/product overview and summary of business plan.",
        ])
        invoice_docx = make_docx([
            "INVOICE",
            "Amount due within 30 days. Total payable on receipt.",
        ])
        body = inline(b"Hi legal, can you please help review and sign the NDA?")
        messages = {
            "msg_moorwand": {
                "id": "msg_moorwand",
                "threadId": "thr_moorwand",
                "labelIds": ["INBOX"],
                "snippet": "Can you please help review and sign the NDA?",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Neha Chawla <neha@example.com>"},
                        {"name": "Subject", "value": "Fwd: Moorwand // Aspora"},
                    ],
                    "parts": [
                        {"partId": "body", "mimeType": "text/plain", "body": {"data": body}},
                        {
                            "partId": "1",
                            "filename": "Moorwand - Programme Manager - Expectations.pdf",
                            "body": {"attachmentId": "att_programme", "data": inline(programme_manager_pdf)},
                        },
                        {
                            "partId": "2",
                            "filename": "Moorwand - Mutual NDA - 2026 v1.0.pdf",
                            "body": {"attachmentId": "att_nda", "data": inline(mutual_nda_pdf)},
                        },
                        {
                            "partId": "3",
                            "filename": "Moorwand Project Proposal Form - 2026.docx",
                            "body": {"attachmentId": "att_proposal", "data": inline(proposal_docx)},
                        },
                        {
                            "partId": "4",
                            "filename": "Moorwand Invoice - 2026.docx",
                            "body": {"attachmentId": "att_invoice", "data": inline(invoice_docx)},
                        },
                    ],
                },
            },
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=FakeGmailService(messages)):
                            result = gmail_integration.import_inbound_matters(limit=25)

        self.assertEqual([item["source_filename"] for item in result["imported"]], ["Moorwand - Mutual NDA - 2026 v1.0.pdf"])
        skipped_by_filename = {item["attachment_filename"]: item["reason"] for item in result["skipped"]}
        self.assertEqual(skipped_by_filename["Moorwand - Programme Manager - Expectations.pdf"], "non_nda_attachment")
        self.assertEqual(skipped_by_filename["Moorwand Project Proposal Form - 2026.docx"], "non_nda_attachment")
        # A pure invoice (score < 40, no content basis, collateral-penalised) is
        # terminally skipped, not triaged.
        self.assertEqual(skipped_by_filename["Moorwand Invoice - 2026.docx"], "non_nda_attachment")
        # Precision intact: the genuine non-NDA collateral is skipped, never
        # imported as a needs_triage matter.
        self.assertNotIn("needs_triage", {item.get("needs_triage") for item in result["imported"]})
        self.assertEqual(len(result["imported"]), 1)
        imported = result["imported"][0]
        self.assertEqual(imported["gmail_detection_sources"], "body, snippet, attachment_filename, attachment_content")
        self.assertIn("NDA", imported["gmail_detection_terms"])
        self.assertIn("non-disclosure agreement", imported["gmail_detection_terms"])

    def test_attachment_nda_validation_accepts_nda_with_business_preamble(self):
        # Regression: a genuine NDA that recites the deal's business context
        # (proposal, SOW, pricing, programme details) must not be vetoed by
        # collateral signals when it carries a strong NDA content basis.
        paragraphs = [
            {"text": "Mutual Non-Disclosure Agreement"},
            {"text": "This Agreement is between the Disclosing Party and the Receiving Party."},
            {"text": (
                "It relates to the project proposal, statement of work (SOW), pricing, "
                "and programme manager expectations for the engagement."
            )},
            {"text": "The Receiving Party shall not disclose the Confidential Information."},
        ]
        validation = gmail_integration._attachment_nda_validation("Agreement.pdf", paragraphs)
        self.assertTrue(validation["accepted"], validation.get("reason"))

    def test_attachment_nda_validation_still_rejects_pure_collateral(self):
        # Guard the other side: a document with only collateral signals and no NDA
        # content basis is still rejected, so the fix does not open false-positives.
        paragraphs = [
            {"text": "Project Proposal Form"},
            {"text": "Programme manager expectations, pricing, statement of work (SOW), and questionnaire."},
        ]
        validation = gmail_integration._attachment_nda_validation("Project Proposal Form.docx", paragraphs)
        self.assertFalse(validation["accepted"], validation.get("reason"))

    def test_gmail_import_uses_qwen_selector_for_multi_attachment_candidates(self):
        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        proposal_docx = make_docx([
            "Project Proposal Form",
            "Confidential Information may be exchanged during the project.",
        ])
        nda_docx = make_docx([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Each Disclosing Party may disclose Confidential Information to a Receiving Party.",
            "The Receiving Party shall not disclose Confidential Information.",
        ])
        attachments = [
            {
                "attachment_id": "att_proposal",
                "data": inline(proposal_docx),
                "filename": "Moorwand Project Proposal Form - 2026.docx",
                "part_id": "1",
            },
            {
                "attachment_id": "att_nda",
                "data": inline(nda_docx),
                "filename": "Moorwand - Mutual NDA - 2026 v1.0.docx",
                "part_id": "2",
            },
        ]
        metadata = {
            "gmail_account": "legal@aspora.com",
            "gmail_message_id": "msg_moorwand",
            "message_snippet": "Can you please help review and sign the NDA?",
            "sender": "Neha <neha@example.com>",
            "subject": "Fwd: Moorwand // Aspora",
        }

        def accepted_validation(filename, paragraphs, *, message_metadata=None):
            return {
                "accepted": True,
                "excerpt": filename,
                "reason": "test accepted candidate",
                "score": 100,
                "sources": ["attachment_filename", "attachment_content"],
                "terms": ["NDA", "confidential information"],
            }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                    with patch.object(gmail_integration, "_attachment_nda_validation", side_effect=accepted_validation):
                        with patch.object(gmail_integration.gmail_attachment_selector, "selector_configured", return_value=True):
                            with patch.object(
                                gmail_integration.gmail_attachment_selector,
                                "select_nda_attachments",
                                return_value={
                                    "status": "selected",
                                    "selected_attachment_ids": ["att_nda"],
                                    "confidence": 0.92,
                                    "reason": "The mutual NDA is the actual legal review document.",
                                    "model": "google/gemini-3.5-flash",
                                },
                            ) as select_nda_attachments:
                                result = gmail_integration._import_inbound_attachments(
                                    None,
                                    "msg_moorwand",
                                    attachments,
                                    metadata,
                                )

        # The selector is now a RANKING authority, not a terminal gate: the
        # selected NDA is imported confidently, while the non-selected proposal --
        # which still carries a deterministic content basis -- is imported as a
        # needs_triage matter (reason ai_selector_not_selected) rather than being
        # silently dropped as ai_not_selected_attachment.
        imported_by_filename = {item["source_filename"]: item for item in result["imported"]}
        self.assertEqual(
            sorted(imported_by_filename),
            ["Moorwand - Mutual NDA - 2026 v1.0.docx", "Moorwand Project Proposal Form - 2026.docx"],
        )
        self.assertEqual(result["skipped"], [])
        nda = imported_by_filename["Moorwand - Mutual NDA - 2026 v1.0.docx"]
        self.assertNotIn("needs_triage", nda)
        self.assertEqual(nda["gmail_attachment_selector"], "openrouter_gemini")
        self.assertEqual(nda["gmail_attachment_selector_model"], "google/gemini-3.5-flash")
        proposal = imported_by_filename["Moorwand Project Proposal Form - 2026.docx"]
        self.assertEqual(proposal["needs_triage"], "true")
        self.assertEqual(proposal["triage_reason"], "ai_selector_not_selected")
        select_nda_attachments.assert_called_once()

    def test_gmail_import_lets_qwen_select_generic_attachment_from_nda_adjacent_email(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeMessages:
            def __init__(self, messages):
                self.messages = messages
                self.query = ""

            def list(self, userId, q, maxResults):
                self.query = q
                return FakeExecutable({"messages": [{"id": message_id} for message_id in self.messages][:maxResults]})

            def get(self, userId=None, messageId=None, id=None, format=None):
                return FakeExecutable(self.messages[id])

        class FakeUsers:
            def __init__(self, messages):
                self.messages_api = FakeMessages(messages)

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "legal@aspora.com"})

            def messages(self):
                return self.messages_api

        class FakeGmailService:
            def __init__(self, messages):
                self.users_api = FakeUsers(messages)

            def users(self):
                return self.users_api

        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        document_bytes = make_docx([
            "Commercial Services Agreement",
            "The parties will discuss launch planning, commercial terms, and rollout milestones.",
            "This document is intentionally generic for the deterministic scorer.",
        ])
        messages = {
            "msg_pismo": {
                "id": "msg_pismo",
                "threadId": "thr_pismo",
                "labelIds": ["INBOX"],
                "snippet": "Could you review this confidentiality agreement for Pismo?",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Pismo Legal <legal@pismo.example>"},
                        {"name": "Subject", "value": "Pismo confidentiality review"},
                    ],
                    "parts": [
                        {
                            "partId": "body",
                            "mimeType": "text/plain",
                            "body": {"data": inline(b"Hi, please review the attached confidentiality agreement for us.")},
                        },
                        {
                            "partId": "1",
                            "filename": "Pismo Agreement.docx",
                            "body": {"data": inline(document_bytes)},
                        },
                    ],
                },
            },
        }
        service = FakeGmailService(messages)

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                with patch.object(app_settings, "gmail_role_enabled", return_value=True):
                    with patch.dict(os.environ, {ACTIVE_REVIEW_ENGINE_ENV: "ai_first", "NDA_AI_REVIEW_ENABLED": "true", "NDA_AI_ASSESSMENT_STUB": "1"}):
                        with patch.object(gmail_integration, "_gmail_service", return_value=service):
                            with patch.object(gmail_integration.gmail_attachment_selector, "selector_configured", return_value=True):
                                with patch.object(
                                    gmail_integration.gmail_attachment_selector,
                                    "select_nda_attachments",
                                    return_value={
                                        "status": "selected",
                                        "selected_attachment_ids": ["inline:1"],
                                        "confidence": 0.88,
                                        "reason": "The email context asks legal to review this attached agreement.",
                                        "model": "google/gemini-3.5-flash",
                                    },
                                ) as select_nda_attachments:
                                    result = gmail_integration.import_inbound_matters(limit=25)

        self.assertEqual(service.users_api.messages_api.query, gmail_integration.DEFAULT_INBOUND_QUERY_WITH_AI_SELECTOR)
        self.assertEqual(result["skipped"], [])
        self.assertEqual([item["source_filename"] for item in result["imported"]], ["Pismo Agreement.docx"])
        imported = result["imported"][0]
        self.assertEqual(imported["gmail_attachment_selector"], "openrouter_gemini")
        self.assertEqual(imported["gmail_attachment_selector_confidence"], "0.88")
        self.assertEqual(imported["gmail_attachment_score"], "10")
        selector_call = select_nda_attachments.call_args.kwargs
        self.assertIn("please review the attached confidentiality agreement", selector_call["message_metadata"]["message_body_preview"])
        self.assertEqual(selector_call["candidates"][0]["validation"]["accepted"], False)

    def test_mark_reviewed_clears_human_review_block_and_resets_on_rereview(self):
        review_result = {
            "clauses": [{"id": "mutuality", "decision": "review"}],
            "requirements_needs_review": 1,
            "review_state": {"state": "review", "requires_human_review": True},
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                created = matter_store.create_matter(
                    source_filename="NDA.docx",
                    document_bytes=make_docx(["Confidential information."]),
                    extracted_text="Confidential information.",
                    review_result=review_result,
                    triage={},
                    source_type="manual_upload",
                    intake_metadata={},
                )
                mid = created["id"]

                before = matter_view.public_matter(matter_store.get_matter(mid))
                self.assertFalse(before["human_reviewed"])
                self.assertEqual(
                    before["send_block_reason"],
                    "NDA needs human review before a redline can be sent.",
                )

                status, payload = self.request("POST", f"/api/matters/{mid}/reviewed", body={"reviewed": True})
                self.assertEqual(status, 200)
                self.assertTrue(payload["matter"]["human_reviewed"])
                self.assertEqual(
                    payload["matter"]["send_block_reason"],
                    "NDA does not have a valid reply recipient email address.",
                )
                matter_store.update_redline_draft(
                    mid,
                    {
                        "redline_decisions": {"old-redline-id": False},
                        "template_selections": {"old-redline-id": "delaware"},
                    },
                )
                self.assertIn("redline_draft", matter_store.get_matter(mid))

                # A fresh review supersedes the human sign-off and any stale redline-ID-keyed draft.
                matter_store.update_matter_review(mid, review_result, {})
                after = matter_view.public_matter(matter_store.get_matter(mid))
                self.assertFalse(after["human_reviewed"])
                self.assertFalse(after["has_redline_draft"])
                self.assertNotIn("redline_draft", matter_store.get_matter(mid))
                self.assertEqual(
                    after["send_block_reason"],
                    "NDA needs human review before a redline can be sent.",
                )

    def test_gmail_message_body_prefers_plain_text_in_multipart_alternative(self):
        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "partId": "plain",
                    "mimeType": "text/plain",
                    "body": {"data": inline(b"Please review the attached NDA.\nPlain-only detail.")},
                },
                {
                    "partId": "html",
                    "mimeType": "text/html",
                    "body": {"data": inline(b"<p>Please review the attached NDA.</p><p>HTML-only duplicate.</p>")},
                },
            ],
        }

        body = gmail_integration._message_body_text(payload)

        self.assertIn("Plain-only detail.", body)
        self.assertNotIn("HTML-only duplicate.", body)
        self.assertEqual(body.count("Please review the attached NDA."), 1)

    def test_gmail_message_body_prefers_plain_text_for_direct_multipart_siblings(self):
        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "partId": "plain",
                    "mimeType": "text/plain",
                    "body": {"data": inline(b"Please review the attached NDA.\nPlain-only detail.")},
                },
                {
                    "partId": "html",
                    "mimeType": "text/html",
                    "body": {"data": inline(b"<p>Please review the attached NDA.</p><p>HTML-only duplicate.</p>")},
                },
                {
                    "filename": "nda.docx",
                    "mimeType": DOCX_MIME,
                    "body": {"attachmentId": "att_1"},
                },
            ],
        }

        body = gmail_integration._message_body_text(payload)

        self.assertIn("Plain-only detail.", body)
        self.assertNotIn("HTML-only duplicate.", body)
        self.assertEqual(body.count("Please review the attached NDA."), 1)

    def test_gmail_message_body_uses_html_when_alternative_has_no_plain_text(self):
        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "partId": "html",
                    "mimeType": "text/html",
                    "body": {"data": inline(b"<html><body><p>Please review the confidentiality agreement.</p></body></html>")},
                },
            ],
        }

        body = gmail_integration._message_body_text(payload)

        self.assertIn("Please review the confidentiality agreement.", body)

    def test_gmail_html_body_ignores_script_and_style_text_for_detection(self):
        def inline(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        payload = {
            "mimeType": "text/html",
            "body": {
                "data": inline(
                    b"""
                    <html>
                      <head>
                        <style>.nda-header { color: purple; } .non-disclosure-agreement { display: block; }</style>
                        <script>const marker = "confidentiality agreement";</script>
                      </head>
                      <body><p>Please review the attached services schedule.</p></body>
                    </html>
                    """
                ),
            },
        }
        message = {
            "snippet": "Please review.",
            "payload": {
                "headers": [{"name": "Subject", "value": "Services schedule"}],
                **payload,
            },
        }

        body = gmail_integration._message_body_text(payload)
        detection = gmail_integration._message_nda_detection(message, [])

        self.assertIn("Please review the attached services schedule.", body)
        self.assertNotIn("nda-header", body)
        self.assertNotIn("non-disclosure-agreement", body)
        self.assertNotIn("confidentiality agreement", body)
        self.assertFalse(detection["matched"])

    def test_gmail_html_body_fallback_ignores_script_and_style_text(self):
        html = """
        <html>
          <head>
            <style>.nda-header { content: "non-disclosure agreement"; }</style>
            <script>const marker = "confidentiality agreement";</script>
          </head>
          <body><p>Please review the attached services schedule.</p></body>
        </html>
        """

        with patch.object(gmail_integration._HTMLTextExtractor, "feed", side_effect=RuntimeError("parser failed")):
            body = gmail_integration._html_to_text(html)

        self.assertIn("Please review the attached services schedule.", body)
        self.assertNotIn("nda-header", body)
        self.assertNotIn("non-disclosure agreement", body)
        self.assertNotIn("confidentiality agreement", body)

    def test_gmail_sync_process_lock_blocks_parallel_processes(self):
        if server_module.fcntl is None:
            self.skipTest("fcntl is unavailable")
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_store.DATA_DIR.mkdir(parents=True, exist_ok=True)
                lock_path = matter_store.DATA_DIR / "gmail_sync.lock"
                holder = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import fcntl, sys, time\n"
                            "lock_file = open(sys.argv[1], 'a+', encoding='utf-8')\n"
                            "fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)\n"
                            "print('locked', flush=True)\n"
                            "time.sleep(10)\n"
                        ),
                        str(lock_path),
                    ],
                    stdout=subprocess.PIPE,
                    text=True,
                )
                try:
                    self.assertEqual(holder.stdout.readline().strip(), "locked")
                    with server_module._gmail_sync_process_lock() as acquired:
                        blocked_acquired = acquired
                finally:
                    holder.terminate()
                    holder.wait(timeout=5)
                    if holder.stdout is not None:
                        holder.stdout.close()
                with server_module._gmail_sync_process_lock() as acquired:
                    free_acquired = acquired

        self.assertEqual(blocked_acquired, False)
        self.assertEqual(free_acquired, True)

    def test_matter_stage_update_persists_workflow_column(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Workflow NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                    },
                )
                matter_id = create_payload["matter"]["id"]
                review_status, review_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/stage",
                    {"board_column": "in_review"},
                )
                list_status, list_payload = self.request("GET", "/api/matters")
                sent_status, sent_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/stage",
                    {"board_column": "sent"},
                )
                reviewed_status, reviewed_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/stage",
                    {"board_column": "reviewed"},
                )
                legacy_status, legacy_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/stage",
                    {"board_column": "redline_ready"},
                )
                invalid_status, invalid_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/stage",
                    {"board_column": "unknown"},
                )
                missing_status, missing_payload = self.request(
                    "POST",
                    "/api/matters/matter_missing/stage",
                    {"board_column": "in_review"},
                )

        self.assertEqual(create_status, 201)
        self.assertEqual(review_status, 200)
        self.assertEqual(review_payload["matter"]["board_column"], "in_review")
        self.assertEqual(review_payload["matter"]["status"], "active")
        self.assertEqual(list_status, 200)
        self.assertEqual(list_payload["matters"][0]["board_column"], "in_review")
        self.assertEqual(sent_status, 200)
        self.assertEqual(sent_payload["matter"]["board_column"], "sent")
        self.assertEqual(sent_payload["matter"]["status"], "active")
        self.assertEqual(reviewed_status, 200)
        self.assertEqual(reviewed_payload["matter"]["board_column"], "reviewed")
        self.assertEqual(reviewed_payload["matter"]["status"], "active")
        self.assertEqual(legacy_status, 400)
        self.assertEqual(legacy_payload["error"], "Unsupported NDA stage.")
        self.assertEqual(invalid_status, 400)
        self.assertEqual(invalid_payload["error"], "Unsupported NDA stage.")
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing_payload["error"], "NDA not found.")

    def test_matter_upload_rejects_gmail_inbound_source(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Forged Gmail NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "gmail_inbound",
                        "gmail_message_id": "msg_123",
                        "gmail_attachment_id": "att_123",
                    },
                )
                matters = matter_store.list_matters()

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Unsupported NDA source.")
        self.assertEqual(matters, [])

    def test_matter_upload_strips_forged_gmail_metadata(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Attached NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "sender": "noreply@example.com",
                        "reply_to": "Legal <legal@example.com>",
                        "subject": "Please review our NDA",
                        "received_at": "2026-05-31T10:15:00+01:00",
                        "message_snippet": "Hi team, please review the attached NDA.",
                        "attachment_filename": "Counterparty NDA.docx",
                        "source_type": "manual_upload",
                        "gmail_account": "inbound@example.com",
                        "gmail_attachment_id": "att_123",
                        "gmail_attachment_sha256": "sha_123",
                        "gmail_message_id": "msg_123",
                        "gmail_part_id": "part_1",
                        "gmail_thread_id": "thr_123",
                    },
                )
                duplicate = matter_store.find_gmail_attachment("msg_123", "att_123")
                stored_matter = matter_store.get_matter(payload["matter"]["id"])

        self.assertEqual(status, 201)
        matter = payload["matter"]
        self.assertEqual(matter["source_type"], "manual_upload")
        self.assertEqual(matter["board_column"], "in_review")
        self.assertEqual(matter["sender"], "noreply@example.com")
        self.assertEqual(matter["reply_to"], "legal@example.com")
        self.assertEqual(matter["recipient_email"], "legal@example.com")
        # Upload now defers the AI review, so no review verdict has flagged this
        # matter for human review yet -- the needs-human-review send block is no
        # longer asserted at create (it re-applies once the operator runs the
        # on-demand review). The recipient resolves cleanly, which is what this
        # gmail-metadata-stripping test cares about.
        self.assertEqual(matter["ai_review_ran"], False)
        self.assertEqual(matter["subject"], "Please review our NDA")
        self.assertEqual(matter["received_at"], "2026-05-31T10:15:00+01:00")
        self.assertEqual(matter["message_snippet"], "Hi team, please review the attached NDA.")
        self.assertEqual(matter["attachment_filename"], "Counterparty NDA.docx")
        for field in (
            "gmail_account",
            "gmail_attachment_id",
            "gmail_attachment_sha256",
            "gmail_message_id",
            "gmail_part_id",
            "gmail_thread_id",
        ):
            self.assertNotIn(field, matter)
            self.assertNotIn(field, stored_matter)
        self.assertEqual(stored_matter["reply_to"], "legal@example.com")
        self.assertIsNone(duplicate)

    def test_matter_upload_rejects_spoofed_sender_for_redline_recipient(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Spoof NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "sender": '"jane@x.com" <attacker@evil.com>',
                        "subject": "Please review our NDA",
                    },
                )

        self.assertEqual(status, 201)
        matter = payload["matter"]
        self.assertEqual(matter["sender"], "Manual upload")
        self.assertEqual(matter["recipient_email"], "")
        self.assertEqual(matter["can_send_redline"], False)

    def test_matter_upload_rejects_invalid_upload_cleanly(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "nda.txt",
                        "content_base64": base64.b64encode(b"not docx").decode("ascii"),
                    },
                )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Upload a .docx Word document or text-based PDF.")

    def test_matter_upload_rejects_unsupported_source(self):
        source_docx = make_docx(["This Agreement shall be governed by the laws of California."])

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Acme NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                        "source_type": "unknown_board",
                    },
                )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Unsupported NDA source.")

    def test_matter_export_uses_preserved_original_docx(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                matter_id = self.seed_reviewed_upload(
                    source_docx, filename="Acme NDA.docx"
                )["id"]
                export_status, export_payload, export_headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {
                        "matter_id": matter_id,
                    },
                )

        self.assertEqual(export_status, 200)
        self.assertEqual(export_headers["Content-Disposition"], 'attachment; filename="Acme-NDA-redlined.docx"')
        assert_source_export_has_no_report_leakage(
            self,
            export_payload,
        )
        with ZipFile(BytesIO(export_payload)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("California", document_xml)

    def test_matter_export_rejects_reviewed_text_only_source_text_change_without_manual_redlines(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                seeded_matter = self.seed_reviewed_upload(
                    source_docx, filename="Reviewed Text Only NDA.docx"
                )
                export_status, export_payload = self.request(
                    "POST",
                    "/api/export-review-docx",
                    {
                        "matter_id": seeded_matter["id"],
                        "reviewed_text": "This Agreement shall be governed by the laws of New York.",
                        "export_redline_edits": [],
                        "manual_redline_edits": [],
                    },
                )

        self.assertEqual(export_status, 409)
        self.assertEqual(
            export_payload["error"],
            "NDA source text was edited after the source document was ingested. "
            "Export or send after those viewer edits are represented as manual redlines.",
        )

    def test_matter_export_rejects_text_that_differs_from_reviewed_text(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                create_status, create_payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "Acme NDA.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                    },
                )
                matter_id = create_payload["matter"]["id"]
                export_status, export_payload = self.request(
                    "POST",
                    "/api/export-review-docx",
                    {
                        "matter_id": matter_id,
                        "text": "This Agreement shall be governed by the laws of England and Wales.",
                        "reviewed_text": "This Agreement shall be governed by the laws of California.",
                    },
                )

        self.assertEqual(create_status, 201)
        self.assertEqual(export_status, 409)
        self.assertEqual(export_payload["error"], "Export text must match the latest reviewed text. Reload the NDA review before exporting.")

    def test_matter_export_rejects_source_text_change_without_manual_redlines(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                seeded_matter = self.seed_reviewed_upload(
                    source_docx, filename="Edited Source NDA.docx"
                )
                export_status, export_payload = self.request(
                    "POST",
                    "/api/export-review-docx",
                    {
                        "matter_id": seeded_matter["id"],
                        "text": "This Agreement shall be governed by the laws of New York.",
                        "reviewed_text": "This Agreement shall be governed by the laws of New York.",
                        "export_redline_edits": [],
                        "manual_redline_edits": [],
                    },
                )

        self.assertEqual(export_status, 409)
        self.assertEqual(
            export_payload["error"],
            "NDA source text was edited after the source document was ingested. "
            "Export or send after those viewer edits are represented as manual redlines.",
        )

    def test_matter_export_allows_source_text_change_with_manual_redline(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])
        manual_redline = {
            "id": "manual-p1",
            "action": "replace_paragraph",
            "paragraph_id": "p1",
            "paragraph_index": 1,
            "source_index": 1,
            "original_text": "This Agreement shall be governed by the laws of California.",
            "replacement_text": "This Agreement shall be governed by the laws of New York.",
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                seeded_matter = self.seed_reviewed_upload(
                    source_docx, filename="Manual Source NDA.docx"
                )
                export_status, export_payload, _headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {
                        "matter_id": seeded_matter["id"],
                        "text": "This Agreement shall be governed by the laws of New York.",
                        "reviewed_text": "This Agreement shall be governed by the laws of New York.",
                        "export_redline_edits": [],
                        "manual_redline_edits": [manual_redline],
                    },
                )

        self.assertEqual(export_status, 200)
        with ZipFile(BytesIO(export_payload)) as archive:
            document_root = ET.fromstring(archive.read("word/document.xml"))
        revision_states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in document_root.findall(".//w:p", W_NS)
        ]
        self.assertIn(
            (
                "This Agreement shall be governed by the laws of California.",
                "This Agreement shall be governed by the laws of New York.",
            ),
            revision_states,
        )

    def test_matter_export_rechecks_edited_source_text_with_manual_redline(self):
        source_docx = make_docx([
            "The confidentiality obligations survive for seven (7) years.",
        ])
        edited_text = "The confidentiality obligations survive for five (5) years."
        manual_redline = {
            "id": "manual-term",
            "action": "replace_paragraph",
            "paragraph_id": "p1",
            "paragraph_index": 1,
            "source_index": 1,
            "original_text": "The confidentiality obligations survive for seven (7) years.",
            "replacement_text": edited_text,
        }
        captured = {}

        def capture_redline_build(source_bytes, review_result, **_kwargs):
            captured["source_bytes"] = source_bytes
            captured["paragraph_texts"] = [
                paragraph.get("text")
                for paragraph in review_result.get("paragraphs", [])
                if isinstance(paragraph, dict)
            ]
            return source_docx

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                seeded_matter = self.seed_reviewed_upload(
                    source_docx, filename="Edited Review Source NDA.docx"
                )
                with patch.object(server_module.redline_export_service.docx_package_renderer, "build_source_redline_docx", side_effect=capture_redline_build):
                    with patch.object(server_module.redline_export_service.docx_package_renderer, "validate_docx_open_health", return_value=[]):
                        with patch.object(server_module.redline_export_service.docx_package_renderer, "verify_export_content_coverage", return_value=[]):
                            export_status, _export_payload, _headers = self.request_with_headers(
                                "POST",
                                "/api/export-review-docx",
                                {
                                    "matter_id": seeded_matter["id"],
                                    "text": edited_text,
                                    "reviewed_text": edited_text,
                                    "export_redline_edits": [],
                                    "manual_redline_edits": [manual_redline],
                                },
                            )

        self.assertEqual(export_status, 200)
        self.assertEqual(captured["source_bytes"], source_docx)
        self.assertEqual(captured["paragraph_texts"], [edited_text])

    @requires_pypdf
    def test_pdf_matter_export_reports_unavailable_reconstruction_and_preserves_pdf_source(self):
        class UnavailablePdfDocxConverter:
            name = "fake-unavailable"

            def is_available(self):
                return False

        source_pdf = make_pdf("This Agreement shall be governed by the laws of California.")

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                seeded_matter = self.seed_reviewed_upload(
                    source_pdf,
                    filename="Acme NDA.pdf",
                )
                # Inject one reviewer redline so the export attempts the PDF
                # reconstruction (the zero-redline case now serves the original PDF
                # unchanged and never reaches the unavailable engine).
                seeded_review = dict(seeded_matter["review_result"])
                seeded_review["redline_edits"] = [
                    {
                        "id": "r1",
                        "clause_id": "governing_law",
                        "paragraph_id": "p1",
                        "action": "replace_paragraph",
                        "original_text": "This Agreement shall be governed by the laws of California.",
                        "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
                    }
                ]
                matter_store.update_matter_review(
                    seeded_matter["id"],
                    seeded_review,
                    {"triage_status": "needs_review"},
                )
                matter = matter_view.public_matter(seeded_matter)
                stored_matter = matter_store.get_matter(matter["id"])
                with patch.object(
                    server_module.redline_export_service.pdf_docx_reconstruction,
                    "Pdf2DocxConverter",
                    return_value=UnavailablePdfDocxConverter(),
                ):
                    export_status, export_payload, export_headers = self.request_with_headers(
                        "POST",
                        "/api/export-review-docx",
                        {"matter_id": matter["id"]},
                    )
                render_status, render_payload = self.request("GET", f"/api/matters/{matter['id']}/render-status")
                pdf_status, pdf_payload, pdf_headers = self.request_with_headers(
                    "GET",
                    f"/api/matters/{matter['id']}/render-pdf",
                )

        self.assertEqual(matter["source_filename"], "Acme NDA.pdf")
        self.assertNotIn("review_result", matter)
        self.assertEqual(stored_matter["review_result"]["source"]["type"], "pdf")
        self.assertEqual(export_status, 503)
        self.assertIn("pdf2docx", export_payload["error"])
        self.assertNotIn("Content-Disposition", export_headers)
        self.assertEqual(render_status, 200)
        self.assertEqual(render_payload["document_render"]["source_kind"], "pdf")
        self.assertEqual(pdf_status, 200)
        self.assertEqual(pdf_headers["Content-Type"], "application/pdf")
        self.assertEqual(pdf_payload, source_pdf)

    def test_matter_export_fails_when_source_docx_is_missing(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                seeded_matter = self.seed_reviewed_upload(
                    source_docx, filename="Acme NDA.docx"
                )
                matter = matter_view.public_matter(seeded_matter)
                stored_matter = matter_store.get_matter(matter["id"])
                (matter_store.UPLOADS_DIR / stored_matter["stored_filename"]).unlink()
                export_status, export_payload = self.request(
                    "POST",
                    "/api/export-review-docx",
                    {"matter_id": matter["id"]},
                )

        self.assertEqual(export_status, 400)
        self.assertEqual(export_payload["error"], "NDA source document is missing from storage.")

    def test_matter_redline_draft_save_and_reset_updates_public_matter(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])
        manual_redline = {
            "id": "manual-p1",
            "action": "replace_paragraph",
            "paragraph_id": "p1",
            "paragraph_index": 1,
            "source_index": 1,
            "original_text": "This Agreement shall be governed by the laws of California.",
            "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_id = self.seed_reviewed_upload(
                    source_docx, filename="Draft NDA.docx"
                )["id"]
                save_status, save_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/redline-draft",
                    {
                        "redline_draft": {
                            "clause_decisions": {"governing_law": False},
                            "template_selections": {"redline-governing_law-1": "england_and_wales"},
                            "export_redline_edits": [],
                            "manual_redline_edits": [manual_redline],
                            "review_comments": [{
                                "clause_id": "governing_law",
                                "clause_name": "Governing Law",
                                "paragraph_id": "p1",
                                "scope": "selection",
                                "selected_text": "California",
                                "selection_start": 53,
                                "selection_end": 63,
                                "text": "Confirm fallback position.",
                            }],
                        },
                    },
                )
                stored_after_save = matter_store.get_matter(matter_id)
                review_status, review_payload = self.request("GET", f"/api/matters/{matter_id}/review")
                reset_status, reset_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/redline-draft",
                    {"redline_draft": None},
                )
                stored_after_reset = matter_store.get_matter(matter_id)

        self.assertEqual(save_status, 200)
        self.assertNotIn("redline_draft", save_payload["matter"])
        self.assertEqual(save_payload["matter"]["has_redline_draft"], True)
        self.assertEqual(review_status, 200)
        saved_draft = review_payload["redline_draft"]
        self.assertEqual(saved_draft["clause_decisions"], {"governing_law": False})
        self.assertEqual(saved_draft["template_selections"], {"redline-governing_law-1": "england_and_wales"})
        self.assertEqual(saved_draft["summary"]["included_redline_count"], 0)
        self.assertEqual(saved_draft["summary"]["manual_redline_count"], 1)
        self.assertEqual(saved_draft["summary"]["review_comment_count"], 1)
        self.assertIn("saved_at", saved_draft)
        self.assertEqual(stored_after_save["redline_draft"]["manual_redline_edits"][0]["paragraph_id"], "p1")
        self.assertEqual(stored_after_save["redline_draft"]["review_comments"][0]["text"], "Confirm fallback position.")
        self.assertEqual(stored_after_save["redline_draft"]["review_comments"][0]["selected_text"], "California")
        self.assertEqual(reset_status, 200)
        self.assertNotIn("redline_draft", reset_payload["matter"])
        self.assertEqual(reset_payload["matter"]["has_redline_draft"], False)
        self.assertNotIn("redline_draft", stored_after_reset)

    def test_matter_export_and_send_use_saved_redline_draft_without_payload_decisions(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])
        captured_redline_counts = []

        def capture_redline_build(_source_bytes, review_result, **_kwargs):
            captured_redline_counts.append(len(review_result.get("redline_edits") or []))
            return source_docx

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                seeded_matter = self.seed_reviewed_upload(
                    source_docx,
                    filename="Saved Draft NDA.docx",
                    intake_metadata={
                        "sender": "legal@example.com",
                        "reply_to": "legal@example.com",
                    },
                )
                matter = matter_view.public_matter(seeded_matter)
                stored_matter = matter_store.get_matter(matter["id"])
                self.assertGreater(len(stored_matter["review_result"].get("redline_edits") or []), 0)
                draft_status, _draft_payload = self.request(
                    "POST",
                    f"/api/matters/{matter['id']}/redline-draft",
                    {
                        "redline_draft": {
                            "clause_decisions": {"governing_law": False, "non_circumvention": False},
                            "template_selections": {},
                            "export_redline_edits": [],
                            "manual_redline_edits": [],
                        },
                    },
                )
                reviewed_status, _reviewed_payload = self.request(
                    "POST",
                    f"/api/matters/{matter['id']}/reviewed",
                    body={"reviewed": True},
                )
                with patch.object(server_module.redline_export_service.docx_package_renderer, "build_source_redline_docx", side_effect=capture_redline_build):
                    with patch.object(server_module.redline_export_service.docx_package_renderer, "validate_docx_open_health", return_value=[]):
                        with patch.object(server_module.gmail_integration, "validate_outbound_send_ready", return_value={}):
                            with patch.object(server_module.gmail_integration, "send_redline_email", return_value={
                                "message_id": "msg_outbound",
                                "outbound_account": "legal@aspora.com",
                                "sent_at": "2026-05-31T12:00:00+00:00",
                                "subject": "Re: Saved Draft NDA",
                                "thread_id": "thread_outbound",
                                "to": "legal@example.com",
                            }):
                                export_status, _export_payload = self.request(
                                    "POST",
                                    "/api/export-review-docx",
                                    {"matter_id": matter["id"]},
                                )
                                send_status, _send_payload = self.request(
                                    "POST",
                                    "/api/gmail/send-redline",
                                    {
                                        "matter_id": matter["id"],
                                        "confirm_send": True,
                                        "confirm_recipient": "legal@example.com",
                                    },
                                )

        self.assertEqual(draft_status, 200)
        self.assertEqual(reviewed_status, 200)
        self.assertEqual(export_status, 200)
        self.assertEqual(send_status, 200)
        self.assertEqual(captured_redline_counts, [0, 0])

    def test_repository_export_and_send_share_matter_redline_builder(self):
        matter_id = "matter_shared"
        matter = {
            "id": matter_id,
            "sender": "Legal Team <legal@example.com>",
            "subject": "Shared matter",
        }
        redline_export = server_module.redline_export_service.RedlineExport(
            data=b"shared redline docx",
            filename="Shared-redlined.docx",
        )

        with patch.object(server_module.redline_export_service, "build_matter_redline", return_value=redline_export) as build_matter_redline:
            with patch.object(server_module.matter_store, "get_matter", return_value=matter):
                with patch.object(server_module.matter_store, "update_matter_fields", return_value=matter):
                    with patch.object(server_module.gmail_integration, "validate_outbound_send_ready", return_value={}):
                        with patch.object(server_module.gmail_integration, "send_redline_email", return_value={
                            "message_id": "msg_outbound",
                            "outbound_account": "legal@aspora.com",
                            "sent_at": "2026-05-31T12:00:00+00:00",
                            "subject": "Re: Shared matter",
                            "thread_id": "thread_outbound",
                            "to": "legal@example.com",
                        }):
                            export_status, export_payload, export_headers = self.request_with_headers(
                                "POST",
                                "/api/export-review-docx",
                                {"matter_id": matter_id, "export_redline_edits": []},
                            )
                            send_status, send_payload = self.request(
                                "POST",
                                "/api/gmail/send-redline",
                                {
                                    "matter_id": matter_id,
                                    "confirm_send": True,
                                    "confirm_recipient": "legal@example.com",
                                },
                            )

        self.assertEqual(export_status, 200)
        self.assertEqual(export_payload, b"shared redline docx")
        self.assertEqual(export_headers["Content-Disposition"], 'attachment; filename="Shared-redlined.docx"')
        self.assertEqual(send_status, 200)
        self.assertEqual(send_payload["filename"], "Shared-redlined.docx")
        self.assertEqual(build_matter_redline.call_count, 2)
        self.assertEqual(build_matter_redline.call_args_list[0].args[0], matter_id)
        self.assertTrue(build_matter_redline.call_args_list[0].kwargs["persist"])
        self.assertEqual(build_matter_redline.call_args_list[1].args[0], matter_id)
        self.assertNotIn("persist", build_matter_redline.call_args_list[1].kwargs)

    def test_gmail_import_endpoint_rejects_manual_sync(self):
        with patch.object(server_module.gmail_integration, "import_inbound_matters", return_value={
            "account": "inbound@example.com",
            "imported": [{"id": "matter_1"}],
            "query": "has:attachment",
            "skipped": [{"message_id": "m1", "reason": "no_reviewable_attachment"}],
        }) as import_inbound_matters:
            status, payload = self.request(
                "POST",
                "/api/gmail/import",
                {"limit": 2, "query": "has:attachment"},
            )

        self.assertEqual(status, 410)
        self.assertEqual(payload["error"], "Manual Gmail sync is disabled. Use Admin sync frequency.")
        import_inbound_matters.assert_not_called()

    def test_gmail_import_endpoint_runs_user_scoped_sync_for_google_user(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
        }
        result = {
            "account": "alice@example.com",
            "imported": [{"id": "matter_1"}],
            "query": "has:attachment",
            "skipped": [{"message_id": "m1", "reason": "no_reviewable_attachment"}],
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.dict(os.environ, auth_env):
                session_headers, user = self.google_session_headers()
                with patch.object(
                    server_module.gmail_integration,
                    "import_inbound_matters",
                    return_value=result,
                ) as import_inbound_matters:
                    with patch.object(
                        server_module.matter_store,
                        "deduplicate_gmail_matters",
                        return_value=1,
                    ) as deduplicate:
                        with patch.object(server_module.app_settings, "record_gmail_sync") as record_sync:
                            status, payload = self.request(
                                "POST",
                                "/api/gmail/import",
                                {"limit": 2, "query": "has:attachment"},
                                headers=session_headers,
                            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], {**result, "deduplicated_count": 1})
        self.assertEqual(payload["gmail"]["sync"]["last_sync_imported_count"], 1)
        self.assertEqual(payload["gmail"]["sync"]["last_sync_skipped_count"], 1)
        self.assertEqual(payload["gmail"]["sync"]["sync_history"][0]["query"], "has:attachment")
        self.assertEqual(payload["gmail"]["sync"]["sync_history"][0]["deduplicated_count"], 1)
        import_inbound_matters.assert_called_once_with(
            limit=2,
            query="has:attachment",
            owner_user_id=user["id"],
        )
        deduplicate.assert_called_once_with(owner_user_id=user["id"])
        record_sync.assert_called_once()

    def test_gmail_send_redline_preflights_outbound_before_building_attachment(self):
        matter = {
            "id": "matter_mismatch",
            "gmail_account": "daniyal.ahmad@aspora.com",
            "sender": "Legal <legal@example.com>",
            "subject": "NDA",
        }
        with patch.object(server_module.matter_store, "get_matter", return_value=matter):
            with patch.object(server_module.app_settings, "gmail_role_enabled", return_value=True):
                with patch.object(
                    server_module.gmail_integration,
                    "validate_outbound_send_ready",
                    side_effect=server_module.gmail_integration.GmailIntegrationError("Outbound Gmail account mismatch."),
                ) as validate_ready:
                    with patch.object(server_module.redline_export_service, "build_matter_redline") as build_matter_redline:
                        status, payload = self.request(
                            "POST",
                            "/api/gmail/send-redline",
                            {
                                "matter_id": "matter_mismatch",
                                "confirm_send": True,
                                "confirm_recipient": "legal@example.com",
                            },
                        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "Outbound Gmail account mismatch.")
        validate_ready.assert_called_once_with(matter, to=None, confirmed_recipient="legal@example.com")
        build_matter_redline.assert_not_called()

    def test_gmail_send_error_status_does_not_treat_generic_mismatch_as_conflict(self):
        self.assertEqual(
            server_module.gmail_routes.gmail_send_error_status(
                server_module.gmail_integration.GmailIntegrationError("Attachment checksum mismatch."),
            ),
            503,
        )
        self.assertEqual(
            server_module.gmail_routes.gmail_send_error_status(
                server_module.gmail_integration.GmailIntegrationError("Outbound Gmail account mismatch."),
            ),
            409,
        )

    def test_gmail_send_redline_rejects_missing_reply_recipient_as_bad_request(self):
        matter = {
            "id": "matter_no_reply",
            "sender": "Manual upload",
            "subject": "NDA",
        }
        with patch.object(server_module.matter_store, "get_matter", return_value=matter):
            with patch.object(server_module.gmail_integration, "validate_outbound_send_ready") as validate_ready:
                with patch.object(server_module.redline_export_service, "build_matter_redline") as build_matter_redline:
                    status, payload = self.request(
                        "POST",
                        "/api/gmail/send-redline",
                        {"matter_id": "matter_no_reply", "confirm_send": True},
                    )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "NDA does not have a valid reply recipient email address.")
        validate_ready.assert_not_called()
        build_matter_redline.assert_not_called()

    def test_gmail_send_redline_accepts_manual_recipient_for_missing_reply(self):
        matter = {
            "id": "matter_manual_reply",
            "human_reviewed": True,
            "sender": "Manual upload",
            "subject": "NDA",
            "review_result": {
                "overall_status": "needs_redline",
                "requirements_needs_review": 0,
            },
        }
        redline_export = server_module.redline_export_service.RedlineExport(
            data=b"redline docx",
            filename="NDA-redlined.docx",
        )
        sent = {
            "message_id": "msg_outbound",
            "outbound_account": "outbound@example.com",
            "sent_at": "2026-06-04T09:30:00+00:00",
            "subject": "Redline for NDA",
            "thread_id": "thread_outbound",
            "to": "counterparty@example.com",
        }
        updated_matter = {
            **matter,
            "board_column": "sent",
            "last_outbound_to": "counterparty@example.com",
        }

        with patch.object(server_module.matter_store, "get_matter", return_value=matter):
            with patch.object(server_module.app_settings, "gmail_role_enabled", return_value=True):
                with patch.object(server_module.gmail_integration, "validate_outbound_send_ready", return_value={}) as validate_ready:
                    with patch.object(server_module.redline_export_service, "build_matter_redline", return_value=redline_export) as build_matter_redline:
                        with patch.object(server_module.gmail_integration, "send_redline_email", return_value=sent) as send_redline_email:
                            with patch.object(server_module.matter_store, "update_matter_fields", return_value=updated_matter) as update_matter_fields:
                                status, payload = self.request(
                                    "POST",
                                    "/api/gmail/send-redline",
                                    {
                                        "matter_id": "matter_manual_reply",
                                        "confirm_send": True,
                                        "confirm_recipient": "counterparty@example.com",
                                        "to": "Counterparty <counterparty@example.com>",
                                    },
                                )

        self.assertEqual(status, 200)
        self.assertEqual(payload["sent"]["to"], "counterparty@example.com")
        build_matter_redline.assert_called_once()
        validate_ready.assert_called_once_with(
            matter,
            to="counterparty@example.com",
            confirmed_recipient="counterparty@example.com",
        )
        send_redline_email.assert_called_once()
        self.assertEqual(send_redline_email.call_args.kwargs["to"], "counterparty@example.com")
        self.assertEqual(send_redline_email.call_args.kwargs["confirmed_recipient"], "counterparty@example.com")
        update_matter_fields.assert_called_once()
        self.assertEqual(update_matter_fields.call_args.args[1]["last_outbound_to"], "counterparty@example.com")

    def test_outbound_context_refuses_when_confirmation_does_not_match_spoofed_reply_to(self):
        # A spoofed Reply-To must not be able to silently redirect the redline.
        # The verified sender is the real counterparty; the attacker controls the
        # Reply-To header. If the operator confirms the verified sender but the
        # matter silently resolves to the spoofed Reply-To, the send is refused
        # rather than quietly exfiltrating the document to the attacker.
        matter = {
            "id": "matter_spoof",
            "sender": "Counterparty <legit@realco.com>",
            "reply_to": "Counsel <attacker@evil.com>",
            "subject": "NDA",
        }
        with patch.object(gmail_integration, "_gmail_service_for_owner") as gmail_service_for_owner:
            with patch.object(gmail_integration, "_gmail_profile_for_role") as gmail_profile_for_role:
                with self.assertRaises(gmail_integration.RecipientConfirmationError):
                    gmail_integration._outbound_send_context(
                        matter,
                        confirmed_recipient="legit@realco.com",
                    )
        # The guard fires before we ever build a Gmail service / send anything.
        gmail_service_for_owner.assert_not_called()
        gmail_profile_for_role.assert_not_called()

    def test_outbound_context_allows_send_when_confirmation_matches_resolved_recipient(self):
        matter = {
            "id": "matter_ok",
            "sender": "Counterparty <legit@realco.com>",
            "subject": "NDA",
        }
        with patch.object(server_module.app_settings, "gmail_role_enabled", return_value=True):
            with patch.object(gmail_integration, "_gmail_service_for_owner", return_value=object()):
                with patch.object(
                    gmail_integration,
                    "_gmail_profile_for_role",
                    return_value={"emailAddress": "legal@aspora.com"},
                ):
                    recipient, _service, outbound_account = gmail_integration._outbound_send_context(
                        matter,
                        confirmed_recipient="legit@realco.com",
                    )
        self.assertEqual(recipient, "legit@realco.com")
        self.assertEqual(outbound_account, "legal@aspora.com")

    def test_public_matter_warns_when_recipient_came_from_diverging_reply_to(self):
        public = matter_view.public_matter({
            "id": "matter_warn",
            "sender": "Counterparty <legit@realco.com>",
            "reply_to": "Counsel <attacker@evil.com>",
            "subject": "NDA",
        })

        # The resolved recipient still defaults to the (attacker-controlled)
        # Reply-To for the no-reply use case, but the divergence from the verified
        # sender is surfaced so the operator confirms the destination deliberately.
        self.assertEqual(public["recipient_email"], "attacker@evil.com")
        self.assertTrue(public["recipient_redirected_from_reply_to"])
        self.assertIn("attacker@evil.com", public["recipient_warning"])
        self.assertIn("legit@realco.com", public["recipient_warning"])

    def test_public_matter_does_not_warn_when_reply_to_matches_sender(self):
        public = matter_view.public_matter({
            "id": "matter_no_warn",
            "sender": "Counterparty <legit@realco.com>",
            "reply_to": "Counterparty Desk <legit@realco.com>",
            "subject": "NDA",
        })

        self.assertEqual(public["recipient_email"], "legit@realco.com")
        self.assertNotIn("recipient_redirected_from_reply_to", public)
        self.assertNotIn("recipient_warning", public)

    def test_public_matter_warns_when_reply_to_present_but_sender_unparseable(self):
        # Fail toward warning: a spoofed Reply-To paired with a malformed/absent
        # From must still surface the redirect warning -- we cannot confirm the
        # Reply-To matches a verified sender, so we treat it as a divergence.
        public = matter_view.public_matter({
            "id": "matter_malformed_from",
            "sender": "not a parseable address",
            "reply_to": "Counsel <attacker@evil.com>",
            "subject": "NDA",
        })

        self.assertEqual(public["recipient_email"], "attacker@evil.com")
        self.assertTrue(public["recipient_redirected_from_reply_to"])
        self.assertIn("attacker@evil.com", public["recipient_warning"])
        self.assertIn("unverified sender", public["recipient_warning"])

    def test_send_redline_route_refuses_spoofed_reply_to_without_matching_confirmation(self):
        # End-to-end: a matter whose recipient silently resolves to a spoofed
        # Reply-To must not send the redline to that address when the operator
        # confirms the verified sender instead. No outbound send is attempted.
        matter = {
            "id": "matter_spoof_route",
            "human_reviewed": True,
            "sender": "Counterparty <legit@realco.com>",
            "reply_to": "Counsel <attacker@evil.com>",
            "subject": "NDA",
            "review_result": {
                "overall_status": "needs_redline",
                "requirements_needs_review": 0,
            },
        }
        with patch.object(server_module.matter_store, "get_matter", return_value=matter):
            with patch.object(server_module.app_settings, "gmail_role_enabled", return_value=True):
                with patch.object(server_module.gmail_integration, "send_redline_email") as send_redline_email:
                    with patch.object(server_module.redline_export_service, "build_matter_redline") as build_matter_redline:
                        status, payload = self.request(
                            "POST",
                            "/api/gmail/send-redline",
                            {
                                "matter_id": "matter_spoof_route",
                                "confirm_send": True,
                                "confirm_recipient": "legit@realco.com",
                            },
                        )

        self.assertEqual(status, 400)
        self.assertIn("attacker@evil.com", payload["error"])
        send_redline_email.assert_not_called()
        build_matter_redline.assert_not_called()

    def test_send_redline_route_rejects_missing_recipient_confirmation(self):
        matter = {
            "id": "matter_unconfirmed",
            "human_reviewed": True,
            "sender": "Counterparty <legit@realco.com>",
            "subject": "NDA",
            "review_result": {
                "overall_status": "needs_redline",
                "requirements_needs_review": 0,
            },
        }
        with patch.object(server_module.matter_store, "get_matter", return_value=matter):
            with patch.object(server_module.gmail_integration, "send_redline_email") as send_redline_email:
                with patch.object(server_module.redline_export_service, "build_matter_redline") as build_matter_redline:
                    status, payload = self.request(
                        "POST",
                        "/api/gmail/send-redline",
                        {"matter_id": "matter_unconfirmed", "confirm_send": True},
                    )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Confirm the outbound recipient email address before sending.")
        send_redline_email.assert_not_called()
        build_matter_redline.assert_not_called()

    def test_outbound_context_rejects_none_confirmation_for_header_derived_recipient(self):
        # The confirmed_recipient=None opt-out is reserved for trusted,
        # operator-supplied recipients. A future caller must NOT be able to
        # inherit it for a recipient resolved from inbound headers: with no
        # operator `to`, the None opt-out is refused before any Gmail call.
        matter = {
            "id": "matter_header_recipient",
            "sender": "Counterparty <legit@realco.com>",
            "subject": "NDA",
        }
        with patch.object(gmail_integration, "_gmail_service_for_owner") as gmail_service_for_owner:
            with self.assertRaises(gmail_integration.RecipientConfirmationError):
                gmail_integration._outbound_send_context(matter)
        gmail_service_for_owner.assert_not_called()

    def test_outbound_context_allows_none_confirmation_for_operator_supplied_recipient(self):
        # Send Document supplies an operator-typed recipient via `to`; that is a
        # trusted source, so the None opt-out is allowed and the send proceeds.
        matter = {"id": "matter_operator_to", "subject": "NDA"}
        with patch.object(server_module.app_settings, "gmail_role_enabled", return_value=True):
            with patch.object(gmail_integration, "_gmail_service_for_owner", return_value=object()):
                with patch.object(
                    gmail_integration,
                    "_gmail_profile_for_role",
                    return_value={"emailAddress": "legal@aspora.com"},
                ):
                    recipient, _service, outbound_account = gmail_integration._outbound_send_context(
                        matter,
                        recipient_override="Counterparty <legit@realco.com>",
                    )
        self.assertEqual(recipient, "legit@realco.com")
        self.assertEqual(outbound_account, "legal@aspora.com")

    def test_gmail_token_write_is_atomic_and_preserves_existing_token_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as token_dir:
            token_path = server_module.Path(token_dir) / "token.json"
            token_path.write_text('{"token": "old"}', encoding="utf-8")
            temporary_path = token_path.parent / ".token.json.tmp"
            lock_path = token_path.parent / ".token.json.lock"

            gmail_integration._write_token_atomically(token_path, '{"token": "new"}')
            saved = token_path.read_text(encoding="utf-8")

            with patch.object(gmail_integration.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(gmail_integration.GmailIntegrationError, "token could not be saved"):
                    gmail_integration._write_token_atomically(token_path, '{"token": "corrupt"}')

            self.assertEqual(saved, '{"token": "new"}')
            self.assertEqual(token_path.read_text(encoding="utf-8"), '{"token": "new"}')
            self.assertFalse(temporary_path.exists())
            self.assertTrue(lock_path.exists())

    def test_user_gmail_token_path_rejects_dot_segments(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with patch.object(matter_store, "DATA_DIR", server_module.Path(data_dir)):
                for owner_user_id in ("", ".", "..", "./", "../"):
                    with self.subTest(owner_user_id=owner_user_id):
                        with self.assertRaisesRegex(google_connection.GoogleConnectionError, "valid signed-in user"):
                            google_connection.user_token_path_for_role(
                                "inbound",
                                owner_user_id,
                                integration_label="Gmail",
                            )

                token_path = google_connection.user_token_path_for_role(
                    "inbound",
                    "google:123",
                    integration_label="Gmail",
                )

        self.assertEqual(token_path.name, "inbound-token.json")
        self.assertIn("google:123", token_path.parts)

    def test_gmail_send_payload_replies_in_thread_for_same_account(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeUsers:
            def __init__(self, email):
                self.email = email
                self.sent_body = None

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": self.email})

            def messages(self):
                return self

            def send(self, userId, body):
                self.sent_body = body
                return FakeExecutable({"id": "sent_1", "threadId": body.get("threadId", "new_thread")})

        class FakeGmailService:
            def __init__(self, email):
                self.users_api = FakeUsers(email)

            def users(self):
                return self.users_api

        same_account_service = FakeGmailService("legal@aspora.com")
        base_matter = {
            "gmail_account": "legal@aspora.com",
            "gmail_thread_id": "thread_inbound",
            "reply_to": "Counterparty Counsel <legal@example.com>",
            "sender": "Noreply <noreply@example.com>",
            "subject": "Please review",
        }

        with patch.object(server_module.gmail_integration.app_settings, "gmail_role_enabled", return_value=True):
            with patch.object(server_module.gmail_integration, "_gmail_service", return_value=same_account_service):
                same_result = server_module.gmail_integration.send_redline_email(
                    base_matter, b"docx", "redline.docx", confirmed_recipient="legal@example.com"
                )

        self.assertEqual(same_account_service.users_api.sent_body["threadId"], "thread_inbound")
        self.assertEqual(same_result["thread_id"], "thread_inbound")
        raw_message = same_account_service.users_api.sent_body["raw"]
        padding = "=" * ((4 - len(raw_message) % 4) % 4)
        decoded_message = base64.urlsafe_b64decode((raw_message + padding).encode("ascii"))
        self.assertIn(b"To: legal@example.com", decoded_message)
        self.assertIn(b'filename="redline.docx"', decoded_message)

    def test_gmail_send_redline_rejects_outbound_account_mismatch_for_gmail_matter(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeUsers:
            sent_body = None

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "personal@gmail.com"})

            def messages(self):
                return self

            def send(self, userId, body):
                self.sent_body = body
                return FakeExecutable({"id": "sent_1"})

        class FakeGmailService:
            def __init__(self):
                self.users_api = FakeUsers()

            def users(self):
                return self.users_api

        service = FakeGmailService()
        matter = {
            "gmail_account": "legal@aspora.com",
            "gmail_thread_id": "thread_inbound",
            "sender": "Counterparty <legal@example.com>",
            "subject": "Please review",
        }

        with patch.object(server_module.gmail_integration.app_settings, "gmail_role_enabled", return_value=True):
            with patch.object(server_module.gmail_integration, "_gmail_service", return_value=service):
                with self.assertRaisesRegex(server_module.gmail_integration.GmailIntegrationError, "does not match inbound Gmail account"):
                    server_module.gmail_integration.send_redline_email(
                        matter, b"docx", "redline.docx", confirmed_recipient="legal@example.com"
                    )

        self.assertIsNone(service.users_api.sent_body)

    def test_gmail_send_redline_rejects_connected_account_as_recipient(self):
        class FakeExecutable:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeUsers:
            sent_body = None

            def getProfile(self, userId):
                return FakeExecutable({"emailAddress": "daniyal.ahmad@aspora.com"})

            def messages(self):
                return self

            def send(self, userId, body):
                self.sent_body = body
                return FakeExecutable({"id": "sent_1"})

        class FakeGmailService:
            def __init__(self):
                self.users_api = FakeUsers()

            def users(self):
                return self.users_api

        service = FakeGmailService()
        matter = {
            "gmail_account": "daniyal.ahmad@aspora.com",
            "sender": "Daniyal Ahmad <daniyal.ahmad@aspora.com>",
            "subject": "Re: NDA",
        }

        with patch.object(server_module.gmail_integration.app_settings, "gmail_role_enabled", return_value=True):
            with patch.object(server_module.gmail_integration, "_gmail_service", return_value=service):
                with self.assertRaisesRegex(server_module.gmail_integration.GmailIntegrationError, "self-sent Gmail message"):
                    server_module.gmail_integration.send_redline_email(
                        matter, b"docx", "redline.docx", confirmed_recipient="daniyal.ahmad@aspora.com"
                    )

        self.assertIsNone(service.users_api.sent_body)

    def test_gmail_send_redline_rejects_when_outbound_disabled(self):
        matter = {
            "sender": "Counterparty <legal@example.com>",
            "subject": "Please review",
        }

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                app_settings.update_gmail_settings({"outbound_enabled": False})
                with patch.object(server_module.gmail_integration, "_gmail_service") as gmail_service:
                    with self.assertRaisesRegex(server_module.gmail_integration.GmailIntegrationError, "Gmail outbound is disabled"):
                        server_module.gmail_integration.send_redline_email(
                            matter, b"docx", "redline.docx", confirmed_recipient="legal@example.com"
                        )

        gmail_service.assert_not_called()

    def test_gmail_default_outbound_body_uses_personalisation_settings(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                app_settings.update_personalisation_settings({
                    "sign_off": "Warm regards,",
                    "signature": "Daniyal Ahmad",
                    "signature_block": "Warm regards,\nDaniyal Ahmad\nAspora Legal",
                })
                body = gmail_matter_outbox.default_outbound_body({
                    "subject": "Partner NDA",
                })

        self.assertEqual(
            body,
            "Hi,\n\n"
            "Please find attached the redlined version of Partner NDA.\n\n"
            "Warm regards,\n"
            "Daniyal Ahmad\n"
            "Aspora Legal",
        )

    def test_gmail_send_redline_rejects_display_name_email_spoof(self):
        matter = {
            "sender": '"jane@x.com" <attacker@evil.com>',
            "subject": "Please review",
        }

        with patch.object(server_module.gmail_integration, "_gmail_service") as gmail_service:
            with self.assertRaisesRegex(server_module.gmail_integration.GmailIntegrationError, "valid reply recipient"):
                server_module.gmail_integration.send_redline_email(matter, b"docx", "redline.docx")

        gmail_service.assert_not_called()

    def test_gmail_send_redline_requires_confirmation_and_records_outbound_send(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = server_module.create_matter_from_document(
                    filename="Email NDA.docx",
                    document_bytes=source_docx,
                    source_type="gmail_inbound",
                    board_column="gmail_demo",
                    intake_metadata={
                        "sender": "Legal Team <legal@example.com>",
                        "subject": "Review Email NDA",
                        "gmail_thread_id": "thr_inbound",
                    },
                )
                matter_id = matter["id"]
                reviewed_status, _reviewed_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/reviewed",
                    body={"reviewed": True},
                )
                with patch.object(server_module.gmail_integration, "validate_outbound_send_ready", return_value={}):
                    with patch.object(server_module.gmail_integration, "send_redline_email", return_value={
                        "message_id": "msg_outbound",
                        "outbound_account": "outbound@example.com",
                        "sent_at": "2026-05-31T12:00:00+00:00",
                        "subject": "Aspora redline Update",
                        "thread_id": "thr_inbound",
                        "to": "legal@example.com",
                    }) as send_redline_email:
                        unconfirmed_status, unconfirmed_payload = self.request(
                            "POST",
                            "/api/gmail/send-redline",
                            {"matter_id": matter_id},
                        )
                        unconfirmed_recipient_status, unconfirmed_recipient_payload = self.request(
                            "POST",
                            "/api/gmail/send-redline",
                            {"matter_id": matter_id, "confirm_send": True},
                        )
                        confirmed_status, confirmed_payload = self.request(
                            "POST",
                            "/api/gmail/send-redline",
                            {
                                "matter_id": matter_id,
                                "confirm_send": True,
                                "confirm_recipient": "legal@example.com",
                                "subject": " Aspora redline\r\nUpdate ",
                                "body": "\r\nAttached redline.\r\nThanks.\r\n",
                            },
                        )
                        stored_matter = matter_store.get_matter(matter_id)

        self.assertEqual(reviewed_status, 200)
        self.assertEqual(unconfirmed_status, 400)
        self.assertEqual(unconfirmed_payload["error"], "Confirm send is required before emailing a redline.")
        self.assertEqual(unconfirmed_recipient_status, 400)
        self.assertEqual(
            unconfirmed_recipient_payload["error"],
            "Confirm the outbound recipient email address before sending.",
        )
        self.assertEqual(confirmed_status, 200)
        self.assertEqual(confirmed_payload["filename"], "Email-NDA-redlined.docx")
        self.assertEqual(confirmed_payload["matter"]["board_column"], "sent")
        self.assertEqual(confirmed_payload["matter"]["last_outbound_to"], "legal@example.com")
        self.assertEqual(confirmed_payload["matter"]["last_outbound_account"], "outbound@example.com")
        self.assertEqual(confirmed_payload["matter"]["last_outbound_message_id"], "msg_outbound")
        self.assertEqual(confirmed_payload["matter"]["last_outbound_subject"], "Aspora redline Update")
        self.assertEqual(stored_matter["board_column"], "sent")
        self.assertEqual(stored_matter["last_outbound_filename"], "Email-NDA-redlined.docx")
        send_redline_email.assert_called_once()
        _matter, attachment_bytes, attachment_filename = send_redline_email.call_args.args
        self.assertEqual(attachment_filename, "Email-NDA-redlined.docx")
        self.assertGreater(len(attachment_bytes), 1000)
        self.assertEqual(send_redline_email.call_args.kwargs["subject"], "Aspora redline Update")
        self.assertEqual(send_redline_email.call_args.kwargs["body"], "Attached redline.\nThanks.")
        self.assertEqual(send_redline_email.call_args.kwargs["to"], None)

    def test_gmail_send_redline_rechecks_review_gate_after_export(self):
        source_docx = make_docx([
            "Each party may disclose Confidential Information to the other party.",
        ])
        redline_export = server_module.redline_export_service.RedlineExport(
            data=b"redline-docx",
            filename="Race-NDA-redlined.docx",
        )

        def make_matter_needs_review(*_args, **_kwargs):
            stale_review = {
                "overall_status": "needs_review",
                "requirements_needs_review": 1,
                "review_state": {"state": "review", "requires_human_review": True},
                "clauses": [{"id": "mutuality", "decision": "review"}],
            }
            matter_store.update_matter_review(
                matter_id,
                stale_review,
                {
                    "triage_status": "needs_redline",
                    "issue_count": 1,
                    "requirements_passed": 0,
                    "requirements_needs_review": 1,
                    "requirements_failed": 0,
                },
            )
            return redline_export

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter = server_module.create_matter_from_document(
                    filename="Race NDA.docx",
                    document_bytes=source_docx,
                    source_type="gmail_inbound",
                    board_column="gmail_demo",
                    intake_metadata={
                        "sender": "Legal Team <legal@example.com>",
                        "subject": "Race NDA",
                    },
                )
                matter_id = matter["id"]
                matter_store.update_matter_fields(matter_id, {"human_reviewed": True})
                self.assertTrue(matter_view.public_matter(matter_store.get_matter(matter_id))["can_send_redline"])
                with patch.object(server_module.gmail_integration, "validate_outbound_send_ready", return_value={}):
                    with patch.object(server_module.redline_export_service, "build_matter_redline", side_effect=make_matter_needs_review):
                        with patch.object(server_module.gmail_integration, "send_redline_email") as send_redline_email:
                            send_status, send_payload = self.request(
                                "POST",
                                "/api/gmail/send-redline",
                                {
                                    "matter_id": matter_id,
                                    "confirm_send": True,
                                    "confirm_recipient": "legal@example.com",
                                },
                            )
                stored_matter = matter_store.get_matter(matter_id)

        self.assertEqual(send_status, 409)
        self.assertEqual(send_payload["error"], "NDA needs human review before a redline can be sent.")
        send_redline_email.assert_not_called()
        self.assertEqual(stored_matter["triage_status"], "needs_redline")

    def test_gmail_send_redline_applies_review_export_decisions(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])
        captured = {}

        def capture_redline_build(_source_bytes, review_result, **_kwargs):
            captured["redline_count"] = len(review_result.get("redline_edits") or [])
            return source_docx

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                seeded_matter = self.seed_reviewed_upload(
                    source_docx,
                    filename="Decision NDA.docx",
                    intake_metadata={
                        "sender": "legal@example.com",
                        "reply_to": "legal@example.com",
                    },
                )
                matter = matter_view.public_matter(seeded_matter)
                stored_matter = matter_store.get_matter(matter["id"])
                self.assertGreater(len(stored_matter["review_result"].get("redline_edits") or []), 0)
                reviewed_status, _reviewed_payload = self.request(
                    "POST",
                    f"/api/matters/{matter['id']}/reviewed",
                    body={"reviewed": True},
                )
                with patch.object(server_module.redline_export_service.docx_package_renderer, "build_source_redline_docx", side_effect=capture_redline_build):
                    with patch.object(server_module.redline_export_service.docx_package_renderer, "validate_docx_open_health", return_value=[]):
                        with patch.object(server_module.gmail_integration, "validate_outbound_send_ready", return_value={}):
                            with patch.object(server_module.gmail_integration, "send_redline_email", return_value={
                                "message_id": "msg_outbound",
                                "outbound_account": "legal@aspora.com",
                                "sent_at": "2026-05-31T12:00:00+00:00",
                                "subject": "Re: Decision NDA",
                                "thread_id": "thread_outbound",
                                "to": "legal@example.com",
                            }):
                                send_status, send_payload = self.request(
                                    "POST",
                                    "/api/gmail/send-redline",
                                    {
                                        "matter_id": matter["id"],
                                        "confirm_send": True,
                                        "confirm_recipient": "legal@example.com",
                                        "export_redline_edits": [],
                                    },
                                )

        self.assertEqual(reviewed_status, 200)
        self.assertEqual(send_status, 200)
        self.assertEqual(send_payload["matter"]["board_column"], "sent")
        self.assertEqual(captured["redline_count"], 0)

    def test_gmail_send_redline_rejects_source_text_change_without_manual_redlines(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                seeded_matter = self.seed_reviewed_upload(
                    source_docx,
                    filename="Send Edited Source NDA.docx",
                    intake_metadata={
                        "sender": "legal@example.com",
                        "reply_to": "legal@example.com",
                    },
                )
                matter_id = seeded_matter["id"]
                reviewed_status, _reviewed_payload = self.request(
                    "POST",
                    f"/api/matters/{matter_id}/reviewed",
                    body={"reviewed": True},
                )
                with patch.object(server_module.gmail_integration, "validate_outbound_send_ready", return_value={}):
                    with patch.object(server_module.gmail_integration, "send_redline_email") as send_redline_email:
                        send_status, send_payload = self.request(
                            "POST",
                            "/api/gmail/send-redline",
                            {
                                "matter_id": matter_id,
                                "confirm_send": True,
                                "confirm_recipient": "legal@example.com",
                                "text": "This Agreement shall be governed by the laws of New York.",
                                "reviewed_text": "This Agreement shall be governed by the laws of New York.",
                                "export_redline_edits": [],
                                "manual_redline_edits": [],
                            },
                        )

        self.assertEqual(reviewed_status, 200)
        self.assertEqual(send_status, 409)
        self.assertIn("NDA source text was edited", send_payload["error"])
        send_redline_email.assert_not_called()

    def test_corrupt_matter_store_does_not_reset_repository(self):
        with tempfile.TemporaryDirectory() as data_dir:
            data_path = server_module.Path(data_dir)
            data_path.mkdir(parents=True, exist_ok=True)
            (data_path / "matters.json").write_text("{not valid json", encoding="utf-8")
            with self.matter_store_patches(data_dir)[0], self.matter_store_patches(data_dir)[1], self.matter_store_patches(data_dir)[2]:
                status, payload = self.request("GET", "/api/matters")

        self.assertEqual(status, 500)
        # Humanized: never leak the "not valid JSON" store-format jargon; the
        # client gets generic copy (the raw cause is logged server-side).
        self.assertEqual(
            payload["error"], "We couldn't load this NDA right now. Please refresh and try again."
        )
        self.assertNotIn("JSON", payload["error"])

    def test_text_review_reports_playbook_template_error(self):
        with patch.object(server_module, "review_nda_with_active_engine", side_effect=PlaybookTemplateError("bad template")):
            status, payload = self.request("POST", "/api/review", {"text": "Reviewable NDA text."})

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_text_review_reports_real_malformed_playbook_template(self):
        with patch("nda_automation.checker.load_playbook", return_value=self.malformed_template_playbook()):
            status, payload = self.request(
                "POST",
                "/api/review",
                {"text": "The confidentiality obligations survive for seven (7) years."},
            )

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_text_review_reports_evidence_provenance_drift_as_error(self):
        with patch.object(server_module, "review_nda_with_active_engine", side_effect=EvidenceProvenanceError("drift")):
            status, payload = self.request("POST", "/api/review", {"text": "Reviewable NDA text."})

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "Clause evidence provenance drift detected.")

    def test_text_review_returns_structured_redline_edits(self):
        with self.deterministic_review_requests():
            status, payload = self.request(
                "POST",
                "/api/review",
                {"text": "This Agreement shall be governed by the laws of California."},
            )

        self.assertEqual(status, 200)
        governing_law_redline = next(edit for edit in payload["redline_edits"] if edit["clause_id"] == "governing_law")
        self.assertEqual(governing_law_redline["action"], "replace_paragraph")
        self.assertEqual(governing_law_redline["status"], "proposed")
        self.assertEqual(governing_law_redline["paragraph_id"], "p1")
        self.assertEqual(
            [option["label"] for option in governing_law_redline["template_options"]],
            ["India", "Delaware", "England and Wales", "DIFC", "Ontario, Canada"],
        )

    def test_text_review_returns_insert_redlines_for_missing_clauses(self):
        with self.deterministic_review_requests():
            status, payload = self.request(
                "POST",
                "/api/review",
                {"text": "The parties will discuss a possible transaction."},
            )

        self.assertEqual(status, 200)
        redlines_by_clause = {edit["clause_id"]: edit for edit in payload["redline_edits"]}
        self.assertEqual(redlines_by_clause["governing_law"]["action"], "insert_after_paragraph")
        self.assertIn("England and Wales", redlines_by_clause["governing_law"]["insert_text"])
        self.assertEqual(
            [option["label"] for option in redlines_by_clause["governing_law"]["template_options"]],
            ["India", "Delaware", "England and Wales", "DIFC", "Ontario, Canada"],
        )
        self.assertEqual(redlines_by_clause["term_and_survival"]["action"], "insert_after_paragraph")
        self.assertIn("up to five years", redlines_by_clause["term_and_survival"]["insert_text"])
        self.assertEqual(redlines_by_clause["signatures"]["action"], "insert_after_paragraph")
        self.assertIn("For [Party 1 legal name]", redlines_by_clause["signatures"]["insert_text"])

    def test_text_review_returns_replace_redline_for_deficient_signature_block(self):
        with self.deterministic_review_requests():
            status, payload = self.request(
                "POST",
                "/api/review",
                {
                    "text": (
                        "This Agreement shall be governed by the laws of the DIFC.\n\n"
                        "By: __________________\n"
                        "Date: 2026-05-30"
                    )
                },
            )

        self.assertEqual(status, 200)
        signatures_redline = next(edit for edit in payload["redline_edits"] if edit["clause_id"] == "signatures")
        self.assertEqual(signatures_redline["action"], "replace_paragraph")
        self.assertEqual(signatures_redline["paragraph_id"], "p2")
        self.assertIn("For [Party 1 legal name]", signatures_redline["replacement_text"])
        self.assertIn("For [Party 2 legal name]", signatures_redline["replacement_text"])

    def test_text_review_returns_term_redline(self):
        with self.deterministic_review_requests():
            status, payload = self.request(
                "POST",
                "/api/review",
                {"text": "The confidentiality obligations survive for seven years."},
            )

        self.assertEqual(status, 200)
        redlines_by_clause = {edit["clause_id"]: edit for edit in payload["redline_edits"]}
        self.assertEqual(redlines_by_clause["term_and_survival"]["action"], "replace_paragraph")
        self.assertIn("up to five years", redlines_by_clause["term_and_survival"]["replacement_text"])
        self.assertNotIn("non_circumvention", redlines_by_clause)

    def test_review_docx_export_returns_track_changes_enabled_docx(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            with patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)):
                status, payload, headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {"text": "This Agreement shall be governed by the laws of California.", "title": "California NDA"},
                )
                saved_payload = (server_module.Path(exports_dir) / "nda-review-report.docx").read_bytes()

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="nda-review-report.docx"')
        self.assertEqual(headers["X-Export-Verified"], "word-package; track-revisions")
        self.assertEqual(headers["X-Export-URL"], "/exports/nda-review-report.docx")
        self.assertNotIn("X-Export-Path", headers)
        self.assertEqual(saved_payload, payload)
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            settings_xml = archive.read("word/settings.xml").decode("utf-8")
            document_xml = archive.read("word/document.xml").decode("utf-8")
        settings_root = ET.fromstring(settings_xml)
        document_root = ET.fromstring(document_xml)
        self.assertIsNotNone(settings_root.find(".//w:trackRevisions", W_NS))
        self.assertIn("California NDA", document_xml)
        self.assertGreaterEqual(len(document_root.findall(".//w:del", W_NS)), 1)
        self.assertGreaterEqual(len(document_root.findall(".//w:ins", W_NS)), 1)

    def test_export_dir_is_opt_in_for_saved_export_routes(self):
        if "NDA_EXPORTS_DIR" not in os.environ:
            self.assertIsNone(export_service.EXPORTS_DIR)
            self.assertIsNone(export_service.persist_export(b"data", "export.docx"))

            status, payload = self.request("GET", "/exports/export.docx")
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"], "Not found")

    def test_review_docx_export_text_path_uses_reviewed_text(self):
        status, payload, _headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "reviewed_text": "This Agreement shall be governed by the laws of California.",
                "title": "Reviewed Text NDA",
            },
        )

        self.assertEqual(status, 200)
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("Reviewed Text NDA", document_xml)
        # The reviewed text drove the export -- it appears in the Redlined NDA body
        # (the deterministic engine redlines the governing-law clause, so the
        # sentence renders as "...laws of" + a tracked California->England edit
        # rather than one contiguous string). The verdict/findings sections that
        # used to carry a verbatim copy are correctly gated off: this text-path
        # export is a deterministic review, which must not ship verdicts to a
        # counterparty (see build_review_report_docx).
        self.assertIn("This Agreement shall be governed by the laws of", document_xml)
        self.assertIn("California", document_xml)
        self.assertIn("AI review has not been run on this document.", document_xml)
        self.assertNotIn("Overall status:", document_xml)
        self.assertNotIn("Clause Findings", document_xml)

    def test_review_docx_export_returns_404_for_missing_matter(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload = self.request(
                    "POST",
                    "/api/export-review-docx",
                    {"matter_id": "matter_missing"},
                )

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "NDA not found.")

    def test_review_docx_export_strips_lone_surrogates(self):
        status, payload, _headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "reviewed_text": "This Agreement shall be governed by the laws of California.\ud800\ufdd0\U0001fffe",
                "title": "Surrogate\udfff\ufdef\U0010ffffNDA",
            },
        )

        self.assertEqual(status, 200)
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            document_xml = archive.read("word/document.xml").decode("utf-8")
            core_xml = archive.read("docProps/core.xml").decode("utf-8")
        ET.fromstring(document_xml)
        ET.fromstring(core_xml)
        self.assertIn("SurrogateNDA", core_xml)
        # Reviewed text reached the (redlined) body; the deterministic engine
        # redlines the governing-law clause, so the sentence is not contiguous.
        self.assertIn("This Agreement shall be governed by the laws of", document_xml)
        self.assertIn("California", document_xml)
        self.assertNotIn("\ud800", document_xml)
        self.assertNotIn("\udfff", core_xml)
        self.assertNotIn("\ufdd0", document_xml)
        self.assertNotIn("\ufdef", core_xml)
        self.assertNotIn("\U0001fffe", document_xml)
        self.assertNotIn("\U0010ffff", core_xml)

    def test_review_docx_export_preserves_manual_viewer_redlines(self):
        manual_redlines = [
            {
                "id": "manual-p1",
                "action": "replace_paragraph",
                "paragraph_id": "p1",
                "paragraph_index": 1,
                "source_index": 1,
                "original_text": "NON-DISCLOSURE AGREEMENT (NDA)",
                "replacement_text": "Do you see problem?",
            }
        ]
        status, payload, _headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "text": "Do you see problem?",
                "reviewed_text": "Do you see problem?",
                "manual_redline_edits": manual_redlines,
            },
        )

        self.assertEqual(status, 200)
        assert_docx_redline_contract(self, payload, manual_redlines)
        with ZipFile(BytesIO(payload)) as archive:
            document_root = ET.fromstring(archive.read("word/document.xml"))
        revision_states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in document_root.findall(".//w:p", W_NS)
        ]
        self.assertIn(("NON-DISCLOSURE AGREEMENT (NDA)", "Do you see problem?"), revision_states)

    def test_manual_export_redline_rejects_blank_replace(self):
        redline = {
            "id": "blank-replace",
            "action": "replace_paragraph",
            "paragraph_id": "p1",
            "source_index": 1,
            "original_text": "The original paragraph.",
            "replacement_text": "",
        }

        self.assertIsNone(export_service.clean_manual_export_redline(redline))

    def test_manual_export_redline_cleaner_trims_direct_api_text_fields(self):
        redline = {
            "id": "manual-p1",
            "action": "replace_paragraph",
            "paragraph_id": " p1 ",
            "original_text": "  Old paragraph.  ",
            "replacement_text": "  New paragraph.  ",
            "anchor_text": "  Anchor paragraph.  ",
            "insert_text": "  Insert paragraph.  ",
        }

        manual = export_service.clean_manual_export_redline(redline)

        self.assertEqual(manual["paragraph_id"], "p1")
        self.assertEqual(manual["original_text"], "Old paragraph.")
        self.assertEqual(manual["replacement_text"], "New paragraph.")

    def test_manual_export_redline_cleaner_ignores_non_finite_indexes(self):
        redline = {
            "id": "manual-p1",
            "action": "replace_paragraph",
            "paragraph_id": "p1",
            "paragraph_index": 1,
            "source_index": float("inf"),
            "original_text": "Old paragraph.",
            "replacement_text": "New paragraph.",
        }

        manual = export_service.clean_manual_export_redline(redline)

        self.assertEqual(manual["paragraph_index"], 1)
        self.assertNotIn("source_index", manual)

    def test_export_rejects_non_finite_json_constants(self):
        for constant in ("Infinity", "-Infinity", "NaN"):
            with self.subTest(constant=constant):
                status, payload, _headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    body=f"""{{
                        "text": "Do you see problem?",
                        "manual_redline_edits": [{{
                            "action": "replace_paragraph",
                            "paragraph_id": "p1",
                            "source_index": {constant},
                            "original_text": "Old paragraph.",
                            "replacement_text": "New paragraph."
                        }}]
                    }}""",
                    headers={"Content-Type": "application/json"},
                )

                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "Request body must be valid JSON.")

    def test_selected_export_redlines_rederive_text_server_side(self):
        malicious_selected_redline = {
            "id": "r1",
            "action": "replace_paragraph",
            "paragraph_id": "p1",
            "source_index": 1,
            "original_text": "Client supplied fake original.",
            "replacement_text": "MALICIOUS CLIENT SUPPLIED REDLINE.",
            "template_options": [
                {"id": "governing_law_delaware", "selected": True, "text": "MALICIOUS TEMPLATE TEXT."}
            ],
        }

        status, payload, _headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "text": "This Agreement shall be governed by the laws of California.",
                "export_redline_edits": [malicious_selected_redline],
            },
        )

        self.assertEqual(status, 200)
        with ZipFile(BytesIO(payload)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        # Security: client-supplied redline/template text is re-derived server-side
        # and never shipped.
        self.assertNotIn("MALICIOUS", document_xml)
        # The server's source text reaches the (redlined) body.
        self.assertIn("California", document_xml)
        # This is a deterministic text-path export (no AI review), so the
        # verdict/finding/template sections -- which previously carried the
        # server-derived "Delaware" template option -- are gated off and must not
        # ship to a counterparty. The gate notice stands in their place.
        self.assertIn("AI review has not been run on this document.", document_xml)
        self.assertNotIn("Template options", document_xml)
        self.assertNotIn("Clause Findings", document_xml)

    def test_selected_export_redlines_ignore_id_collision_for_different_clause(self):
        review_result = {
            "redline_edits": [
                {
                    "id": "r1",
                    "clause_id": "governing_law",
                    "paragraph_id": "p1",
                    "action": "replace_paragraph",
                    "original_text": "This Agreement shall be governed by the laws of California.",
                    "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
                }
            ],
        }

        export_service.apply_selected_export_redlines(
            review_result,
            [
                {
                    "id": "r1",
                    "clause_id": "signatures",
                    "paragraph_id": "p2",
                    "action": "insert_after_paragraph",
                }
            ],
        )

        self.assertEqual(review_result["redline_edits"], [])

    def test_selected_source_redlines_rederive_original_anchor_server_side(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                # The HTTP upload route now DEFERS the AI review (defer_ai_review=True),
                # so it no longer produces a review_result at create. This test
                # exercises the EXPORT route's server-side redline anchor
                # re-derivation, which needs a matter that already carries a
                # deterministic review with redlines. Seed that matter directly
                # through the ingestion function (which still runs the forced
                # deterministic review synchronously), then exercise the export route.
                with self.deterministic_matter_intake():
                    created_matter = ingestion_service.create_matter_from_document(
                        filename="Selected Source NDA.docx",
                        document_bytes=source_docx,
                        source_type="manual_upload",
                        board_column="manual_upload",
                    )
                create_status = 201
                matter_id = created_matter["id"]
                stored_matter = matter_store.get_matter(matter_id)
                governing_law_redline = next(
                    edit
                    for edit in stored_matter["review_result"]["redline_edits"]
                    if edit["clause_id"] == "governing_law"
                )
                edited_text_anchored_redline = {
                    "id": governing_law_redline["id"],
                    "clause_id": governing_law_redline["clause_id"],
                    "paragraph_id": governing_law_redline["paragraph_id"],
                    "action": governing_law_redline["action"],
                    "original_text": "Edited browser text that is not present in the uploaded DOCX.",
                    "replacement_text": "CLIENT SUPPLIED REPLACEMENT.",
                }
                export_status, export_payload, _headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {
                        "matter_id": matter_id,
                        "export_redline_edits": [edited_text_anchored_redline],
                    },
                )

        self.assertEqual(create_status, 201)
        self.assertEqual(export_status, 200)
        with ZipFile(BytesIO(export_payload)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
            document_root = ET.fromstring(document_xml)
        self.assertNotIn("CLIENT SUPPLIED", document_xml)
        self.assertNotIn("Edited browser text", document_xml)
        revision_states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in document_root.findall(".//w:p", W_NS)
        ]
        self.assertIn(
            (
                "This Agreement shall be governed by the laws of California.",
                "This Agreement shall be governed by the laws of England and Wales.",
            ),
            revision_states,
        )

    def test_review_docx_export_download_does_not_require_saved_copy(self):
        with patch.object(export_service, "EXPORTS_DIR", None):
            status, payload, headers = self.request_with_headers(
                "POST",
                "/api/export-review-docx",
                {"text": "This Agreement shall be governed by the laws of California."},
            )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        self.assertEqual(headers["X-Export-Verified"], "word-package; track-revisions")
        self.assertNotIn("X-Export-Path", headers)
        self.assertNotIn("X-Export-URL", headers)
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())

    def test_review_docx_export_fails_before_download_when_docx_health_fails(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            with (
                patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)),
                patch.object(
                    server_module.redline_export_service,
                    "validate_docx_open_health",
                    return_value=["Missing DOCX parts: _rels/.rels."],
                ),
                patch("builtins.print") as mocked_print,
            ):
                status, payload, headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {"text": "This Agreement shall be governed by the laws of California."},
                )
                saved_files = list(server_module.Path(exports_dir).iterdir())

        self.assertEqual(status, 500)
        # Humanized: the OOXML internals (error.details) are dropped from the body
        # (logged server-side); the client gets generic integrity-check copy.
        self.assertEqual(
            payload["error"],
            "The reviewed Word document failed an integrity check and was not produced. "
            "Please contact support.",
        )
        self.assertNotIn("details", payload)
        self.assertNotEqual(headers.get("Content-Type"), DOCX_MIME)
        self.assertEqual(saved_files, [])
        mocked_print.assert_called_once_with("DOCX export health check failed: 1 issue(s)")
        self.assertEqual(telemetry.snapshot()["counters"]["docx_export_health_failures"], 1)

    def test_review_docx_export_rejects_text_that_differs_from_reviewed_text(self):
        status, payload = self.request(
            "POST",
            "/api/export-review-docx",
            {
                "text": "This Agreement shall be governed by the laws of England and Wales.",
                "reviewed_text": "This Agreement shall be governed by the laws of California.",
            },
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "Export text must match the latest reviewed text. Reload the NDA review before exporting.")

    def test_review_docx_export_reports_playbook_template_error(self):
        with patch.object(server_module.redline_export_service, "review_nda", side_effect=PlaybookTemplateError("bad template")):
            status, payload = self.request(
                "POST",
                "/api/export-review-docx",
                {"reviewed_text": "This Agreement shall be governed by the laws of California."},
            )

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_review_docx_export_reports_real_malformed_playbook_template(self):
        with patch("nda_automation.checker.load_playbook", return_value=self.malformed_template_playbook()):
            status, payload = self.request(
                "POST",
                "/api/export-review-docx",
                {"reviewed_text": "The confidentiality obligations survive for seven (7) years."},
            )

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_review_docx_export_preserves_word_source_index(self):
        source_docx = make_docx([
            "Intro paragraph.",
            "This Agreement shall be governed by the laws of California.",
        ])
        status, payload, headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "text": "Stale browser text should not drive DOCX export.",
                "title": "Uploaded NDA",
                "filename": "uploaded.docx",
                "content_base64": base64.b64encode(source_docx).decode("ascii"),
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="uploaded-redlined.docx"')
        assert_source_export_has_no_report_leakage(
            self,
            payload,
            extra_forbidden=["Stale browser text should not drive DOCX export."],
        )
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            document_xml = archive.read("word/document.xml").decode("utf-8")
        document_root = ET.fromstring(document_xml)
        deletion_text = [
            "".join(node.text or "" for node in deletion.findall(".//w:delText", W_NS))
            for deletion in document_root.findall(".//w:del", W_NS)
        ]
        self.assertTrue(any("California" in text for text in deletion_text))
        self.assertFalse(any("This Agreement shall be governed by the laws of California." in text for text in deletion_text))

    def test_review_docx_export_uses_uploaded_docx_over_stale_reviewed_text(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])
        status, payload, _headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "reviewed_text": "Stale reviewed text should not drive DOCX export.",
                "filename": "uploaded.docx",
                "content_base64": base64.b64encode(source_docx).decode("ascii"),
            },
        )

        self.assertEqual(status, 200)
        assert_source_export_has_no_report_leakage(
            self,
            payload,
            extra_forbidden=["Stale reviewed text should not drive DOCX export."],
        )
        with ZipFile(BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("California", document_xml)

    def test_uploaded_docx_export_preserves_manual_viewer_redlines_on_source(self):
        source_docx = make_docx([
            "NON-DISCLOSURE AGREEMENT (NDA)",
            "This reciprocal confidentiality agreement is dated 2025.",
        ])
        manual_redlines = [
            {
                "id": "manual-p1",
                "action": "replace_paragraph",
                "paragraph_id": "p1",
                "paragraph_index": 1,
                "source_index": 1,
                "original_text": "NON-DISCLOSURE AGREEMENT (NDA)",
                "replacement_text": "Do you see problem?",
            },
            {
                "id": "manual-p2",
                "action": "replace_paragraph",
                "paragraph_id": "p2",
                "paragraph_index": 2,
                "source_index": 2,
                "original_text": "This reciprocal confidentiality agreement is dated 2025.",
                "replacement_text": "Hello",
            },
        ]
        status, payload, headers = self.request_with_headers(
            "POST",
            "/api/export-review-docx",
            {
                "reviewed_text": "Do you see problem?\n\nHello",
                "filename": "Orbii - NDA DIFC.docx",
                "content_base64": base64.b64encode(source_docx).decode("ascii"),
                "manual_redline_edits": manual_redlines,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Orbii---NDA-DIFC-redlined.docx"')
        assert_source_export_has_no_report_leakage(self, payload)
        assert_docx_redline_contract(self, payload, manual_redlines)
        with ZipFile(BytesIO(payload)) as archive:
            document_root = ET.fromstring(archive.read("word/document.xml"))
        revision_states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in document_root.findall(".//w:p", W_NS)
        ]
        self.assertIn(("NON-DISCLOSURE AGREEMENT (NDA)", "Do you see problem?"), revision_states)
        self.assertIn(("This reciprocal confidentiality agreement is dated 2025.", "Hello"), revision_states)

    def test_saved_export_route_returns_exact_docx_bytes(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            with patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)):
                status, payload, headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {"text": "This Agreement shall be governed by the laws of California."},
                )
                self.assert_saved_export_url_matches_response(headers, payload)

        self.assertEqual(status, 200)

    def test_saved_export_route_reports_read_errors(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            export_path = server_module.Path(exports_dir) / "saved.docx"
            export_path.write_bytes(b"docx")
            with (
                patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)),
                patch.object(server_module.Path, "read_bytes", side_effect=OSError("cannot read export")),
            ):
                status, payload = self.request("GET", "/exports/saved.docx")

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "Export file could not be read.")

    def test_saved_export_head_uses_metadata_without_reading_body(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            export_path = server_module.Path(exports_dir) / "saved.docx"
            export_path.write_bytes(b"docx")
            with (
                patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)),
                patch.object(server_module.Path, "read_bytes", side_effect=AssertionError("HEAD should not read file bytes")),
            ):
                status, payload, headers = self.request_with_headers("HEAD", "/exports/saved.docx")

        self.assertEqual(status, 200)
        self.assertEqual(payload, b"")
        self.assertEqual(headers["Content-Type"], DOCX_MIME)
        self.assertEqual(headers["Content-Length"], "4")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="saved.docx"')

    def test_saved_uploaded_docx_export_route_returns_exact_docx_bytes(self):
        source_docx = make_docx([
            "This Agreement shall be governed by the laws of California.",
        ])
        with tempfile.TemporaryDirectory() as exports_dir:
            with patch.object(export_service, "EXPORTS_DIR", server_module.Path(exports_dir)):
                status, payload, headers = self.request_with_headers(
                    "POST",
                    "/api/export-review-docx",
                    {
                        "filename": "uploaded.docx",
                        "content_base64": base64.b64encode(source_docx).decode("ascii"),
                    },
                )
                self.assert_saved_export_url_matches_response(headers, payload)

        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Export-URL"], "/exports/uploaded-redlined.docx")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="uploaded-redlined.docx"')
        assert_source_export_has_no_report_leakage(self, payload)

    def test_persisted_exports_prune_old_saved_docx_files(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            exports_path = server_module.Path(exports_dir)
            with patch.object(export_service, "EXPORTS_DIR", exports_path):
                with patch.object(export_service, "MAX_SAVED_EXPORTS", 2):
                    old_one = exports_path / "old-one.docx"
                    old_two = exports_path / "old-two.docx"
                    old_one.write_bytes(b"old-one")
                    old_two.write_bytes(b"old-two")
                    os.utime(old_one, (1, 1))
                    os.utime(old_two, (2, 2))

                    saved_path = export_service.persist_export(b"new", "new.docx")

                    self.assertEqual(saved_path.resolve(), (exports_path / "new.docx").resolve())
                    self.assertEqual(
                        sorted(path.name for path in exports_path.glob("*.docx")),
                        ["new.docx", "old-two.docx"],
                    )
                    self.assertEqual((exports_path / "new.docx").read_bytes(), b"new")

    def test_persisted_exports_do_not_overwrite_same_name_exports(self):
        with tempfile.TemporaryDirectory() as exports_dir:
            exports_path = server_module.Path(exports_dir)
            with patch.object(export_service, "EXPORTS_DIR", exports_path):
                first_path = export_service.persist_export(b"first", "same.docx")
                second_path = export_service.persist_export(b"second", "same.docx")
                first_bytes = first_path.read_bytes()
                second_bytes = second_path.read_bytes()

        self.assertIsNotNone(first_path)
        self.assertIsNotNone(second_path)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.name, "same.docx")
        self.assertRegex(second_path.name, r"^same-[0-9a-f]{12}\.docx$")
        self.assertEqual(first_bytes, b"first")
        self.assertEqual(second_bytes, b"second")

    def test_playbook_save_updates_local_playbook_file_after_validation(self):
        playbook = deepcopy(load_playbook())
        mutuality = next(clause for clause in playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Mutual NDA policy saved by admin."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = server_module.Path(playbook_dir) / "playbook.json"
            with patch.object(server_module, "PLAYBOOK_PATH", playbook_path):
                status, payload = self.request("POST", "/api/playbook", {"playbook": playbook})

                self.assertEqual(status, 200)
                self.assertEqual(payload["playbook"]["clauses"][0]["preferred_position"], playbook["clauses"][0]["preferred_position"])
                saved = json.loads(playbook_path.read_text(encoding="utf-8"))
                self.assertFalse((playbook_path.parent / ".playbook.json.tmp").exists())

        saved_mutuality = next(clause for clause in saved["clauses"] if clause["id"] == "mutuality")
        self.assertEqual(saved_mutuality["preferred_position"], "Mutual NDA policy saved by admin.")

    def test_playbook_api_get_returns_playbook_with_public_history(self):
        playbook = deepcopy(load_playbook())

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = server_module.Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(playbook), encoding="utf-8")
            history_path = playbook_path.parent / "playbook.history.json"
            history_path.write_text(json.dumps({
                "version": 1,
                "entries": [{
                    "id": "pbv_test",
                    "recorded_at": "2026-06-04T12:00:00+00:00",
                    "actor": "admin",
                    "action": "save",
                    "summary": "Saved changes to Mutuality.",
                    "playbook_name": playbook["name"],
                    "playbook_version": playbook["version"],
                    "changed_clause_ids": ["mutuality"],
                    "snapshot": playbook,
                }],
            }), encoding="utf-8")

            with patch.object(server_module, "PLAYBOOK_PATH", playbook_path):
                status, payload = self.request("GET", "/api/playbook")

        self.assertEqual(status, 200)
        self.assertEqual(payload["playbook"]["name"], playbook["name"])
        self.assertEqual(payload["history"][0]["id"], "pbv_test")
        self.assertEqual(payload["history"][0]["changed_clause_ids"], ["mutuality"])
        self.assertNotIn("snapshot", payload["history"][0])

    def test_playbook_save_records_history_and_restore_recovers_snapshot(self):
        original_playbook = deepcopy(load_playbook())
        changed_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in changed_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Versioned Playbook save."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = server_module.Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")

            with patch.object(server_module, "PLAYBOOK_PATH", playbook_path):
                status, payload = self.request("POST", "/api/playbook", {"playbook": changed_playbook, "actor": "legal-admin"})

                self.assertEqual(status, 200)
                self.assertEqual(payload["history"][0]["action"], "save")
                self.assertEqual(payload["history"][0]["actor"], "legal-admin")
                self.assertEqual(payload["history"][0]["changed_clause_ids"], ["mutuality"])
                self.assertEqual(payload["history"][1]["action"], "baseline")
                self.assertNotIn("snapshot", payload["history"][0])

                baseline_id = payload["history"][1]["id"]
                status, restore_payload = self.request("POST", "/api/playbook/restore", {
                    "history_id": baseline_id,
                    "actor": "legal-admin",
                })

            self.assertEqual(status, 200)
            self.assertEqual(restore_payload["history"][0]["action"], "restore")
            self.assertEqual(restore_payload["history"][0]["restored_from_id"], baseline_id)
            self.assertEqual(restore_payload["history"][0]["changed_clause_ids"], ["mutuality"])
            restored = json.loads(playbook_path.read_text(encoding="utf-8"))

        restored_mutuality = next(clause for clause in restored["clauses"] if clause["id"] == "mutuality")
        original_mutuality = next(clause for clause in original_playbook["clauses"] if clause["id"] == "mutuality")
        self.assertEqual(restored_mutuality["preferred_position"], original_mutuality["preferred_position"])

    def test_playbook_save_preserves_existing_file_when_atomic_replace_fails(self):
        original_playbook = deepcopy(load_playbook())
        changed_playbook = deepcopy(original_playbook)
        mutuality = next(clause for clause in changed_playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "This should not replace the saved playbook."

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = server_module.Path(playbook_dir) / "playbook.json"
            playbook_path.write_text(json.dumps(original_playbook), encoding="utf-8")
            temporary_path = playbook_path.parent / ".playbook.json.tmp"

            with patch.object(server_module, "PLAYBOOK_PATH", playbook_path):
                with patch.object(server_module.os, "replace", side_effect=OSError("disk full")):
                    status, payload = self.request("POST", "/api/playbook", {"playbook": changed_playbook})

            self.assertEqual(status, 500)
            self.assertEqual(payload["error"], "Playbook could not be saved.")
            self.assertEqual(json.loads(playbook_path.read_text(encoding="utf-8")), original_playbook)
            self.assertFalse(temporary_path.exists())

    def test_playbook_save_rejects_invalid_playbook_without_writing_file(self):
        playbook = deepcopy(load_playbook())
        mutuality = next(clause for clause in playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["search_terms"] = []

        with tempfile.TemporaryDirectory() as playbook_dir:
            playbook_path = server_module.Path(playbook_dir) / "playbook.json"
            with patch.object(server_module, "PLAYBOOK_PATH", playbook_path):
                status, payload = self.request("POST", "/api/playbook", {"playbook": playbook})

                self.assertEqual(status, 400)
                self.assertFalse(playbook_path.exists())

        self.assertIn("must include search_terms", payload["error"])

    def test_text_review_reports_malformed_playbook_search_terms(self):
        playbook = deepcopy(load_playbook())
        mutuality = next(clause for clause in playbook["clauses"] if clause["id"] == "mutuality")
        mutuality["search_terms"] = []

        with patch("nda_automation.checker.load_playbook", return_value=playbook):
            status, payload = self.request("POST", "/api/review", {"text": "Reviewable NDA text."})

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_text_review_reports_empty_playbook_json_as_playbook_error(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as playbook:
            playbook.write("")
            playbook.flush()
            with patch("nda_automation.checker.PLAYBOOK_PATH", server_module.Path(playbook.name)):
                status, payload = self.request("POST", "/api/review", {"text": "Reviewable NDA text."})

        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], server_module.PLAYBOOK_TEMPLATE_ERROR_MESSAGE)

    def test_review_docx_export_rejects_empty_text(self):
        status, payload = self.request("POST", "/api/export-review-docx", {"text": " "})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Provide NDA text to export.")

    def test_matter_upload_rejects_oversize_upload_at_ingestion_boundary(self):
        with patch.object(document_limits, "MAX_DOCUMENT_BYTES", 4):
            status, payload = self.request(
                "POST",
                "/api/matters",
                {
                    "filename": "nda.docx",
                    "content_base64": base64.b64encode(b"too large").decode("ascii"),
                },
            )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "The document is larger than the 10 MB upload limit.")

    def test_matter_upload_rejects_oversize_pdf_before_ingestion(self):
        with patch.object(document_limits, "MAX_DOCUMENT_BYTES", 4):
            with patch.object(
                server_module,
                "create_matter_from_document",
                side_effect=AssertionError("Matter ingestion should not run"),
            ) as create_matter_from_document:
                status, payload = self.request(
                    "POST",
                    "/api/matters",
                    {
                        "filename": "nda.pdf",
                        "content_base64": base64.b64encode(b"too large").decode("ascii"),
                    },
                )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "The document is larger than the 10 MB upload limit.")
        create_matter_from_document.assert_not_called()

    def test_static_route_blocks_directory_traversal(self):
        status, payload = self.request("GET", "/static/../README.md")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "Not found")

    def test_static_files_use_etag_cache_validation(self):
        status, payload, headers = self.request_with_headers("GET", "/static/app.js")
        etag = headers.get("ETag")
        cached_status, cached_payload, cached_headers = self.request_with_headers(
            "GET",
            "/static/app.js",
            headers={"If-None-Match": etag},
        )

        self.assertEqual(status, 200)
        self.assertIsInstance(payload, bytes)
        self.assertTrue(etag)
        self.assertEqual(headers["Cache-Control"], "no-cache, max-age=0, must-revalidate")
        self.assertEqual(cached_status, 304)
        self.assertEqual(cached_payload, b"")
        self.assertEqual(cached_headers["ETag"], etag)

    def test_head_static_route_uses_app_cache_strategy(self):
        status, payload, headers = self.request_with_headers("HEAD", "/static/app.js")

        self.assertEqual(status, 200)
        self.assertEqual(payload, b"")
        self.assertTrue(headers.get("ETag"))
        self.assertEqual(headers["Cache-Control"], "no-cache, max-age=0, must-revalidate")

    def test_head_matter_routes_do_not_send_error_body(self):
        with patch.object(matter_store, "get_matter", side_effect=matter_store.MatterStoreError("store failed")):
            for path in ("/api/matters/matter_1", "/api/matters/matter_1/review"):
                with self.subTest(path=path):
                    connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
                    try:
                        connection.request("HEAD", path)
                        response = connection.getresponse()
                        raw_body = response.read()
                    finally:
                        connection.close()

                    self.assertEqual(response.status, 500)
                    self.assertEqual(raw_body, b"")
                    self.assertEqual(response.getheader("Content-Length"), "0")


def make_docx(paragraphs):
    body = "".join(
        f"<w:p><w:r><w:t>{escape_xml(paragraph)}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body}</w:body>
</w:document>"""
    with BytesIO() as output:
        with ZipFile(output, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
        return output.getvalue()


def make_unsafe_docx():
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE w:document [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body>
</w:document>"""
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)
        return output.getvalue()


def make_pdf(text):
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        "4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n"
    objects.append(f"5 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n")
    with BytesIO() as output:
        output.write(b"%PDF-1.4\n")
        offsets = [0]
        for pdf_object in objects:
            offsets.append(output.tell())
            output.write(pdf_object.encode("latin-1"))
        xref_offset = output.tell()
        output.write(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
        output.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
        output.write(f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1"))
        return output.getvalue()


def escape_xml(value):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def assert_source_export_has_no_report_leakage(testcase, docx_bytes, extra_forbidden=()):
    with ZipFile(BytesIO(docx_bytes)) as archive:
        testcase.assertIsNone(archive.testzip())
        document_xml = archive.read("word/document.xml").decode("utf-8")
    for phrase in [*SOURCE_EXPORT_REPORT_LEAKAGE_PHRASES, *extra_forbidden]:
        testcase.assertNotIn(phrase, document_xml)


def revision_text_for_state(node, accepted):
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "del":
        return "".join(item.text or "" for item in node.findall(".//w:delText", W_NS)) if not accepted else ""
    if tag == "ins":
        return "".join(item.text or "" for item in node.findall(".//w:t", W_NS)) if accepted else ""
    if tag == "t":
        return node.text or ""
    if tag == "br":
        return "\n"
    return "".join(revision_text_for_state(child, accepted) for child in list(node))


class RateLimitClientKeyTests(unittest.TestCase):
    def test_authenticated_identity_takes_priority_over_ip(self):
        key = server_module._rate_limit_client_key(
            "10.0.0.5", "203.0.113.9", "alice@example.com"
        )
        self.assertEqual(key, "user:alice@example.com")

    def test_tcp_peer_is_used_when_no_trusted_proxy_configured(self):
        # Without a declared proxy chain, X-Forwarded-For is attacker-controlled
        # and must be ignored so it cannot be used to dodge the limit.
        with patch.dict(os.environ, {"NDA_TRUSTED_PROXY_COUNT": "0"}):
            key = server_module._rate_limit_client_key(
                "10.0.0.5", "1.2.3.4, 5.6.7.8", ""
            )
        self.assertEqual(key, "ip:10.0.0.5")

    def test_trusted_proxy_count_selects_real_client_from_forwarded_for(self):
        # One proxy (Render) in front: peer is the proxy, and the rightmost XFF
        # entry it appended is the real client. Skip the trusted hop.
        with patch.dict(os.environ, {"NDA_TRUSTED_PROXY_COUNT": "1"}):
            key = server_module._rate_limit_client_key(
                "10.0.0.1", "203.0.113.9", ""
            )
        self.assertEqual(key, "ip:203.0.113.9")

    def test_spoofed_extra_forwarded_for_hops_do_not_change_real_client(self):
        # An attacker prepends fake hops; with one trusted proxy we still take
        # the single rightmost untrusted hop, ignoring the spoofed prefix.
        with patch.dict(os.environ, {"NDA_TRUSTED_PROXY_COUNT": "1"}):
            key = server_module._rate_limit_client_key(
                "10.0.0.1", "9.9.9.9, 8.8.8.8, 203.0.113.9", ""
            )
        self.assertEqual(key, "ip:203.0.113.9")

    def test_forwarded_for_shorter_than_proxy_count_falls_back_to_leftmost(self):
        with patch.dict(os.environ, {"NDA_TRUSTED_PROXY_COUNT": "3"}):
            key = server_module._rate_limit_client_key(
                "10.0.0.1", "203.0.113.9", ""
            )
        self.assertEqual(key, "ip:203.0.113.9")

    def test_missing_forwarded_for_with_trusted_proxy_falls_back_to_peer(self):
        with patch.dict(os.environ, {"NDA_TRUSTED_PROXY_COUNT": "1"}):
            key = server_module._rate_limit_client_key("10.0.0.1", "", "")
        self.assertEqual(key, "ip:10.0.0.1")

    def test_heaviest_ai_endpoints_are_bucketed(self):
        # The two heaviest endpoints each fan out to AI work; given the app's
        # cost-storm history they must be rate-limit bucketed (not short-circuit
        # to unlimited like an unbucketed path). Distinct bucket names isolate
        # them from each other and from the review buckets.
        self.assertEqual(
            server_module._rate_limit_bucket_name("POST", "/api/generate-nda"),
            "generate-nda",
        )
        self.assertEqual(
            server_module._rate_limit_bucket_name("POST", "/api/dashboard/assistant"),
            "dashboard-assistant",
        )


class TelemetryHealthSummaryTest(unittest.TestCase):
    def test_all_zero_counters_are_division_safe_and_ok(self):
        summary = telemetry.health_summary({})
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["review"]["fail_closed_rate"], 0.0)
        self.assertEqual(summary["review"]["partial_rate"], 0.0)
        self.assertEqual(summary["generation"]["failure_rate"], 0.0)
        self.assertEqual(summary["generation"]["gate_block_rate"], 0.0)
        self.assertEqual(
            summary["alerts"],
            ["No AI-review or generation failure thresholds crossed."],
        )
        self.assertIn("cumulative", summary["note"])
        # Every watched 'other' failure counter defaults to 0.
        self.assertEqual(set(summary["other"].values()), {0})

    def test_shape_and_rate_math(self):
        counters = {
            "active_review_ai_first_attempted": 100,
            "active_review_ai_first_completed": 80,
            "active_review_ai_first_failed": 12,
            "active_review_ai_first_fail_closed": 4,
            "active_review_ai_first_partial": 10,
            "active_review_deterministic_completed": 7,
            "generate_nda_requests": 40,
            "generate_nda_succeeded": 30,
            "generate_nda_rejected": 6,
            "generate_nda_failed": 4,
            "generate_nda_safety_gate_blocked": 2,
        }
        summary = telemetry.health_summary(counters)
        self.assertEqual(
            set(summary),
            {"review", "inbound_review", "generation", "other", "status", "alerts", "note"},
        )
        review = summary["review"]
        self.assertEqual(review["attempted"], 100)
        self.assertEqual(review["completed"], 80)
        self.assertEqual(review["failed"], 12)
        self.assertEqual(review["fail_closed"], 4)
        self.assertEqual(review["partial"], 10)
        self.assertEqual(review["deterministic_completed"], 7)
        self.assertAlmostEqual(review["fail_closed_rate"], 0.04)
        self.assertAlmostEqual(review["partial_rate"], 0.10)
        generation = summary["generation"]
        self.assertEqual(generation["requests"], 40)
        self.assertEqual(generation["succeeded"], 30)
        self.assertEqual(generation["rejected"], 6)
        self.assertEqual(generation["failed"], 4)
        self.assertEqual(generation["safety_gate_blocked"], 2)
        self.assertAlmostEqual(generation["failure_rate"], 0.10)
        self.assertAlmostEqual(generation["gate_block_rate"], 0.05)

    def test_ok_below_all_thresholds(self):
        # Failures present but below every warn threshold.
        counters = {
            "active_review_ai_first_attempted": 50,
            "active_review_ai_first_fail_closed": 2,
            "generate_nda_requests": 20,
            "generate_nda_failed": 2,
            "generate_nda_safety_gate_blocked": 4,
            "gmail_sync_failures": 9,
        }
        summary = telemetry.health_summary(counters)
        self.assertEqual(summary["status"], "ok")

    def test_warn_on_absolute_fail_closed(self):
        summary = telemetry.health_summary({"active_review_ai_first_fail_closed": 3})
        self.assertEqual(summary["status"], "warn")
        self.assertTrue(any("fail-closed 3 times" in alert for alert in summary["alerts"]))

    def test_warn_on_fail_closed_rate(self):
        summary = telemetry.health_summary({
            "active_review_ai_first_attempted": 40,
            "active_review_ai_first_fail_closed": 2,  # 5% of 40
        })
        self.assertEqual(summary["status"], "warn")
        self.assertTrue(any("fail-closed rate" in alert for alert in summary["alerts"]))

    def test_warn_on_generation_failures(self):
        summary = telemetry.health_summary({
            "generate_nda_requests": 100,
            "generate_nda_failed": 3,
        })
        self.assertEqual(summary["status"], "warn")
        self.assertTrue(any("generation has failed 3" in alert for alert in summary["alerts"]))

    def test_warn_on_safety_gate_blocks(self):
        summary = telemetry.health_summary({
            "generate_nda_requests": 100,
            "generate_nda_safety_gate_blocked": 5,
        })
        self.assertEqual(summary["status"], "warn")
        self.assertTrue(any("safety gate" in alert for alert in summary["alerts"]))

    def test_warn_on_other_failure_counter(self):
        summary = telemetry.health_summary({"csrf_rejections": 10})
        self.assertEqual(summary["status"], "warn")
        self.assertTrue(any("csrf_rejections" in alert for alert in summary["alerts"]))

    def test_warn_on_ai_verifier_errors(self):
        summary = telemetry.health_summary({"ai_verifier_errors": 10})
        self.assertEqual(summary["status"], "warn")
        self.assertTrue(any("ai_verifier_errors" in alert for alert in summary["alerts"]))

    def test_alert_on_absolute_fail_closed(self):
        summary = telemetry.health_summary({"active_review_ai_first_fail_closed": 10})
        self.assertEqual(summary["status"], "alert")

    def test_alert_on_fail_closed_rate(self):
        summary = telemetry.health_summary({
            "active_review_ai_first_attempted": 20,
            "active_review_ai_first_fail_closed": 3,  # 15% of 20
        })
        self.assertEqual(summary["status"], "alert")
        self.assertTrue(any("fail-closed rate" in alert for alert in summary["alerts"]))

    def test_alert_on_generation_failure_rate(self):
        summary = telemetry.health_summary({
            "generate_nda_requests": 10,
            "generate_nda_failed": 3,  # 30% of 10
        })
        self.assertEqual(summary["status"], "alert")
        self.assertTrue(any("generation failure rate" in alert for alert in summary["alerts"]))

    def test_fail_closed_rate_ignored_below_minimum_attempts(self):
        # 100% fail-closed but only 2 attempts: below the attempted>=20 guard,
        # so the rate-based thresholds do not fire. (Absolute count still does.)
        summary = telemetry.health_summary({
            "active_review_ai_first_attempted": 2,
            "active_review_ai_first_fail_closed": 2,
        })
        self.assertEqual(summary["status"], "ok")

    def test_status_is_maximum_severity(self):
        # A warn-tier generation failure plus an alert-tier fail_closed -> alert.
        summary = telemetry.health_summary({
            "generate_nda_requests": 100,
            "generate_nda_failed": 3,
            "active_review_ai_first_fail_closed": 10,
        })
        self.assertEqual(summary["status"], "alert")


if __name__ == "__main__":
    unittest.main()
