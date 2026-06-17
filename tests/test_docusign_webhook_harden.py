"""Targeted tests for the hardened DocuSign Connect webhook (security finding #22).

The DocuSign Connect callback (``POST /api/docusign/webhook``) is the one PUBLIC
endpoint: DocuSign's servers call it with no session, so it is authenticated by
its HMAC signature instead of CSRF/session. This file pins the fail-CLOSED HMAC
behaviour and the full on-``completed`` drive:

(a) a valid-HMAC "completed" Connect payload flips the matter to executed, captures
    the signed PDF artifact, and ATTEMPTS the Drive archive (no session -> the
    archiver resolves the Drive-token owner from the matter, the #10 path).
(b) an invalid OR missing signature WITH a key configured is rejected 401 with NO
    state change (the matter never flips).
(c) with NO key configured the webhook still proceeds (unconfigured demo keeps
    working) but a LOUD WARNING is logged that the webhook is unauthenticated.

Reuses the existing fakes: ``FakeDocuSignClient`` (the DocuSign double),
``InMemoryMatterRepository``, and the ``_FakeHandler`` route-driver shape from
``test_docusign_route``. The Drive layer is never called live — the workflow's
``_archive_to_drive`` archiver is monkeypatched to a recorder so we assert the
archive was attempted (and under no explicit Drive-token owner) without standing
up a fake Google client.

Secret hygiene: the only "secret" here is the obviously-fake ``test-hmac-key``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging

import pytest

from nda_automation import (
    artifact_service,
    docusign_connection,
    docusign_integration,
    docusign_workflow,
)
from nda_automation.artifact_registry import ACTOR_HUMAN, ROLE_REVIEWED, SOURCE_GENERATED
from nda_automation.docusign_test_double import FakeDocuSignClient
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.routes import docusign as docusign_routes

OWNER = "google:webhook-owner"
PDF_BYTES = b"%PDF-1.4 reviewed body"
# Obviously-fake HMAC secret — NEVER a real key.
HMAC_KEY = "test-hmac-key"


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path, monkeypatch):
    """Pin the user store to a per-test tmp file so token writes never touch ./data."""
    monkeypatch.setenv("NDA_USERS_PATH", str(tmp_path / "users.json"))


class _FakeHandler:
    """Minimal request double matching the seam in test_docusign_route."""

    def __init__(self, repo, *, owner=OWNER, raw_body=b"", headers=None):
        self.matter_repository = repo
        self.current_user_id = owner
        self.current_user = {"id": owner, "provider": "google", "email": "u@x.com"}
        self.path = "/api/docusign/webhook"
        self.rfile = io.BytesIO(raw_body)
        self.headers = headers or {"Content-Length": str(len(raw_body)), "Host": "app.test"}
        self.status = 200
        self.response = None

    def _read_content_length(self):
        raw = self.headers.get("Content-Length")
        return int(raw) if raw is not None else 0

    def _send_json(self, payload, *, status=200, send_body=True, headers=None):
        self.status = status
        self.response = payload


class _SendHandler:
    """Driver for the send-for-signature route (creates the envelope under test)."""

    def __init__(self, repo, *, owner=OWNER):
        self.matter_repository = repo
        self.current_user_id = owner
        self.current_user = {"id": owner, "provider": "google", "email": "u@x.com"}
        self.path = "/"
        self.headers = {"Host": "app.test"}
        self.status = 200
        self.response = None

    def _read_json_payload(self):
        return {}

    def _send_json(self, payload, *, status=200, send_body=True, headers=None):
        self.status = status
        self.response = payload


@pytest.fixture
def repo():
    return InMemoryMatterRepository()


@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "int-key")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    docusign_connection.save_user_token(
        OWNER,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
        {"account_id": "a1", "base_uri": "https://demo.docusign.net", "account_name": "Acme", "email": "u@x.com"},
    )


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeDocuSignClient()
    monkeypatch.setattr(docusign_integration, "get_client", lambda *, owner_user_id="": client)
    return client


@pytest.fixture
def archive_recorder(monkeypatch):
    """Record every executed-transition Drive archive attempt (never call Drive)."""
    calls: list[dict] = []

    def _record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(docusign_workflow, "_archive_to_drive", _record)
    return calls


def _matter_with_reviewed(repo, *, owner=OWNER):
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
    return matter_id


def _sent_envelope(repo, fake_client):
    """Send an NDA for signature and mark its envelope completed at DocuSign."""
    matter_id = _matter_with_reviewed(repo)
    docusign_routes.handle_send_for_signature(
        _SendHandler(repo), f"/api/matters/{matter_id}/send-for-signature"
    )
    envelope_id = repo.get_matter(matter_id, owner_user_id=OWNER)[
        docusign_workflow.SIGNATURE_FIELD
    ]["envelope_id"]
    fake_client.complete(envelope_id)
    return matter_id, envelope_id


def _webhook_body(envelope_id, status="completed"):
    return json.dumps(
        {"event": "envelope-completed", "data": {"envelopeId": envelope_id, "envelopeSummary": {"status": status}}}
    ).encode("utf-8")


def _signed_headers(body, key=HMAC_KEY):
    signature = base64.b64encode(hmac.new(key.encode("utf-8"), body, hashlib.sha256).digest()).decode("ascii")
    return {"Content-Length": str(len(body)), docusign_routes.HMAC_SIGNATURE_HEADER: signature}


# --------------------------------------------------------------------------
# (a) valid HMAC + completed -> executed + signed captured + Drive attempted
# --------------------------------------------------------------------------
def test_valid_hmac_completed_executes_captures_and_archives(
    repo, connected, fake_client, archive_recorder, monkeypatch
):
    monkeypatch.setenv(docusign_connection.CONNECT_HMAC_KEY_ENV, HMAC_KEY)
    matter_id, envelope_id = _sent_envelope(repo, fake_client)

    body = _webhook_body(envelope_id)
    handler = _FakeHandler(repo, owner="", raw_body=body, headers=_signed_headers(body))
    docusign_routes.handle_docusign_webhook(handler)

    assert handler.status == 200
    assert handler.response["matched"] is True
    assert handler.response["completed"] is True

    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["status"] == "fully_signed"
    assert stored.get("executed") is True
    # The executed combined PDF was captured as the matter's signed artifact.
    from nda_automation.artifact_registry import ROLE_SIGNED, latest_artifact_for_role

    assert latest_artifact_for_role(stored, ROLE_SIGNED) is not None

    # The Drive archive was ATTEMPTED, and with NO explicit Drive-token owner
    # (None) — the webhook has no session, so the archiver resolves the token
    # owner from the matter itself (the #10 path).
    assert len(archive_recorder) == 1
    assert archive_recorder[0]["drive_token_owner_user_id"] is None
    assert archive_recorder[0]["matter_id"] == matter_id


# --------------------------------------------------------------------------
# (b) invalid OR missing HMAC with a key set -> 401, NO state change
# --------------------------------------------------------------------------
def test_invalid_hmac_with_key_rejected_401_no_state_change(
    repo, connected, fake_client, archive_recorder, monkeypatch
):
    monkeypatch.setenv(docusign_connection.CONNECT_HMAC_KEY_ENV, HMAC_KEY)
    matter_id, envelope_id = _sent_envelope(repo, fake_client)

    body = _webhook_body(envelope_id)
    handler = _FakeHandler(
        repo,
        owner="",
        raw_body=body,
        headers={"Content-Length": str(len(body)), docusign_routes.HMAC_SIGNATURE_HEADER: "not-the-signature"},
    )
    docusign_routes.handle_docusign_webhook(handler)

    assert handler.status == 401
    # The matter is untouched: still awaiting, never flipped to executed.
    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["status"] != "fully_signed"
    assert stored.get("executed") is not True
    assert archive_recorder == []


def test_missing_hmac_header_with_key_rejected_401_no_state_change(
    repo, connected, fake_client, archive_recorder, monkeypatch
):
    monkeypatch.setenv(docusign_connection.CONNECT_HMAC_KEY_ENV, HMAC_KEY)
    matter_id, envelope_id = _sent_envelope(repo, fake_client)

    body = _webhook_body(envelope_id)
    # No X-DocuSign-Signature-1 header at all, but a key IS configured.
    handler = _FakeHandler(repo, owner="", raw_body=body, headers={"Content-Length": str(len(body))})
    docusign_routes.handle_docusign_webhook(handler)

    assert handler.status == 401
    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["status"] != "fully_signed"
    assert stored.get("executed") is not True
    assert archive_recorder == []


# --------------------------------------------------------------------------
# (c) no key configured -> proceeds, but logs a LOUD WARNING
# --------------------------------------------------------------------------
def test_no_key_logs_unauthenticated_warning(repo, monkeypatch, caplog):
    monkeypatch.delenv(docusign_connection.CONNECT_HMAC_KEY_ENV, raising=False)
    # Unknown envelope keeps the test focused on the auth branch; the request must
    # still be accepted (matched:false ack) and the warning must fire.
    body = _webhook_body("env-unknown")
    handler = _FakeHandler(repo, owner="", raw_body=body)

    with caplog.at_level(logging.WARNING, logger="nda_automation.routes.docusign"):
        docusign_routes.handle_docusign_webhook(handler)

    assert handler.status == 200
    assert handler.response["received"] is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING that the webhook is unauthenticated"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "UNAUTHENTICATED" in joined
    assert docusign_connection.CONNECT_HMAC_KEY_ENV in joined
