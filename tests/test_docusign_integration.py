"""Unit tests for the DocuSign integration + connection layers.

Covers the REAL client and OAuth connection against injected fakes (no live
DocuSign), plus the test double's deterministic lifecycle. The running app uses
the real HttpDocuSignClient; these tests inject a fake HTTP transport / fake
urlopen so the real code paths are exercised without a network.
"""

from __future__ import annotations

import base64
import json

import pytest

from nda_automation import docusign_connection, docusign_integration
from nda_automation.docusign_integration import (
    DEFAULT_SIGNING_ORDER,
    SIGNING_ORDER_PARALLEL,
    SIGNING_ORDER_SEQUENTIAL,
    STATUS_COMPLETED,
    STATUS_SENT,
    STATUS_VOIDED,
    DocuSignEnvelopeNotFoundError,
    DocuSignError,
    HttpDocuSignClient,
    build_envelope_definition,
    normalize_signers,
)
from nda_automation.docusign_test_double import FakeDocuSignClient


# --------------------------------------------------------------------------
# Signer normalization + envelope definition
# --------------------------------------------------------------------------
def test_default_signing_order_is_parallel():
    assert DEFAULT_SIGNING_ORDER == SIGNING_ORDER_PARALLEL


def test_parallel_signers_share_routing_order_one():
    signers = normalize_signers(
        [{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@y.com"}]
    )
    assert [s.routing_order for s in signers] == [1, 1]


def test_sequential_signers_increment_routing_order():
    signers = normalize_signers(
        [{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@y.com"}],
        signing_order=SIGNING_ORDER_SEQUENTIAL,
    )
    assert [s.routing_order for s in signers] == [1, 2]


def test_parallel_collapses_explicit_order_to_one():
    signers = normalize_signers(
        [{"name": "A", "email": "a@x.com", "routing_order": 5}],
        signing_order=SIGNING_ORDER_PARALLEL,
    )
    assert signers[0].routing_order == 1


def test_signer_missing_email_rejected():
    with pytest.raises(DocuSignError):
        normalize_signers([{"name": "A", "email": ""}])


def test_empty_signers_rejected():
    with pytest.raises(DocuSignError):
        normalize_signers([])


def test_envelope_definition_has_document_recipients_and_anchor_tabs():
    signers = normalize_signers(
        [{"name": "Alice", "email": "alice@x.com", "anchor": "Alice"}]
    )
    definition = build_envelope_definition(b"%PDF-1.4 data", "nda.pdf", signers, email_subject="Sign")
    assert definition["status"] == STATUS_SENT
    assert definition["emailSubject"] == "Sign"
    document = definition["documents"][0]
    assert base64.b64decode(document["documentBase64"]) == b"%PDF-1.4 data"
    assert document["fileExtension"] == "pdf"
    recipient = definition["recipients"]["signers"][0]
    assert recipient["email"] == "alice@x.com"
    assert recipient["tabs"]["signHereTabs"][0]["anchorString"] == "Alice"
    assert recipient["tabs"]["dateSignedTabs"][0]["anchorString"] == "Alice"


def test_anchor_tab_offsets_stay_on_page():
    """Every anchor X/Y offset must be on-page (non-negative, bounded).

    A negative anchorXOffset (the old ``-180``) drove the tab past the left page
    edge and DocuSign 400'd the whole envelope with INVALID_USER_OFFSET. Guard
    that the emitted offsets can never leave the page on the left/top edges.
    """
    signers = normalize_signers(
        [
            {"name": "Alice", "email": "alice@x.com", "anchor": "\\sig_party_aspora\\"},
            {"name": "Bob", "email": "bob@x.com", "anchor": "\\sig_party_counterparty\\"},
        ]
    )
    definition = build_envelope_definition(b"%PDF-1.4 data", "nda.pdf", signers)
    max_offset = docusign_integration._MAX_ANCHOR_OFFSET_PIXELS
    for recipient in definition["recipients"]["signers"]:
        tabs = recipient["tabs"]
        for tab in tabs["signHereTabs"] + tabs["dateSignedTabs"]:
            x = int(tab["anchorXOffset"])
            y = int(tab["anchorYOffset"])
            assert 0 <= x <= max_offset, f"X offset {x} off-page"
            assert 0 <= y <= max_offset, f"Y offset {y} off-page"


def test_clamp_offset_forces_negative_and_oversize_on_page():
    """The clamp degrades a bad offset gracefully instead of letting it 400."""
    clamp = docusign_integration._clamp_offset
    max_offset = docusign_integration._MAX_ANCHOR_OFFSET_PIXELS
    assert clamp("-180") == "0"  # the exact value from the live 400
    assert clamp("-1") == "0"
    assert clamp("0") == "0"
    assert clamp("12") == "12"
    assert clamp(str(max_offset + 500)) == str(max_offset)
    assert clamp("not-a-number") == "0"


def test_envelope_definition_rejects_empty_document():
    with pytest.raises(DocuSignError):
        build_envelope_definition(b"", "nda.pdf", normalize_signers([{"name": "A", "email": "a@x.com"}]))


# --------------------------------------------------------------------------
# Real HttpDocuSignClient against an injected fake transport
# --------------------------------------------------------------------------
class _FakeTransport:
    """Records calls and returns scripted (status_code, payload/bytes) responses."""

    def __init__(self):
        self.json_calls = []
        self.byte_calls = []
        self.json_response = (201, {"envelopeId": "env-123", "status": "sent"})
        self.byte_response = (200, b"%PDF-1.4 executed")

    def request_json(self, method, url, *, headers, json_body):
        self.json_calls.append((method, url, headers, json_body))
        return self.json_response

    def request_bytes(self, method, url, *, headers):
        self.byte_calls.append((method, url, headers))
        return self.byte_response


@pytest.fixture
def fake_token(monkeypatch):
    monkeypatch.setattr(docusign_connection, "access_token_for_user", lambda owner: "tok-abc")


def _client(transport):
    return HttpDocuSignClient(
        owner_user_id="google:1",
        account_id="acct-9",
        base_uri="https://demo.docusign.net",
        http=transport,
    )


def test_real_client_create_envelope_posts_to_account_envelopes(fake_token):
    transport = _FakeTransport()
    client = _client(transport)
    result = client.create_envelope(
        b"%PDF-1.4 data", "nda.pdf", normalize_signers([{"name": "A", "email": "a@x.com"}])
    )
    assert result == {"envelope_id": "env-123", "status": "sent"}
    method, url, headers, body = transport.json_calls[0]
    assert method == "POST"
    assert url == "https://demo.docusign.net/restapi/v2.1/accounts/acct-9/envelopes"
    assert headers["Authorization"] == "Bearer tok-abc"
    assert body["status"] == STATUS_SENT


def test_real_client_get_status(fake_token):
    transport = _FakeTransport()
    transport.json_response = (200, {"status": "delivered"})
    client = _client(transport)
    assert client.get_envelope_status("env-123") == "delivered"
    method, url, _headers, _body = transport.json_calls[0]
    assert method == "GET"
    assert url.endswith("/envelopes/env-123")


def test_real_client_download_completed_returns_pdf_bytes(fake_token):
    transport = _FakeTransport()
    client = _client(transport)
    assert client.download_completed("env-123") == b"%PDF-1.4 executed"
    method, url, _headers = transport.byte_calls[0]
    assert method == "GET"
    assert url.endswith("/envelopes/env-123/documents/combined")


def test_real_client_void_envelope_puts_status(fake_token):
    transport = _FakeTransport()
    transport.json_response = (200, {"envelopeId": "env-123", "status": "voided"})
    client = _client(transport)
    result = client.void_envelope("env-123", "duplicate")
    assert result == {"envelope_id": "env-123", "status": STATUS_VOIDED}
    method, _url, _headers, body = transport.json_calls[0]
    assert method == "PUT"
    assert body["voidedReason"] == "duplicate"


def test_real_client_maps_404_to_not_found(fake_token):
    transport = _FakeTransport()
    transport.json_response = (404, {})
    client = _client(transport)
    with pytest.raises(DocuSignEnvelopeNotFoundError):
        client.get_envelope_status("missing")


def test_real_client_maps_401_to_not_connected(fake_token):
    transport = _FakeTransport()
    transport.json_response = (401, {})
    client = _client(transport)
    with pytest.raises(docusign_connection.DocuSignNotConnectedError):
        client.get_envelope_status("env-123")


def test_400_body_surfaces_docusign_error_code_and_message(fake_token):
    """A 400 with DocuSign's {errorCode, message} body is no longer a bare
    'HTTP 400' — the real reason rides along in the raised error so prod can be
    diagnosed (this is what unblocks the 'Send for signature' 400)."""
    transport = _FakeTransport()
    transport.json_response = (
        400,
        {
            "errorCode": "ENVELOPE_HAS_INVALID_RECIPIENTS",
            "message": "The recipient you have specified has no tabs assigned.",
        },
    )
    client = _client(transport)
    with pytest.raises(DocuSignError) as excinfo:
        client.create_envelope(
            b"%PDF-1.4 data", "nda.pdf", normalize_signers([{"name": "A", "email": "a@x.com"}])
        )
    text = str(excinfo.value)
    assert "HTTP 400" in text
    assert "ENVELOPE_HAS_INVALID_RECIPIENTS" in text
    assert "has no tabs assigned" in text


def test_400_folds_nested_error_details_into_message(fake_token):
    """When the offender is only in errorDetails (a common DocuSign 400 shape),
    that nested message is folded in too."""
    transport = _FakeTransport()
    transport.json_response = (
        400,
        {
            "errorCode": "INVALID_TAB_DEFINITION",
            "message": "A tab definition is invalid.",
            "errorDetails": [
                {"errorCode": "ANCHOR_TAB_STRING_NOT_FOUND", "message": "Anchor string not found for recipient 2."}
            ],
        },
    )
    client = _client(transport)
    with pytest.raises(DocuSignError) as excinfo:
        client.create_envelope(
            b"%PDF-1.4 data", "nda.pdf", normalize_signers([{"name": "A", "email": "a@x.com"}])
        )
    text = str(excinfo.value)
    assert "INVALID_TAB_DEFINITION" in text
    assert "Anchor string not found for recipient 2." in text


def test_400_detail_logged_to_stderr_sanitized(fake_token, capsys):
    """The detail is also logged as a single sanitized line for prod triage."""
    transport = _FakeTransport()
    transport.json_response = (
        400,
        {"errorCode": "USER_LACKS_PERMISSIONS", "message": "Line one.\nLine two."},
    )
    client = _client(transport)
    with pytest.raises(DocuSignError):
        client.create_envelope(
            b"%PDF-1.4 data", "nda.pdf", normalize_signers([{"name": "A", "email": "a@x.com"}])
        )
    err = capsys.readouterr().err
    assert "USER_LACKS_PERMISSIONS" in err
    assert "status=400" in err
    # Collapsed to one physical line (no raw newline from the message body).
    assert "Line one. Line two." in err
    # The bearer token must never appear in the log line.
    assert "tok-abc" not in err
    assert "Bearer" not in err


def test_400_without_body_still_raises_generic(fake_token):
    """A 400 with an empty/unparseable body degrades to the bare status (no crash)."""
    transport = _FakeTransport()
    transport.json_response = (400, {})
    client = _client(transport)
    with pytest.raises(DocuSignError) as excinfo:
        client.get_envelope_status("env-123")
    assert "HTTP 400" in str(excinfo.value)


def test_generated_matter_envelope_is_well_formed():
    """End-to-end well-formedness of the envelope built for a GENERATED NDA:
    every recipient has both signHere + dateSigned tabs, a unique recipientId, the
    document carries non-empty base64, and each recipient's anchor references that
    party's token (so neither side is tabless — the classic envelope-create 400)."""
    counterparty = {
        "name": "Acme Innovations",
        "email": "cp@acme.com",
        "role": "counterparty",
        "anchor": "\\sig_party_counterparty\\",
    }
    aspora = {
        "name": "Priya Nair",
        "email": "priya@aspora.com",
        "role": "aspora",
        "anchor": "\\sig_party_aspora\\",
    }
    signers = normalize_signers([counterparty, aspora])
    definition = build_envelope_definition(b"%PDF-1.4 generated nda body", "NDA.pdf", signers)

    document = definition["documents"][0]
    assert base64.b64decode(document["documentBase64"])  # non-empty
    assert document["fileExtension"] == "pdf"
    assert document["documentId"] == "1"

    recipients = definition["recipients"]["signers"]
    assert len(recipients) == 2
    # Unique recipientIds.
    assert len({r["recipientId"] for r in recipients}) == 2
    by_email = {r["email"]: r for r in recipients}
    for recipient in recipients:
        tabs = recipient["tabs"]
        # No signer is tabless — both a signHere and a dateSigned tab are present.
        assert tabs["signHereTabs"], "recipient has no signHere tab (DocuSign 400s on this)"
        assert tabs["dateSignedTabs"], "recipient has no dateSigned tab"
    # Each recipient's tabs anchor to ITS party's token, not the other party's.
    assert by_email["cp@acme.com"]["tabs"]["signHereTabs"][0]["anchorString"] == "\\sig_party_counterparty\\"
    assert by_email["priya@aspora.com"]["tabs"]["signHereTabs"][0]["anchorString"] == "\\sig_party_aspora\\"


def test_real_client_segment_escaping_blocks_path_injection(fake_token):
    transport = _FakeTransport()
    transport.json_response = (200, {"status": "sent"})
    client = _client(transport)
    client.get_envelope_status("../accounts/evil")
    _method, url, _headers, _body = transport.json_calls[0]
    # The id is percent-escaped, so it cannot climb out of the envelopes path.
    assert "/envelopes/..%2Faccounts%2Fevil" in url


# --------------------------------------------------------------------------
# OAuth connection layer (real code, fake urlopen)
# --------------------------------------------------------------------------
@pytest.fixture
def configured_oauth(monkeypatch):
    monkeypatch.setenv(docusign_connection.CLIENT_ID_ENV, "int-key")
    monkeypatch.setenv(docusign_connection.CLIENT_SECRET_ENV, "secret")
    monkeypatch.setenv(docusign_connection.REDIRECT_URI_ENV, "https://app.test/auth/docusign/callback")
    monkeypatch.delenv(docusign_connection.AUTH_SERVER_ENV, raising=False)


def test_oauth_configured_reflects_env(configured_oauth):
    assert docusign_connection.oauth_configured() is True


def test_auth_server_defaults_to_demo(configured_oauth):
    assert docusign_connection.auth_server() == docusign_connection.AUTH_SERVER_DEMO
    assert docusign_connection.is_production() is False


def test_auth_server_production_when_set(configured_oauth, monkeypatch):
    monkeypatch.setenv(docusign_connection.AUTH_SERVER_ENV, "production")
    assert docusign_connection.auth_server() == docusign_connection.AUTH_SERVER_PRODUCTION
    assert docusign_connection.is_production() is True


def test_authorization_url_carries_code_grant_params(configured_oauth):
    url = docusign_connection.build_authorization_url(
        redirect_uri="https://app.test/auth/docusign/callback", state="st-1"
    )
    assert url.startswith("https://account-d.docusign.com/oauth/auth?")
    assert "response_type=code" in url
    assert "scope=signature+openid" in url
    assert "client_id=int-key" in url
    assert "state=st-1" in url


class _FakeHttpResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_exchange_code_for_token_uses_basic_auth(configured_oauth, monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=15):
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.data.decode("utf-8")
        return _FakeHttpResponse(
            {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "token_type": "Bearer"}
        )

    monkeypatch.setattr(docusign_connection.urllib.request, "urlopen", fake_urlopen)
    token = docusign_connection.exchange_code_for_token("the-code")
    assert token["access_token"] == "at"
    assert captured["url"] == "https://account-d.docusign.com/oauth/token"
    expected_basic = base64.b64encode(b"int-key:secret").decode("ascii")
    assert captured["auth"] == f"Basic {expected_basic}"
    assert "grant_type=authorization_code" in captured["body"]
    assert "code=the-code" in captured["body"]


def test_refresh_access_token_uses_refresh_grant(configured_oauth, monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=15):
        captured["body"] = request.data.decode("utf-8")
        return _FakeHttpResponse({"access_token": "at2", "expires_in": 3600})

    monkeypatch.setattr(docusign_connection.urllib.request, "urlopen", fake_urlopen)
    token = docusign_connection.refresh_access_token("rt")
    assert token["access_token"] == "at2"
    assert "grant_type=refresh_token" in captured["body"]
    assert "refresh_token=rt" in captured["body"]


def test_default_account_picks_default_with_base_uri():
    userinfo = {
        "email": "u@x.com",
        "accounts": [
            {"account_id": "a1", "is_default": False, "base_uri": "https://eu.docusign.net"},
            {"account_id": "a2", "is_default": True, "base_uri": "https://na.docusign.net", "account_name": "Acme"},
        ],
    }
    account = docusign_connection.default_account(userinfo)
    assert account["account_id"] == "a2"
    assert account["base_uri"] == "https://na.docusign.net"
    assert account["account_name"] == "Acme"


def test_default_account_raises_without_accounts():
    with pytest.raises(docusign_connection.DocuSignConnectionError):
        docusign_connection.default_account({"accounts": []})


# --------------------------------------------------------------------------
# Per-user token storage + refresh-on-expiry (real disk, tmp DATA_DIR)
# --------------------------------------------------------------------------
def _account():
    return {"account_id": "a1", "base_uri": "https://na.docusign.net", "account_name": "Acme", "email": "u@x.com"}


def test_save_and_read_token_round_trip(configured_oauth):
    owner = "google:save-read"
    docusign_connection.save_user_token(
        owner, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}, _account()
    )
    assert docusign_connection.is_connected(owner) is True
    account = docusign_connection.account_for_user(owner)
    assert account["account_id"] == "a1"
    assert account["base_uri"] == "https://na.docusign.net"
    # A fresh token is returned without a refresh.
    assert docusign_connection.access_token_for_user(owner) == "at"


def test_access_token_refreshes_when_expired(configured_oauth, monkeypatch):
    owner = "google:refresh-expired"
    docusign_connection.save_user_token(
        owner, {"access_token": "old", "refresh_token": "rt", "expires_in": -10}, _account()
    )

    def fake_urlopen(request, timeout=15):
        return _FakeHttpResponse({"access_token": "fresh", "refresh_token": "rt2", "expires_in": 3600})

    monkeypatch.setattr(docusign_connection.urllib.request, "urlopen", fake_urlopen)
    assert docusign_connection.access_token_for_user(owner) == "fresh"
    # The refreshed token is persisted, so a second call needs no refresh.
    monkeypatch.setattr(
        docusign_connection.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not refresh")),
    )
    assert docusign_connection.access_token_for_user(owner) == "fresh"


def test_disconnect_removes_token(configured_oauth):
    owner = "google:disconnect"
    docusign_connection.save_user_token(
        owner, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}, _account()
    )
    assert docusign_connection.disconnect_user(owner) is True
    assert docusign_connection.is_connected(owner) is False


def test_access_token_for_unconnected_user_raises():
    with pytest.raises(docusign_connection.DocuSignNotConnectedError):
        docusign_connection.access_token_for_user("google:never-connected")


# --------------------------------------------------------------------------
# Factory: real client by default, not-connected -> raise (no demo fallback)
# --------------------------------------------------------------------------
def test_get_client_returns_real_http_client_for_connected_user(configured_oauth):
    owner = "google:factory"
    docusign_connection.save_user_token(
        owner, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}, _account()
    )
    client = docusign_integration.get_client(owner_user_id=owner)
    assert isinstance(client, HttpDocuSignClient)


def test_get_client_raises_when_not_connected():
    with pytest.raises(docusign_connection.DocuSignNotConnectedError):
        docusign_integration.get_client(owner_user_id="google:not-connected")


def test_connection_status_shape(configured_oauth):
    owner = "google:status"
    docusign_connection.save_user_token(
        owner, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}, _account()
    )
    status = docusign_integration.connection_status(owner_user_id=owner)
    assert status["connected"] is True
    assert status["configured"] is True
    assert status["account_label"] == "Acme"
    assert status["production"] is False


