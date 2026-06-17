"""Adversarial tests for the DocuSign send-for-signature hardening.

These pin the security/robustness fixes folded onto ``fix/docusign-recipient-gate``
(the DocuSign hardening branch). Every test in this file asserts the FIXED
behaviour and PASSES; the four originally-``xfail(strict=True)`` defect repros have
been turned green and are documented inline (``DEFECT N`` markers below) so the
provenance of each fix is clear:

* DEFECT 1 (P0) — recipient-confirmation gate. The counterparty signer email is
  read verbatim from the matter's inbound Reply-To/From (matter_reply_recipient),
  an attacker-controllable header, with no confirmation. A spoofed Reply-To could
  route a signature envelope to an attacker. FIXED: ``confirm_recipient`` must
  match the header-derived counterparty signer or the send is rejected (400).
* DEFECT 2 (HIGH) — double-send / concurrency. ``send_for_signature`` created a
  new envelope without checking for an existing live one, so a double-click/retry
  minted multiple real envelopes (earlier ones orphaned). FIXED: a live
  (non-terminal) envelope blocks a second send (409 ``already_sent``).
* DEFECT 3 (MEDIUM) — token-death mid-completion. ``_capture_executed_document``
  caught ``DocuSignError`` but not ``DocuSignNotConnectedError`` (a separate
  taxonomy), so a token expiring between status=completed and the download blocked
  the matter's flip to executed. FIXED: the download arm catches both.
* DEFECT 4 (P2) — signer email validation. ``normalize_signers`` accepted a
  malformed (no ``@``) or multi-address (comma-joined) signer email. FIXED:
  rejected before any envelope is built.

Plus a P3 self-send / duplicate-recipient guard mirroring the Gmail outbound flow.
"""

from __future__ import annotations

import pytest

from nda_automation import (
    artifact_service,
    docusign_connection,
    docusign_integration,
    docusign_workflow,
)
from nda_automation.artifact_registry import ACTOR_HUMAN, ROLE_REVIEWED, SOURCE_GENERATED
from nda_automation.docusign_test_double import FakeDocuSignClient

OWNER = "google:adv"
PDF_BYTES = b"%PDF-1.4 reviewed nda body"
INBOUND_RECIPIENT = "attacker-or-cp@acme.com"


