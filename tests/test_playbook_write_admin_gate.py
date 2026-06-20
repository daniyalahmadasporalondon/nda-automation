"""Authorization gate on the Playbook WRITE/authoring POST routes.

Security invariant under test: the playbook authoring endpoints rewrite the
GLOBAL playbook (the single source of truth every review runs against), so they
must be admin-only. Before this fix they passed through ``_authorize_request``
(any logged-in user) but NOT ``require_admin`` -- so any authenticated non-admin
could rewrite the global playbook. This module proves:

  * a NON-admin session gets 403 on every one of the six write/authoring POST
    handlers, and the gate fires at handler ENTRY (no mutation, no payload read);
  * an ADMIN session is NOT blocked by the gate on any of them (status != 403);
  * the GET playbook routes stay open to any logged-in user (reviews/display
    depend on them), so the gate did not over-reach onto the read paths.

The route bodies are driven through a fake handler (the same pattern
``test_admin_manager`` / ``test_playbook_publish_workflow`` use). ``request_is_admin``
is the real predicate; ``NDA_REQUIRE_AUTH`` is set so the admin gate is actually
enforced (loopback short-circuits to admin, which would mask the hole).
"""

from __future__ import annotations

import pytest

from nda_automation import app_settings, matter_store
from nda_automation.routes import playbook as playbook_routes


# --- fixtures ---------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_and_enforce_auth(tmp_path, monkeypatch):
    # Isolate persisted admin settings to a temp data dir.
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    # Force the non-loopback auth-required branch so the admin gate is REAL --
    # a loopback host would short-circuit every caller to admin and hide the hole.
    monkeypatch.setenv("NDA_REQUIRE_AUTH", "1")
    # No env-root admins unless a test opts in.
    monkeypatch.delenv("NDA_ADMIN_USERS", raising=False)
    yield


class _ExplodingPayload:
    """A sentinel that fails the test if a handler reads the body.

    A correctly-gated handler returns 403 at ENTRY, before ``_read_json_payload``
    is ever called. If the gate is missing (or placed after the body read), the
    handler will touch this and raise -- a louder failure than a wrong status.
    """

    def __getitem__(self, key):  # pragma: no cover - defensive
        raise AssertionError("handler read the request body before the admin gate")


class _FakeHandler:
    def __init__(self, *, user, host="app.example.com", payload=None):
        self.current_user = user
        self.current_user_id = (user or {}).get("id", "")
        self._payload = _ExplodingPayload() if payload is None else payload
        self.status = None
        self.response = None
        self.send_body = None
        self.read_count = 0
        self.server = _FakeServer(host)

    def _read_json_payload(self):
        self.read_count += 1
        return self._payload

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.response = payload
        self.send_body = send_body


class _FakeServer:
    def __init__(self, host):
        self.server_address = (host, 0)


def _google(email, *, sub="google:42"):
    return {"id": sub, "provider": "google", "email": email, "name": "X"}


def _set_admin(email):
    app_settings.update_admin_settings(
        {"admins": [{"email": email, "added_at": "t", "added_by": "seed"}]}
    )


# The six write/authoring POST handlers, by the playbook route function name.
WRITE_HANDLERS = [
    "handle_playbook_publish",  # POST /api/playbook/publish
    "handle_playbook_save",  # POST /api/playbook
    "handle_playbook_draft_save",  # POST /api/playbook/draft
    "handle_playbook_restore",  # POST /api/playbook/restore
    "handle_playbook_draft_discard",  # POST /api/playbook/discard-draft
    "handle_playbook_validate_draft",  # POST /api/playbook/validate-draft
]


# --- the hole / the fix: non-admins are blocked on every write route --------
@pytest.mark.parametrize("handler_name", WRITE_HANDLERS)
def test_non_admin_gets_403_on_every_write_route(handler_name):
    handler = _FakeHandler(user=_google("nobody@example.com"))
    getattr(playbook_routes, handler_name)(handler)
    assert handler.status == 403, f"{handler_name} did not 403 a non-admin"
    # Gate fires at ENTRY: the body was never read (no mutation could have run).
    assert handler.read_count == 0, f"{handler_name} read the body before gating"


@pytest.mark.parametrize("handler_name", WRITE_HANDLERS)
def test_admin_is_not_blocked_by_the_gate_on_every_write_route(handler_name):
    # An admin caller. We feed an empty-dict payload so the handler proceeds past
    # the gate into normal validation. The gate must NOT 403 the admin; whatever
    # status the body validation then produces is fine -- it just must not be 403.
    _set_admin("admin@example.com")
    handler = _FakeHandler(user=_google("admin@example.com"), payload={})
    getattr(playbook_routes, handler_name)(handler)
    assert handler.status != 403, f"{handler_name} wrongly 403'd an admin"


# --- the read paths stay open to any logged-in user -------------------------
def test_get_routes_remain_open_to_non_admin():
    for handler_name in ("handle_playbook_get", "handle_playbook_draft_get"):
        handler = _FakeHandler(user=_google("nobody@example.com"))
        getattr(playbook_routes, handler_name)(handler)
        assert handler.status == 200, f"{handler_name} should stay open to non-admins"
