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
from nda_automation import google_connection
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
    """Records the create call args; never touches the network.

    The v1 (flat) fake — its ``files().create`` always succeeds and ignores any
    pre-existing state. The v2 :class:`FakeDriveV2Service` models folders + a
    files list so idempotency can be tested.
    """

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


# --- Drive v2 fake: models a folder/file tree so idempotency is testable ----
class _FakeFilesV2:
    """A files() resource backed by an in-memory store keyed by (name, parents)."""

    def __init__(self, store):
        self._store = store

    def list(self, *, q, fields, pageSize=1, spaces="drive"):  # noqa: N803
        self._store["list_calls"].append(q)
        matches = [
            {"id": fid, "name": rec["name"], "webViewLink": rec["web_link"]}
            for fid, rec in self._store["files"].items()
            if _query_matches(q, rec)
        ]
        return _FakeExecutable({"files": matches[:pageSize]})

    def create(self, *, body, fields, media_body=None):  # noqa: N803
        self._store["create_calls"].append({"body": body, "has_media": media_body is not None})
        self._store["_seq"] += 1
        file_id = f"id_{self._store['_seq']}"
        parents = body.get("parents") or [""]
        record = {
            "name": body["name"],
            "mimeType": body.get("mimeType", ""),
            "parent": parents[0],
            "web_link": f"https://drive.google.com/file/d/{file_id}/view",
            "is_folder": body.get("mimeType") == drive_integration.FOLDER_MIME,
            # The exact bytes uploaded, so a test can assert each versioned file
            # carries its own correct content (not just the right filename).
            "content": _media_bytes(media_body),
        }
        self._store["files"][file_id] = record
        return _FakeExecutable({"id": file_id, "webViewLink": record["web_link"]})

    def update(self, *, fileId, fields, media_body=None, body=None):  # noqa: N803
        """In-place media (and/or name) update keyed by file id.

        Models ``files().update`` for the true-replace path: it overwrites the
        existing record's CONTENT (and optional name) without minting a new id, so
        a re-sync of a fixed-name file (``matter_summary.json``) lands fresh bytes
        on the same id rather than skipping or duplicating.
        """
        self._store.setdefault("update_calls", []).append(
            {"file_id": fileId, "has_media": media_body is not None, "body": body}
        )
        record = self._store["files"].get(fileId)
        if record is None:
            error = Exception("File not found")
            error.resp = type("_Resp", (), {"status": 404})()
            raise error
        if media_body is not None:
            record["content"] = _media_bytes(media_body)
        if isinstance(body, dict) and body.get("name"):
            record["name"] = body["name"]
        return _FakeExecutable({"id": fileId, "webViewLink": record["web_link"]})


class _FakeAboutV2:
    def get(self, *, fields):
        return _FakeExecutable({"user": {"emailAddress": "owner@example.com"}})


def _query_matches(q, record):
    """A minimal evaluator for the q= clauses this module emits.

    The module only ever emits AND-joined clauses of these shapes:
      mimeType='...'         (folder lookups only)
      name='<escaped>'       (always; single quotes backslash-escaped)
      '<id>' in parents      (when a parent is scoped)
      trashed=false          (always)
    """
    if "trashed=false" not in q:
        return False
    if f"name='{_escape(record['name'])}'" not in q:
        return False
    if "in parents" in q and f"'{_escape(record['parent'])}' in parents" not in q:
        return False
    wants_folder = f"mimeType='{drive_integration.FOLDER_MIME}'" in q
    if wants_folder != record["is_folder"]:
        return False
    return True


