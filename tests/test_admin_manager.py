"""Admin-manager tests: the persisted admin-grant predicate + the route handlers.

Covers the security invariants the gate checks:
  * ``request_is_admin`` grants a Google caller whose VERIFIED email is in the
    persisted list, but NOT a basic-auth username equal to that email.
  * Env roots win first and are immutable; at least one admin always remains
    (lockout impossible).
  * The three endpoints are admin-gated (403 for non-admins) and audit on mutate.

The route bodies are driven through a fake handler (the same pattern the other
route tests use); ``request_is_admin`` is exercised directly. ``NDA_REQUIRE_AUTH``
is set so a non-loopback admin gate is actually enforced (loopback short-circuits
to admin).
"""

from __future__ import annotations

import io
import json

import pytest

from nda_automation import app_settings, http_auth, matter_store, telemetry
from nda_automation.routes import admin as admin_routes


# --- fixtures ---------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    # Force the non-loopback auth-required branch so the admin gate is real.
    monkeypatch.setenv("NDA_REQUIRE_AUTH", "1")
    # Default: no env-root admins unless a test opts in.
    monkeypatch.delenv("NDA_ADMIN_USERS", raising=False)
    yield


class _FakeHandler:
    def __init__(self, *, user, payload=None, host="app.example.com"):
        self.current_user = user
        self.current_user_id = (user or {}).get("id", "")
        self._payload = payload
        self.status = None
        self.response = None
        self.server = _FakeServer(host)

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.response = payload


class _FakeServer:
    def __init__(self, host):
        self.server_address = (host, 0)


def _google(email, *, sub="google:42"):
    return {"id": sub, "provider": "google", "email": email, "name": "X"}


def _basic(username):
    return {"id": username, "provider": "basic", "email": username, "name": "X"}


def _set_persisted(*emails):
    app_settings.update_admin_settings(
        {"admins": [{"email": e, "added_at": "t", "added_by": "seed"} for e in emails]}
    )


# --- predicate: the critical positive + polarity tests ----------------------
def test_google_verified_email_in_persisted_list_is_admin():
    _set_persisted("alice@example.com")
    assert http_auth.request_is_admin(
        user_id="google:42", provider="google", host="1.2.3.4", email="alice@example.com"
    )


def test_basic_username_equal_to_admin_email_is_not_admin():
    # The SAME email under provider="basic" must NOT inherit admin -- a shared
    # basic-auth username colliding with an admin email is not a Google identity.
    _set_persisted("alice@example.com")
    assert not http_auth.request_is_admin(
        user_id="alice@example.com", provider="basic", host="1.2.3.4", email="alice@example.com"
    )


def test_persisted_email_match_is_case_insensitive():
    _set_persisted("mixed.case@x.com")
    assert http_auth.request_is_admin(
        user_id="google:42", provider="google", host="1.2.3.4", email="Mixed.Case@X.com"
    )


def test_unlisted_google_email_is_not_admin():
    _set_persisted("alice@example.com")
    assert not http_auth.request_is_admin(
        user_id="google:42", provider="google", host="1.2.3.4", email="bob@example.com"
    )


def test_env_root_wins_first(monkeypatch):
    monkeypatch.setenv("NDA_ADMIN_USERS", "google:42")
    # No persisted list at all -- env root alone grants admin.
    assert http_auth.request_is_admin(
        user_id="google:42", provider="google", host="1.2.3.4", email=""
    )


# --- env-root bootstrap BY EMAIL (NDA_ADMIN_USERS may hold emails) ----------
def test_env_root_email_grants_google_verified_caller(monkeypatch):
    monkeypatch.setenv("NDA_ADMIN_USERS", "alice@example.com")
    # No persisted list -- the env-root email alone bootstraps admin. Case of the
    # caller's verified email is irrelevant (normalized the same as persisted).
    assert http_auth.request_is_admin(
        user_id="google:42", provider="google", host="1.2.3.4", email="Alice@Example.com"
    )


