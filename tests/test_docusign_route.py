"""Route-layer tests for the DocuSign endpoints.

Drives the route bodies with a fake handler + the test double injected via the
real factory seam (``docusign_integration.get_client`` monkeypatched to return
the double). Covers send-for-signature, signature-status, signed-document, the
webhook (HMAC verify + complete), connect/status, owner-mismatch 404 no-op, and
that the routes are wired behind the central auth/CSRF gates in server.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from nda_automation import docusign_connection, docusign_integration, user_store
from nda_automation.docusign_test_double import FakeDocuSignClient
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.routes import docusign as docusign_routes
from nda_automation import artifact_service
from nda_automation.artifact_registry import ACTOR_HUMAN, ROLE_REVIEWED, SOURCE_GENERATED

OWNER = "google:route-owner"
OTHER = "google:other-owner"
PDF_BYTES = b"%PDF-1.4 reviewed body"


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path, monkeypatch):
    """Pin the user store to a per-test tmp file.

    These tests exercise user_store.create_oauth_state / consume_oauth_state via
    the DocuSign connect flow, which read and rewrite the on-disk store. Without
    isolation those writes land on NDA_USERS_PATH / NDA_DATA_DIR/users.json — the
    operator's real ./data when a developer runs pytest with .env loaded — and a
    store loaded empty is saved back empty, wiping their session. The global
    conftest already redirects this, but isolating here too keeps the file safe
    even if it is ever collected without that conftest.
    """
    monkeypatch.setenv("NDA_USERS_PATH", str(tmp_path / "users.json"))


class _FakeServer:
    """Server double exposing ``server_address`` (bind host/port).

    Defaults to a LOOPBACK bind: these route tests model the trusted local-dev
    context, under which the unsigned DocuSign webhook is permitted. A public bind
    with no HMAC key fails CLOSED (covered in test_docusign_webhook_harden).
    """

    def __init__(self, bind_host="127.0.0.1"):
        self.server_address = (bind_host, 8788)


class _FakeHandler:
    def __init__(self, repo, *, owner=OWNER, payload=None, path="/", raw_body=b"", headers=None, bind_host="127.0.0.1"):
        self.matter_repository = repo
        self.current_user_id = owner
        self.current_user = {"id": owner, "provider": "google", "email": "u@x.com"}
        self._payload = payload
        self.path = path
        self.rfile = io.BytesIO(raw_body)
        self.headers = headers or {"Content-Length": str(len(raw_body)), "Host": "app.test"}
        self.server = _FakeServer(bind_host)
        self.status = 200
        self.response = None
        self.sent_bytes = None
        self.redirect_to = None
        self.redirect_headers = None

    def _read_json_payload(self):
        return self._payload

    def _read_content_length(self):
        raw = self.headers.get("Content-Length")
        return int(raw) if raw is not None else 0

    def _send_json(self, payload, *, status=200, send_body=True, headers=None):
        self.status = status
        self.response = payload

    def _send_bytes(self, data, *, filename="", content_type=None, send_body=True):
        self.status = 200
        self.sent_bytes = data

    def _send_redirect(self, location, *, headers=None, send_body=True):
        self.status = 302
        self.redirect_to = location
        self.redirect_headers = headers or {}


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
    # Cleared for the send-for-signature review/approval gate: a genuinely
    # sendable matter has been human-reviewed (matter_cleared_for_signature).
    repo.update_matter_fields(matter_id, {"human_reviewed": True}, owner_user_id=owner)
    return matter_id


# --------------------------------------------------------------------------
# status / connect
# --------------------------------------------------------------------------
def test_status_reports_connected(repo, connected):
    handler = _FakeHandler(repo)
    docusign_routes.handle_docusign_status(handler)
    assert handler.response["connected"] is True
    assert handler.response["configured"] is True
    assert handler.response["account_label"] == "Acme"


def test_status_reports_needs_config_when_unconfigured(repo, monkeypatch):
    monkeypatch.delenv(docusign_connection.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(docusign_connection.CLIENT_SECRET_ENV, raising=False)
    handler = _FakeHandler(repo, owner="google:unconfigured")
    docusign_routes.handle_docusign_status(handler)
    assert handler.response["configured"] is False
    assert handler.response["needs_config"] is True


def test_connect_get_redirects_to_docusign_consent(repo, monkeypatch):
    """GET /api/docusign/connect 302s to DocuSign consent (the FE's primary path)."""
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "int-key")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    monkeypatch.delenv(docusign_connection.AUTH_SERVER_ENV, raising=False)
    handler = _FakeHandler(repo, path="/api/docusign/connect?next=%2Fadmin")
    docusign_routes.handle_docusign_connect_start(handler)
    assert handler.status == 302
    location = handler.redirect_to
    assert location.startswith("https://account-d.docusign.com/oauth/auth?")
    parsed = parse_qs(urlsplit(location).query)
    assert parsed["client_id"][0] == "int-key"
    assert parsed["redirect_uri"][0].endswith("/auth/docusign/callback")
    assert parsed["state"][0]