# --------------------------------------------------------------------------
# Test double — deterministic lifecycle (NEVER the product path)
# --------------------------------------------------------------------------
def _signers():
    return normalize_signers([{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@y.com"}])


def test_fake_walks_sent_delivered_completed():
    client = FakeDocuSignClient()
    created = client.create_envelope(b"%PDF-1.4 d", "nda.pdf", _signers())
    assert created["status"] == STATUS_SENT
    envelope_id = created["envelope_id"]
    assert client.advance(envelope_id) == "delivered"
    assert client.advance(envelope_id) == STATUS_COMPLETED
    # Terminal: further advances are no-ops.
    assert client.advance(envelope_id) == STATUS_COMPLETED


def test_fake_download_completed_mints_valid_pdf():
    client = FakeDocuSignClient(auto_complete=True)
    created = client.create_envelope(b"%PDF-1.4 d", "nda.pdf", _signers())
    pdf = client.download_completed(created["envelope_id"])
    assert pdf.startswith(b"%PDF-")
    assert b"EXECUTED" in pdf


def test_fake_download_before_completed_raises():
    client = FakeDocuSignClient()
    created = client.create_envelope(b"%PDF-1.4 d", "nda.pdf", _signers())
    with pytest.raises(DocuSignError):
        client.download_completed(created["envelope_id"])


def test_fake_void_then_unknown_envelope():
    client = FakeDocuSignClient()
    created = client.create_envelope(b"%PDF-1.4 d", "nda.pdf", _signers())
    voided = client.void_envelope(created["envelope_id"], "no longer needed")
    assert voided["status"] == STATUS_VOIDED
    with pytest.raises(DocuSignEnvelopeNotFoundError):
        client.get_envelope_status("does-not-exist")
