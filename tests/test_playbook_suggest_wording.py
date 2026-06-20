"""POST /api/playbook/clause/<clause_id>/suggest-wording.

The endpoint asks the AI to propose MINIMAL edits to a clause's dependent free-text so
it reflects an admin's in-progress list change. It NEVER persists, never silently
ships invalid legal text (every proposed ``new`` is run through the SAME validation the
publish gate uses), and fails SOFT (an AI error returns empty suggestions + a warning,
never a 500 that would lose the admin's draft).

Covered:
  (a) a list change produces a ``new`` that INCORPORATES it while PRESERVING the rest;
  (b) the admin gate is enforced (a non-admin gets 403, body never read);
  (c) a suggestion that violates publish-gate validation sets ``validation_ok=False``
      and surfaces the reason in ``warnings`` (the bad wording is NOT hidden);
  (d) an AI error degrades to fail-soft: empty suggestions + a warning, status 200.

The AI is driven through the in-memory reviewer seam (a) / the stub env (b is gate-only)
so the tests are deterministic and network-free -- the same pattern the assessor tests
use.
"""

from __future__ import annotations

import pytest

from nda_automation import app_settings, matter_store
from nda_automation.checker import PLAYBOOK_PATH
from nda_automation.playbook_runtime import read_playbook_from_path
from nda_automation.playbook_suggest_wording import (
    AI_WORDING_SUGGEST_STUB_ENV,
    InMemoryWordingSuggestionReviewer,
    WordingSuggestionError,
    suggest_clause_wording,
)
from nda_automation.routes import playbook as playbook_routes


# --------------------------------------------------------------------------- #
# Fixtures: the real active confidential_information clause is our subject.    #
# --------------------------------------------------------------------------- #
@pytest.fixture
def ci_clause():
    playbook = read_playbook_from_path(PLAYBOOK_PATH)
    for clause in playbook["clauses"]:
        if clause.get("id") == "confidential_information":
            return dict(clause)
    raise AssertionError("confidential_information clause not found in active playbook")


# --------------------------------------------------------------------------- #
# (a) A list change is INCORPORATED while the rest of the wording is PRESERVED #
# --------------------------------------------------------------------------- #
def test_list_change_incorporated_and_rest_preserved(ci_clause):
    old_position = ci_clause["preferred_position"]
    # The admin has added a brand-new category to the structured list. The AI is asked
    # to weave it into the EXISTING preferred_position sentence, not rewrite it.
    new_category = "biometric data"
    ci_clause["definition_categories"] = list(ci_clause["definition_categories"]) + [new_category]
    woven = f"{old_position.rstrip()[:-1]}, {new_category}." if old_position.rstrip().endswith(".") else f"{old_position} {new_category}"
    reviewer = InMemoryWordingSuggestionReviewer(
        response={"preferred_position": woven}
    )

    result = suggest_clause_wording(
        clause=ci_clause,
        fields=["preferred_position"],
        reviewer=reviewer,
    )

    suggestion = result["suggestions"]["preferred_position"]
    assert suggestion["old"] == old_position
    assert suggestion["changed"] is True
    # The new wording INCORPORATES the added category...
    assert new_category in suggestion["new"]
    # ...while PRESERVING the admin's existing phrasing (everything before the splice
    # is verbatim -- a minimal, targeted edit, not a rewrite).
    assert suggestion["new"].startswith(old_position.rstrip()[:-1])
    assert result["validation_ok"] is True
    # The reviewer saw the UPDATED list in its packet (the change it must reflect).
    assert new_category in reviewer.packets[0]["updated_lists"]["definition_categories"]


def test_unchanged_field_is_marked_changed_false(ci_clause):
    # The model returns the wording verbatim -> changed=False, no validation needed.
    reviewer = InMemoryWordingSuggestionReviewer(
        response={"preferred_position": ci_clause["preferred_position"]}
    )
    result = suggest_clause_wording(
        clause=ci_clause, fields=["preferred_position"], reviewer=reviewer
    )
    suggestion = result["suggestions"]["preferred_position"]
    assert suggestion["changed"] is False
    assert suggestion["new"] == ci_clause["preferred_position"]
    assert result["validation_ok"] is True


def test_stub_env_incorporates_new_category(ci_clause, monkeypatch):
    # The key-free stub (the test seam used by the route smoke test) does the same
    # minimal-edit job mechanically, so the contract holds without an injected reviewer.
    monkeypatch.setenv(AI_WORDING_SUGGEST_STUB_ENV, "1")
    ci_clause["definition_categories"] = list(ci_clause["definition_categories"]) + ["geolocation"]
    result = suggest_clause_wording(clause=ci_clause, fields=["preferred_position"])
    suggestion = result["suggestions"]["preferred_position"]
    assert "geolocation" in suggestion["new"]
    assert suggestion["changed"] is True


# --------------------------------------------------------------------------- #
# (c) A suggestion that violates validation -> validation_ok=False + warning   #
# --------------------------------------------------------------------------- #
def test_validation_failure_sets_validation_ok_false(ci_clause):
    # The model proposes an absurdly long preferred_position. The publish gate caps
    # authored long-text length, so this must be flagged -- NOT returned as if safe.
    overlong = "x" * 5000
    reviewer = InMemoryWordingSuggestionReviewer(
        response={"preferred_position": overlong}
    )
    result = suggest_clause_wording(
        clause=ci_clause, fields=["preferred_position"], reviewer=reviewer
    )
    assert result["validation_ok"] is False
    # The proposed wording is still surfaced (so the admin SEES what was rejected),
    # but the verdict is unambiguous and the reason is in warnings.
    assert result["suggestions"]["preferred_position"]["new"] == overlong
    assert any("too long" in warning for warning in result["warnings"])
    assert any(warning.startswith("preferred_position:") for warning in result["warnings"])


