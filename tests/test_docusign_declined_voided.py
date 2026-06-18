"""DocuSign terminal-but-not-signed transitions: DECLINED and VOIDED.

Regression guard for matters getting stuck on "Awaiting signature" forever when a
DocuSign envelope is declined (counterparty refused) or voided (sender cancelled).
``sync_signature_status`` only ever acted on ``completed``; declined/voided were
written into the docusign block but never cleared the awaiting state, so a dead
deal read identically to a live one in ``workflow._derive_phase_and_status``.

The two are split deliberately:

* DECLINED -> flagged "needs attention", stays visible (Sent column). NOT executed.
* VOIDED   -> re-sendable (back to Approval phase, Send re-enabled). NOT a failure.

The ``completed`` path is asserted UNCHANGED here so the split never regresses it.
"""

from __future__ import annotations

from nda_automation import docusign_workflow, workflow
from nda_automation.artifact_registry import (
    ACTOR_HUMAN,
    ROLE_REVIEWED,
    ROLE_SIGNED,
    SOURCE_GENERATED,
    latest_artifact_for_role,
)
from nda_automation import artifact_service
from nda_automation.docusign_test_double import FakeDocuSignClient

import pytest

OWNER = "google:wf"
PDF_BYTES = b"%PDF-1.4 reviewed nda body"


@pytest.fixture
def sent_matter(in_memory_matters):
    """A matter with a reviewed NDA already sent for signature (envelope live)."""
    matter = in_memory_matters.create_matter(
        source_filename="acme-nda.docx",
        document_bytes=b"original",
        extracted_text="text",
        review_result={},
        triage={},
        owner_user_id=OWNER,
        intake_metadata={"reply_to": "cp@acme.com", "sender": "cp@acme.com", "subject": "Acme NDA"},
    )
    matter_id = matter["id"]
    artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_REVIEWED,
        document_bytes=PDF_BYTES,
        repository=in_memory_matters,
        owner_user_id=OWNER,
    )
    matter = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    fake = FakeDocuSignClient()
    send = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    # Precondition: it really is in the awaiting-counterparty limbo before we sync.
    assert stored["awaiting_signature"] is True
    state = workflow.workflow_state(stored)
    assert state["status"] == workflow.STATUS_SENT_AWAITING_COUNTERPARTY
    return matter_id, fake, send.envelope_id


def _timeline_types(matter):
    timeline = matter.get("matter_timeline") or []
    return [str(e.get("type") or "") for e in timeline if isinstance(e, dict)]


# --------------------------------------------------------------------------- #
# DECLINED -> flagged, no longer awaiting, NOT executed
# --------------------------------------------------------------------------- #
def test_declined_sync_clears_awaiting_and_flags_attention(sent_matter, in_memory_matters):
    matter_id, fake, envelope_id = sent_matter

    fake.decline_envelope(envelope_id, email="cp@acme.com")
    result = docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )

    # NOT executed: the deal is dead, not done.
    assert result.completed is False
    assert result.status == "declined"
    assert result.signed_artifact_id == ""

    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    # Awaiting limbo cleared; declined marker set; never flipped to executed.
    assert stored["awaiting_signature"] is False
    assert stored["signature_declined"] is True
    assert stored.get("executed") is not True
    # Raw docusign status preserved.
    assert stored[docusign_workflow.SIGNATURE_FIELD]["status"] == "declined"

    # Timeline event recorded.
    assert "signature_declined" in _timeline_types(stored)

    # Workflow derivation surfaces it as a flagged, visible state (not awaiting).
    state = workflow.workflow_state(stored)
    assert state["status"] == workflow.STATUS_SIGNATURE_DECLINED
    assert state["needs_attention"] is True
    assert state["label"] == "Declined — needs attention"
    # Stays on the board (Sent column), not dropped off.
    assert state["board_column"] == workflow.BOARD_SENT
    assert state["phase"] == workflow.PHASE_SENT


# --------------------------------------------------------------------------- #
# VOIDED -> re-sendable, Send re-enabled, NOT a failure
# --------------------------------------------------------------------------- #
def test_voided_sync_returns_matter_to_resendable(sent_matter, in_memory_matters):
    matter_id, fake, envelope_id = sent_matter

    fake.void_envelope(envelope_id, reason="Cancelled to reissue.")
    result = docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )

    assert result.completed is False
    assert result.status == "voided"
    assert result.signed_artifact_id == ""

    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["awaiting_signature"] is False
    assert stored["signature_voided"] is True
    assert stored.get("executed") is not True
    assert stored[docusign_workflow.SIGNATURE_FIELD]["status"] == "voided"

    assert "signature_voided" in _timeline_types(stored)

    # Re-sendable: Approval phase, Reviewed column, Send re-enabled (not blocked,
    # NOT needs_attention).
    state = workflow.workflow_state(stored)
    assert state["status"] == workflow.STATUS_SIGNATURE_VOIDED
    assert state["needs_attention"] is False
    assert state["label"] == "Voided — ready to re-send"
    assert state["phase"] == workflow.PHASE_APPROVAL
    assert state["board_column"] == workflow.BOARD_REVIEWED
    # The next action lets the user re-send (a human gate, not a hard block).
    assert state["next_action"]["blocked"] is False
    assert state["next_action"]["owner"] == workflow.OWNER_HUMAN


