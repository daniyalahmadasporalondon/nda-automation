"""Targeted tests for the executed-transition Drive archive (P0 #10 + P1 #11).

Covers the confirmed defects that made a completed DocuSign envelope's signed PDF
silently never reach Drive, plus the wired signed-upload / manual-mark archive
paths:

* #10 — the on-complete archive must resolve the Drive-TOKEN owner the same way
  the deliberate Save-to-Drive route does, NOT thread the matter/request id into
  the Drive layer. Asserted on BOTH the poll path (a session-resolved Google id is
  passed) and the webhook path (no session — resolved from the matter's connected
  Google account, falling back to the server-global "" token). The no-login ""
  path must still archive.
* #11 — ``matter_summary.json`` must be TRULY overwritten on re-sync (the signed
  facets must land), not skipped on the fixed-name hit.
* best-effort + observability — a Drive outage must not break the executed
  transition, and the skip/failure must now be logged (no longer silent).
* signed-upload archives with the "uploaded" label + ``NN_signed_uploaded.pdf``
  name; a bare manual mark-executed with no signed document archives nothing and
  does not crash.

The Drive layer is exercised through fakes/stubs only — never a live Google call.
``_drive_service`` is monkeypatched to a recorder so the test can assert WHICH
token owner the upload authenticated as; ``drive_connected`` is modelled over a
configured token set so the webhook owner-resolution is real.
"""

from __future__ import annotations

import base64
import json
import logging

import pytest

from nda_automation import (
    app_settings,
    artifact_service,
    docusign_workflow,
    drive_integration,
    lifecycle_signed,
    matter_view,
)
from nda_automation.artifact_registry import (
    ACTOR_HUMAN,
    ROLE_REVIEWED,
    SOURCE_GENERATED,
)
from nda_automation.docusign_test_double import FakeDocuSignClient

MATTER_OWNER = "session:matter-owner"
GOOGLE_OWNER = "google:drive-token-owner"
PDF_BYTES = b"%PDF-1.4 reviewed nda body"


# --------------------------------------------------------------------------
# Fake Drive service (stateful: folders + files, models update for true-replace)
# --------------------------------------------------------------------------
class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


def _media_bytes(media_body):
    if media_body is None:
        return None
    return media_body.getbytes(0, media_body.size())


class _FakeFiles:
    def __init__(self, store):
        self._store = store

    def list(self, *, q, fields, pageSize=1, spaces="drive"):  # noqa: N803
        matches = [
            {"id": fid, "name": rec["name"], "webViewLink": rec["web_link"]}
            for fid, rec in self._store["files"].items()
            if rec["name"] in q and rec["parent"] in q and "trashed=false" in q
            and ((drive_integration.FOLDER_MIME in q) == rec["is_folder"])
        ]
        return _Exec({"files": matches[:pageSize]})

    def create(self, *, body, fields, media_body=None):  # noqa: N803
        self._store["create_calls"].append({"name": body["name"], "has_media": media_body is not None})
        self._store["_seq"] += 1
        file_id = f"id_{self._store['_seq']}"
        parents = body.get("parents") or [""]
        self._store["files"][file_id] = {
            "name": body["name"],
            "parent": parents[0],
            "web_link": f"https://drive.google.com/file/d/{file_id}/view",
            "is_folder": body.get("mimeType") == drive_integration.FOLDER_MIME,
            "content": _media_bytes(media_body),
        }
        return _Exec({"id": file_id, "webViewLink": self._store["files"][file_id]["web_link"]})

    def update(self, *, fileId, fields, media_body=None, body=None):  # noqa: N803
        self._store["update_calls"].append({"file_id": fileId, "has_media": media_body is not None})
        rec = self._store["files"][fileId]
        if media_body is not None:
            rec["content"] = _media_bytes(media_body)
        return _Exec({"id": fileId, "webViewLink": rec["web_link"]})


class FakeDriveService:
    def __init__(self):
        self.store = {"files": {}, "create_calls": [], "update_calls": [], "_seq": 0}

    def files(self):
        return _FakeFiles(self.store)

    # assertion helpers
    def file_names(self):
        return sorted(r["name"] for r in self.store["files"].values() if not r["is_folder"])

    def content_for(self, name):
        for r in self.store["files"].values():
            if r["name"] == name:
                return r["content"]
        return None