def test_connect_get_next_round_trips_into_oauth_state(repo, monkeypatch):
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "int-key")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    handler = _FakeHandler(repo, path="/api/docusign/connect?next=%2Fadmin%3Ftab%3Dintegrations")
    docusign_routes.handle_docusign_connect_start(handler)
    state = parse_qs(urlsplit(handler.redirect_to).query)["state"][0]
    record = user_store.consume_oauth_state(state, purpose="docusign", user_id=OWNER)
    assert record is not None
    assert record["next_path"] == "/admin?tab=integrations"


def test_connect_get_unauthenticated_redirects_with_error_not_404(repo, monkeypatch):
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "int-key")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    # Force a real-auth deployment so an empty request owner is genuinely
    # unauthenticated (NOT the no-login local-dev path, which substitutes an owner).
    monkeypatch.setenv("NDA_REQUIRE_AUTH", "1")
    handler = _FakeHandler(repo, owner="", path="/api/docusign/connect?next=%2Fadmin")
    docusign_routes.handle_docusign_connect_start(handler)
    assert handler.status == 302
    assert handler.redirect_to == "/admin?docusign_error=signin_required"


def test_connect_get_unconfigured_redirects_with_error_not_404(repo, monkeypatch):
    monkeypatch.delenv(docusign_connection.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(docusign_connection.CLIENT_SECRET_ENV, raising=False)
    handler = _FakeHandler(repo, path="/api/docusign/connect?next=%2Fadmin")
    docusign_routes.handle_docusign_connect_start(handler)
    assert handler.status == 302
    assert handler.redirect_to == "/admin?docusign_error=docusign_not_configured"


def test_connect_get_rejects_open_redirect_next(repo, monkeypatch):
    monkeypatch.delenv(docusign_connection.CLIENT_ID_ENV, raising=False)
    handler = _FakeHandler(repo, path="/api/docusign/connect?next=https%3A%2F%2Fevil.com")
    docusign_routes.handle_docusign_connect_start(handler)
    # An absolute/external next is forced back to a safe relative root.
    assert handler.redirect_to == "/?docusign_error=docusign_not_configured"


def test_connect_post_returns_authorization_url(repo, monkeypatch):
    """POST fallback still returns the consent URL as JSON."""
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "int-key")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    handler = _FakeHandler(repo, payload={"next": "/dashboard"})
    docusign_routes.handle_docusign_connect(handler)
    assert handler.response["authorization_url"].startswith("https://account-d.docusign.com/oauth/auth?")


def test_connect_post_blocked_when_unconfigured(repo, monkeypatch):
    monkeypatch.delenv(docusign_connection.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(docusign_connection.CLIENT_SECRET_ENV, raising=False)
    handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_docusign_connect(handler)
    assert handler.status == 409
    assert handler.response["needs_config"] is True


def test_disconnect_removes_token(repo, connected):
    handler = _FakeHandler(repo)
    docusign_routes.handle_docusign_disconnect(handler)
    assert handler.response["disconnected"] is True


# --------------------------------------------------------------------------
# no-login (loopback) mode: the OAuth connect must STICK under a local-dev owner
# --------------------------------------------------------------------------
@pytest.fixture
def _no_login_env(monkeypatch):
    """Put the app in no-login mode: OAuth configured but NO auth method/forcing."""
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "int-key")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    for var in ("NDA_GOOGLE_OAUTH_CLIENT_ID", "NDA_GOOGLE_OAUTH_CLIENT_SECRET",
                "NDA_AUTH_USERNAME", "NDA_AUTH_PASSWORD", "NDA_REQUIRE_AUTH"):
        monkeypatch.delenv(var, raising=False)