def test_env_root_email_does_not_grant_basic_username_via_normalized_match(monkeypatch):
    # Only a Google-VERIFIED email may match the env list via NORMALIZATION
    # (mirrors the persisted-admin polarity). Here the env entry is mixed-case so
    # it does NOT verbatim-equal the basic username; the only way a basic caller
    # could be granted is the normalized-email path, which the provider gate must
    # block. (The legacy verbatim user_id==entry path is a separate, intentional
    # backward-compat match and is covered elsewhere.)
    monkeypatch.setenv("NDA_ADMIN_USERS", "Alice@Example.com")
    assert not http_auth.request_is_admin(
        user_id="alice@example.com", provider="basic", host="1.2.3.4", email="alice@example.com"
    )
    # ...but the SAME mixed-case env email DOES grant the Google-verified caller.
    assert http_auth.request_is_admin(
        user_id="google:42", provider="google", host="1.2.3.4", email="alice@example.com"
    )


def test_env_root_sub_still_grants_backward_compat(monkeypatch):
    # A bare google:<sub> entry must keep granting that user (user_id match path).
    monkeypatch.setenv("NDA_ADMIN_USERS", "google:12345")
    assert http_auth.request_is_admin(
        user_id="google:12345", provider="google", host="1.2.3.4", email=""
    )


def test_env_root_mixed_sub_and_email_both_grant(monkeypatch):
    monkeypatch.setenv("NDA_ADMIN_USERS", "google:12345, bob@example.com")
    # The sub-user matches by user_id...
    assert http_auth.request_is_admin(
        user_id="google:12345", provider="google", host="1.2.3.4", email=""
    )
    # ...and bob matches by verified email (different sub).
    assert http_auth.request_is_admin(
        user_id="google:99", provider="google", host="1.2.3.4", email="bob@example.com"
    )


def test_env_root_empty_and_no_persisted_is_fail_closed():
    # Autouse fixture deletes NDA_ADMIN_USERS; with no persisted admins either,
    # nobody is admin (fail closed) -- unchanged.
    assert not http_auth.request_is_admin(
        user_id="google:42", provider="google", host="1.2.3.4", email="alice@example.com"
    )


def test_predicate_fails_closed_when_settings_unreadable(monkeypatch):
    _set_persisted("alice@example.com")

    def _boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(app_settings, "persisted_admin_emails", _boom)
    assert not http_auth.request_is_admin(
        user_id="google:42", provider="google", host="1.2.3.4", email="alice@example.com"
    )


# --- route gate: non-admins get 403 on all three ---------------------------
def test_non_admin_gets_403_on_all_three_endpoints():
    handler_get = _FakeHandler(user=_google("nobody@example.com"))
    admin_routes.handle_admin_list(handler_get)
    assert handler_get.status == 403

    handler_add = _FakeHandler(user=_google("nobody@example.com"), payload={"email": "x@y.com"})
    admin_routes.handle_admin_add(handler_add)
    assert handler_add.status == 403

    handler_del = _FakeHandler(user=_google("nobody@example.com"), payload={"email": "x@y.com"})
    admin_routes.handle_admin_remove(handler_del)
    assert handler_del.status == 403
    # Nothing was written.
    assert app_settings.admin_settings()["admins"] == []


# --- add ---------------------------------------------------------------------
def _admin_handler(payload=None, *, admin_email="admin@example.com"):
    _set_persisted(admin_email)
    return _FakeHandler(user=_google(admin_email), payload=payload)


def test_add_grants_and_is_idempotent():
    telemetry.reset()
    handler = _admin_handler({"email": "New.Admin@Example.com"})
    admin_routes.handle_admin_add(handler)
    assert handler.status == 200
    emails = {e["email"] for e in handler.response["persisted_admins"]}
    assert "new.admin@example.com" in emails

    # Idempotent re-add: still 200, no duplicate.
    handler2 = _FakeHandler(user=_google("admin@example.com"), payload={"email": "new.admin@example.com"})
    admin_routes.handle_admin_add(handler2)
    assert handler2.status == 200
    persisted = [e for e in handler2.response["persisted_admins"] if e["email"] == "new.admin@example.com"]
    assert len(persisted) == 1


