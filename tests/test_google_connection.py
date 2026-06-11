from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlparse

from nda_automation import google_connection, matter_store


class _FakeServer:
    server_address = ("127.0.0.1", 8787)


class _FakeHandler:
    def __init__(self, headers):
        self.headers = headers
        self.server = _FakeServer()


def test_connected_owner_user_id_requires_google_provider():
    assert google_connection.connected_owner_user_id(
        {"provider": "google", "email": "alice@example.com"},
        owner_user_id="google:alice",
    ) == "google:alice"
    assert google_connection.connected_owner_user_id(
        {"provider": "basic", "email": "alice@example.com"},
        owner_user_id="basic:alice",
    ) == ""


def test_request_base_url_prefers_forwarded_headers():
    handler = _FakeHandler({
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "nda.example.com",
        "Host": "127.0.0.1:8787",
    })

    assert google_connection.request_base_url(handler) == "https://nda.example.com"


def test_drive_role_uses_only_drive_file_scope():
    assert google_connection.GOOGLE_OAUTH_SCOPES_BY_ROLE["drive"] == (
        "https://www.googleapis.com/auth/drive.file",
    )
    assert google_connection.oauth_roles_for_role("drive") == ("drive",)
    assert google_connection.oauth_roles_for_role("all") == ("inbound", "outbound", "drive")
    assert "https://www.googleapis.com/auth/drive" not in google_connection.oauth_scopes_for_role("drive")


def test_user_token_path_lives_under_google_user_data(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)

    token_path = google_connection.user_token_path_for_role("inbound", "google:alice@example.com")

    assert token_path == (
        tmp_path
        / "users"
        / "google"
        / "google:alice@example.com"
        / google_connection.ROLE_LOCAL_TOKEN_FILENAME["inbound"]
    )


def test_save_user_oauth_token_writes_google_owned_token(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "client-123")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "secret-xyz")
    token_response = {"access_token": "access", "refresh_token": "refresh"}

    roles = google_connection.save_user_oauth_token("google:alice", token_response, role="drive")

    token_path = google_connection.user_token_path_for_role("drive", "google:alice")
    assert roles == ["drive"]
    assert token_path.is_file()
    payload = json.loads(token_path.read_text(encoding="utf-8"))
    assert payload["client_id"] == "client-123"
    assert payload["client_secret"] == "secret-xyz"
    assert payload["refresh_token"] == "refresh"
    assert payload["token"] == "access"
    assert payload["token_uri"] == google_connection.GOOGLE_OAUTH_TOKEN_URL
    assert payload["scopes"] == list(google_connection.oauth_scopes_for_role("drive"))
    assert tmp_path.joinpath("users", "gmail").exists() is False


def test_save_user_oauth_token_preserves_existing_refresh_token(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "client-123")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "secret-xyz")
    token_path = google_connection.user_token_path_for_role("inbound", "google:alice")
    token_path.parent.mkdir(parents=True)
    token_path.write_text(json.dumps({"refresh_token": "existing-refresh"}), encoding="utf-8")

    roles = google_connection.save_user_oauth_token("google:alice", {"access_token": "new-access"}, role="inbound")

    assert roles == ["inbound"]
    payload = json.loads(token_path.read_text(encoding="utf-8"))
    assert payload["refresh_token"] == "existing-refresh"
    assert payload["token"] == "new-access"


def test_save_user_oauth_token_preserves_legacy_gmail_refresh_token(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "client-123")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "secret-xyz")
    legacy_token_path = google_connection.legacy_user_token_path_for_role("inbound", "google:alice")
    legacy_token_path.parent.mkdir(parents=True)
    legacy_token_path.write_text(json.dumps({"refresh_token": "legacy-refresh"}), encoding="utf-8")

    roles = google_connection.save_user_oauth_token("google:alice", {"access_token": "new-access"}, role="inbound")

    token_path = google_connection.user_token_path_for_role("inbound", "google:alice")
    assert roles == ["inbound"]
    payload = json.loads(token_path.read_text(encoding="utf-8"))
    assert payload["refresh_token"] == "legacy-refresh"
    assert payload["token"] == "new-access"


def test_disconnect_user_oauth_removes_google_owned_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    inbound_token = google_connection.user_token_path_for_role("inbound", "google:alice")
    outbound_token = google_connection.user_token_path_for_role("outbound", "google:alice")
    inbound_token.parent.mkdir(parents=True)
    inbound_token.write_text("{}", encoding="utf-8")
    outbound_token.write_text("{}", encoding="utf-8")

    removed = google_connection.disconnect_user_oauth("google:alice", role="inbound")

    assert removed == 1
    assert not inbound_token.exists()
    assert outbound_token.exists()