def _stub_token_exchange(monkeypatch, captured):
    """Stub the DocuSign OAuth network calls; capture the save_user_token owner."""
    monkeypatch.setattr(
        docusign_connection, "exchange_code_for_token",
        lambda code, redirect_uri="": {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    )
    monkeypatch.setattr(
        docusign_connection, "fetch_userinfo",
        lambda access_token: {
            "email": "dev@local",
            "accounts": [{"account_id": "a1", "base_uri": "https://demo.docusign.net",
                          "account_name": "LocalDev", "is_default": True}],
        },
    )
    real_save = docusign_connection.save_user_token

    def _capture_save(owner_user_id, token_response, account):
        captured["owner"] = owner_user_id
        return real_save(owner_user_id, token_response, account)

    monkeypatch.setattr(docusign_connection, "save_user_token", _capture_save)


def test_no_login_callback_saves_token_under_local_dev_owner(repo, monkeypatch, _no_login_env):
    """The callback (empty request owner) must persist a token under a stable owner."""
    captured = {}
    _stub_token_exchange(monkeypatch, captured)
    # An empty request owner = no-login mode (server leaves current_user_id "").
    state = user_store.create_oauth_state(
        purpose="docusign",
        user_id=docusign_connection.local_dev_owner_user_id(),
        next_path="/admin",
        metadata={"role": "docusign"},
    )
    handler = _FakeHandler(repo, owner="", path=f"/auth/docusign/callback?code=abc&state={state}")
    docusign_routes.handle_docusign_callback(handler)

    # save_user_token received a NON-EMPTY owner (the local-dev id), not "".
    assert captured["owner"] == docusign_connection.local_dev_owner_user_id()
    assert captured["owner"]
    assert handler.status == 302
    assert handler.redirect_headers.get("X-DocuSign-Connected") == "1"


def test_no_login_status_reads_connected_true_after_callback(repo, monkeypatch, _no_login_env):
    """A status read in no-login mode reports connected:true once the token is saved."""
    captured = {}
    _stub_token_exchange(monkeypatch, captured)
    state = user_store.create_oauth_state(
        purpose="docusign",
        user_id=docusign_connection.local_dev_owner_user_id(),
        next_path="/",
        metadata={"role": "docusign"},
    )
    connect_handler = _FakeHandler(repo, owner="", path=f"/auth/docusign/callback?code=abc&state={state}")
    docusign_routes.handle_docusign_callback(connect_handler)

    status_handler = _FakeHandler(repo, owner="")
    docusign_routes.handle_docusign_status(status_handler)
    assert status_handler.response["connected"] is True
    assert status_handler.response["configured"] is True
    assert status_handler.response["account_label"] == "LocalDev"


def test_resolve_owner_unchanged_when_auth_required(monkeypatch):
    """PROD-SAFETY: with auth forced, an empty owner is NEVER substituted."""
    monkeypatch.setenv("NDA_REQUIRE_AUTH", "1")
    assert docusign_connection.resolve_owner_user_id("") == ""
    # A real authenticated owner always passes through verbatim.
    assert docusign_connection.resolve_owner_user_id("google:123") == "google:123"


def test_resolve_owner_unchanged_when_google_configured(monkeypatch):
    """PROD-SAFETY: with Google OAuth configured, an empty owner is NOT substituted."""
    monkeypatch.delenv("NDA_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "csec")
    assert docusign_connection.resolve_owner_user_id("") == ""


# --------------------------------------------------------------------------
# send-for-signature
# --------------------------------------------------------------------------
def test_send_for_signature_success(repo, connected, fake_client):
    matter_id = _matter_with_reviewed(repo)
    handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(handler, f"/api/matters/{matter_id}/send-for-signature")
    assert handler.status == 201
    assert handler.response["envelope_id"]
    assert handler.response["status"] == "sent"
    assert "matter" in handler.response


def _matter_unreviewed(repo, *, owner=OWNER):
    """A freshly-generated (never-reviewed) matter: a generated-role document but
    NO approval/human-review. Mirrors what nda_generation_workflow creates with
    defer_ai_review=True. Must be BLOCKED by the send-for-signature gate.
    """
    from nda_automation.artifact_registry import ROLE_GENERATED  # noqa: PLC0415

    matter = repo.create_matter(
        source_filename="generated-nda.docx",
        document_bytes=b"original",
        extracted_text="text",
        review_result={},
        triage={},
        source_type="generated",
        board_column="generated",
        owner_user_id=owner,
        intake_metadata={"reply_to": "cp@acme.com", "sender": "cp@acme.com", "subject": "Gen NDA"},
    )
    matter_id = matter["id"]
    artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_GENERATED,
        document_bytes=PDF_BYTES,
        repository=repo,
        owner_user_id=owner,
    )
    return matter_id


def test_send_for_signature_unreviewed_is_blocked_403(repo, connected, fake_client):
    """P0: a freshly-generated, never-reviewed NDA must NOT be sent for signature.

    DocuSign is connected and the document is signable, so the ONLY thing that
    blocks the send is the review/approval gate. No envelope is created.
    """
    matter_id = _matter_unreviewed(repo)
    handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(handler, f"/api/matters/{matter_id}/send-for-signature")
    assert handler.status == 403
    assert handler.response["needs_review"] is True
    assert "Review and approve" in handler.response["error"]
    # No envelope was ever created on the unreviewed matter.
    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert docusign_routes.docusign_workflow.SIGNATURE_FIELD not in stored


def test_send_for_signature_owner_mismatch_is_404_noop(repo, connected, fake_client):
    matter_id = _matter_with_reviewed(repo, owner=OTHER)
    handler = _FakeHandler(repo, owner=OWNER, payload={})
    docusign_routes.handle_send_for_signature(handler, f"/api/matters/{matter_id}/send-for-signature")
    assert handler.status == 404
    # No envelope was created on the other tenant's matter.
    stored = repo.get_matter(matter_id, owner_user_id=OTHER)
    assert docusign_routes.docusign_workflow.SIGNATURE_FIELD not in stored


def test_send_for_signature_not_connected_is_409(repo, monkeypatch):
    matter_id = _matter_with_reviewed(repo)
    # get_client raises NotConnected (no token for this owner).
    monkeypatch.setattr(
        docusign_integration,
        "get_client",
        lambda *, owner_user_id="": (_ for _ in ()).throw(
            docusign_connection.DocuSignNotConnectedError("nope")
        ),
    )
    handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(handler, f"/api/matters/{matter_id}/send-for-signature")
    assert handler.status == 409
    assert handler.response["needs_connect"] is True


def test_send_for_signature_dead_grant_is_409_needs_reconnect(repo, monkeypatch):
    """A revoked/expired DocuSign grant on send surfaces a RECONNECT prompt (not a
    generic outage, and distinct from the never-connected case)."""
    matter_id = _matter_with_reviewed(repo)
    monkeypatch.setattr(
        docusign_integration,
        "get_client",
        lambda *, owner_user_id="": (_ for _ in ()).throw(
            docusign_connection.DocuSignReconnectRequiredError(
                "Your DocuSign authorization is no longer valid. Reconnect DocuSign to continue."
            )
        ),
    )
    handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(handler, f"/api/matters/{matter_id}/send-for-signature")
    assert handler.status == 409
    assert handler.response["needs_reconnect"] is True
    # Still carries connect_url so an existing connect-only FE can route the user.
    assert handler.response["connect_url"] == docusign_routes.DOCUSIGN_CONNECT_START_URL
    assert "reconnect" in handler.response["error"].lower()


def test_status_reports_needs_reconnect_after_dead_grant(repo, monkeypatch):
    """The status panel surfaces needs_reconnect once a refresh found the grant dead."""
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    docusign_connection.save_user_token(
        OWNER,
        {"access_token": "old", "refresh_token": "rt", "expires_in": -10},
        {"account_id": "a1", "base_uri": "https://demo.docusign.net", "account_name": "Acme", "email": "u@x.com"},
    )

    def dead_refresh(request, timeout=15):
        raise docusign_connection.urllib.error.HTTPError(
            "https://account-d.docusign.com/oauth/token", 400, "err", {}, None
        )

    monkeypatch.setattr(docusign_connection.urllib.request, "urlopen", dead_refresh)
    with pytest.raises(docusign_connection.DocuSignReconnectRequiredError):
        docusign_connection.access_token_for_user(OWNER)

    handler = _FakeHandler(repo, owner=OWNER)
    docusign_routes.handle_docusign_status(handler)
    assert handler.response["needs_reconnect"] is True
    assert "reconnect_message" in handler.response


def test_send_for_signature_duplicate_on_active_envelope_is_409(repo, connected, fake_client):
    """#30: a SECOND send on a matter with an active envelope is refused (409),
    not allowed to create a duplicate envelope to the counterparty."""
    matter_id = _matter_with_reviewed(repo)
    # First send creates the envelope.
    first = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(first, f"/api/matters/{matter_id}/send-for-signature")
    assert first.status == 201
    first_envelope = first.response["envelope_id"]
    assert first_envelope

    # Second send must be rejected WITHOUT minting a new envelope.
    second = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(second, f"/api/matters/{matter_id}/send-for-signature")
    assert second.status == 409
    assert second.response["already_sent"] is True
    # The stored envelope id is unchanged (no duplicate was created).
    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert stored[docusign_routes.docusign_workflow.SIGNATURE_FIELD]["envelope_id"] == first_envelope


def test_send_for_signature_resend_allowed_after_terminal_envelope(repo, connected, fake_client):
    """#30: a terminal envelope (voided) is NOT active — a legitimate resend is
    allowed (does not 409)."""
    matter_id = _matter_with_reviewed(repo)
    first = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(first, f"/api/matters/{matter_id}/send-for-signature")
    assert first.status == 201

    # Force the stored envelope to a terminal state.
    field = docusign_routes.docusign_workflow.SIGNATURE_FIELD
    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    signature = dict(stored[field])
    signature["status"] = "voided"
    repo.update_matter_fields(matter_id, {field: signature}, owner_user_id=OWNER)

    resend = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(resend, f"/api/matters/{matter_id}/send-for-signature")
    assert resend.status == 201


# --------------------------------------------------------------------------
# signature-status / signed-document
# --------------------------------------------------------------------------
def test_signature_status_after_send(repo, connected, fake_client):
    matter_id = _matter_with_reviewed(repo)
    send_handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(send_handler, f"/api/matters/{matter_id}/send-for-signature")

    status_handler = _FakeHandler(repo)
    docusign_routes.handle_signature_status(status_handler, f"/api/matters/{matter_id}/signature-status")
    assert status_handler.response["status"] == "sent"
    assert status_handler.response["completed"] is False


def test_signed_document_download_after_completion(repo, connected, fake_client):
    matter_id = _matter_with_reviewed(repo)
    send_handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(send_handler, f"/api/matters/{matter_id}/send-for-signature")
    envelope_id = repo.get_matter(matter_id, owner_user_id=OWNER)[
        docusign_routes.docusign_workflow.SIGNATURE_FIELD
    ]["envelope_id"]
    fake_client.complete(envelope_id)

    download_handler = _FakeHandler(repo)
    docusign_routes.handle_signed_document(download_handler, f"/api/matters/{matter_id}/signed-document")
    assert download_handler.sent_bytes is not None
    assert download_handler.sent_bytes.startswith(b"%PDF-")


def test_signed_document_404_before_completion(repo, connected, fake_client):
    matter_id = _matter_with_reviewed(repo)
    send_handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(send_handler, f"/api/matters/{matter_id}/send-for-signature")
    handler = _FakeHandler(repo)
    docusign_routes.handle_signed_document(handler, f"/api/matters/{matter_id}/signed-document")
    assert handler.status == 404


# --------------------------------------------------------------------------
# webhook (HMAC + completion)
# --------------------------------------------------------------------------
def _webhook_body(envelope_id, status="completed"):
    return json.dumps(
        {"event": "envelope-completed", "data": {"envelopeId": envelope_id, "envelopeSummary": {"status": status}}}
    ).encode("utf-8")


def test_webhook_completes_matter_and_captures_signed(repo, connected, fake_client, monkeypatch):
    monkeypatch.delenv(docusign_connection.CONNECT_HMAC_KEY_ENV, raising=False)
    matter_id = _matter_with_reviewed(repo)
    send_handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(send_handler, f"/api/matters/{matter_id}/send-for-signature")
    envelope_id = repo.get_matter(matter_id, owner_user_id=OWNER)[
        docusign_routes.docusign_workflow.SIGNATURE_FIELD
    ]["envelope_id"]
    fake_client.complete(envelope_id)

    body = _webhook_body(envelope_id)
    handler = _FakeHandler(repo, owner="", raw_body=body)
    docusign_routes.handle_docusign_webhook(handler)
    assert handler.response["matched"] is True
    assert handler.response["completed"] is True
    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["status"] == "fully_signed"


def test_webhook_unknown_envelope_is_acked(repo, monkeypatch):
    monkeypatch.delenv(docusign_connection.CONNECT_HMAC_KEY_ENV, raising=False)
    handler = _FakeHandler(repo, owner="", raw_body=_webhook_body("env-unknown"))
    docusign_routes.handle_docusign_webhook(handler)
    assert handler.response["matched"] is False
    assert handler.response["received"] is True


def test_webhook_rejects_bad_hmac_when_key_configured(repo, monkeypatch):
    monkeypatch.setenv(docusign_connection.CONNECT_HMAC_KEY_ENV, "the-secret")
    body = _webhook_body("env-1")
    handler = _FakeHandler(repo, owner="", raw_body=body, headers={
        "Content-Length": str(len(body)),
        docusign_routes.HMAC_SIGNATURE_HEADER: "wrong-signature",
    })
    docusign_routes.handle_docusign_webhook(handler)
    assert handler.status == 401


def test_webhook_accepts_valid_hmac(repo, connected, fake_client, monkeypatch):
    monkeypatch.setenv(docusign_connection.CONNECT_HMAC_KEY_ENV, "the-secret")
    matter_id = _matter_with_reviewed(repo)
    send_handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_send_for_signature(send_handler, f"/api/matters/{matter_id}/send-for-signature")
    envelope_id = repo.get_matter(matter_id, owner_user_id=OWNER)[
        docusign_routes.docusign_workflow.SIGNATURE_FIELD
    ]["envelope_id"]
    fake_client.complete(envelope_id)

    body = _webhook_body(envelope_id)
    signature = base64.b64encode(
        hmac.new(b"the-secret", body, hashlib.sha256).digest()
    ).decode("ascii")
    handler = _FakeHandler(repo, owner="", raw_body=body, headers={
        "Content-Length": str(len(body)),
        docusign_routes.HMAC_SIGNATURE_HEADER: signature,
    })
    docusign_routes.handle_docusign_webhook(handler)
    assert handler.response["matched"] is True


# --------------------------------------------------------------------------
# server wiring: routes behind the central auth/CSRF gates
# --------------------------------------------------------------------------
def test_routes_registered_in_server():
    from nda_automation import server

    assert server._GET_EXACT_ROUTES["/api/docusign/status"] is docusign_routes.handle_docusign_status
    # GET /api/docusign/connect is the primary connect path (302-redirect to consent);
    # the POST variant is a JSON fallback that returns the same authorization URL.
    assert server._GET_EXACT_ROUTES["/api/docusign/connect"] is docusign_routes.handle_docusign_connect_start
    assert server._POST_EXACT_ROUTES["/api/docusign/connect"] is docusign_routes.handle_docusign_connect
    assert server._POST_EXACT_ROUTES["/api/docusign/disconnect"] is docusign_routes.handle_docusign_disconnect
    # The OAuth callback is an authenticated GET (carries the app session cookie).
    assert server._GET_EXACT_ROUTES["/auth/docusign/callback"] is docusign_routes.handle_docusign_callback