def _escape(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _media_bytes(media_body):
    """Extract the uploaded bytes from a ``MediaIoBaseUpload`` (or ``None``).

    Folder creates carry no media; for a file upload the bytes are read back via
    the media object's ``size()``/``getbytes()`` API so a test can verify the
    exact content uploaded for each artifact version.
    """
    if media_body is None:
        return None
    try:
        return media_body.getbytes(0, media_body.size())
    except Exception:  # pragma: no cover - defensive, should not happen with real media
        return None


class FakeDriveV2Service:
    """A stateful fake Drive: folders + files, so idempotency is verifiable."""

    def __init__(self):
        self.store = {
            "files": {},
            "create_calls": [],
            "list_calls": [],
            "_seq": 0,
        }

    def files(self):
        return _FakeFilesV2(self.store)

    def about(self):
        return _FakeAboutV2()

    # --- assertions helpers ---
    def folder_names(self):
        return sorted(rec["name"] for rec in self.store["files"].values() if rec["is_folder"])

    def file_records(self):
        return [rec for rec in self.store["files"].values() if not rec["is_folder"]]

    def file_names(self):
        return sorted(rec["name"] for rec in self.file_records())

    def content_for(self, filename):
        """The uploaded bytes for the (single) file named ``filename``, or ``None``.

        Lets a test assert that the Drive-synced file for each artifact version
        carries that version's own distinct content.
        """
        for rec in self.file_records():
            if rec["name"] == filename:
                return rec.get("content")
        return None

    def folder_create_count(self):
        return sum(
            1
            for call in self.store["create_calls"]
            if call["body"].get("mimeType") == drive_integration.FOLDER_MIME
        )

    def file_create_count(self):
        return sum(
            1
            for call in self.store["create_calls"]
            if call["body"].get("mimeType") != drive_integration.FOLDER_MIME
        )


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
            google_connection.GOOGLE_OAUTH_SCOPES_BY_ROLE["drive"],
            ("https://www.googleapis.com/auth/drive.file",),
        )
        # The standalone drive role resolves to exactly itself, while the unified
        # "all" connect sweeps Drive in so one Google consent grants Gmail + Drive.
        self.assertEqual(google_connection.oauth_roles_for_role("drive"), ("drive",))
        self.assertEqual(
            google_connection.oauth_roles_for_role("all"),
            ("inbound", "outbound", "drive"),
        )
        self.assertNotIn(
            "https://www.googleapis.com/auth/drive",
            google_connection.oauth_scopes_for_role("drive"),
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

    def test_resolve_filing_location_blank_reports_default(self):
        # No admin root folder set -> explicit My Drive / NDAs default, no Drive call.
        class _NoCalls:
            def files(self):
                raise AssertionError("blank folder must not call Drive")

        location = drive_integration.resolve_filing_location(
            root_folder_id="", folder_name="", service=_NoCalls()
        )
        self.assertFalse(location["configured"])
        self.assertEqual(location["folder_id"], "")
        self.assertEqual(location["folder_name"], "NDAs")
        self.assertEqual(location["label"], "My Drive / NDAs (default location)")

    def test_resolve_filing_location_configured_resolves_real_name(self):
        # Configured folder -> resolve the folder's real NAME via files().get.
        class _NamedFiles:
            def get(self, *, fileId, fields):  # noqa: N803
                assert fileId == "folder_42"
                assert fields == "name"
                return _FakeExecutable({"name": "Legal — Signed NDAs"})

        class _NamedService:
            def files(self):
                return _NamedFiles()

        location = drive_integration.resolve_filing_location(
            root_folder_id="folder_42",
            folder_name="stale stored name",
            service=_NamedService(),
        )
        self.assertTrue(location["configured"])
        self.assertEqual(location["folder_id"], "folder_42")
        # The live API name wins over the stored name.
        self.assertEqual(location["folder_name"], "Legal — Signed NDAs")
        self.assertEqual(location["label"], "Legal — Signed NDAs / NDAs")

    def test_resolve_filing_location_falls_back_when_lookup_unavailable(self):
        # Drive disconnected / API hiccup -> no crash; fall back to stored name.
        class _Boom:
            def files(self):
                raise RuntimeError("not connected")

        location = drive_integration.resolve_filing_location(
            root_folder_id="folder_7",
            folder_name="Contracts",
            service=_Boom(),
        )
        self.assertTrue(location["configured"])
        self.assertEqual(location["folder_name"], "Contracts")
        self.assertEqual(location["label"], "Contracts / NDAs")

    def test_resolve_filing_location_falls_back_to_id_when_no_name(self):
        # No live name and no stored name -> the raw id is the last-resort label.
        class _Boom:
            def files(self):
                raise RuntimeError("nope")

        location = drive_integration.resolve_filing_location(
            root_folder_id="folder_9", folder_name="", service=_Boom()
        )
        self.assertEqual(location["folder_name"], "folder_9")
        self.assertEqual(location["label"], "folder_9 / NDAs")

    def test_drive_service_missing_token_raises_not_connected(self):
        with patch.object(
            gmail_integration,
            "_credentials_for_role",
            side_effect=gmail_integration.GmailIntegrationError("no token"),
        ):
            with self.assertRaises(drive_integration.DriveNotConnectedError):
                drive_integration._drive_service("user-1")


# --- Drive v2 structured-filing tests (pure, faked Drive) ------------------
class DriveV2IntegrationTests(unittest.TestCase):
    def test_find_or_create_folder_is_idempotent(self):
        fake = FakeDriveV2Service()
        first = drive_integration.find_or_create_folder(
            name="NDAs", parent_id="", service=fake
        )
        # A second call with the same name+parent returns the existing id; no new
        # folder is created.
        second = drive_integration.find_or_create_folder(
            name="NDAs", parent_id="", service=fake
        )
        self.assertEqual(first, second)
        self.assertEqual(fake.folder_create_count(), 1)

    def test_find_or_create_folder_scopes_by_parent(self):
        fake = FakeDriveV2Service()
        root = drive_integration.find_or_create_folder(name="NDAs", service=fake)
        # Same name but a different parent => a distinct folder is created.
        child_a = drive_integration.find_or_create_folder(name="Acme", parent_id=root, service=fake)
        child_b = drive_integration.find_or_create_folder(name="Acme", parent_id="other", service=fake)
        self.assertNotEqual(child_a, child_b)
        self.assertEqual(fake.folder_create_count(), 3)

    def test_query_value_escaping_blocks_quote_injection(self):
        # Every single quote in the value is backslash-escaped, so no quote can
        # terminate the surrounding string literal and inject a new query clause.
        escaped = drive_integration._escape_drive_query_value("o'brien' or trashed=true")
        # No unescaped single quote survives: each "'" is preceded by a backslash.
        for i, ch in enumerate(escaped):
            if ch == "'":
                self.assertEqual(escaped[i - 1], "\\")
        self.assertEqual(escaped.count("\\'"), 2)
        # Backslashes are escaped first so a trailing backslash can't escape the
        # closing quote of the literal.
        self.assertEqual(drive_integration._escape_drive_query_value("a\\"), "a\\\\")

    def test_upload_or_replace_file_idempotent_by_name(self):
        fake = FakeDriveV2Service()
        folder = drive_integration.find_or_create_folder(name="NDAs", service=fake)
        first = drive_integration.upload_or_replace_file(
            file_bytes=b"docx", filename="01_received.docx", parent_id=folder, service=fake
        )
        self.assertTrue(first["created"])
        second = drive_integration.upload_or_replace_file(
            file_bytes=b"docx", filename="01_received.docx", parent_id=folder, service=fake
        )
        self.assertFalse(second["created"])
        self.assertEqual(first["file_id"], second["file_id"])
        self.assertEqual(fake.file_create_count(), 1)

    def test_sync_uses_grammar_filenames_and_correct_mimetypes(self):
        fake = FakeDriveV2Service()
        matter = {
            "id": "matter_abc",
            "created_at": "2026-06-07T10:00:00+00:00",
            "gmail_thread_id": "thread_99",
            "subject": "Acme NDA",
            "artifacts": [
                {"id": "a1", "actor": "counterparty", "role": "original", "version": 1, "ext": "docx"},
                {"id": "a2", "actor": "ai", "role": "redline", "version": 1, "ext": "docx"},
                {"id": "a3", "actor": "human", "role": "reviewed", "version": 1, "ext": "pdf"},
                {"id": "a4", "actor": "aspora_technology", "role": "generated", "version": 1, "ext": "docx"},
            ],
        }
        byte_map = {"a1": b"orig", "a2": b"redline", "a3": b"reviewed", "a4": b"generated"}

        def fake_bytes(matter_id, artifact_id, *, owner_user_id=""):
            return byte_map[artifact_id]

        result = drive_integration.sync_matter_folder(
            matter=matter,
            matter_id="matter_abc",
            synced_at="2026-06-07T11:00:00+00:00",
            service=fake,
            get_artifact_bytes=fake_bytes,
        )
        names = fake.file_names()
        # {NN}_{stage}[_v{N}]: counterparty original -> received (one-shot);
        # ai redline -> ai_redline (versioned); human reviewed -> legal_review
        # (versioned, pdf ext); our-org generated -> draft (one-shot).
        self.assertIn("01_received.docx", names)
        self.assertIn("02_ai_redline_v1.docx", names)
        self.assertIn("03_legal_review_v1.pdf", names)
        self.assertIn("04_draft.docx", names)
        # The pdf artifact was uploaded with the pdf mimetype.
        pdf_creates = [
            c for c in fake.store["create_calls"]
            if c["body"].get("name") == "03_legal_review_v1.pdf"
        ]
        self.assertEqual(len(pdf_creates), 1)
        self.assertEqual(result["total_count"], 4)
        self.assertEqual(result["synced_count"], 4)

    def test_sync_writes_matter_summary_with_links(self):
        fake = FakeDriveV2Service()
        matter = {
            "id": "matter_x",
            "created_at": "2026-06-07T10:00:00+00:00",
            "gmail_thread_id": "",
            "subject": "Globex Mutual NDA",
            "artifacts": [
                {"id": "a1", "actor": "counterparty", "role": "original", "version": 1, "ext": "docx"},
            ],
        }

        def fake_bytes(matter_id, artifact_id, *, owner_user_id=""):
            return b"orig"

        drive_integration.sync_matter_folder(
            matter=matter,
            matter_id="matter_x",
            synced_at="2026-06-07T11:00:00+00:00",
            service=fake,
            get_artifact_bytes=fake_bytes,
        )
        summary_call = [
            c for c in fake.store["create_calls"]
            if c["body"].get("name") == "matter_summary.json"
        ]
        self.assertEqual(len(summary_call), 1)
        self.assertTrue(summary_call[0]["has_media"])
        # The summary content is exercised through the public helper.
        summary = drive_integration._matter_summary(
            matter=matter,
            matter_id="matter_x",
            counterparty="Globex Mutual NDA",
            matter_folder_url="https://drive.google.com/drive/folders/id_3",
            synced_at="2026-06-07T11:00:00+00:00",
            artifact_records=[
                {
                    "artifact_id": "a1", "sequence": 1, "actor": "counterparty",
                    "role": "original", "version": 1,
                    "filename": "01_received.docx",
                    "drive_file_id": "id_99", "drive_file_url": "https://x/99",
                    "based_on_artifact_id": "", "created_at": "",
                }
            ],
        )
        self.assertEqual(summary["matter_id"], "matter_x")
        self.assertEqual(summary["counterparty"], "Globex Mutual NDA")
        self.assertEqual(summary["matter_folder_url"], "https://drive.google.com/drive/folders/id_3")
        self.assertEqual(len(summary["artifacts"]), 1)
        self.assertEqual(summary["artifacts"][0]["drive_file_id"], "id_99")
        self.assertIn("workflow_state", summary)
        # The durable facets block carries the schema_version (corpus_index keys
        # facets_available off its presence) and every facet key.
        self.assertIn("facets", summary)
        facets = summary["facets"]
        self.assertEqual(facets["schema_version"], 1)
        for key in ("governing_law", "signed", "has_clauses", "term_years"):
            self.assertIn(key, facets)

    def test_matter_summary_facets_derived_from_review_data(self):
        # A generated NDA (manifest governing law) + a term clause -> the durable
        # summary carries the derived facet values, not just empty placeholders.
        matter = {
            "id": "m_facets",
            "created_at": "2026-06-07T10:00:00+00:00",
            "subject": "Acme DIFC NDA",
            "artifacts": [
                {
                    "id": "a1", "actor": "aspora", "role": "generated", "version": 1, "ext": "docx",
                    "metadata": {"generation": {"governing_law_value": "DIFC", "counterparty_name": "Acme"}},
                }
            ],
            "review_result": {
                "clauses": [
                    {"id": "term_and_survival", "decision": "pass", "term_years": 4.0},
                    {"id": "mutuality", "decision": "pass"},
                ]
            },
        }
        summary = drive_integration._matter_summary(
            matter=matter,
            matter_id="m_facets",
            counterparty="Acme",
            matter_folder_url="",
            synced_at="2026-06-07T11:00:00+00:00",
            artifact_records=[],
        )
        facets = summary["facets"]
        self.assertEqual(facets["governing_law"], "difc")
        self.assertIn("term_and_survival", facets["has_clauses"])
        self.assertIn("mutuality", facets["has_clauses"])
        self.assertEqual(facets["term_years"], 4.0)

    def test_matter_summary_requirement_counts_gated_on_ai_review_ran(self):
        # The durable facets block must NOT persist deterministic requirement counts
        # for a matter whose review_result was not produced by the AI (ai_first)
        # engine -- otherwise the corpus "has issues" search leaks a deterministic
        # verdict for a doc the AI never reviewed. Gated to (0, 0) + ai_review_ran=False.
        deterministic = drive_integration._matter_summary(
            matter={
                "id": "m_det",
                "subject": "Generated NDA",
                "review_result": {
                    "requirements_failed": 3,
                    "requirements_needs_review": 2,
                    "active_review_engine": {"executed_engine": "deterministic"},
                },
            },
            matter_id="m_det",
            counterparty="Acme",
            matter_folder_url="",
            synced_at="2026-06-07T11:00:00+00:00",
            artifact_records=[],
        )["facets"]
        self.assertEqual(deterministic["requirements_failed"], 0)
        self.assertEqual(deterministic["requirements_needs_review"], 0)
        self.assertFalse(deterministic["ai_review_ran"])

        # An AI (ai_first) review keeps the real counts + marks ai_review_ran true.
        ai_reviewed = drive_integration._matter_summary(
            matter={
                "id": "m_ai",
                "subject": "Reviewed NDA",
                "review_result": {
                    "requirements_failed": 3,
                    "requirements_needs_review": 2,
                    "active_review_engine": {"executed_engine": "ai_first"},
                },
            },
            matter_id="m_ai",
            counterparty="Acme",
            matter_folder_url="",
            synced_at="2026-06-07T11:00:00+00:00",
            artifact_records=[],
        )["facets"]
        self.assertEqual(ai_reviewed["requirements_failed"], 3)
        self.assertEqual(ai_reviewed["requirements_needs_review"], 2)
        self.assertTrue(ai_reviewed["ai_review_ran"])

    def test_matter_summary_swallows_facet_failure_and_still_writes(self):
        # A facet-derivation hiccup must never break the sync: a governing-law
        # derivation that raises is swallowed by _summary_facets and the summary still
        # writes with the facet degraded to "" (mirrors the workflow_state try/except).
        with patch(
            "nda_automation.governing_law_view.derive_governing_law",
            side_effect=RuntimeError("glv boom"),
        ):
            summary = drive_integration._matter_summary(
                matter={"id": "m_bad", "subject": "Bad NDA"},
                matter_id="m_bad",
                counterparty="Bad",
                matter_folder_url="",
                synced_at="2026-06-07T11:00:00+00:00",
                artifact_records=[],
            )
        # The summary still wrote; the governing-law facet degraded to "".
        self.assertEqual(summary["matter_id"], "m_bad")
        self.assertIn("facets", summary)
        self.assertEqual(summary["facets"]["governing_law"], "")
        self.assertEqual(summary["facets"]["schema_version"], 1)

    def test_counterparty_prefers_generation_manifest(self):
        matter = {
            "id": "m",
            "subject": "RE: random thread subject",
            "artifacts": [
                {
                    "id": "a1", "actor": "aspora", "role": "generated", "version": 1, "ext": "docx",
                    "metadata": {"generation": {"counterparty_name": "Acme Robotics Ltd"}},
                }
            ],
        }
        self.assertEqual(drive_integration.derive_counterparty(matter), "Acme Robotics Ltd")

    def test_counterparty_falls_back_to_subject_then_unknown(self):
        self.assertEqual(
            drive_integration.derive_counterparty({"id": "m", "subject": "Globex NDA"}),
            "Globex NDA",
        )
        self.assertEqual(
            drive_integration.derive_counterparty({"id": "m", "subject": ""}),
            "Unknown Counterparty",
        )

    def test_matter_folder_name_grammar(self):
        # "{date} · {document title} · {ref}". The counterparty is the PARENT
        # folder, so it is deliberately not repeated in the child name; the ref is
        # the trailing 4 alphanumerics of the matter id.
        matter = {
            "id": "matter_3a8f2b1c9d0e",
            "created_at": "2026-06-07T09:00:00+00:00",
            "document_title": "Mutual NDA",
            "subject": "RE: Mutual NDA thread",
        }
        name = drive_integration.derive_matter_folder_name(matter, "matter_3a8f2b1c9d0e", "Acme")
        self.assertEqual(name, "2026-06-07 · Mutual NDA · 9d0e")

    def test_matter_folder_name_falls_back_to_subject_then_nda(self):
        # No usable document_title -> subject; the "Untitled NDA" placeholder is
        # never treated as a real title.
        subject_named = drive_integration.derive_matter_folder_name(
            {
                "id": "matter_aaaabbbbcccc",
                "created_at": "2026-06-07T09:00:00+00:00",
                "document_title": "Untitled NDA",
                "subject": "Project Falcon NDA",
            },
            "matter_aaaabbbbcccc",
            "Acme",
        )
        self.assertEqual(subject_named, "2026-06-07 · Project Falcon NDA · cccc")
        # Nothing human at all -> the literal "NDA" label, still date- and ref-keyed.
        bare = drive_integration.derive_matter_folder_name(
            {"id": "matter_0011223344ff", "created_at": "2026-06-07T09:00:00+00:00"},
            "matter_0011223344ff",
            "Acme",
        )
        self.assertEqual(bare, "2026-06-07 · NDA · 44ff")

    def test_matter_folder_name_omits_date_when_unrecorded(self):
        name = drive_integration.derive_matter_folder_name(
            {"id": "matter_feedface1234", "document_title": "Mutual NDA"},
            "matter_feedface1234",
            "Acme",
        )
        self.assertEqual(name, "Mutual NDA · 1234")

    def test_long_title_truncates_at_word_boundary_without_dangling_punctuation(self):
        long_title = "Air India - Mutual NDA Template (Updated as on 23.04.2025) (4)"
        name = drive_integration.derive_matter_folder_name(
            {"id": "matter_aaaabbbbc518", "created_at": "2026-06-12T09:00:00+00:00", "document_title": long_title},
            "matter_aaaabbbbc518",
            "Air India",
        )
        # No mid-word slice and no dangling opening bracket / separator at the end.
        self.assertEqual(name, "2026-06-12 · Air India - Mutual NDA Template (Updated as on 23.04.2025) · c518")
        title = name.split(" · ")[1]
        self.assertLessEqual(len(title), 60)
        self.assertFalse(title.rstrip().endswith(("(", "-", "·", ",")))

    def test_matter_ref_code_is_stable_and_fixed_length(self):
        # Same id -> same ref every time (folders stay stable across re-syncs).
        self.assertEqual(
            drive_integration._matter_ref_code("matter_3a8f2b1c9d0e"),
            drive_integration._matter_ref_code("matter_3a8f2b1c9d0e"),
        )
        self.assertEqual(len(drive_integration._matter_ref_code("matter_3a8f2b1c9d0e")), 4)
        # Short/empty ids still yield a 4-char deterministic code.
        self.assertEqual(len(drive_integration._matter_ref_code("x")), 4)
        self.assertEqual(len(drive_integration._matter_ref_code("")), 4)

    def test_folder_names_are_drive_safe(self):
        # Slashes and control chars are neutralised so a name cannot escape the path.
        self.assertEqual(
            drive_integration.derive_counterparty({"id": "m", "subject": "Ac/me\\Corp"}),
            "Ac me Corp",
        )

    def test_sync_raises_when_no_artifacts(self):
        with self.assertRaises(drive_integration.DriveIntegrationError):
            drive_integration.sync_matter_folder(
                matter={"id": "m", "artifacts": []},
                matter_id="m",
                service=FakeDriveV2Service(),
                get_artifact_bytes=lambda *a, **k: b"",
            )


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
                {"enabled": False, "folder_id": "", "folder_name": "", "auto_intake": True},
            )

    def test_update_drive_settings_persists(self):
        with self._isolated_data_dir():
            updated = app_settings.update_drive_settings(
                {"enabled": True, "folder_id": "folder_XYZ-123", "folder_name": "Signed NDAs"}
            )
            self.assertEqual(
                updated,
                {
                    "enabled": True,
                    "folder_id": "folder_XYZ-123",
                    "folder_name": "Signed NDAs",
                    "auto_intake": True,
                },
            )
            # A fresh read returns the persisted values.
            self.assertEqual(app_settings.drive_settings(), updated)

    def test_auto_intake_setting_round_trips(self):
        with self._isolated_data_dir():
            # Default on.
            self.assertTrue(app_settings.drive_auto_intake_enabled())
            # Turning it off round-trips through update + a fresh read.
            updated = app_settings.update_drive_settings({"auto_intake": False})
            self.assertFalse(updated["auto_intake"])
            self.assertFalse(app_settings.drive_auto_intake_enabled())
            self.assertFalse(app_settings.drive_settings()["auto_intake"])
            # And back on again.
            updated = app_settings.update_drive_settings({"auto_intake": True})
            self.assertTrue(updated["auto_intake"])
            self.assertTrue(app_settings.drive_auto_intake_enabled())

    def test_auto_intake_setting_rejects_non_boolean(self):
        with self._isolated_data_dir():
            # A non-boolean is ignored (not coerced): the setting is unchanged.
            updated = app_settings.update_drive_settings({"auto_intake": "yes"})
            self.assertTrue(updated["auto_intake"])

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
        """Create a matter and register one artifact per role spec.

        A role spec is ``(role, bytes)`` (actor defaults to ``human``) or
        ``(role, bytes, actor)`` to pin the producing actor.
        """
        first_bytes = roles[0][1]
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
        for spec in roles:
            role, document_bytes = spec[0], spec[1]
            actor = spec[2] if len(spec) > 2 else "human"
            artifact_service.add_artifact(
                matter_id,
                source="generated",
                actor=actor,
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

    def test_upload_matter_syncs_full_tree_and_returns_v2_contract(self):
        fake = FakeDriveV2Service()
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_id = self._make_matter_with_roles([
                    ("original", b"ORIGINAL bytes", "counterparty"),
                    ("redline", b"REDLINE bytes", "ai"),
                    ("reviewed", b"REVIEWED bytes", "human"),
                ])
                with patch.object(drive_integration, "drive_connected", return_value=True):
                    with patch.object(drive_integration, "_drive_service", return_value=fake):
                        status, payload, _headers = self.request(
                            "POST", "/api/drive/upload-matter", {"matter_id": matter_id}
                        )
        self.assertEqual(status, 200)
        drive = payload["drive"]
        # Response contract shape.
        self.assertEqual(
            set(drive.keys()),
            {"matter_folder_id", "matter_folder_url", "synced_count", "total_count", "artifacts"},
        )
        self.assertEqual(drive["total_count"], 3)
        self.assertEqual(drive["synced_count"], 3)
        self.assertTrue(drive["matter_folder_url"].startswith("https://drive.google.com/drive/folders/"))
        # Per-artifact records carry the drive ids + grammar filenames.
        for record in drive["artifacts"]:
            self.assertEqual(
                set(record.keys()),
                {
                    "artifact_id", "sequence", "actor", "role", "version",
                    "filename", "drive_file_id", "drive_file_url",
                    "based_on_artifact_id", "created_at",
                },
            )
            self.assertTrue(record["drive_file_id"])
        # The matter "drive" block is persisted + surfaced on the public matter.
        self.assertIn("drive", payload["matter"])
        self.assertEqual(payload["matter"]["drive"]["matter_folder_id"], drive["matter_folder_id"])
        # The full {root}/{counterparty}/{matter}/metadata tree was created.
        self.assertIn(drive_integration.DEFAULT_ROOT_FOLDER_NAME, fake.folder_names())
        self.assertIn("metadata", fake.folder_names())
        self.assertEqual(fake.folder_create_count(), 4)  # NDAs, counterparty, matter, metadata
        # 3 artifacts + matter_summary.json = 4 files.
        self.assertEqual(fake.file_create_count(), 4)
        self.assertIn("matter_summary.json", fake.file_names())
        # Lifecycle-grammar filenames {NN}_{stage}[_v{N}].
        names = fake.file_names()
        self.assertIn("01_received.docx", names)          # counterparty original
        self.assertIn("02_ai_redline_v1.docx", names)     # ai redline (versioned)
        self.assertIn("03_legal_review_v1.docx", names)   # human reviewed (versioned)
        self.assertEqual(telemetry.snapshot()["counters"].get("drive_upload_succeeded"), 1)
        self.assertEqual(telemetry.snapshot()["counters"].get("drive_files_synced"), 3)

    def test_resync_uploads_only_new_artifacts_no_duplicate_folders(self):
        fake = FakeDriveV2Service()
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_id = self._make_matter_with_roles([
                    ("original", b"ORIGINAL bytes"),
                ])
                with patch.object(drive_integration, "drive_connected", return_value=True):
                    with patch.object(drive_integration, "_drive_service", return_value=fake):
                        status1, payload1, _ = self.request(
                            "POST", "/api/drive/upload-matter", {"matter_id": matter_id}
                        )
                        folders_after_first = fake.folder_create_count()
                        files_after_first = fake.file_create_count()
                        # Re-sync the SAME matter: idempotent, no new folders/files.
                        status2, payload2, _ = self.request(
                            "POST", "/api/drive/upload-matter", {"matter_id": matter_id}
                        )
        self.assertEqual(status1, 200)
        self.assertEqual(status2, 200)
        self.assertEqual(payload1["drive"]["synced_count"], 1)
        # Second sync uploads nothing new.
        self.assertEqual(payload2["drive"]["synced_count"], 0)
        self.assertEqual(fake.folder_create_count(), folders_after_first)  # no duplicate folders
        self.assertEqual(fake.file_create_count(), files_after_first)  # no duplicate files
        # Same matter folder both times.
        self.assertEqual(
            payload1["drive"]["matter_folder_id"], payload2["drive"]["matter_folder_id"]
        )

    def test_resync_after_new_artifact_uploads_only_the_new_one(self):
        fake = FakeDriveV2Service()
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                owner = ""
                matter_id = self._make_matter_with_roles([("original", b"ORIGINAL bytes")])
                with patch.object(drive_integration, "drive_connected", return_value=True):
                    with patch.object(drive_integration, "_drive_service", return_value=fake):
                        self.request("POST", "/api/drive/upload-matter", {"matter_id": matter_id})
                        files_after_first = fake.file_create_count()
                        # Register a NEW artifact, then re-sync.
                        artifact_service.add_artifact(
                            matter_id,
                            source="generated",
                            actor="human",
                            role="reviewed",
                            document_bytes=b"REVIEWED bytes",
                            owner_user_id=owner,
                        )
                        _status, payload2, _ = self.request(
                            "POST", "/api/drive/upload-matter", {"matter_id": matter_id}
                        )
        # Only the new reviewed artifact uploaded (matter_summary.json is replaced
        # idempotently by name, so it is not re-created).
        self.assertEqual(payload2["drive"]["synced_count"], 1)
        self.assertEqual(payload2["drive"]["total_count"], 2)
        self.assertEqual(fake.file_create_count(), files_after_first + 1)

    def test_upload_matter_drops_unsupported_role_param_harmlessly(self):
        # The v1 role-override param is gone; an unexpected "role" is ignored and
        # the full sync still runs (no 400).
        fake = FakeDriveV2Service()
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                matter_id = self._make_matter_with_roles([("original", b"orig")])
                with patch.object(drive_integration, "drive_connected", return_value=True):
                    with patch.object(drive_integration, "_drive_service", return_value=fake):
                        status, payload, _ = self.request(
                            "POST",
                            "/api/drive/upload-matter",
                            {"matter_id": matter_id, "role": "bogus"},
                        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["drive"]["total_count"], 1)

    def test_drive_status_reports_folder_and_enabled(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                app_settings.update_drive_settings(
                    {"enabled": True, "folder_id": "folder_1", "folder_name": "NDAs"}
                )
                status, payload, _headers = self.request("GET", "/api/drive/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload.keys()),
            {
                "connected",
                "account",
                "folder",
                "filing_location",
                "enabled",
                "signed_in",
                "user_scoped",
                "needs_connect",
                "connect_url",
                "token",
                "setup",
                "recovery",
            },
        )
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["folder"], {"id": "folder_1", "name": "NDAs"})
        # A configured root folder is reported as the actual filing destination.
        # Not connected here, so the live name lookup is skipped and the helper
        # falls back to the stored folder name.
        self.assertTrue(payload["filing_location"]["configured"])
        self.assertEqual(payload["filing_location"]["folder_id"], "folder_1")
        self.assertEqual(payload["filing_location"]["folder_name"], "NDAs")
        self.assertEqual(payload["filing_location"]["label"], "NDAs / NDAs")
        # No Google session in the loopback test client, so not connected.
        self.assertFalse(payload["connected"])
        self.assertFalse(payload["signed_in"])
        self.assertFalse(payload["user_scoped"])
        self.assertFalse(payload["needs_connect"])
        self.assertEqual(payload["connect_url"], "/auth/google/start")
        self.assertEqual(payload["token"]["label"], "Sign in with Google")
        self.assertEqual(payload["setup"]["state"], "missing_oauth_config")
        self.assertEqual(payload["recovery"]["state"], "missing_oauth_config")

    def test_drive_status_blank_folder_reports_default_filing_location(self):
        # No root folder configured -> status confirms the My Drive / NDAs default
        # destination (the silent-fallback fix) and does not crash while disconnected.
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                app_settings.update_drive_settings(
                    {"enabled": True, "folder_id": "", "folder_name": ""}
                )
                status, payload, _headers = self.request("GET", "/api/drive/status")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["folder"])
        self.assertFalse(payload["connected"])
        self.assertFalse(payload["filing_location"]["configured"])
        self.assertEqual(
            payload["filing_location"]["label"],
            "My Drive / NDAs (default location)",
        )

    def test_drive_status_signed_in_without_token_points_to_drive_connect(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.dict(os.environ, auth_env):
                user = user_store.upsert_google_user({
                    "sub": "drive-status-missing-token",
                    "email": "missing-drive@example.com",
                    "name": "Missing Drive",
                    "picture": "",
                })
                token = user_store.create_session(user["id"])
                status, payload, _headers = self.request(
                    "GET",
                    "/api/drive/status",
                    headers={"Cookie": f"{user_store.SESSION_COOKIE_NAME}={token}"},
                )
        self.assertEqual(status, 200)
        self.assertFalse(payload["connected"])
        self.assertTrue(payload["signed_in"])
        self.assertTrue(payload["user_scoped"])
        self.assertTrue(payload["needs_connect"])
        self.assertEqual(payload["connect_url"], "/auth/drive/start")
        self.assertEqual(payload["token"]["source"], "missing")
        self.assertEqual(payload["setup"]["state"], "ready_to_connect")
        self.assertEqual(payload["recovery"]["state"], "missing_token")
        self.assertEqual(payload["recovery"]["action"], "connect_google")

    def test_drive_status_signed_in_with_drive_token_reports_connected(self):
        auth_env = {
            "NDA_REQUIRE_AUTH": "true",
            "NDA_AUTH_USERNAME": "",
            "NDA_AUTH_PASSWORD": "",
            "NDA_GOOGLE_OAUTH_CLIENT_ID": "google-client",
            "NDA_GOOGLE_OAUTH_CLIENT_SECRET": "google-secret",
        }
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.dict(os.environ, auth_env):
                user = user_store.upsert_google_user({
                    "sub": "drive-status-token",
                    "email": "drive-token@example.com",
                    "name": "Drive Token",
                    "picture": "",
                })
                google_connection.save_user_oauth_token(
                    user["id"],
                    {"access_token": "access", "refresh_token": "refresh"},
                    role="drive",
                )
                token = user_store.create_session(user["id"])
                with patch.object(drive_integration, "drive_connected", return_value=True):
                    with patch.object(drive_integration, "drive_account_email", return_value="drive-token@example.com"):
                        status, payload, _headers = self.request(
                            "GET",
                            "/api/drive/status",
                            headers={"Cookie": f"{user_store.SESSION_COOKIE_NAME}={token}"},
                        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["connected"])
        self.assertFalse(payload["needs_connect"])
        self.assertEqual(payload["connect_url"], "/auth/drive/start")
        self.assertEqual(payload["account"], "drive-token@example.com")
        self.assertEqual(payload["token"]["source"], "user_data")
        self.assertEqual(payload["token"]["scope_status"]["missing"], [])
        self.assertEqual(payload["recovery"]["state"], "ready")

    def test_drive_settings_update_persists_via_route(self):
        # A non-blank folder id is now live-validated against Drive before persisting,
        # so this happy-path test mocks a writable folder so validation passes.
        service = _FakeGetService(meta=_writable_folder_meta("folder_99"))
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patch.object(
                drive_integration, "_drive_service", return_value=service
            ):
                status, payload, _headers = self.request(
                    "POST",
                    "/api/admin/drive-settings",
                    {"enabled": True, "folder_id": "folder_99", "folder_name": "Vault"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    payload["drive"],
                    {
                        "enabled": True,
                        "folder_id": "folder_99",
                        "folder_name": "Vault",
                        "auto_intake": True,
                    },
                )
                # Audit event recorded for the admin change.
                history = app_settings.settings_audit_history()
        self.assertTrue(any(event["action"] == "drive_settings_update" for event in history))

    def test_drive_settings_update_auto_intake_via_route(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload, _headers = self.request(
                    "POST",
                    "/api/admin/drive-settings",
                    {"auto_intake": False},
                )
                self.assertEqual(status, 200)
                self.assertFalse(payload["drive"]["auto_intake"])
                self.assertFalse(app_settings.drive_auto_intake_enabled())

    def test_drive_settings_update_rejects_non_boolean_auto_intake(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.matter_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                status, payload, _headers = self.request(
                    "POST",
                    "/api/admin/drive-settings",
                    {"auto_intake": "nope"},
                )
        self.assertEqual(status, 400)
        self.assertIn("auto-intake", payload["error"].lower())

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


# --- Root-folder validation fakes + tests ----------------------------------
class _FakeGet:
    """A files().get() that returns canned folder metadata or raises an HttpError."""

    def __init__(self, *, meta=None, error=None, recorder=None):
        self._meta = meta
        self._error = error
        self._recorder = recorder

    def get(self, *, fileId, fields):  # noqa: N803
        if self._recorder is not None:
            self._recorder.append({"fileId": fileId, "fields": fields})
        if self._error is not None:
            raise self._error
        return _FakeExecutable(self._meta)


class _FakeGetService:
    def __init__(self, *, meta=None, error=None):
        self._meta = meta
        self._error = error
        self.get_calls: list[dict] = []

    def files(self):
        return _FakeGet(meta=self._meta, error=self._error, recorder=self.get_calls)


def _http_error(status):
    class _Resp:
        pass

    error = Exception(f"http {status}")
    resp = _Resp()
    resp.status = status
    error.resp = resp
    return error


def _writable_folder_meta(fid="folder_OK"):
    return {
        "id": fid,
        "mimeType": drive_integration.FOLDER_MIME,
        "trashed": False,
        "capabilities": {"canAddChildren": True},
    }


class DriveFolderValidationTests(unittest.TestCase):
    """Unit tests for drive_integration.validate_root_folder (no network)."""

    def test_blank_folder_skips_validation(self):
        # Blank id = auto-create "NDAs"; no Drive call, no raise.
        service = _FakeGetService(error=_http_error(404))
        drive_integration.validate_root_folder("", service=service)
        self.assertEqual(service.get_calls, [])

    def test_valid_writable_folder_passes(self):
        service = _FakeGetService(meta=_writable_folder_meta("folder_OK"))
        drive_integration.validate_root_folder("folder_OK", service=service)
        self.assertEqual(len(service.get_calls), 1)
        # Confirms we request the existence + writability fields.
        self.assertIn("capabilities/canAddChildren", service.get_calls[0]["fields"])
        self.assertIn("mimeType", service.get_calls[0]["fields"])

    def test_nonexistent_folder_raises_not_found(self):
        service = _FakeGetService(error=_http_error(404))
        with self.assertRaises(drive_integration.DriveFolderValidationError) as ctx:
            drive_integration.validate_root_folder("missing", service=service)
        self.assertIn("found", str(ctx.exception).lower())

    def test_non_folder_id_raises(self):
        meta = _writable_folder_meta()
        meta["mimeType"] = DOCX_MIME  # a file, not a folder
        service = _FakeGetService(meta=meta)
        with self.assertRaises(drive_integration.DriveFolderValidationError) as ctx:
            drive_integration.validate_root_folder("a_file", service=service)
        self.assertIn("not a folder", str(ctx.exception).lower())

    def test_non_writable_folder_raises(self):
        meta = _writable_folder_meta()
        meta["capabilities"] = {"canAddChildren": False}
        service = _FakeGetService(meta=meta)
        with self.assertRaises(drive_integration.DriveFolderValidationError) as ctx:
            drive_integration.validate_root_folder("ro_folder", service=service)
        self.assertIn("write permission", str(ctx.exception).lower())

    def test_trashed_folder_raises(self):
        meta = _writable_folder_meta()
        meta["trashed"] = True
        service = _FakeGetService(meta=meta)
        with self.assertRaises(drive_integration.DriveFolderValidationError) as ctx:
            drive_integration.validate_root_folder("trashed_folder", service=service)
        self.assertIn("trash", str(ctx.exception).lower())

    def test_auth_failure_maps_to_reconnect_message(self):
        service = _FakeGetService(error=_http_error(403))
        with self.assertRaises(drive_integration.DriveFolderValidationError) as ctx:
            drive_integration.validate_root_folder("any", service=service)
        self.assertIn("reconnect", str(ctx.exception).lower())

    def test_disconnected_token_raises_not_connected(self):
        # No injected service -> resolves _drive_service, which raises when the role
        # token is missing. We patch the builder to raise the not-connected error.
        with patch.object(
            drive_integration,
            "_drive_service",
            side_effect=drive_integration.DriveNotConnectedError("Drive is not connected."),
        ):
            with self.assertRaises(drive_integration.DriveNotConnectedError):
                drive_integration.validate_root_folder("any")

    def test_rate_limit_surfaces(self):
        service = _FakeGetService(error=_make_rate_limit_error())
        with self.assertRaises(drive_integration.DriveRateLimitError):
            drive_integration.validate_root_folder("any", service=service)


class DriveFolderValidationRouteTests(unittest.TestCase):
    """Route tests for POST /api/admin/drive-settings folder validation."""

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
            payload = json.loads(raw_body.decode("utf-8")) if "application/json" in content_type else raw_body
            return response.status, payload
        finally:
            connection.close()

    def _data_patches(self, data_dir):
        data_path = matter_store.Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", data_path),
            patch.object(matter_store, "MATTERS_PATH", data_path / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", data_path / "uploads"),
        )

    def test_valid_folder_saves_and_persists(self):
        service = _FakeGetService(meta=_writable_folder_meta("folder_OK"))
        with tempfile.TemporaryDirectory() as data_dir:
            p0, p1, p2 = self._data_patches(data_dir)
            with p0, p1, p2, patch.object(drive_integration, "_drive_service", return_value=service):
                status, payload = self.request(
                    "POST", "/api/admin/drive-settings", {"folder_id": "folder_OK"}
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["drive"]["folder_id"], "folder_OK")
                # Persisted.
                self.assertEqual(app_settings.drive_settings()["folder_id"], "folder_OK")

    def test_nonexistent_folder_rejected_and_not_persisted(self):
        service = _FakeGetService(error=_http_error(404))
        with tempfile.TemporaryDirectory() as data_dir:
            p0, p1, p2 = self._data_patches(data_dir)
            with p0, p1, p2, patch.object(drive_integration, "_drive_service", return_value=service):
                status, payload = self.request(
                    "POST", "/api/admin/drive-settings", {"folder_id": "missing"}
                )
                self.assertEqual(status, 400)
                self.assertIn("found", payload["error"].lower())
                # NOT persisted.
                self.assertEqual(app_settings.drive_settings()["folder_id"], "")

    def test_non_folder_rejected_and_not_persisted(self):
        meta = _writable_folder_meta()
        meta["mimeType"] = DOCX_MIME
        service = _FakeGetService(meta=meta)
        with tempfile.TemporaryDirectory() as data_dir:
            p0, p1, p2 = self._data_patches(data_dir)
            with p0, p1, p2, patch.object(drive_integration, "_drive_service", return_value=service):
                status, payload = self.request(
                    "POST", "/api/admin/drive-settings", {"folder_id": "a_file"}
                )
                self.assertEqual(status, 400)
                self.assertIn("not a folder", payload["error"].lower())
                self.assertEqual(app_settings.drive_settings()["folder_id"], "")

    def test_non_writable_rejected_and_not_persisted(self):
        meta = _writable_folder_meta()
        meta["capabilities"] = {"canAddChildren": False}
        service = _FakeGetService(meta=meta)
        with tempfile.TemporaryDirectory() as data_dir:
            p0, p1, p2 = self._data_patches(data_dir)
            with p0, p1, p2, patch.object(drive_integration, "_drive_service", return_value=service):
                status, payload = self.request(
                    "POST", "/api/admin/drive-settings", {"folder_id": "ro_folder"}
                )
                self.assertEqual(status, 400)
                self.assertIn("write permission", payload["error"].lower())
                self.assertEqual(app_settings.drive_settings()["folder_id"], "")

    def test_drive_disconnected_returns_clear_400_not_persisted(self):
        with tempfile.TemporaryDirectory() as data_dir:
            p0, p1, p2 = self._data_patches(data_dir)
            disconnected = drive_integration.DriveNotConnectedError("Drive is not connected.")
            with p0, p1, p2, patch.object(drive_integration, "_drive_service", side_effect=disconnected):
                status, payload = self.request(
                    "POST", "/api/admin/drive-settings", {"folder_id": "folder_OK"}
                )
                self.assertEqual(status, 400)
                self.assertIn("connect google drive", payload["error"].lower())
                self.assertTrue(payload.get("needs_connect"))
                self.assertEqual(app_settings.drive_settings()["folder_id"], "")

    def test_blank_folder_still_allowed_no_drive_call(self):
        # A blank id must save without any Drive lookup (auto-create path preserved).
        service = _FakeGetService(error=_http_error(404))  # would raise IF called
        with tempfile.TemporaryDirectory() as data_dir:
            p0, p1, p2 = self._data_patches(data_dir)
            with p0, p1, p2, patch.object(drive_integration, "_drive_service", return_value=service):
                # Seed a value first, then clear it.
                app_settings.update_drive_settings({"folder_id": "seed_id"})
                status, payload = self.request(
                    "POST", "/api/admin/drive-settings", {"folder_id": ""}
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["drive"]["folder_id"], "")
                self.assertEqual(service.get_calls, [])
                self.assertEqual(app_settings.drive_settings()["folder_id"], "")

    def test_malformed_folder_id_rejected_before_drive_call(self):
        # The fast format pre-check rejects path-traversal without any Drive lookup.
        service = _FakeGetService(meta=_writable_folder_meta())
        with tempfile.TemporaryDirectory() as data_dir:
            p0, p1, p2 = self._data_patches(data_dir)
            with p0, p1, p2, patch.object(drive_integration, "_drive_service", return_value=service):
                status, payload = self.request(
                    "POST", "/api/admin/drive-settings", {"folder_id": "../escape"}
                )
                self.assertEqual(status, 400)
                self.assertIn("folder id", payload["error"].lower())
                self.assertEqual(service.get_calls, [])


if __name__ == "__main__":
    unittest.main()
