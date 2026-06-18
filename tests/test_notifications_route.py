"""HTTP route coverage for the failure-notification feed.

Exercises ``GET /api/notifications`` (list + unread_count, auth gate matching
``/api/matters``) and ``POST /api/notifications/<id>/dismiss`` over the real
``ThreadingHTTPServer`` + ``QuietHandler`` harness (mirrors
``tests/test_dashboard_assistant_route.py``). The notification store writes to its
own DATA_DIR, so each test points ``notification_store`` at a fresh temp dir.
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
from pathlib import Path
from unittest.mock import patch

from nda_automation import notification_store
from nda_automation import server as server_module
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.server import NdaAutomationHandler


class QuietHandler(NdaAutomationHandler):
    def log_message(self, *args, **kwargs):
        return


class NotificationsRouteTests(unittest.TestCase):
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
        QuietHandler.matter_repository = InMemoryMatterRepository()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._store_patches = [
            patch.object(notification_store, "DATA_DIR", root),
            patch.object(notification_store, "NOTIFICATIONS_PATH", root / "notifications.json"),
        ]
        for p in self._store_patches:
            p.start()

    def tearDown(self):
        for p in self._store_patches:
            p.stop()
        self._tmp.cleanup()
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

    class _EnvPatch:
        def __init__(self, env):
            self.env = env
            self.previous = {}

        def __enter__(self):
            for key, value in self.env.items():
                self.previous[key] = os.environ.get(key)
                os.environ[key] = value
            return self

        def __exit__(self, *exc):
            for key, value in self.previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # --- auth gate: matches /api/matters (authenticated, not admin-only) ---
    def test_list_requires_auth(self):
        with self._EnvPatch(self.auth_env()):
            status, payload, _ = self.request("GET", "/api/notifications")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], server_module.AUTH_REQUIRED_MESSAGE)

    def test_list_succeeds_for_any_authenticated_operator(self):
        # A non-admin authenticated user can read the feed (same gate as the
        # inbound-email toasts on /api/matters), NOT an admin-only 403.
        notification_store.emit_event(
            source="drive", severity="error", title="Drive archive failed", dedupe_key="d1"
        )
        with self._EnvPatch(self.auth_env()):
            status, payload, _ = self.request(
                "GET", "/api/notifications", headers=self.basic_auth_headers()
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["unread_count"], 1)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["title"], "Drive archive failed")

    def test_dismiss_marks_event_and_drops_unread(self):
        event = notification_store.emit_event(
            source="ai", severity="error", title="AI key invalid", dedupe_key="ai_key_invalid"
        )
        with self._EnvPatch(self.auth_env()):
            status, payload, _ = self.request(
                "POST",
                f"/api/notifications/{event['id']}/dismiss",
                headers=self.basic_auth_headers(),
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["event"]["status"], "dismissed")
        self.assertEqual(notification_store.unread_count(), 0)

    def test_dismiss_unknown_id_404(self):
        with self._EnvPatch(self.auth_env()):
            status, payload, _ = self.request(
                "POST",
                "/api/notifications/does-not-exist/dismiss",
                headers=self.basic_auth_headers(),
            )
        self.assertEqual(status, 404, payload)

    def test_dismiss_requires_auth(self):
        with self._EnvPatch(self.auth_env()):
            status, _payload, _ = self.request(
                "POST", "/api/notifications/whatever/dismiss"
            )
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