@pytest.fixture
def drive_env(monkeypatch):
    """Wire the Drive layer to a fake: a recorder for the token owner + a token set.

    Returns a small handle exposing the fake Drive service, the captured token
    owners, and the set of owners that "have" a Drive token (drive_connected True).
    The set defaults to {GOOGLE_OWNER, ""} so the per-user token AND the
    server-global ("") token both resolve; a test narrows/widens it as needed.
    """

    class _Env:
        def __init__(self):
            self.service = FakeDriveService()
            self.token_owners = []  # owner ids passed to _drive_service, in order
            self.connected = {GOOGLE_OWNER, ""}  # owners with a usable Drive token

    env = _Env()

    def fake_drive_service(owner_user_id=""):
        env.token_owners.append(owner_user_id)
        return env.service

    def fake_drive_connected(owner_user_id=""):
        return owner_user_id in env.connected

    monkeypatch.setattr(drive_integration, "_drive_service", fake_drive_service)
    monkeypatch.setattr(drive_integration, "drive_connected", fake_drive_connected)
    monkeypatch.setattr(app_settings, "drive_auto_intake_enabled", lambda: True)
    monkeypatch.setattr(app_settings, "drive_settings", lambda: {"folder_id": "root123"})
    return env


def _make_matter(repo, owner):
    matter = repo.create_matter(
        source_filename="acme-nda.docx",
        document_bytes=b"original",
        extracted_text="text",
        review_result={},
        triage={},
        owner_user_id=owner,
        intake_metadata={"reply_to": "cp@acme.com", "sender": "cp@acme.com", "subject": "Acme NDA"},
    )
    matter_id = matter["id"]
    artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_REVIEWED,
        document_bytes=PDF_BYTES,
        repository=repo,
        owner_user_id=owner,
    )
    return repo.get_matter(matter_id, owner_user_id=owner), matter_id


def _summary_json(service):
    raw = service.content_for(drive_integration.MATTER_SUMMARY_FILENAME)
    assert raw is not None, "matter_summary.json was never written"
    return json.loads(bytes(raw).decode("utf-8"))


def _has_docusign_signed_pdf(service):
    """A plain DocuSign signed file (NN_signed.pdf, NOT the _uploaded variant)
    reached Drive. Sequence-number-agnostic (depends on the artifact count)."""
    return any(
        n.endswith("_signed.pdf") and not n.endswith("_signed_uploaded.pdf")
        for n in service.file_names()
    )


# --------------------------------------------------------------------------
# #10 — poll path (handler present): the session Google id is the token owner
# --------------------------------------------------------------------------
def test_poll_path_archives_with_session_google_token_owner(drive_env, in_memory_matters):
    """On completion via the status poll, the archive authenticates to Drive as the
    SESSION-resolved Google id (passed as drive_token_owner_user_id) — NOT the
    matter/request owner. The signed PDF reaches Drive."""
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake, confirm_recipient="cp@acme.com")

    result = docusign_workflow.sync_signature_status(
        None,
        matter_id,
        MATTER_OWNER,
        repository=in_memory_matters,
        client=fake,
        # The route passes _google_owner_user_id(handler); the matter owner differs.
        drive_token_owner_user_id=GOOGLE_OWNER,
    )

    assert result.completed is True
    # The Drive upload authenticated as the GOOGLE token owner, never the matter id.
    assert GOOGLE_OWNER in drive_env.token_owners
    assert MATTER_OWNER not in drive_env.token_owners
    # The executed PDF landed in Drive under the plain DocuSign signed name.
    assert _has_docusign_signed_pdf(drive_env.service)
    assert _summary_json(drive_env.service)["facets"]["signed_via"] == "docusign"


def test_poll_path_wrong_owner_would_have_skipped(drive_env, in_memory_matters):
    """Regression proof for #10: had the matter/request owner been threaded to the
    Drive layer (the bug), drive_connected(MATTER_OWNER) is False -> skip. Passing
    the resolved Google id is what makes the archive fire."""
    drive_env.connected = {GOOGLE_OWNER}  # only the Google id has a token
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake, confirm_recipient="cp@acme.com")

    docusign_workflow.sync_signature_status(
        None, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
        drive_token_owner_user_id=GOOGLE_OWNER,
    )
    # Archived because we used the Google id (matter owner would have been skipped).
    assert _has_docusign_signed_pdf(drive_env.service)


