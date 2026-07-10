"""Per-user, no-admin-wall connect/archive contract for a NON-admin user.

A fresh non-admin Google user must be able to connect THEIR OWN Gmail / Drive /
DocuSign and archive a matter to THEIR OWN Drive — with no admin gate on any
per-user step. The global config endpoints (Gmail settings, Drive root-folder
config, drive-folders browse) stay admin-only; this file does NOT touch those.

The non-admin identity is modelled exactly like the production request path:

* host ``app.test`` (a non-loopback host) so ``request_is_admin`` is consulted
  for real and FAILS CLOSED (no ``NDA_ADMIN_USERS`` entry, no persisted admin
  email) — i.e. the handler is a genuine non-admin, not a loopback-trusted dev.
* a Google-provider session whose connected owner id is the user's own id
  (mirrors ``routes/common.request_owner_user_id`` +
  ``google_connection.connected_owner_user_id``).

Each test drives the REAL route body against that handler and asserts the
per-user action is NOT 403-gated and is scoped to the caller's own id.
"""

from __future__ import annotations

import io
from urllib.parse import parse_qs, urlparse

import pytest

from nda_automation import (
    app_settings,
    artifact_service,
    docusign_connection,
    docusign_integration,
    drive_integration,
    gmail_integration,
    google_connection,
    google_identity,
    matter_store,
    user_store,
)
from nda_automation.artifact_registry import ACTOR_HUMAN, ROLE_REVIEWED, SOURCE_GENERATED
from nda_automation.docusign_test_double import FakeDocuSignClient
from nda_automation.http_auth import request_is_admin
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.routes import docusign as docusign_routes
from nda_automation.routes import drive as drive_routes
from nda_automation.routes import gmail as gmail_routes

# Two distinct NON-admin Google users (neither is in NDA_ADMIN_USERS, which is
# unset in the test env, so both fail the admin check closed on a remote host).
USER_A = "google:nonadmin-alice"
USER_B = "google:nonadmin-bob"
NONADMIN_HOST = "app.test"  # non-loopback => admin check is enforced for real


class _FakeServer:
    def __init__(self, host=NONADMIN_HOST):
        self.server_address = (host, 8000)


class _FakeHandler:
    """A signed-in NON-admin Google session on a remote (non-loopback) host."""

    def __init__(
        self,
        repo,
        *,
        owner=USER_A,
        payload=None,
        path="/",
        host=NONADMIN_HOST,
        provider="google",
        email="alice@example.com",
    ):
        self.matter_repository = repo
        self.current_user_id = owner
        self.current_user = {"id": owner, "provider": provider, "email": email}
        self._payload = payload
        self.path = path
        self.server = _FakeServer(host)
        self.headers = {"Host": host}
        self.status = 200
        self.response = None
        self.redirect_to = None
        self.redirect_headers = None
        self.rfile = io.BytesIO(b"")

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, *, status=200, send_body=True, headers=None):
        self.status = status
        self.response = payload

    def _send_redirect(self, location, *, headers=None, send_body=True):
        self.status = 302
        self.redirect_to = location
        self.redirect_headers = headers or {}


@pytest.fixture
def repo():
    return InMemoryMatterRepository()


@pytest.fixture(autouse=True)
def _user_store_isolation(tmp_path, monkeypatch):
    # Keep oauth-state read/write off the operator's real users.json.
    monkeypatch.setenv("NDA_USERS_PATH", str(tmp_path / "users.json"))


def _reviewed_matter(repo, *, owner=USER_A):
    matter = repo.create_matter(
        source_filename="counterparty-nda.docx",
        document_bytes=b"original",
        extracted_text="text",
        review_result={},
        triage={},
        owner_user_id=owner,
        intake_metadata={"reply_to": "cp@cp.com", "sender": "cp@cp.com", "subject": "NDA"},
    )
    matter_id = matter["id"]
    artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_REVIEWED,
        document_bytes=b"%PDF-1.4 reviewed body",
        repository=repo,
        owner_user_id=owner,
    )
    # Cleared for the send-for-signature review/approval gate.
    repo.update_matter_fields(matter_id, {"human_reviewed": True}, owner_user_id=owner)
    return matter_id


