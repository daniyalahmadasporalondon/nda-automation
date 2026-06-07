"""Tests for the Save-NDA-to-Google-Drive backend.

The Drive API is always faked — no test makes a live Google call. The fake Drive
service mirrors how the Gmail tests mock the service builder: tests inject a fake
into ``drive_integration._drive_service`` (the service builder) so they can
record the ``files().create`` arguments without any network.
"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from nda_automation import app_settings
from nda_automation import artifact_service
from nda_automation import drive_integration
from nda_automation import gmail_integration
from nda_automation import matter_store
from nda_automation import server as server_module
from nda_automation import telemetry
from nda_automation import user_store
from nda_automation.server import NdaAutomationHandler


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# --- Drive API fakes -------------------------------------------------------
class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeFiles:
    def __init__(self, recorder, file_result, error=None):
        self._recorder = recorder
        self._file_result = file_result
        self._error = error

    def create(self, *, body, media_body, fields):
        self._recorder["create_calls"].append(
            {"body": body, "media_body": media_body, "fields": fields}
        )
        if self._error is not None:
            raise self._error
        return _FakeExecutable(self._file_result)


class _FakeAbout:
    def __init__(self, about_result):
        self._about_result = about_result

    def get(self, *, fields):
        return _FakeExecutable(self._about_result)


class FakeDriveService:
    """Records the create call args; never touches the network."""

    def __init__(self, *, file_result=None, about_result=None, error=None):
        self.recorder = {"create_calls": []}
        self._file_result = file_result if file_result is not None else {
            "id": "file_123",
            "webViewLink": "https://drive.google.com/file/d/file_123/view",
        }
        self._about_result = about_result if about_result is not None else {
            "user": {"emailAddress": "owner@example.com"}
        }
        self._error = error

    def files(self):
        return _FakeFiles(self.recorder, self._file_result, error=self._error)

    def about(self):
        return _FakeAbout(self._about_result)


def _make_rate_limit_error():
    class _Resp:
        status = 429

    error = Exception("rateLimitExceeded")
    error.resp = _Resp()
    error.content = json.dumps(
        {"error": {"message": "Rate Limit Exceeded", "errors": [{"reason": "rateLimitExceeded"}]}}
    ).encode("utf-8")
    return error


# --- Pure integration-layer tests ------------------------------------------
class DriveIntegrationModuleTests(unittest.TestCase):
    def test_drive_role_uses_only_drive_file_scope(self):
        self.assertEqual(
            gmail_integration.GMAIL_OAUTH_SCOPES_BY_ROLE["drive"],
            ("https://www.googleapis.com/auth/drive.file",),
        )
        # The drive role resolves to exactly itself and is NOT swept into "all"
        # (connecting Gmail must never grant Drive, and vice versa).
        self.assertEqual(gmail_integration._gmail_oauth_roles_for_role("drive"), ("drive",))
        self.assertEqual(gmail_integration._gmail_oauth_roles_for_role("all"), ("inbound", "outbound"))
        self.assertNotIn(
            "https://www.googleapis.com/auth/drive",
            gmail_integration._gmail_oauth_scopes_for_role("drive"),
        )

    def test_upload_docx_records_create_args(self):
        service = FakeDriveService()
        result = drive_integration.upload_docx_to_drive(
            file_bytes=b"PK\x03\x04 docx bytes",
            filename="NDA - Acme.docx",
            folder_id="folder_abc",
            service=service,
        )
        self.assertEqual(len(service.recorder["create_calls"]), 1)
        call = service.recorder["create_calls"][0]
        self.assertEqual(call["body"]["name"], "NDA - Acme.docx")
        self.assertEqual(call["body"]["parents"], ["folder_abc"])
        self.assertEqual(call["media_body"].mimetype(), DOCX_MIME)
        self.assertEqual(call["fields"], "id,webViewLink")
        self.assertEqual(
            result,
            {
                "file_id": "file_123",
                "web_link": "https://drive.google.com/file/d/file_123/view",
                "filename": "NDA - Acme.docx",
                "folder_id": "folder_abc",
            },
        )

    def test_upload_docx_without_folder_uses_empty_parents(self):
        service = FakeDriveService()
        result = drive_integration.upload_docx_to_drive(
            file_bytes=b"bytes",
            filename="NDA.docx",
            folder_id="",
            service=service,
        )
        self.assertEqual(service.recorder["create_calls"][0]["body"]["parents"], [])
        self.assertEqual(result["folder_id"], "")

    def test_upload_docx_rejects_empty_bytes(self):
        with self.assertRaises(drive_integration.DriveIntegrationError):
            drive_integration.upload_docx_to_drive(
                file_bytes=b"",
                filename="NDA.docx",
                service=FakeDriveService(),
            )

    def test_upload_docx_maps_rate_limit(self):
        service = FakeDriveService(error=_make_rate_limit_error())
        with self.assertRaises(drive_integration.DriveRateLimitError):
            drive_integration.upload_docx_to_drive(
                file_bytes=b"bytes",
                filename="NDA.docx",
                service=service,
            )

    def test_drive_account_email_best_effort(self):
        service = FakeDriveService(about_result={"user": {"emailAddress": "me@example.com"}})
        self.assertEqual(drive_integration.drive_account_email(service=service), "me@example.com")

    def test_drive_account_email_swallows_errors(self):
        class _Boom:
            def about(self):
                raise RuntimeError("nope")

        self.assertEqual(drive_integration.drive_account_email(service=_Boom()), "")

    def test_drive_service_missing_token_raises_not_connected(self):
        with patch.object(
            gmail_integration,
            "_credentials_for_role",
            side_effect=gmail_integration.GmailIntegrationError("no token"),
        ):
            with self.assertRaises(drive_integration.DriveNotConnectedError):
                drive_integration._drive_service("user-1")


# --- app_settings drive settings tests -------------------------------------
class DriveSettingsTests(unittest.TestCase):
    @contextmanager
    def _isolated_data_dir(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with patch.object(matter_store, "DATA_DIR", matter_store.Path(data_dir)):
                yield

    def test_default_drive_settings(self):
        with self._isolated_data_dir():
            self.assertEqual(
                app_settings.drive_settings(),
                {"enabled": False, "folder_id": "", "folder_name": ""},
            )

    def test_update_drive_settings_persists(self):
        with self._isolated_data_dir():
            updated = app_settings.update_drive_settings(
                {"enabled": True, "folder_id": "folder_XYZ-123", "folder_name": "Signed NDAs"}
            )
            self.assertEqual(
                updated,
                {"enabled": True, "folder_id": "folder_XYZ-123", "folder_name": "Signed NDAs"},
            )
            # A fresh read returns the persisted values.
            self.assertEqual(app_settings.drive_settings(), updated)

    def test_update_drive_settings_records_audit_event(self):
        with self._isolated_data_dir():
            app_settings.update_drive_settings({"enabled": True, "folder_id": "abc123"})
            # The route records the audit event; here we exercise the lower-level
            # audit recorder the route reuses.
            app_settings.record_settings_audit_event(
                {
                    "recorded_at": "2026-06-07T00:00:00+00:00",
                    "actor": "admin",
                    "action": "drive_settings_update",
                    "changes": [{"setting": "drive.enabled", "before": "false", "after": "true"}],
                }
            )
            history = app_settings.settings_audit_history()
            self.assertTrue(history)
            self.assertEqual(history[0]["action"], "drive_settings_update")

    def test_folder_id_rejects_traversal_and_urls(self):
        with self._isolated_data_dir():
            for bad in ("../etc", "a/b", "https://drive.google.com/x", "id with space"):
                with self.assertRaises(app_settings.AppSettingsError):
                    app_settings.update_drive_settings({"folder_id": bad})

    def test_folder_id_accepts_plain_id(self):
        with self._isolated_data_dir():
            updated = app_settings.update_drive_settings({"folder_id": "1aZ_b-2CdE"})
            self.assertEqual(updated["folder_id"], "1aZ_b-2CdE")


# --- HTTP route tests (live server, faked Drive) ---------------------------
class QuietDriveHandler(NdaAutomationHandler):
    def log_message(self, format, *args):
        return


class DriveRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietDriveHandler)
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

    def request(self, method, path, body=None, headers=None):
        request_headers = dict(headers or {})
        request_body = body
        if isinstance(body, dict):
            request_body = json.dumps(body).encode("utf-8")
            request_headers = {"Content-Type": "application/json", **request_headers}
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

    def matter_store_patches(self, data_dir):
        data_path = matter_store.Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        )

    def _make_matter_with_roles(self, roles, *, owner_user_id=""):
        """Create a matter and register one artifact per (role, bytes) pair."""
        first_role, first_bytes = roles[0]
        matter = matter_store.create_matter(
            source_filename="counterparty.docx",
            document_bytes=first_bytes,
            extracted_text="",
            review_result={},
            triage={},
            source_type="upload",
            owner_user_id=owner_user_id,
        )
        matter_id = matter["id"]
        for role, document_bytes in roles:
            artifact_service.add_artifact(
                matter_id,
                source="generated",
                actor="human",
                role=role,
                document_bytes=document_bytes,
                owner_user_id=owner_user_id,
            )
        return matter_id

    def test_upload_matter_not_connected_returns_409(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_id = self._make_matter_with_roles([("original", b"orig docx")])
                with patch.object(drive_integration, "drive_connected", return_value=False):
                    status, payload, _headers = self.request(
                        "POST", "/api/drive/upload-matter", {"matter_id": matter_id}
                    )
        self.assertEqual(status, 409)
        self.assertTrue(payload["needs_connect"])
        self.assertEqual(payload["connect_url"], "/auth/drive/start")
        self.assertEqual(telemetry.snapshot()["counters"].get("drive_upload_failed"), 1)

    def test_upload_matter_unknown_matter_returns_400(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload, _headers = self.request(
                    "POST", "/api/drive/upload-matter", {"matter_id": "does_not_exist"}
                )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "Matter not found.")

    def test_upload_matter_no_document_returns_400(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                # Matter exists but has no registered artifacts at all.
                matter = matter_store.create_matter(
                    source_filename="x.docx",
                    document_bytes=b"x",
                    extracted_text="",
                    review_result={},
                    triage={},
                    source_type="upload",
                )
                with patch.object(drive_integration, "drive_connected", return_value=True):
                    status, payload, _headers = self.request(
                        "POST", "/api/drive/upload-matter", {"matter_id": matter["id"]}
                    )
        self.assertEqual(status, 400)
        self.assertIn("no document", payload["error"].lower())

    def test_upload_matter_prefers_reviewed_over_generated_over_original(self):
        fake = FakeDriveService()
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_id = self._make_matter_with_roles([
                    ("original", b"ORIGINAL bytes"),
                    ("generated", b"GENERATED bytes"),
                    ("reviewed", b"REVIEWED bytes"),
                ])
                with patch.object(drive_integration, "drive_connected", return_value=True):
                    with patch.object(drive_integration, "_drive_service", return_value=fake):
                        status, payload, _headers = self.request(
                            "POST", "/api/drive/upload-matter", {"matter_id": matter_id}
                        )
        self.assertEqual(status, 200)
        # Reviewed wins: the bytes uploaded are the reviewed artifact's.
        media = fake.recorder["create_calls"][0]["media_body"]
        uploaded_bytes = media.getbytes(0, media.size())
        self.assertEqual(uploaded_bytes, b"REVIEWED bytes")
        self.assertEqual(payload["uploaded"]["file_id"], "file_123")
        self.assertIn("matter", payload)
        self.assertEqual(telemetry.snapshot()["counters"].get("drive_upload_succeeded"), 1)

    def test_upload_matter_explicit_role_overrides_preference(self):
        fake = FakeDriveService()
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_id = self._make_matter_with_roles([
                    ("original", b"ORIGINAL bytes"),
                    ("reviewed", b"REVIEWED bytes"),
                ])
                with patch.object(drive_integration, "drive_connected", return_value=True):
                    with patch.object(drive_integration, "_drive_service", return_value=fake):
                        status, _payload, _headers = self.request(
                            "POST",
                            "/api/drive/upload-matter",
                            {"matter_id": matter_id, "role": "original"},
                        )
        self.assertEqual(status, 200)
        media = fake.recorder["create_calls"][0]["media_body"]
        uploaded_bytes = media.getbytes(0, media.size())
        self.assertEqual(uploaded_bytes, b"ORIGINAL bytes")

    def test_upload_matter_unknown_role_returns_400(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_id = self._make_matter_with_roles([("original", b"orig")])
                status, payload, _headers = self.request(
                    "POST",
                    "/api/drive/upload-matter",
                    {"matter_id": matter_id, "role": "bogus"},
                )
        self.assertEqual(status, 400)
        self.assertIn("role", payload["error"].lower())

    def test_drive_status_reports_folder_and_enabled(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                app_settings.update_drive_settings(
                    {"enabled": True, "folder_id": "folder_1", "folder_name": "NDAs"}
                )
                status, payload, _headers = self.request("GET", "/api/drive/status")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload.keys()), {"connected", "account", "folder", "enabled"})
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["folder"], {"id": "folder_1", "name": "NDAs"})
        # No Google session in the loopback test client, so not connected.
        self.assertFalse(payload["connected"])

    def test_drive_settings_update_persists_via_route(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload, _headers = self.request(
                    "POST",
                    "/api/admin/drive-settings",
                    {"enabled": True, "folder_id": "folder_99", "folder_name": "Vault"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    payload["drive"],
                    {"enabled": True, "folder_id": "folder_99", "folder_name": "Vault"},
                )
                # Audit event recorded for the admin change.
                history = app_settings.settings_audit_history()
        self.assertTrue(any(event["action"] == "drive_settings_update" for event in history))

    def test_drive_settings_update_rejects_bad_folder_id(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload, _headers = self.request(
                    "POST",
                    "/api/admin/drive-settings",
                    {"folder_id": "../escape"},
                )
        self.assertEqual(status, 400)
        self.assertIn("folder id", payload["error"].lower())

    def test_drive_settings_update_denies_non_admin_google_user(self):
        # With auth required and the caller a per-user Google account (not in the
        # admin list), /api/admin/drive-settings must be refused with 403.
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
                user = user_store.upsert_google_user({
                    "sub": "drive-user",
                    "email": "user@example.com",
                    "name": "Drive User",
                    "picture": "",
                })
                token = user_store.create_session(user["id"])
                session_headers = {"Cookie": f"{user_store.SESSION_COOKIE_NAME}={token}"}
                with patch.dict(os.environ, auth_env):
                    status, payload, _headers = self.request(
                        "POST",
                        "/api/admin/drive-settings",
                        {"enabled": True},
                        headers=session_headers,
                    )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], server_module.ADMIN_REQUIRED_MESSAGE)

    def test_drive_connect_start_requires_google_session(self):
        # Loopback dev: no Google session means gmail_owner_user_id is empty, so
        # the connect flow is refused (it needs a signed-in Google user).
        status, payload, _headers = self.request("GET", "/auth/drive/start")
        self.assertEqual(status, 403)
        self.assertIn("Sign in with Google", payload["error"])

    def test_drive_connect_start_redirects_to_google_consent(self):
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
                user = user_store.upsert_google_user({
                    "sub": "drive-connect",
                    "email": "connect@example.com",
                    "name": "Connect User",
                    "picture": "",
                })
                token = user_store.create_session(user["id"])
                session_headers = {"Cookie": f"{user_store.SESSION_COOKIE_NAME}={token}"}
                connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
                with patch.dict(os.environ, auth_env):
                    connection.request("GET", "/auth/drive/start", headers=session_headers)
                    response = connection.getresponse()
                    response.read()
                    location = response.getheader("Location", "")
                    status = response.status
                connection.close()
        self.assertIn(status, (302, 303, 307))
        self.assertTrue(location.startswith("https://accounts.google.com"))
        self.assertIn("scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.file", location)
        self.assertNotIn("gmail", location)


if __name__ == "__main__":
    unittest.main()