@pytest.fixture
def matter_with_inbound_recipient(in_memory_matters):
    """A received-paper matter whose counterparty signer is derived from the
    inbound Reply-To header (the attacker-controllable surface)."""
    matter = in_memory_matters.create_matter(
        source_filename="acme-nda.docx",
        document_bytes=b"original",
        extracted_text="text",
        review_result={},
        triage={},
        owner_user_id=OWNER,
        intake_metadata={
            "reply_to": INBOUND_RECIPIENT,
            "sender": INBOUND_RECIPIENT,
            "subject": "Acme NDA",
        },
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
    return in_memory_matters.get_matter(matter_id, owner_user_id=OWNER), matter_id


# --------------------------------------------------------------------------- #
# DEFECT 1 (P0) — recipient-confirmation gate
# --------------------------------------------------------------------------- #
def test_DEFECT_send_without_confirm_recipient_is_rejected(
    matter_with_inbound_recipient, in_memory_matters
):
    """A header-derived counterparty signer with NO confirm_recipient is refused."""
    matter, matter_id = matter_with_inbound_recipient
    with pytest.raises(docusign_workflow.RecipientConfirmationError):
        docusign_workflow.send_for_signature(
            matter, matter_id, OWNER, repository=in_memory_matters, client=FakeDocuSignClient()
        )


def test_DEFECT_send_with_mismatched_confirm_recipient_is_rejected(
    matter_with_inbound_recipient, in_memory_matters
):
    """A confirm_recipient that does NOT match the header-derived signer is refused
    — this is the spoofed-Reply-To redirect the gate exists to stop."""
    matter, matter_id = matter_with_inbound_recipient
    with pytest.raises(docusign_workflow.RecipientConfirmationError):
        docusign_workflow.send_for_signature(
            matter,
            matter_id,
            OWNER,
            repository=in_memory_matters,
            client=FakeDocuSignClient(),
            confirm_recipient="someone-else@evil.com",
        )


def test_legit_send_with_matching_confirm_recipient_succeeds(
    matter_with_inbound_recipient, in_memory_matters
):
    """REGRESSION GUARD: the legit operator flow — confirm_recipient matches the
    counterparty signer — still sends end-to-end (case/whitespace tolerant)."""
    matter, matter_id = matter_with_inbound_recipient
    result = docusign_workflow.send_for_signature(
        matter,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=FakeDocuSignClient(),
        confirm_recipient=f"  {INBOUND_RECIPIENT.upper()} ",
    )
    assert result.envelope_id
    assert result.status == "sent"
    assert any(s["email"] == INBOUND_RECIPIENT for s in result.signers)


def test_explicit_signer_override_does_not_require_confirmation(
    matter_with_inbound_recipient, in_memory_matters
):
    """An explicit operator-supplied signer list is not header-derived, so it needs
    no confirm_recipient — the internal/operator path keeps working."""
    matter, matter_id = matter_with_inbound_recipient
    result = docusign_workflow.send_for_signature(
        matter,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=FakeDocuSignClient(),
        signers=[{"name": "Chosen Person", "email": "chosen@partner.com"}],
    )
    assert result.envelope_id
    assert {s["email"] for s in result.signers} == {"chosen@partner.com"}


# --------------------------------------------------------------------------- #
# DEFECT 2 (HIGH) — double-send / concurrency guard (adv-lifecycle C4/C5)
# --------------------------------------------------------------------------- #
def test_DEFECT_C4_second_send_with_live_envelope_is_blocked(
    matter_with_inbound_recipient, in_memory_matters
):
    """A second send while the first envelope is still live (non-terminal) is
    refused with AlreadySentError — no duplicate real envelope is minted."""
    matter, matter_id = matter_with_inbound_recipient
    fake = FakeDocuSignClient()
    first = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=fake,
        confirm_recipient=INBOUND_RECIPIENT,
    )
    assert first.envelope_id

    with pytest.raises(docusign_workflow.AlreadySentError) as excinfo:
        docusign_workflow.send_for_signature(
            None, matter_id, OWNER, repository=in_memory_matters, client=fake,
            confirm_recipient=INBOUND_RECIPIENT,
        )
    # The error surfaces the existing envelope so the caller can show its status.
    assert excinfo.value.envelope_id == first.envelope_id
    # Exactly ONE envelope exists at the provider (no orphan).
    assert len(fake._envelopes) == 1  # type: ignore[attr-defined]


def test_DEFECT_C5_terminal_envelope_allows_a_fresh_send(
    matter_with_inbound_recipient, in_memory_matters
):
    """A finished envelope (voided/declined/completed) does NOT block a resend."""
    matter, matter_id = matter_with_inbound_recipient
    fake = FakeDocuSignClient()
    first = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=fake,
        confirm_recipient=INBOUND_RECIPIENT,
    )
    # Drive the stored envelope to a terminal (voided) state.
    in_memory_matters.update_matter_fields(
        matter_id,
        {"docusign": {**first.matter[docusign_workflow.SIGNATURE_FIELD], "status": "voided"}},
        owner_user_id=OWNER,
    )
    # A fresh send is now allowed.
    second = docusign_workflow.send_for_signature(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake,
        confirm_recipient=INBOUND_RECIPIENT,
    )
    assert second.envelope_id
    assert second.envelope_id != first.envelope_id