def test_global_token_path_falls_back_to_legacy_gmail_data_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.delenv(google_connection.ROLE_TOKEN_ENV["inbound"], raising=False)
    legacy_token = tmp_path / "gmail" / google_connection.ROLE_LOCAL_TOKEN_FILENAME["inbound"]
    legacy_token.parent.mkdir(parents=True)
    legacy_token.write_text(
        json.dumps({"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}),
        encoding="utf-8",
    )

    token_path = google_connection.token_path_for_role("inbound")
    status = google_connection.role_token_status("inbound")

    assert token_path == legacy_token
    assert status == {
        "configured": True,
        "label": "data/gmail/inbound-token.json",
        "source": "local_data",
        "scope_status": {
            "required": ["https://www.googleapis.com/auth/gmail.readonly"],
            "granted": ["https://www.googleapis.com/auth/gmail.readonly"],
            "missing": [],
            "ok": True,
        },
    }


def test_drive_token_path_can_recover_legacy_gmail_token_with_drive_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.delenv(google_connection.ROLE_TOKEN_ENV["drive"], raising=False)
    legacy_token = tmp_path / "gmail" / google_connection.ROLE_LOCAL_TOKEN_FILENAME["inbound"]
    legacy_token.parent.mkdir(parents=True)
    legacy_token.write_text(
        json.dumps({
            "scopes": [
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/drive.file",
            ],
        }),
        encoding="utf-8",
    )

    token_path = google_connection.token_path_for_role("drive")
    status = google_connection.role_token_status("drive")

    assert token_path == legacy_token
    assert status["configured"] is True
    assert status["source"] == "legacy_gmail_scope"
    assert status["scope_status"]["ok"] is True
    assert status["scope_status"]["missing"] == []


def test_user_drive_token_path_can_recover_legacy_gmail_token_with_drive_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    legacy_token = google_connection.legacy_user_token_path_for_role("inbound", "google:alice")
    legacy_token.parent.mkdir(parents=True)
    legacy_token.write_text(
        json.dumps({
            "scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/drive.file",
            ],
        }),
        encoding="utf-8",
    )

    token_path = google_connection.token_path_for_role("drive", owner_user_id="google:alice")
    status = google_connection.role_token_status("drive", owner_user_id="google:alice")

    assert token_path == legacy_token
    assert status["configured"] is True
    assert status["source"] == "legacy_gmail_scope"
    assert status["scope_status"]["ok"] is True
    assert status["scope_status"]["missing"] == []


def test_role_recovery_status_distinguishes_missing_oauth_from_missing_token(monkeypatch):
    monkeypatch.delenv("NDA_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)

    missing_oauth = google_connection.role_recovery_status(
        "drive",
        owner_user_id="",
        connect_url="/auth/google/start",
        integration="Drive",
    )

    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "client")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    missing_token = google_connection.role_recovery_status(
        "drive",
        owner_user_id="google:alice",
        connect_url="/auth/drive/start",
        integration="Drive",
    )

    assert missing_oauth["state"] == "missing_oauth_config"
    assert missing_oauth["action"] == "configure_google_oauth"
    assert missing_token["state"] == "missing_token"
    assert missing_token["action"] == "connect_google"


def test_role_recovery_status_reports_missing_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "client")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    token = google_connection.user_token_path_for_role("drive", "google:alice")
    token.parent.mkdir(parents=True)
    token.write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}), encoding="utf-8")

    recovery = google_connection.role_recovery_status(
        "drive",
        owner_user_id="google:alice",
        connect_url="/auth/drive/start",
        integration="Drive",
    )

    assert recovery["state"] == "missing_scope"
    assert recovery["action"] == "reconnect_google"
    assert recovery["scope_status"]["missing"] == ["https://www.googleapis.com/auth/drive.file"]


def test_build_authorization_url_uses_google_scopes_and_login_hint(monkeypatch):
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_ID", "client-123")
    monkeypatch.setenv("NDA_GOOGLE_OAUTH_CLIENT_SECRET", "secret-xyz")

    url = google_connection.build_authorization_url(
        redirect_uri="https://nda.example.com/auth/drive/callback",
        state="state-token",
        role="drive",
        login_hint="alice@example.com",
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["client-123"]
    assert query["redirect_uri"] == ["https://nda.example.com/auth/drive/callback"]
    assert query["scope"] == ["https://www.googleapis.com/auth/drive.file"]
    assert query["state"] == ["state-token"]
    assert query["prompt"] == ["select_account consent"]
    assert query["login_hint"] == ["alice@example.com"]


def test_write_token_atomically_preserves_existing_token_on_replace_failure(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token": "old"}', encoding="utf-8")
    temporary_path = token_path.parent / ".token.json.tmp"
    lock_path = token_path.parent / ".token.json.lock"

    google_connection.write_token_atomically(token_path, '{"token": "new"}')
    saved = token_path.read_text(encoding="utf-8")

    monkeypatch.setattr(os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    try:
        google_connection.write_token_atomically(token_path, '{"token": "corrupt"}')
    except google_connection.GoogleConnectionError as error:
        assert "token could not be saved" in str(error)
    else:
        raise AssertionError("Expected GoogleConnectionError")

    assert saved == '{"token": "new"}'
    assert token_path.read_text(encoding="utf-8") == '{"token": "new"}'
    assert not temporary_path.exists()
    assert lock_path.exists()
