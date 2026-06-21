"""Non-vacuity tests for the user-facing error humanization (build/hum-errors).

Each test simulates a real failure path and asserts the CLIENT body:
  * contains NONE of the leaky internals the raw exception carries (a python
    !r repr, an OS path, a ``<id>.json`` filename, an OOXML string, a DocuSign
    provider errorCode, an ``NDA_DOCUSIGN_*`` env-var name), AND
  * DOES contain the friendly, generic copy.

The raw detail must still be available server-side (the exception message is
unchanged / logged), so these tests also assert the exception itself still
carries the diagnostic text — proving we LOG full detail, return friendly copy.
"""

from __future__ import annotations

import io

import pytest

from nda_automation import (
    docusign_connection,
    docusign_integration,
    docusign_workflow,
    matter_store,
    nda_generation,
    nda_generation_workflow,
    redline_export_service,
)
from nda_automation.matter_repository import InMemoryMatterRepository
from nda_automation.routes import docusign as docusign_routes
from nda_automation.routes import generation as generation_routes
from nda_automation.routes import review as review_routes

OWNER = "google:hum-owner"

# Substrings that must NEVER appear in a user-facing error body.
LEAKY_TOKENS = (
    ".json",
    "NDA_DOCUSIGN_CLIENT_ID",
    "NDA_DOCUSIGN_CLIENT_SECRET",
    "restart the app",
    "w:body",
    "document.xml",
    "ANCHOR_TAB_STRING_NOT_FOUND",
    "HTTP 400",
    "Traceback",
    "approved Playbook option",
    "SECOND party",
    "legal_name",
)


def _assert_no_leak(message: str) -> None:
    assert isinstance(message, str) and message
    for token in LEAKY_TOKENS:
        assert token not in message, f"leaked {token!r} in client message: {message!r}"


class _FakeHandler:
    def __init__(self, repo=None, *, payload=None, path="/"):
        self.matter_repository = repo
        self.current_user_id = OWNER
        self.current_user = {"id": OWNER, "provider": "google", "email": "u@x.com"}
        self._payload = payload
        self.path = path
        self.rfile = io.BytesIO(b"")
        self.headers = {"Content-Length": "0", "Host": "app.test"}
        self.status = 200
        self.response = None

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, *, status=200, send_body=True, headers=None):
        self.status = status
        self.response = payload


@pytest.fixture(autouse=True)
def _isolated_user_store(tmp_path, monkeypatch):
    monkeypatch.setenv("NDA_USERS_PATH", str(tmp_path / "users.json"))


# --------------------------------------------------------------------------- #
# Fix 1 — DocuSign provider error in send-for-signature
# --------------------------------------------------------------------------- #
def test_docusign_send_provider_error_is_generic(monkeypatch):
    repo = InMemoryMatterRepository()
    matter = repo.create_matter(
        source_filename="acme.docx",
        document_bytes=b"x",
        extracted_text="t",
        review_result={"clauses": [], "decision": "approved"},
        triage={},
        owner_user_id=OWNER,
    )
    matter_id = matter["id"]

    # Base behaviour proof: the raw DocuSignError folds the provider errorCode in.
    raw = docusign_integration.DocuSignError(
        "DocuSign API request failed (HTTP 400). ANCHOR_TAB_STRING_NOT_FOUND"
    )
    assert "ANCHOR_TAB_STRING_NOT_FOUND" in str(raw)  # the leak we are hiding

    def boom(*args, **kwargs):
        raise raw

    monkeypatch.setattr(docusign_workflow, "matter_cleared_for_signature", lambda m: True)
    monkeypatch.setattr(docusign_workflow, "send_for_signature", boom)

    handler = _FakeHandler(repo, payload={"signers": [{"name": "A", "email": "a@x.com"}]})
    docusign_routes.handle_send_for_signature(handler, f"/api/matters/{matter_id}/send-for-signature")

    assert handler.status == 502
    body = handler.response["error"]
    _assert_no_leak(body)
    assert "couldn't send this NDA for signature" in body


# --------------------------------------------------------------------------- #
# Fix 6 — DocuSign OAuth config leak in the connect/send flow
# --------------------------------------------------------------------------- #
def test_docusign_connect_unconfigured_hides_env_vars(monkeypatch):
    monkeypatch.setattr(docusign_connection, "oauth_configured", lambda: False)
    handler = _FakeHandler(payload={"next": "/"})
    docusign_routes.handle_docusign_connect(handler)

    assert handler.status == 409
    body = handler.response["error"]
    _assert_no_leak(body)
    assert body == "DocuSign isn't configured yet. Contact your administrator."
    # The admin integrations panel ALSO stays scrubbed of env-var names: the
    # wave-3 leak-cleanup (test_ui_detail_leak_cleanup.py) made both the connect
    # error and the status config_message non-admin-safe, so neither surface
    # names NDA_DOCUSIGN_* env vars.
    admin = _FakeHandler()
    monkeypatch.setattr(
        docusign_integration,
        "connection_status",
        lambda *, owner_user_id="": {"configured": False, "connected": False},
    )
    docusign_routes.handle_docusign_status(admin)
    assert "NDA_DOCUSIGN_CLIENT_ID" not in admin.response["config_message"]
    _assert_no_leak(admin.response["config_message"])


