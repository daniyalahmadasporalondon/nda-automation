"""Guards for the non-admin developer-detail leak cleanup.

Two NON-admin-reachable backend surfaces used to leak internal developer
detail and must now stay clean:

* DocuSign config errors — ``/api/docusign/status`` ``config_message`` and the
  ``/api/docusign/connect`` (POST) 409 ``error``. Non-admins hit these, so the
  message must read "DocuSign isn't configured yet. Contact your
  administrator." and must NOT name ``NDA_DOCUSIGN_*`` env vars. The validation
  behaviour (status codes, ``needs_config`` flag) is unchanged.

* Dashboard-assistant citations — the ``citations[].title`` values that used to
  be ``nda_automation/...py`` paths must now be functional/feature names.

A leak is any Python module/file path (``nda_automation/``, ``*.py``) or an
env-var KEY name (``NDA_*``, ``OPENROUTER_API_KEY``).
"""

from __future__ import annotations

import io
import re

import pytest

from nda_automation import docusign_connection, dashboard_assistant
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.routes import docusign as docusign_routes

EXPECTED_MESSAGE = "DocuSign isn't configured yet. Contact your administrator."

# Developer-detail patterns that must NOT appear on a non-admin surface.
FORBIDDEN = {
    "nda_automation/ path": re.compile(r"nda_automation/"),
    ".py file path": re.compile(r"\.py\b"),
    "NDA_* env var": re.compile(r"\bNDA_[A-Z0-9_]+"),
    "OPENROUTER_API_KEY env var": re.compile(r"OPENROUTER_API_KEY"),
}


def _assert_clean(value: str, where: str) -> None:
    for name, pattern in FORBIDDEN.items():
        assert pattern.search(value) is None, f"{where} leaks {name}: {value!r}"


# --------------------------------------------------------------------------- #
# DocuSign config messages
# --------------------------------------------------------------------------- #

class _FakeHandler:
    def __init__(self, repo, *, owner="google:nonadmin", payload=None):
        self.matter_repository = repo
        self.current_user_id = owner
        self.current_user = {"id": owner, "provider": "google", "email": "u@x.com"}
        self._payload = payload
        self.path = "/"
        self.rfile = io.BytesIO(b"")
        self.headers = {"Content-Length": "0", "Host": "app.test"}
        self.status = 200
        self.response = None

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, *, status=200, send_body=True, headers=None):
        self.status = status
        self.response = payload

    def _send_redirect(self, location, *, headers=None, send_body=True):
        self.status = 302
        self.response = {"location": location}


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path, monkeypatch):
    monkeypatch.setenv("NDA_USERS_PATH", str(tmp_path / "users.json"))


@pytest.fixture
def repo():
    return InMemoryMatterRepository()


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    monkeypatch.delenv(docusign_connection.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(docusign_connection.CLIENT_SECRET_ENV, raising=False)


def test_status_config_message_is_clean_copy(repo):
    handler = _FakeHandler(repo)
    docusign_routes.handle_docusign_status(handler)
    # Behaviour unchanged: still flags needs_config.
    assert handler.response["configured"] is False
    assert handler.response["needs_config"] is True
    message = handler.response["config_message"]
    assert message == EXPECTED_MESSAGE
    _assert_clean(message, "docusign status config_message")


def test_connect_post_409_error_is_clean_copy(repo):
    handler = _FakeHandler(repo, payload={})
    docusign_routes.handle_docusign_connect(handler)
    # Behaviour unchanged: still a 409 with needs_config.
    assert handler.status == 409
    assert handler.response["needs_config"] is True
    error = handler.response["error"]
    assert error == EXPECTED_MESSAGE
    _assert_clean(error, "docusign connect 409 error")


# --------------------------------------------------------------------------- #
# Assistant citation titles
# --------------------------------------------------------------------------- #

def _all_assistant_citation_titles() -> list[str]:
    titles: list[str] = []

    # The how-it-works knowledge cards carry the bulk of the code citations.
    knowledge = dashboard_assistant._trusted_how_it_works_knowledge()
    for card in knowledge.values():
        for citation in card.get("citations", []):
            title = citation.get("title")
            if title:
                titles.append(str(title))

    # The outbound-email-template response builds its citations inline.
    class _Ctx:
        query = "what email templates do you send?"

    outbound = dashboard_assistant.outbound_email_template_response(_Ctx())
    for citation in outbound.get("citations", []):
        title = citation.get("title")
        if title:
            titles.append(str(title))

    return titles


def test_assistant_citation_titles_have_no_dev_detail():
    titles = _all_assistant_citation_titles()
    assert titles, "expected the assistant to expose code citation titles"
    for title in titles:
        _assert_clean(title, "assistant citation title")


def test_assistant_citation_titles_use_functional_names():
    titles = set(_all_assistant_citation_titles())
    # The map's functional names must have landed (proves the paths were
    # actually renamed, not merely deleted).
    for expected in {
        "Matter workflow",
        "Review Engine",
        "NDA generation",
        "Playbook Engine",
        "Gmail intake",
        "Assistant",
        "Outbound email",
        "Document sending",
    }:
        assert expected in titles, f"missing functional citation title: {expected!r}"