# --------------------------------------------------------------------------
# Baseline: the modelled handler is a genuine NON-admin.
# --------------------------------------------------------------------------
def test_modelled_user_is_a_real_nonadmin():
    """Guard: if the env ever granted admin to this user the per-user assertions
    below would pass vacuously. Pin that the handler is truly non-admin."""
    assert (
        request_is_admin(
            user_id=USER_A,
            provider="google",
            host=NONADMIN_HOST,
            email="alice@example.com",
        )
        is False
    )


# --------------------------------------------------------------------------
# GMAIL: connect + import are per-user, never admin-gated.
# --------------------------------------------------------------------------
def test_nonadmin_gmail_connect_start_redirects_not_403(repo, monkeypatch):
    """A non-admin hitting /auth/gmail/start is sent to the Google consent screen
    (302), NOT blocked with a 403. The connect flow is per-user."""
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "client-123")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "secret-xyz")
    handler = _FakeHandler(repo, path="/auth/gmail/start?role=all")
    gmail_routes.handle_gmail_connect_start(handler)
    assert handler.status == 302, handler.response
    assert "accounts.google.com" in (handler.redirect_to or "")


def test_nonadmin_gmail_import_uses_own_owner_and_is_not_admin_gated(repo, monkeypatch):
    """POST /api/gmail/import imports the NON-admin caller's OWN inbox (scoped to
    their owner id) without any admin gate."""
    # A logged-in non-admin exists in the user store (per-user sync results are
    # recorded under their record). Mirrors the post-login state.
    user_store.upsert_google_user({"sub": "nonadmin-alice", "email": "alice@example.com", "name": "Alice"})
    captured = {}

    def fake_import(*, limit=10, query=None, owner_user_id=""):
        captured["owner_user_id"] = owner_user_id
        return {"imported": 0, "scanned": 0, "skipped": 0, "errors": []}

    monkeypatch.setattr(gmail_integration, "import_inbound_matters", fake_import)
    # DiskMatterRepository().deduplicate_gmail_matters is called after import;
    # stub it on the route module so we never touch disk.
    monkeypatch.setattr(
        gmail_routes,
        "DiskMatterRepository",
        lambda: type("_R", (), {"deduplicate_gmail_matters": lambda self, *, owner_user_id="": 0})(),
    )
    monkeypatch.setattr(gmail_integration, "gmail_status", lambda owner_user_id="": {"ok": True, "owner": owner_user_id})

    handler = _FakeHandler(repo, payload={"limit": 5})
    gmail_routes.handle_gmail_import(handler)

    assert handler.status == 200, handler.response
    # The import was scoped to the NON-admin caller's own id, not a global/admin one.
    assert captured["owner_user_id"] == USER_A


# --------------------------------------------------------------------------
# DRIVE: a non-admin archives to THEIR OWN Drive even with NO admin root folder.
# --------------------------------------------------------------------------
def test_nonadmin_drive_upload_falls_back_to_own_my_drive_without_admin_folder(repo, monkeypatch):
    """With NO admin-configured global root folder (folder_id ""), a non-admin's
    upload-matter still archives to THEIR OWN My Drive: sync_matter_folder is
    invoked with the EMPTY root id (which resolves to "NDAs" under My Drive) and
    the caller's OWN owner id — no admin gate, no admin folder required."""
    matter_id = _reviewed_matter(repo, owner=USER_A)

    # No admin global root folder is configured.
    monkeypatch.setattr(app_settings, "drive_settings", lambda: {"folder_id": "", "enabled": False})
    # The non-admin has connected their OWN Drive token.
    monkeypatch.setattr(drive_integration, "drive_connected", lambda owner_user_id="": owner_user_id == USER_A)

    captured = {}

    def fake_sync(**kwargs):
        captured.update(kwargs)
        return {
            "matter_folder_id": "mf1",
            "matter_folder_url": "https://drive.google.com/mf1",
            "synced_count": 1,
            "total_count": 1,
            "artifacts": [],
        }

    monkeypatch.setattr(drive_integration, "sync_matter_folder", fake_sync)

    handler = _FakeHandler(repo, payload={"matter_id": matter_id})
    drive_routes.handle_drive_upload_matter(handler)

    assert handler.status == 200, handler.response
    # Per-user destination: empty admin root => the user's own My Drive fallback.
    assert captured["root_folder_id"] == ""
    # The artifact-owner AND the Drive-token owner are BOTH the non-admin caller.
    assert captured["owner_user_id"] == USER_A
    assert captured["drive_token_owner_user_id"] == USER_A