# --------------------------------------------------------------------------- #
# COMPLETED path UNCHANGED by the split
# --------------------------------------------------------------------------- #
def test_completed_path_unchanged(sent_matter, in_memory_matters):
    matter_id, fake, envelope_id = sent_matter

    fake.complete(envelope_id)
    result = docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )

    assert result.completed is True
    assert result.status == "completed"
    assert result.signed_artifact_id

    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["executed"] is True
    assert stored["executed_at"]
    assert stored["status"] == "fully_signed"
    assert stored["awaiting_signature"] is False
    # The completed path never sets the terminal-not-signed markers.
    assert stored.get("signature_declined") is not True
    assert stored.get("signature_voided") is not True
    # Signed artifact captured, exactly as before the split.
    assert latest_artifact_for_role(stored, ROLE_SIGNED) is not None

    state = workflow.workflow_state(stored)
    assert state["status"] == workflow.STATUS_FULLY_SIGNED
    assert state["phase"] == workflow.PHASE_EXECUTED


# --------------------------------------------------------------------------- #
# RE-SEND after a terminal envelope clears the stale terminal flag.
#
# ``send_for_signature`` allows a re-send after a void/decline (routes/docusign.py
# explicitly permits it). A FRESH envelope must NOT inherit the previous
# envelope's terminal pin: ``_derive_phase_and_status`` checks signature_voided /
# signature_declined BEFORE the sent-status, so a stale flag would out-rank the
# live outbound and mislabel the matter as Voided/Declined.
# --------------------------------------------------------------------------- #
def test_resend_after_void_clears_voided_pin(sent_matter, in_memory_matters):
    matter_id, fake, envelope_id = sent_matter

    # 1) Void the first envelope -> matter reads "Voided — ready to re-send".
    fake.void_envelope(envelope_id, reason="Cancelled to reissue.")
    docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["signature_voided"] is True
    assert workflow.workflow_state(stored)["status"] == workflow.STATUS_SIGNATURE_VOIDED

    # 2) Re-send: a fresh envelope goes out.
    resend = docusign_workflow.send_for_signature(
        stored, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    assert resend.envelope_id and resend.envelope_id != envelope_id

    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    # The stale terminal pin (flag AND stamp) is gone; fresh outbound is awaiting.
    assert stored["signature_voided"] is False
    assert stored.get("signature_voided_at") is None
    assert stored["awaiting_signature"] is True

    state = workflow.workflow_state(stored)
    # Reads Sent / awaiting counterparty (NOT voided, NOT the Reviewed column).
    assert state["status"] == workflow.STATUS_SENT_AWAITING_COUNTERPARTY
    assert state["phase"] == workflow.PHASE_SENT
    assert state["board_column"] == workflow.BOARD_SENT
    assert state["needs_attention"] is False


def test_resend_after_decline_clears_declined_pin(sent_matter, in_memory_matters):
    matter_id, fake, envelope_id = sent_matter

    # 1) Decline the first envelope -> "Declined — needs attention".
    fake.decline_envelope(envelope_id, email="cp@acme.com")
    docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["signature_declined"] is True
    declined_state = workflow.workflow_state(stored)
    assert declined_state["status"] == workflow.STATUS_SIGNATURE_DECLINED
    assert declined_state["needs_attention"] is True

    # 2) Re-send: a fresh envelope is genuinely awaiting signature again.
    resend = docusign_workflow.send_for_signature(
        stored, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    assert resend.envelope_id and resend.envelope_id != envelope_id

    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["signature_declined"] is False
    assert stored.get("signature_declined_at") is None
    assert stored["awaiting_signature"] is True

    state = workflow.workflow_state(stored)
    # Sent / awaiting, NOT "Declined — needs attention" over a live envelope.
    assert state["status"] == workflow.STATUS_SENT_AWAITING_COUNTERPARTY
    assert state["phase"] == workflow.PHASE_SENT
    assert state["board_column"] == workflow.BOARD_SENT
    assert state["needs_attention"] is False


def test_first_time_send_is_clean_awaiting(sent_matter, in_memory_matters):
    """A first-time send (no prior terminal) still derives to Sent/awaiting and
    never carries the terminal flags. Guards against the re-send clear changing
    the ordinary first-send behaviour."""
    matter_id, _fake, _envelope_id = sent_matter

    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert stored["awaiting_signature"] is True
    # Re-send clear writes explicit False/None; first send must not read truthy.
    assert stored.get("signature_voided") is not True
    assert stored.get("signature_declined") is not True

    state = workflow.workflow_state(stored)
    assert state["status"] == workflow.STATUS_SENT_AWAITING_COUNTERPARTY
    assert state["phase"] == workflow.PHASE_SENT
    assert state["needs_attention"] is False