def test_add_rejects_missing_invalid_and_oversized_email():
    for bad in [None, "", "not-an-email", "a@b", "  "]:
        handler = _admin_handler({"email": bad})
        admin_routes.handle_admin_add(handler)
        assert handler.status == 400, bad

    oversized = "a" * 250 + "@example.com"  # > 254
    handler = _admin_handler({"email": oversized})
    admin_routes.handle_admin_add(handler)
    assert handler.status == 400


def test_add_rejects_markup_and_quote_bearing_email():
    # Defense-in-depth: an injection-style payload must be rejected with 400 at the
    # add endpoint AND never stored, even though the FE also escapes on render.
    for payload in ['"><img onerror=x>@evil.com', "a<b@x.com", "a>b@x.com", "a'b@x.com", 'a"b@x.com']:
        handler = _admin_handler({"email": payload})
        admin_routes.handle_admin_add(handler)
        assert handler.status == 400, payload
    # And nothing leaked into the stored list (only the seeded admin remains).
    assert {e["email"] for e in app_settings.admin_settings()["admins"]} == {"admin@example.com"}


def test_normalizer_filters_markup_bearing_email_on_write():
    # The write-path normalizer drops a markup/quote-bearing entry even if it
    # somehow reaches update_admin_settings directly (not via the route). The
    # payload is space-free so it PASSES the old local@domain regex -- only the
    # new forbidden-character block stops it (a genuine guard, not one the regex
    # incidentally catches via its whitespace exclusion).
    evil = '"><svg/onload=x>@evil.com'  # no spaces; passes the regex
    app_settings.update_admin_settings(
        {"admins": [
            {"email": evil, "added_at": "t", "added_by": "x"},
            {"email": "clean@example.com", "added_at": "t", "added_by": "x"},
        ]}
    )
    stored = {e["email"] for e in app_settings.admin_settings()["admins"]}
    assert stored == {"clean@example.com"}, stored
    assert app_settings.normalize_admin_email(evil) == ""


def test_add_refuses_env_root_email(monkeypatch):
    monkeypatch.setenv("NDA_ADMIN_USERS", "root@example.com")
    handler = _admin_handler({"email": "root@example.com"})
    admin_routes.handle_admin_add(handler)
    assert handler.status == 409


def test_add_records_audit_event():
    handler = _admin_handler({"email": "audited@example.com"})
    admin_routes.handle_admin_add(handler)
    events = app_settings.settings_audit_history()
    assert any(
        e["action"] == "admin_added"
        and any(c["after"] == "audited@example.com" for c in e["changes"])
        for e in events
    )


# --- remove ------------------------------------------------------------------
def test_remove_revokes_and_audits():
    _set_persisted("admin@example.com", "victim@example.com")
    handler = _FakeHandler(user=_google("admin@example.com"), payload={"email": "victim@example.com"})
    admin_routes.handle_admin_remove(handler)
    assert handler.status == 200
    emails = {e["email"] for e in handler.response["persisted_admins"]}
    assert "victim@example.com" not in emails
    assert "admin@example.com" in emails
    events = app_settings.settings_audit_history()
    assert any(e["action"] == "admin_removed" for e in events)


def test_remove_404s_unknown_email():
    handler = _admin_handler({"email": "ghost@example.com"})
    admin_routes.handle_admin_remove(handler)
    assert handler.status == 404


def test_remove_refuses_env_root(monkeypatch):
    monkeypatch.setenv("NDA_ADMIN_USERS", "root@example.com")
    # Make the caller an admin via a persisted entry so the gate passes first.
    _set_persisted("root@example.com", "admin@example.com")
    handler = _FakeHandler(user=_google("admin@example.com"), payload={"email": "root@example.com"})
    admin_routes.handle_admin_remove(handler)
    assert handler.status == 409