# --------------------------------------------------------------------------- #
# (d) AI error -> fail-soft: empty suggestions + warning, no exception          #
# --------------------------------------------------------------------------- #
def test_ai_error_fails_soft(ci_clause):
    reviewer = InMemoryWordingSuggestionReviewer(
        error=WordingSuggestionError("OpenRouter API request failed: timed out")
    )
    result = suggest_clause_wording(
        clause=ci_clause, fields=["preferred_position"], reviewer=reviewer
    )
    # No suggestions, a warning explaining the AI failure, and validation stays ok
    # (there is nothing unsafe to flag) -- crucially, no exception bubbled up.
    assert result["suggestions"] == {}
    assert any("failed" in warning.lower() for warning in result["warnings"])
    assert result["validation_ok"] is True


def test_unexpected_reviewer_exception_fails_soft(ci_clause):
    # Even a non-WordingSuggestionError (a programming bug in a reviewer) is caught so
    # the admin's draft is never lost to a 500.
    reviewer = InMemoryWordingSuggestionReviewer(error=ValueError("boom"))
    result = suggest_clause_wording(
        clause=ci_clause, fields=["preferred_position"], reviewer=reviewer
    )
    assert result["suggestions"] == {}
    assert any("failed" in warning.lower() for warning in result["warnings"])


def test_no_relevant_fields_returns_empty_ok(ci_clause):
    # A field not in the suggestible allowlist is ignored; nothing to propose is not
    # an error.
    reviewer = InMemoryWordingSuggestionReviewer(response={"preferred_position": "x"})
    result = suggest_clause_wording(
        clause=ci_clause, fields=["search_terms", "not_a_field"], reviewer=reviewer
    )
    assert result == {"suggestions": {}, "warnings": [], "validation_ok": True}
    assert reviewer.packets == []  # the reviewer was never even called


# --------------------------------------------------------------------------- #
# (b) The admin gate is enforced on the route handler.                         #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_admin_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    # Force the real (non-loopback) admin gate -- a loopback host short-circuits every
    # caller to admin and would hide the hole.
    monkeypatch.setenv("NDA_REQUIRE_AUTH", "1")
    monkeypatch.delenv("NDA_ADMIN_USERS", raising=False)
    yield


class _ExplodingPayload:
    def __getitem__(self, key):  # pragma: no cover - defensive
        raise AssertionError("handler read the request body before the admin gate")


class _FakeServer:
    def __init__(self, host):
        self.server_address = (host, 0)


class _FakeHandler:
    def __init__(self, *, user, host="app.example.com", payload=None):
        self.current_user = user
        self.current_user_id = (user or {}).get("id", "")
        self._payload = _ExplodingPayload() if payload is None else payload
        self.status = None
        self.response = None
        self.read_count = 0
        self.server = _FakeServer(host)

    def _read_json_payload(self):
        self.read_count += 1
        return self._payload

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.response = payload


def _google(email, *, sub="google:42"):
    return {"id": sub, "provider": "google", "email": email, "name": "X"}


def _set_admin(email):
    app_settings.update_admin_settings(
        {"admins": [{"email": email, "added_at": "t", "added_by": "seed"}]}
    )


_SUGGEST_PATH = "/api/playbook/clause/confidential_information/suggest-wording"


def test_non_admin_gets_403_and_body_not_read():
    handler = _FakeHandler(user=_google("nobody@example.com"))
    playbook_routes.handle_playbook_suggest_wording(handler, _SUGGEST_PATH)
    assert handler.status == 403
    # The gate fires at ENTRY -- before the body (and any AI call) could run.
    assert handler.read_count == 0


def test_admin_is_not_blocked_by_the_gate(monkeypatch, ci_clause):
    monkeypatch.setenv(AI_WORDING_SUGGEST_STUB_ENV, "1")
    _set_admin("admin@example.com")
    ci_clause["definition_categories"] = list(ci_clause["definition_categories"]) + ["telemetry"]
    handler = _FakeHandler(
        user=_google("admin@example.com"),
        payload={"clause": ci_clause, "fields": ["preferred_position"]},
    )
    playbook_routes.handle_playbook_suggest_wording(handler, _SUGGEST_PATH)
    assert handler.status == 200
    assert "telemetry" in handler.response["suggestions"]["preferred_position"]["new"]
    assert "suggestions" in handler.response
    assert "warnings" in handler.response
    assert "validation_ok" in handler.response


def test_route_path_id_is_authoritative_over_body(monkeypatch):
    # A mismatched id in the body must not redirect the proposal/validation to a
    # different clause: the URL clause id wins.
    monkeypatch.setenv(AI_WORDING_SUGGEST_STUB_ENV, "1")
    _set_admin("admin@example.com")
    handler = _FakeHandler(
        user=_google("admin@example.com"),
        payload={"clause": {"id": "some_other_clause"}, "fields": []},
    )
    playbook_routes.handle_playbook_suggest_wording(handler, _SUGGEST_PATH)
    assert handler.status == 200


def test_missing_clause_object_is_400(monkeypatch):
    _set_admin("admin@example.com")
    handler = _FakeHandler(
        user=_google("admin@example.com"), payload={"fields": ["preferred_position"]}
    )
    playbook_routes.handle_playbook_suggest_wording(handler, _SUGGEST_PATH)
    assert handler.status == 400


def test_clause_id_parser():
    assert (
        playbook_routes.parse_suggest_wording_clause_id(_SUGGEST_PATH)
        == "confidential_information"
    )
    assert playbook_routes.parse_suggest_wording_clause_id("/api/playbook/publish") is None
    assert (
        playbook_routes.parse_suggest_wording_clause_id(
            "/api/playbook/clause//suggest-wording"
        )
        is None
    )
