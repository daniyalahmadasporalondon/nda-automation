"""Backend tests for the human counterparty-confirmation endpoint.

POST /api/matters/<id>/counterparty records a human override of the AI-extracted
counterparty as the authoritative value
(``{"name": <given>, "confidence": 1.0, "verified": true, "source": "human"}``)
at the durable ``matter["intake_metadata"]["counterparty"]`` location, which flips
``counterparty_needs_confirmation`` to false in ``public_matter``.

The owner is taken from the AUTHENTICATED request (handler.current_user_id), never
from the client body, so an owner-mismatched matter is treated as not-found with no
write performed. Blank names are rejected.
"""

from __future__ import annotations

import json

from nda_automation import matter_store
from nda_automation.matter_view import public_matter
from nda_automation.routes.matters import handle_matter_counterparty_confirm


class _FakeHandler:
    """Minimal handler stub: carries the authenticated owner + JSON body, and
    captures the route's _send_json response (status + parsed body)."""

    def __init__(self, payload, *, owner_user_id=""):
        self._payload = payload
        self.current_user_id = owner_user_id
        self.status = None
        self.body = None

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, body, status=200, headers=None, send_body=True):
        self.status = status
        # Round-trip through json so the captured body matches the wire shape.
        self.body = json.loads(json.dumps(body))


def _seed_matter(*, owner_user_id="", counterparty=None):
    # The counterparty block lands at intake_metadata["counterparty"] via the
    # creation-time copy from review_result["counterparty"] (the intake_metadata
    # PARAMETER flattens to top-level strings and drops nested dicts), so seed it
    # on the review_result to mirror the real intake path.
    review_result = {"clauses": []}
    if counterparty is not None:
        review_result["counterparty"] = counterparty
    return matter_store.create_matter(
        source_filename="Mutual NDA.docx",
        document_bytes=b"PK\x03\x04 original nda bytes",
        extracted_text="This Agreement is mutual.",
        review_result=review_result,
        triage={"triage_status": "review"},
        intake_metadata={"subject": "RE: NDA thread"},
        owner_user_id=owner_user_id,
    )


def _confirm(matter_id, name, *, owner_user_id=""):
    handler = _FakeHandler({"name": name}, owner_user_id=owner_user_id)
    handle_matter_counterparty_confirm(handler, f"/api/matters/{matter_id}/counterparty")
    return handler


def test_confirm_persists_human_override_and_clears_needs_confirmation():
    # Seed an UNVERIFIED extraction so the matter starts as needs_confirmation=True.
    matter = _seed_matter(
        owner_user_id="user-1",
        counterparty={"name": "Maybe Acme", "confidence": 0.4, "verified": False, "source": "ai"},
    )
    before = public_matter(matter)
    assert before["counterparty_needs_confirmation"] is True

    handler = _confirm(matter["id"], "  Acme Holdings Ltd  ", owner_user_id="user-1")
    assert handler.status == 200

    returned = handler.body["matter"]
    assert returned["counterparty_needs_confirmation"] is False
    assert returned["counterparty_verified"] is True
    assert returned["counterparty_source"] == "human"
    assert returned["counterparty_confidence"] == 1.0
    assert returned["counterparty"] == "Acme Holdings Ltd"  # trimmed display name

    # Durable: reloading the matter from the store reflects the override.
    reloaded = public_matter(matter_store.get_matter(matter["id"], owner_user_id="user-1"))
    assert reloaded["counterparty_needs_confirmation"] is False
    assert reloaded["counterparty_source"] == "human"
    stored = matter_store.get_matter(matter["id"], owner_user_id="user-1")
    assert stored["intake_metadata"]["counterparty"]["name"] == "Acme Holdings Ltd"
    assert stored["intake_metadata"]["counterparty"]["verified"] is True


def test_confirm_works_when_no_prior_counterparty_dict():
    matter = _seed_matter(owner_user_id="user-1")  # no counterparty block at all
    assert public_matter(matter)["counterparty_needs_confirmation"] is True

    handler = _confirm(matter["id"], "Globex Ltd", owner_user_id="user-1")
    assert handler.status == 200
    assert handler.body["matter"]["counterparty_needs_confirmation"] is False
    assert handler.body["matter"]["counterparty_source"] == "human"


def test_owner_mismatch_is_not_found_and_does_not_write():
    matter = _seed_matter(
        owner_user_id="owner-a",
        counterparty={"name": "Original", "confidence": 0.3, "verified": False, "source": "ai"},
    )
    handler = _confirm(matter["id"], "Attacker Co", owner_user_id="owner-b")
    assert handler.status == 404
    assert "error" in handler.body

    # No write happened: the real owner still sees the original unverified extraction.
    stored = matter_store.get_matter(matter["id"], owner_user_id="owner-a")
    assert stored["intake_metadata"]["counterparty"]["name"] == "Original"
    assert stored["intake_metadata"]["counterparty"]["source"] == "ai"
    assert public_matter(stored)["counterparty_needs_confirmation"] is True


def test_unknown_matter_is_not_found():
    handler = _confirm("matter_does_not_exist", "Acme", owner_user_id="user-1")
    assert handler.status == 404
    assert "error" in handler.body


def test_blank_name_is_rejected_and_does_not_write():
    matter = _seed_matter(
        owner_user_id="user-1",
        counterparty={"name": "Original", "confidence": 0.3, "verified": False, "source": "ai"},
    )
    for blank in ("", "   ", None, 123):
        handler = _FakeHandler({"name": blank}, owner_user_id="user-1")
        handle_matter_counterparty_confirm(handler, f"/api/matters/{matter['id']}/counterparty")
        assert handler.status == 400, blank
        assert "error" in handler.body

    # The original extraction is untouched -- still needs confirmation.
    stored = matter_store.get_matter(matter["id"], owner_user_id="user-1")
    assert stored["intake_metadata"]["counterparty"]["source"] == "ai"
    assert public_matter(stored)["counterparty_needs_confirmation"] is True


def test_malformed_matter_id_path_is_not_found():
    handler = _FakeHandler({"name": "Acme"}, owner_user_id="user-1")
    # A path with no id (empty segment) parses to None -> 404, no write attempted.
    handle_matter_counterparty_confirm(handler, "/api/matters//counterparty")
    assert handler.status == 404