# --------------------------------------------------------------------------
# #10 — webhook path (no session): resolve token owner from the matter
# --------------------------------------------------------------------------
def test_webhook_path_resolves_token_owner_from_matter(drive_env, in_memory_matters):
    """The webhook has no session, so sync_signature_status is called WITHOUT a
    drive_token_owner_user_id. The archiver resolves it from the matched matter's
    connected Google account: here the matter owner HAS a per-user Drive token, so
    it archives as that owner."""
    drive_env.connected = {MATTER_OWNER}  # the matter owner has a per-user token
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake, confirm_recipient="cp@acme.com")

    # No drive_token_owner_user_id passed (the webhook call shape).
    result = docusign_workflow.sync_signature_status(
        None, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake
    )

    assert result.completed is True
    assert drive_env.token_owners and all(o == MATTER_OWNER for o in drive_env.token_owners)
    assert _has_docusign_signed_pdf(drive_env.service)


def test_webhook_path_falls_back_to_server_global_token(drive_env, in_memory_matters):
    """When the matter owner has NO per-user Drive token, the webhook resolution
    falls back to the SERVER-GLOBAL "" token and still archives."""
    drive_env.connected = {""}  # only the server-global token exists
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake, confirm_recipient="cp@acme.com")

    result = docusign_workflow.sync_signature_status(
        None, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake
    )

    assert result.completed is True
    # The upload authenticated as the server-global ("") token.
    assert drive_env.token_owners and all(o == "" for o in drive_env.token_owners)
    assert _has_docusign_signed_pdf(drive_env.service)


def test_no_login_empty_owner_archives_via_server_global(drive_env, in_memory_matters):
    """No-login / local-demo mode: the matter owner is "" and resolves to the
    server-global "" token. The archive must still fire (don't regress local demo)."""
    drive_env.connected = {""}
    matter, matter_id = _make_matter(in_memory_matters, "")
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, "", repository=in_memory_matters, client=fake, confirm_recipient="cp@acme.com")

    result = docusign_workflow.sync_signature_status(
        None, matter_id, "", repository=in_memory_matters, client=fake
    )

    assert result.completed is True
    assert _has_docusign_signed_pdf(drive_env.service)


# --------------------------------------------------------------------------
# #11 — matter_summary.json is truly overwritten on re-sync
# --------------------------------------------------------------------------
def test_resync_overwrites_matter_summary_with_signed_facets(drive_env, in_memory_matters):
    """A first sync (pre-signed) then a completion re-sync must OVERWRITE the
    fixed-name matter_summary.json with the signed facets — not skip on the name
    hit (the #11 bug)."""
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient()
    send = docusign_workflow.send_for_signature(matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake, confirm_recipient="cp@acme.com")

    # First archive while still awaiting signature (not signed yet).
    drive_integration.archive_executed_matter(
        matter=in_memory_matters.get_matter(matter_id, owner_user_id=MATTER_OWNER),
        matter_id=matter_id,
        owner_user_id=MATTER_OWNER,
        repository=in_memory_matters,
        drive_token_owner_user_id=GOOGLE_OWNER,
    )
    first = _summary_json(drive_env.service)
    assert first["facets"]["signed"] is False  # sent/awaiting -> not signed

    # Complete the envelope and re-sync (the executed transition's archive).
    fake.advance(send.envelope_id)  # delivered
    fake.advance(send.envelope_id)  # completed
    docusign_workflow.sync_signature_status(
        None, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
        drive_token_owner_user_id=GOOGLE_OWNER,
    )

    # The summary was UPDATED in place (files().update), not duplicated, and now
    # reflects the signed state.
    assert drive_env.service.store["update_calls"], "summary was not truly overwritten"
    summary_creates = [c for c in drive_env.service.store["create_calls"] if c["name"] == drive_integration.MATTER_SUMMARY_FILENAME]
    assert len(summary_creates) == 1  # created once, then replaced in place
    refreshed = _summary_json(drive_env.service)
    assert refreshed["facets"]["signed"] is True
    assert refreshed["facets"]["signed_via"] == "docusign"


# --------------------------------------------------------------------------
# best-effort + observability — Drive outage must not break the transition
# --------------------------------------------------------------------------
def test_drive_outage_does_not_break_executed_and_is_logged(in_memory_matters, monkeypatch, caplog):
    """A Drive failure during the executed transition is swallowed: the matter
    still flips executed/fully-signed, and the failure is now LOGGED (no longer
    silent)."""
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake, confirm_recipient="cp@acme.com")

    monkeypatch.setattr(drive_integration, "drive_connected", lambda owner_user_id="": True)
    monkeypatch.setattr(app_settings, "drive_auto_intake_enabled", lambda: True)
    monkeypatch.setattr(app_settings, "drive_settings", lambda: {"folder_id": "root123"})

    def _boom(**kwargs):
        raise drive_integration.DriveIntegrationError("Drive is down")

    monkeypatch.setattr(drive_integration, "sync_matter_folder", _boom)

    with caplog.at_level(logging.WARNING, logger="nda_automation.drive_integration"):
        result = docusign_workflow.sync_signature_status(
            None, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
            drive_token_owner_user_id=GOOGLE_OWNER,
        )

    # The executed transition completed regardless of the Drive failure.
    assert result.completed is True
    refreshed = in_memory_matters.get_matter(matter_id, owner_user_id=MATTER_OWNER)
    assert refreshed["executed"] is True
    assert refreshed["status"] == "fully_signed"
    # The failure is observable now (previously a silent telemetry-only miss).
    assert any("Drive archive failed" in r.message for r in caplog.records)