# --------------------------------------------------------------------------- #
# DEFECT 3 (MEDIUM) — token-death between completed and download
# --------------------------------------------------------------------------- #
def test_DEFECT_token_death_mid_completion_still_flips_executed(
    matter_with_inbound_recipient, in_memory_matters
):
    """If the token dies between get_envelope_status()=completed and the executed
    PDF download, the matter must still flip to executed (it IS signed at DocuSign).
    The signed-artifact capture is best-effort and retries next sync."""
    matter, matter_id = matter_with_inbound_recipient
    fake = FakeDocuSignClient(auto_complete=True)
    send = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=fake,
        confirm_recipient=INBOUND_RECIPIENT,
    )
    envelope_id = send.envelope_id

    class _TokenDiesOnDownload(FakeDocuSignClient):
        def download_completed(self, envelope_id):  # noqa: A002 - shadow ok in stub
            raise docusign_connection.DocuSignNotConnectedError("token expired mid-completion")

    dying = _TokenDiesOnDownload()
    # status=completed succeeds; only the download raises NotConnected.
    dying._envelopes[envelope_id] = fake._envelopes[envelope_id]  # type: ignore[attr-defined]

    result = docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=dying
    )
    # The matter completes despite the failed capture.
    assert result.completed is True
    assert result.status == "completed"
    assert result.signed_artifact_id == ""
    refreshed = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert refreshed["status"] == "fully_signed"
    assert refreshed["executed"] is True


# --------------------------------------------------------------------------- #
# DEFECT 4 (P2) — malformed / multi-address signer emails
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_email",
    [
        "not-an-email",                       # no @
        "cp@acme.com, attacker@evil.com",     # comma-joined multi-address
        "cp@acme.com;attacker@evil.com",      # semicolon-joined multi-address
        "cp @acme.com",                        # embedded whitespace
        "cp@localhost",                        # dotless domain
    ],
)
def test_DEFECT_normalize_signers_rejects_bad_email(bad_email):
    with pytest.raises(docusign_integration.DocuSignError):
        docusign_integration.normalize_signers([{"name": "X", "email": bad_email}])


def test_normalize_signers_accepts_a_clean_single_address():
    signers = docusign_integration.normalize_signers([{"name": "X", "email": "x@acme.com"}])
    assert signers[0].email == "x@acme.com"


# --------------------------------------------------------------------------- #
# P3 — self-send / duplicate-recipient guard
# --------------------------------------------------------------------------- #
def test_duplicate_signer_emails_are_rejected(matter_with_inbound_recipient, in_memory_matters):
    matter, matter_id = matter_with_inbound_recipient
    with pytest.raises(docusign_workflow.SignerResolutionError):
        docusign_workflow.send_for_signature(
            matter,
            matter_id,
            OWNER,
            repository=in_memory_matters,
            client=FakeDocuSignClient(),
            signers=[
                {"name": "A", "email": "dup@acme.com"},
                {"name": "B", "email": "DUP@acme.com"},  # same address, different case
            ],
        )


def test_counterparty_equal_to_aspora_default_is_rejected(
    matter_with_inbound_recipient, in_memory_matters, monkeypatch
):
    """A counterparty resolved to Aspora's own configured signer address is a
    self-send and is refused (mirrors Gmail's ensure_recipient_is_not_own_account)."""
    monkeypatch.setenv(docusign_connection.ASPORA_SIGNER_NAME_ENV, "Aspora Signer")
    monkeypatch.setenv(docusign_connection.ASPORA_SIGNER_EMAIL_ENV, "signatory@aspora.com")
    matter, matter_id = matter_with_inbound_recipient
    with pytest.raises(docusign_workflow.SignerResolutionError):
        docusign_workflow.send_for_signature(
            matter,
            matter_id,
            OWNER,
            repository=in_memory_matters,
            client=FakeDocuSignClient(),
            # Explicit override so the only counterparty IS the Aspora address.
            signers=[
                {"name": "Counterparty", "email": "signatory@aspora.com", "role": "counterparty"},
            ],
        )