def test_nonadmin_drive_upload_respects_admin_global_folder_when_set(repo, monkeypatch):
    """When an admin HAS set a global root folder, the non-admin's archive nests
    under it (the admin global config still applies) — confirming the fallback is
    a fallback, not a bypass of the admin setting."""
    matter_id = _reviewed_matter(repo, owner=USER_A)
    monkeypatch.setattr(app_settings, "drive_settings", lambda: {"folder_id": "ADMIN_ROOT", "enabled": True})
    monkeypatch.setattr(drive_integration, "drive_connected", lambda owner_user_id="": True)

    captured = {}

    def fake_sync(**kwargs):
        captured.update(kwargs)
        return {
            "matter_folder_id": "mf1",
            "matter_folder_url": "u",
            "synced_count": 1,
            "total_count": 1,
            "artifacts": [],
        }

    monkeypatch.setattr(drive_integration, "sync_matter_folder", fake_sync)

    handler = _FakeHandler(repo, payload={"matter_id": matter_id})
    drive_routes.handle_drive_upload_matter(handler)

    assert handler.status == 200, handler.response
    assert captured["root_folder_id"] == "ADMIN_ROOT"


def test_nonadmin_cannot_archive_another_users_matter(repo, monkeypatch):
    """Per-owner isolation: USER_A cannot archive USER_B's matter. The matter is
    not visible to A, so the route is a 400 'Matter not found' no-op and Drive is
    never touched."""
    other_matter = _reviewed_matter(repo, owner=USER_B)
    monkeypatch.setattr(drive_integration, "drive_connected", lambda owner_user_id="": True)

    called = []
    monkeypatch.setattr(drive_integration, "sync_matter_folder", lambda **kw: called.append(kw))

    handler = _FakeHandler(repo, owner=USER_A, payload={"matter_id": other_matter})
    drive_routes.handle_drive_upload_matter(handler)

    assert handler.status == 400
    assert "not found" in str(handler.response.get("error", "")).lower()
    assert called == []  # B's matter never reached the Drive layer for A


# --------------------------------------------------------------------------
# DOCUSIGN: a non-admin sends their own matter for signature end-to-end.
# --------------------------------------------------------------------------
def test_nonadmin_docusign_send_for_signature_completes(repo, monkeypatch):
    """A non-admin matter owner with their OWN connected DocuSign can POST
    /api/matters/<id>/send-for-signature and get a 201 with a real envelope —
    no admin gate, no global-identity assumption blocks it."""
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "int-key")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    # The non-admin has connected THEIR OWN DocuSign account.
    docusign_connection.save_user_token(
        USER_A,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
        {"account_id": "a1", "base_uri": "https://demo.docusign.net", "account_name": "Alice Co", "email": "alice@example.com"},
    )
    client = FakeDocuSignClient()
    monkeypatch.setattr(docusign_integration, "get_client", lambda *, owner_user_id="": client)

    matter_id = _reviewed_matter(repo, owner=USER_A)
    handler = _FakeHandler(repo, owner=USER_A, payload={}, path=f"/api/matters/{matter_id}/send-for-signature")
    docusign_routes.handle_send_for_signature(handler, handler.path)

    assert handler.status == 201, handler.response
    assert handler.response["envelope_id"]
    assert handler.response["status"] == "sent"
    # The send stamped the signature workflow onto the caller's OWN matter.
    stored = repo.get_matter(matter_id, owner_user_id=USER_A)
    assert docusign_routes.docusign_workflow.SIGNATURE_FIELD in stored