def test_archive_skip_when_not_connected_is_logged(drive_env, in_memory_matters, caplog):
    """When no Drive token resolves (skip), the miss is logged, not silent."""
    drive_env.connected = set()  # nobody has a token
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake, confirm_recipient="cp@acme.com")

    with caplog.at_level(logging.INFO, logger="nda_automation.drive_integration"):
        result = docusign_workflow.sync_signature_status(
            None, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
            drive_token_owner_user_id=GOOGLE_OWNER,
        )

    assert result.completed is True  # transition still happened
    assert not _has_docusign_signed_pdf(drive_env.service)  # nothing archived
    assert any("Drive archive skipped" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# signed-upload + manual mark-executed archive paths (coordinator scope)
# --------------------------------------------------------------------------
class _FakeHandler:
    """Minimal handler stub: a Google-provider session whose connected owner id is
    GOOGLE_OWNER, mirroring routes/common.request_owner_user_id +
    google_connection.connected_owner_user_id."""

    def __init__(self, owner_id):
        self.current_user_id = owner_id
        self.current_user = {"provider": "google", "id": owner_id, "email": "u@aspora.com"}


def test_signed_upload_archives_with_uploaded_label_and_name(drive_env, in_memory_matters, monkeypatch):
    """An uploaded paper-signed PDF archives to Drive labelled "uploaded" in both
    the durable summary facet AND the file name (NN_signed_uploaded.pdf)."""
    monkeypatch.setattr(lifecycle_signed, "DiskMatterRepository", lambda: in_memory_matters)
    matter, matter_id = _make_matter(in_memory_matters, GOOGLE_OWNER)

    captured = {}
    real_archive = drive_integration.archive_executed_matter

    def fake_archive(**kwargs):
        captured.update(kwargs)
        # Run the REAL archiver so the filename/summary facets are actually exercised.
        real_archive(**kwargs)

    monkeypatch.setattr(drive_integration, "archive_executed_matter", fake_archive)

    handler = _FakeHandler(GOOGLE_OWNER)
    payload = {"filename": "executed.pdf", "content_base64": _b64(b"%PDF-1.7 paper signed")}
    handler_calls = _StubJsonHandler(handler, payload)
    lifecycle_signed.handle_signed_upload(handler_calls, f"/api/matters/{matter_id}/signed")

    assert handler_calls.status == 201
    assert captured.get("signed_via") == "uploaded"
    # The archived signed file carries the _uploaded suffix, distinct from DocuSign.
    names = drive_env.service.file_names()
    assert any(n.endswith("_signed_uploaded.pdf") for n in names), names
    assert not any(n.endswith("_signed.pdf") and not n.endswith("_uploaded.pdf") for n in names)
    summary = _summary_json(drive_env.service)
    assert summary["facets"]["signed_via"] == "uploaded"


def test_manual_mark_with_no_signed_document_archives_nothing(drive_env, in_memory_matters, monkeypatch):
    """A bare manual mark-executed (no signed PDF) is just an attestation — there is
    nothing to mirror, so it archives nothing and does not crash."""
    monkeypatch.setattr(lifecycle_signed, "DiskMatterRepository", lambda: in_memory_matters)
    matter, matter_id = _make_matter(in_memory_matters, GOOGLE_OWNER)

    archive_calls = []
    monkeypatch.setattr(
        drive_integration, "archive_executed_matter", lambda **kw: archive_calls.append(kw)
    )

    handler = _FakeHandler(GOOGLE_OWNER)
    handler_calls = _StubJsonHandler(handler, {})
    lifecycle_signed.handle_mark_executed(handler_calls, f"/api/matters/{matter_id}/mark-executed")

    assert handler_calls.status == 200  # the mark itself succeeded
    assert archive_calls == []  # no signed doc -> no archive attempt
    assert drive_env.service.file_names() == []  # nothing reached Drive
    # The matter is still executed despite no Drive mirror.
    refreshed = in_memory_matters.get_matter(matter_id, owner_user_id=GOOGLE_OWNER)
    assert refreshed["executed"] is True


# --------------------------------------------------------------------------
# drive_archive outcome recording — the previously-silent failure is now visible
# --------------------------------------------------------------------------
def test_archive_failure_records_drive_archive_failed_and_surfaces(
    in_memory_matters, monkeypatch, caplog
):
    """A Drive archive failure during the executed transition is RECORDED onto the
    matter as drive_archive.status == "failed" (with a short reason) AND surfaced in
    public_matter — while the matter still flips executed. Previously the miss was a
    silent telemetry-only increment with NO user-visible signal."""
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(
        matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
        confirm_recipient="cp@acme.com",
    )

    monkeypatch.setattr(drive_integration, "drive_connected", lambda owner_user_id="": True)
    monkeypatch.setattr(app_settings, "drive_auto_intake_enabled", lambda: True)
    monkeypatch.setattr(app_settings, "drive_settings", lambda: {"folder_id": "root123"})

    def _boom(**kwargs):
        raise drive_integration.DriveIntegrationError("Root folder is not writable")

    monkeypatch.setattr(drive_integration, "sync_matter_folder", _boom)

    with caplog.at_level(logging.WARNING, logger="nda_automation.drive_integration"):
        result = docusign_workflow.sync_signature_status(
            None, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
            drive_token_owner_user_id=GOOGLE_OWNER,
        )

    assert result.completed is True
    refreshed = in_memory_matters.get_matter(matter_id, owner_user_id=MATTER_OWNER)
    # The executed transition is unaffected by the Drive failure.
    assert refreshed["executed"] is True
    assert refreshed["status"] == "fully_signed"
    # The failure is RECORDED onto the matter with a short, user-safe reason.
    archive = refreshed["drive_archive"]
    assert archive["status"] == "failed"
    assert "Root folder is not writable" in archive["error"]
    assert archive["attempted_at"]
    # ...and SURFACED through the public serialization the frontend reads.
    public = matter_view.public_matter(refreshed)
    assert public["drive_archive"]["status"] == "failed"
    assert "Root folder is not writable" in public["drive_archive"]["error"]


def test_archive_success_records_drive_archive_ok_no_warning(drive_env, in_memory_matters):
    """A SUCCESSFUL executed archive records drive_archive.status == "ok" (no error),
    so the UI shows no warning. The drive folder pointer is also stamped as before."""
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(
        matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
        confirm_recipient="cp@acme.com",
    )

    docusign_workflow.sync_signature_status(
        None, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
        drive_token_owner_user_id=GOOGLE_OWNER,
    )

    refreshed = in_memory_matters.get_matter(matter_id, owner_user_id=MATTER_OWNER)
    assert refreshed["drive_archive"]["status"] == "ok"
    assert refreshed["drive_archive"]["error"] == ""
    assert refreshed.get("drive", {}).get("matter_folder_id")
    public = matter_view.public_matter(refreshed)
    assert public["drive_archive"]["status"] == "ok"


def test_archive_skip_when_not_connected_records_no_drive_archive_block(
    drive_env, in_memory_matters
):
    """When Drive is not connected (no archive ATTEMPTED), NO drive_archive block is
    written — a matter with no Drive connection must never show a false failed-archive
    warning."""
    drive_env.connected = set()  # nobody has a token -> skip, no attempt
    matter, matter_id = _make_matter(in_memory_matters, MATTER_OWNER)
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(
        matter, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
        confirm_recipient="cp@acme.com",
    )

    result = docusign_workflow.sync_signature_status(
        None, matter_id, MATTER_OWNER, repository=in_memory_matters, client=fake,
        drive_token_owner_user_id=GOOGLE_OWNER,
    )

    assert result.completed is True
    refreshed = in_memory_matters.get_matter(matter_id, owner_user_id=MATTER_OWNER)
    # No archive attempted -> no block at all -> no warning.
    assert "drive_archive" not in refreshed or refreshed.get("drive_archive") in (None, {})
    public = matter_view.public_matter(refreshed)
    assert not public.get("drive_archive")


# --- tiny handler/json plumbing for the route-body tests -------------------
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _StubJsonHandler:
    """Wrap a fake session handler with the _read_json_payload / _send_json hooks
    the lifecycle route bodies call, recording the response status."""

    def __init__(self, session, payload):
        self.current_user_id = session.current_user_id
        self.current_user = session.current_user
        self._payload = payload
        self.status = None
        self.body = None

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, body, status=200, send_body=True):
        self.status = status
        self.body = body