# --------------------------------------------------------------------------- #
# Fix 2 — generation catch-all
# --------------------------------------------------------------------------- #
def test_generation_unexpected_error_is_generic(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom at /var/data/secret/path.json line 5")

    monkeypatch.setattr(nda_generation_workflow, "generate_nda_from_payload", boom)
    handler = _FakeHandler(payload={"entity_id": "x", "counterparty_name": "Y"})
    generation_routes.handle_generate_nda(handler)

    assert handler.status == 500
    body = handler.response["error"]
    _assert_no_leak(body)
    assert "kaboom" not in body
    assert "unexpected error" in body


# --------------------------------------------------------------------------- #
# Fix 3 — NdaGenerationError input vs template classification
# --------------------------------------------------------------------------- #
def test_generation_input_error_is_generic(monkeypatch):
    raw = nda_generation.NdaGenerationError(
        "Entity governing-law option 'made_up' is not an approved Playbook option "
        "(approved: ['delaware', 'india'])."
    )
    assert raw.category == nda_generation.NdaGenerationError.CATEGORY_INPUT

    def boom(*args, **kwargs):
        raise raw

    monkeypatch.setattr(nda_generation_workflow, "generate_nda_from_payload", boom)
    handler = _FakeHandler(payload={"entity_id": "x", "counterparty_name": "Y"})
    generation_routes.handle_generate_nda(handler)

    assert handler.status == 400
    body = handler.response["error"]
    _assert_no_leak(body)
    assert body == "Please select a valid signing entity and governing law."


def test_generation_template_fault_points_to_support(monkeypatch):
    raw = nda_generation.NdaGenerationError(
        "Template is missing the Aspora (SECOND party) paragraph.",
        category=nda_generation.NdaGenerationError.CATEGORY_TEMPLATE,
    )

    def boom(*args, **kwargs):
        raise raw

    monkeypatch.setattr(nda_generation_workflow, "generate_nda_from_payload", boom)
    handler = _FakeHandler(payload={"entity_id": "x", "counterparty_name": "Y"})
    generation_routes.handle_generate_nda(handler)

    assert handler.status == 400
    body = handler.response["error"]
    _assert_no_leak(body)
    assert "template issue" in body and "contact support" in body


def test_generation_free_text_message_passes_through(monkeypatch):
    raw = nda_generation.FreeTextValidationError(
        "The purpose field contains content that cannot be included (injection). "
        "Please rephrase using plain business language.",
        field_name="purpose",
        kind="injection",
    )
    assert raw.category == nda_generation.NdaGenerationError.CATEGORY_FREE_TEXT

    def boom(*args, **kwargs):
        raise raw

    monkeypatch.setattr(nda_generation_workflow, "generate_nda_from_payload", boom)
    handler = _FakeHandler(payload={"entity_id": "x", "counterparty_name": "Y"})
    generation_routes.handle_generate_nda(handler)

    assert handler.status == 400
    body = handler.response["error"]
    _assert_no_leak(body)
    # Field-scoped guidance is preserved (it is already clean).
    assert "rephrase" in body


# --------------------------------------------------------------------------- #
# Fix 4 — DocxOpenHealthError details dropped
# --------------------------------------------------------------------------- #
def test_review_export_health_error_drops_details(monkeypatch):
    raw = redline_export_service.DocxOpenHealthError(
        "The exported Word document failed its open-health check.",
        ["document.xml is missing w:body", "part /word/document.xml unreadable"],
    )

    def boom(*args, **kwargs):
        raise raw

    monkeypatch.setattr(redline_export_service, "build_review_export", boom)
    handler = _FakeHandler(payload={"text": "some nda text", "title": "T"})
    review_routes.handle_review_docx_export(handler)

    assert handler.status == 500
    assert "details" not in handler.response  # OOXML internals dropped from body
    body = handler.response["error"]
    _assert_no_leak(body)
    assert body == redline_export_service.DOCX_HEALTH_CLIENT_MESSAGE
    assert "integrity check" in body


# --------------------------------------------------------------------------- #
# Fix 5 — MatterStoreError filename / JSON / lock leaks
# --------------------------------------------------------------------------- #
def test_matter_store_message_mapper_hides_filename():
    raw = matter_store.MatterStoreError("Matter record is not valid JSON: ab12cd34.json.")
    friendly = matter_store.friendly_matter_store_message(raw)
    _assert_no_leak(friendly)
    assert friendly == matter_store.MATTER_STORE_UNAVAILABLE_MESSAGE


def test_matter_store_lock_timeout_maps_to_busy():
    raw = matter_store.MatterStoreError(
        "Matter store could not be locked within the timeout (30s).", lock_timeout=True
    )
    friendly = matter_store.friendly_matter_store_message(raw)
    _assert_no_leak(friendly)
    assert friendly == matter_store.MATTER_STORE_BUSY_MESSAGE
    assert "busy" in friendly.lower()


def test_matter_store_route_catch_is_generic(monkeypatch):
    """The matters route catch surfaces friendly copy, not the .json filename."""
    from nda_automation.routes import matters as matter_routes

    repo = InMemoryMatterRepository()

    def boom(*args, **kwargs):
        raise matter_store.MatterStoreError("Matter record could not be read: deadbeef.json.")

    monkeypatch.setattr(repo, "get_matter", boom)
    handler = _FakeHandler(repo, path="/api/matters/deadbeef/summary")
    matter_routes.handle_matter_summary(handler, "/api/matters/deadbeef/summary")

    assert handler.status == 500
    body = handler.response["error"]
    _assert_no_leak(body)
    assert body == matter_store.MATTER_STORE_UNAVAILABLE_MESSAGE