def test_nonadmin_cannot_send_another_users_matter_for_signature(repo, monkeypatch):
    """Per-owner isolation: USER_A cannot send USER_B's matter for signature —
    404 no-op, no envelope created on A's DocuSign account."""
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "int-key")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    docusign_connection.save_user_token(
        USER_A,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
        {"account_id": "a1", "base_uri": "https://demo.docusign.net", "account_name": "Alice Co", "email": "alice@example.com"},
    )
    client = FakeDocuSignClient()
    monkeypatch.setattr(docusign_integration, "get_client", lambda *, owner_user_id="": client)

    other_matter = _reviewed_matter(repo, owner=USER_B)
    handler = _FakeHandler(repo, owner=USER_A, payload={}, path=f"/api/matters/{other_matter}/send-for-signature")
    docusign_routes.handle_send_for_signature(handler, handler.path)

    assert handler.status == 404
    # B's matter was never sent (no signature workflow stamped on it).
    stored = repo.get_matter(other_matter, owner_user_id=USER_B)
    assert docusign_routes.docusign_workflow.SIGNATURE_FIELD not in stored


# --------------------------------------------------------------------------
# SIMPLE Gmail connect (aspora-people model): connect is provider-agnostic, the
# tokens bind to the SESSION's OWN id, and the connected mailbox is captured as
# display metadata + the domain-gate subject only -- the tenant NEVER moves.
# --------------------------------------------------------------------------
def _connect_gmail(
    repo,
    tmp_path,
    monkeypatch,
    *,
    owner,
    provider="google",
    session_email="user@example.com",
    connected_email=None,
    exchange=None,
    id_token_valid=True,
):
    """Drive the REAL /auth/gmail/start -> /auth/gmail/callback against a session.

    Stubs only the two network hops (the code->token exchange and Google's ID
    token verification); the identity + domain gate runs for real.
    """
    connected_email = session_email if connected_email is None else connected_email
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "client-123")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "secret-xyz")
    if exchange is None:
        exchange = {"access_token": "at", "refresh_token": "rt", "id_token": "idtok"}
    monkeypatch.setattr(google_connection, "exchange_oauth_code", lambda code, *, redirect_uri: dict(exchange))
    if id_token_valid:
        monkeypatch.setattr(
            google_identity,
            "verify_google_id_token",
            lambda id_token, **_kw: {"email": connected_email, "sub": "connected-sub"},
        )
    else:
        def _raise(id_token, **_kw):
            raise google_identity.GoogleIdentityError("id token invalid")

        monkeypatch.setattr(google_identity, "verify_google_id_token", _raise)

    start = _FakeHandler(
        repo, owner=owner, provider=provider, email=session_email, path="/auth/gmail/start?role=all"
    )
    gmail_routes.handle_gmail_connect_start(start)
    assert start.status == 302, start.response
    state = parse_qs(urlparse(start.redirect_to).query)["state"][0]

    callback = _FakeHandler(
        repo,
        owner=owner,
        provider=provider,
        email=session_email,
        path=f"/auth/gmail/callback?code=code&state={state}",
    )
    gmail_routes.handle_gmail_connect_callback(callback)
    return callback


def test_google_session_connects_own_mailbox_tokens_under_session_id(repo, tmp_path, monkeypatch):
    """(a) A Google session connects its OWN mailbox: tokens land under the
    session id (today's behavior), and the connected mailbox is recorded as
    display metadata under that same id -- the owner key is never the mailbox."""
    callback = _connect_gmail(
        repo, tmp_path, monkeypatch, owner=USER_A, provider="google",
        session_email="alice@example.com", connected_email="alice@example.com",
    )
    assert callback.status == 302, callback.response
    assert google_connection.user_token_path_for_role("inbound", USER_A).is_file()
    assert google_connection.user_token_path_for_role("outbound", USER_A).is_file()
    # The connected mailbox is NOT a token owner: no dir named after the email.
    assert not (tmp_path / "users" / "google" / "alice@example.com").exists()
    assert google_connection.connection_metadata_email(USER_A) == "alice@example.com"