def test_remove_refuses_last_admin_lockout():
    # Single persisted admin, NO env roots: removing it would lock everyone out.
    _set_persisted("only@example.com")
    handler = _FakeHandler(user=_google("only@example.com"), payload={"email": "only@example.com"})
    admin_routes.handle_admin_remove(handler)
    assert handler.status == 409
    assert {e["email"] for e in app_settings.admin_settings()["admins"]} == {"only@example.com"}


def test_remove_last_persisted_allowed_when_env_root_remains(monkeypatch):
    # An env root keeps the floor, so removing the last PERSISTED admin is fine.
    monkeypatch.setenv("NDA_ADMIN_USERS", "google:root")
    _set_persisted("only@example.com")
    handler = _FakeHandler(user=_google("only@example.com"), payload={"email": "only@example.com"})
    admin_routes.handle_admin_remove(handler)
    assert handler.status == 200
    assert app_settings.admin_settings()["admins"] == []


# --- list --------------------------------------------------------------------
def test_list_returns_env_roots_and_persisted(monkeypatch):
    monkeypatch.setenv("NDA_ADMIN_USERS", "google:root, two@example.com")
    _set_persisted("two@example.com" if False else "admin@example.com", "extra@example.com")
    handler = _FakeHandler(user=_google("admin@example.com"))
    admin_routes.handle_admin_list(handler)
    assert handler.status == 200
    # env_root_admins is now an enriched view ({id, kind, email, display,
    # is_self, ...}); the SET of immutable ids must still be exactly the env
    # entries (the authorization surface is unchanged -- display only).
    env = handler.response["env_root_admins"]
    assert {row["id"] for row in env} == {"google:root", "two@example.com"}
    by_id = {row["id"]: row for row in env}
    # An email-shaped env root is shown by its email; a bare google:<sub> with no
    # known name gets the friendly "Google account ···<sub>" label, never a blank.
    assert by_id["two@example.com"]["display"] == "two@example.com"
    assert by_id["two@example.com"]["kind"] == "email"
    assert by_id["google:root"]["kind"] == "google"
    assert by_id["google:root"]["display"].startswith("Google account")
    assert {e["email"] for e in handler.response["persisted_admins"]} == {
        "admin@example.com",
        "extra@example.com",
    }


def test_list_labels_the_callers_own_env_root_with_email_and_is_self(monkeypatch):
    # The current session is a Google user whose bare google:<sub> id is an env
    # root. The list must surface THEIR verified email + is_self so the UI can
    # render "<email> (you)" instead of the opaque subject id.
    monkeypatch.setenv("NDA_ADMIN_USERS", "google:12345, other@example.com")
    handler = _FakeHandler(
        user={"id": "google:12345", "provider": "google", "email": "Me@Example.com", "name": "Mia"}
    )
    admin_routes.handle_admin_list(handler)
    assert handler.status == 200
    by_id = {row["id"]: row for row in handler.response["env_root_admins"]}
    mine = by_id["google:12345"]
    assert mine["is_self"] is True
    assert mine["email"] == "me@example.com"  # normalized verified email
    assert mine["display"] == "me@example.com"
    assert mine["name"] == "Mia"
    # The OTHER (email) env root is not the caller and stays not-self.
    assert by_id["other@example.com"]["is_self"] is False
    # A non-matching google root must NOT borrow the caller's email. The caller
    # stays an env root (google:12345) so the list still authorizes (200); the
    # OTHER google root (google:99999) is the one under test.
    monkeypatch.setenv("NDA_ADMIN_USERS", "google:12345, google:99999")
    handler2 = _FakeHandler(
        user={"id": "google:12345", "provider": "google", "email": "me@example.com"}
    )
    admin_routes.handle_admin_list(handler2)
    assert handler2.status == 200
    other = {row["id"]: row for row in handler2.response["env_root_admins"]}["google:99999"]
    assert other["is_self"] is False
    assert other["email"] == ""
    assert other["display"].startswith("Google account")
