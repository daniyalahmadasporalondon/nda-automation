"""HTTP route coverage for the Corpus tab backend.

Exercises ``GET /api/corpus`` (owner-scoping, reconciliation, empty wrapper) and
``GET /api/corpus/artifacts/<matter_id>/<artifact_id>`` (owned download 200,
other-tenant 404) over the real ``ThreadingHTTPServer`` + ``QuietHandler``
harness (mirrors ``tests/test_dashboard_assistant_route.py``). A stateful
``FakeDriveService`` from ``test_corpus`` is injected via
``QuietHandler.corpus_drive_service`` so the Drive crawl runs without a network.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer

from nda_automation import corpus_index, drive_integration
from nda_automation import server as server_module
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.server import NdaAutomationHandler

from test_corpus import (
    FakeDriveService,
    _build_drive_tree,
    _register_original_artifact,
    _seed_matter,
    _summary_for,
)


class QuietHandler(NdaAutomationHandler):
    def log_message(self, *args, **kwargs):
        return


class CorpusRouteTests(unittest.TestCase):
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
        corpus_index.invalidate_cache()
        self.repository = InMemoryMatterRepository()
        QuietHandler.matter_repository = self.repository

    def tearDown(self):
        corpus_index.invalidate_cache()
        for attr in ("matter_repository", "corpus_drive_service"):
            if hasattr(QuietHandler, attr):
                delattr(QuietHandler, attr)

    # --- request plumbing ---
    def request(self, method, path, headers=None):
        request_headers = dict(headers or {})
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(method, path, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            if "application/json" in content_type:
                payload = json.loads(raw.decode("utf-8"))
            else:
                payload = raw
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def basic_auth_headers(self, username):
        token = base64.b64encode(f"{username}:secret".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def auth_env(self, username):
        return {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": username,
            "NDA_AUTH_PASSWORD": "secret",
        }

    # 1a. Requires auth.
    def test_requires_auth(self):
        with _EnvPatch(self.auth_env("nda-admin")):
            status, payload, _ = self.request("GET", "/api/corpus")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], server_module.AUTH_REQUIRED_MESSAGE)

    # 1b. Owner-scoping: A sees only A's matters; B's never appear.
    def test_corpus_is_owner_scoped(self):
        _seed_matter(self.repository, owner="owner-a", title="Alpha NDA", subject="Alpha co")
        _seed_matter(self.repository, owner="owner-b", title="Bravo NDA", subject="Bravo co")

        with _EnvPatch(self.auth_env("owner-a")):
            status, payload, _ = self.request(
                "GET", "/api/corpus", headers=self.basic_auth_headers("owner-a")
            )

        self.assertEqual(status, 200, payload)
        titles = [m["title"] for g in payload["groups"] for m in g["matters"]]
        self.assertEqual(titles, ["Alpha NDA"])
        self.assertEqual(payload["matter_count"], 1)
        self.assertIn("drive", payload)
        self.assertIn("reconciled", payload["drive"])

    # 2. Reconciliation incl. a Drive-only matter, over HTTP.
    def test_reconciliation_surfaces_drive_only_matter(self):
        matter = _seed_matter(self.repository, owner="owner-a", title="Acme NDA", subject="Acme Corp")
        fake = FakeDriveService()
        _build_drive_tree(fake, counterparty="Acme Corp", summary=_summary_for(matter["id"], counterparty="Acme Corp"))
        _build_drive_tree(
            fake,
            counterparty="Globex Inc",
            summary=_summary_for("matter_driveonly42", counterparty="Globex Inc"),
        )
        QuietHandler.corpus_drive_service = fake

        with _EnvPatch(self.auth_env("owner-a")):
            status, payload, _ = self.request(
                "GET", "/api/corpus", headers=self.basic_auth_headers("owner-a")
            )

        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["drive"]["reconciled"])
        matters = {m["matter_id"]: m for g in payload["groups"] for m in g["matters"]}
        self.assertEqual(matters[matter["id"]]["source"], "both")
        self.assertEqual(matters["matter_driveonly42"]["source"], "drive")
        self.assertFalse(matters["matter_driveonly42"]["in_app"])
        self.assertEqual(matters["matter_driveonly42"]["open_matter_url"], "")

    # 4. Empty corpus -> 200 with empty wrapper, no crash.
    def test_empty_corpus_returns_200(self):
        with _EnvPatch(self.auth_env("owner-a")):
            status, payload, _ = self.request(
                "GET", "/api/corpus", headers=self.basic_auth_headers("owner-a")
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["matter_count"], 0)
        self.assertEqual(payload["counterparty_count"], 0)

    # 1c. Artifact download: 200 for owned, 404 for another tenant's matter.
    def test_artifact_download_owned_streams_bytes(self):
        matter = _seed_matter(self.repository, owner="owner-a", title="Downloadable NDA")
        artifact = _register_original_artifact(self.repository, matter, "owner-a")

        with _EnvPatch(self.auth_env("owner-a")):
            status, payload, headers = self.request(
                "GET",
                f"/api/corpus/artifacts/{matter['id']}/{artifact.id}",
                headers=self.basic_auth_headers("owner-a"),
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, b"PK\x03\x04 fake docx")
        self.assertEqual(headers.get("Content-Type"), drive_integration.DOCX_MIME)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))

    def test_artifact_download_404_for_other_tenant_matter(self):
        matter_b = _seed_matter(self.repository, owner="owner-b", title="Bravo NDA")
        artifact_b = _register_original_artifact(self.repository, matter_b, "owner-b")

        # owner-a requests owner-b's matter+artifact: must 404 (never owned/visible).
        with _EnvPatch(self.auth_env("owner-a")):
            status, payload, _ = self.request(
                "GET",
                f"/api/corpus/artifacts/{matter_b['id']}/{artifact_b.id}",
                headers=self.basic_auth_headers("owner-a"),
            )

        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_artifact_download_404_for_unknown_artifact(self):
        matter = _seed_matter(self.repository, owner="owner-a", title="No-artifact NDA")

        with _EnvPatch(self.auth_env("owner-a")):
            status, payload, _ = self.request(
                "GET",
                f"/api/corpus/artifacts/{matter['id']}/artifact_missing",
                headers=self.basic_auth_headers("owner-a"),
            )

        self.assertEqual(status, 404)
        self.assertIn("error", payload)


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