def test_sso_session_connects_under_its_own_id(repo, tmp_path, monkeypatch):
    """(b) A NON-google (SSO) session connects: tokens key to ITS OWN id, and its
    matters isolate to that id. No google:<sub> tenant is involved."""
    sso_owner = "sso:okta-bob"
    callback = _connect_gmail(
        repo, tmp_path, monkeypatch, owner=sso_owner, provider="okta",
        session_email="bob@corp.example", connected_email="bob@gmail.example",
    )
    assert callback.status == 302, callback.response
    for role in ("inbound", "outbound", "drive"):
        assert google_connection.user_token_path_for_role(role, sso_owner).is_file()
    # Owner key is the SSO session id, NOT the connected Google mailbox.
    assert not (tmp_path / "users" / "google" / "bob@gmail.example").exists()
    assert google_connection.connection_metadata_email(sso_owner) == "bob@gmail.example"
    # Its matters/ledger/cursor all key to that same id: an owned matter isolates.
    matter = repo.create_matter(
        source_filename="n.docx", document_bytes=b"x", extracted_text="t",
        review_result={}, triage={}, owner_user_id=sso_owner,
    )
    assert repo.get_matter(matter["id"], owner_user_id=sso_owner) is not None
    assert repo.get_matter(matter["id"], owner_user_id="google:someone-else") is None


def test_absent_id_token_rejects_and_writes_nothing(repo, tmp_path, monkeypatch):
    """(c) An unverifiable/absent ID token rejects the connect and writes NOTHING
    (no tokens, no metadata) -- fail closed."""
    callback = _connect_gmail(
        repo, tmp_path, monkeypatch, owner=USER_A, provider="google",
        connected_email="alice@example.com",
        exchange={"access_token": "at", "refresh_token": "rt"},  # NO id_token
        id_token_valid=False,
    )
    assert callback.status == 502, callback.response
    assert not (tmp_path / "users" / "google" / USER_A).exists()
    assert google_connection.connection_metadata_email(USER_A) == ""


def test_domain_allowlist_unset_allows_any_mailbox(repo, tmp_path, monkeypatch):
    """(d.1) Allowlist UNSET -> any connected mailbox is accepted (preserves the
    default Render behavior; do NOT fail closed on an unconfigured allowlist)."""
    monkeypatch.delenv("NDA_ALLOWED_EMAIL_DOMAINS", raising=False)
    monkeypatch.delenv("NDA_ALLOWED_EMAILS", raising=False)
    callback = _connect_gmail(
        repo, tmp_path, monkeypatch, owner=USER_A, provider="google",
        connected_email="anyone@wherever.example",
    )
    assert callback.status == 302, callback.response
    assert google_connection.user_token_path_for_role("inbound", USER_A).is_file()


def test_domain_allowlist_set_in_domain_connects(repo, tmp_path, monkeypatch):
    """(d.2) Allowlist SET + in-domain mailbox -> connects."""
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", "aspora.com")
    monkeypatch.delenv("NDA_ALLOWED_EMAILS", raising=False)
    callback = _connect_gmail(
        repo, tmp_path, monkeypatch, owner=USER_A, provider="google",
        connected_email="ops@aspora.com",
    )
    assert callback.status == 302, callback.response
    assert google_connection.user_token_path_for_role("inbound", USER_A).is_file()
    assert google_connection.connection_metadata_email(USER_A) == "ops@aspora.com"


def test_domain_allowlist_set_out_of_domain_rejects_nothing_written(repo, tmp_path, monkeypatch):
    """(d.3) Allowlist SET + out-of-domain mailbox -> 403, NOTHING written, and
    the error names the rejected address."""
    monkeypatch.setenv("NDA_ALLOWED_EMAIL_DOMAINS", "aspora.com")
    monkeypatch.delenv("NDA_ALLOWED_EMAILS", raising=False)
    callback = _connect_gmail(
        repo, tmp_path, monkeypatch, owner=USER_A, provider="google",
        connected_email="stranger@evil.example",
    )
    assert callback.status == 403, callback.response
    assert "stranger@evil.example" in str(callback.response.get("error", ""))
    assert not (tmp_path / "users" / "google" / USER_A).exists()
    assert google_connection.connection_metadata_email(USER_A) == ""


def test_empty_session_user_id_refuses_connect_and_owner_never_wildcards(repo, monkeypatch):
    """(e) An empty/whitespace session user id cannot connect, and the matter
    owner-match never treats a real owner (or an ownerless matter) as a wildcard
    for an authenticated caller."""
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "client-123")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "secret-xyz")
    handler = _FakeHandler(repo, owner="   ", provider="google", path="/auth/gmail/start?role=all")
    gmail_routes.handle_gmail_connect_start(handler)
    assert handler.status == 403, handler.response
    # Cross-tenant isolation is intact: an owned matter matches only its owner,
    # and an ownerless matter is never served to an authenticated caller.
    assert matter_store._matter_owner_matches({"owner_user_id": "sso:alice"}, "sso:alice") is True
    assert matter_store._matter_owner_matches({"owner_user_id": "sso:alice"}, "sso:bob") is False
    assert matter_store._matter_owner_matches({}, "sso:alice") is False


def test_google_session_connecting_a_different_mailbox_keeps_tenancy(repo, tmp_path, monkeypatch):
    """(f) The aspora-people semantic: a Google session may connect a DIFFERENT
    mailbox. The tokens still key to the SESSION's id, "connected as" shows the
    other mailbox, and the session's matter list is UNCHANGED (no tenancy move)."""
    matter = repo.create_matter(
        source_filename="n.docx", document_bytes=b"x", extracted_text="t",
        review_result={}, triage={}, owner_user_id=USER_A,
    )
    before = [record["id"] for record in repo.list_matters(owner_user_id=USER_A)]

    callback = _connect_gmail(
        repo, tmp_path, monkeypatch, owner=USER_A, provider="google",
        session_email="alice@example.com", connected_email="other@othermail.example",
    )
    assert callback.status == 302, callback.response
    # Tokens STILL under the session id; connected-as shows the OTHER mailbox.
    assert google_connection.user_token_path_for_role("inbound", USER_A).is_file()
    assert not (tmp_path / "users" / "google" / "other@othermail.example").exists()
    assert google_connection.connection_metadata_email(USER_A) == "other@othermail.example"
    # Tenancy did NOT move: the matter list is unchanged and still owned by USER_A.
    after = [record["id"] for record in repo.list_matters(owner_user_id=USER_A)]
    assert after == before
    assert repo.get_matter(matter["id"], owner_user_id=USER_A) is not None


def test_disconnect_removes_only_the_session_tokens(repo, tmp_path, monkeypatch):
    """(g) Disconnect removes ONLY the session's own tokens (and its metadata);
    another user's tokens are never touched."""
    callback = _connect_gmail(
        repo, tmp_path, monkeypatch, owner=USER_A, provider="google",
        session_email="alice@example.com", connected_email="alice@example.com",
    )
    assert callback.status == 302, callback.response

    # A different user's token exists and must survive USER_A's disconnect.
    other_inbound = google_connection.user_token_path_for_role("inbound", USER_B)
    other_inbound.parent.mkdir(parents=True, exist_ok=True)
    other_inbound.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(gmail_integration, "gmail_status", lambda owner_user_id="": {"ok": True})
    handler = _FakeHandler(repo, owner=USER_A, provider="google", payload={"role": "all"})
    gmail_routes.handle_gmail_disconnect(handler)

    assert handler.status == 200, handler.response
    assert not google_connection.user_token_path_for_role("inbound", USER_A).exists()
    assert google_connection.connection_metadata_email(USER_A) == ""
    assert other_inbound.exists()  # USER_B untouched
