"""Unit tests for the DocuSign send-for-signature workflow.

Exercises the full flow with the test double injected: send -> advance/sign ->
completed -> signed artifact stored + lifecycle transitioned. Document bytes are
seeded as PDF so the DOCX->PDF converter path is skipped (no LibreOffice needed).
"""

from __future__ import annotations

import pytest

from nda_automation import artifact_service, docusign_workflow
from nda_automation.artifact_registry import (
    ACTOR_HUMAN,
    ROLE_REVIEWED,
    ROLE_SIGNED,
    SOURCE_GENERATED,
    latest_artifact_for_role,
)
from nda_automation.docusign_test_double import FakeDocuSignClient

OWNER = "google:wf"
PDF_BYTES = b"%PDF-1.4 reviewed nda body"


@pytest.fixture
def matter_with_reviewed(in_memory_matters):
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
    return in_memory_matters.get_matter(matter_id, owner_user_id=OWNER), matter_id


def test_send_for_signature_creates_envelope_and_records_state(matter_with_reviewed, in_memory_matters):
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient()
    result = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    assert result.envelope_id
    assert result.status == "sent"
    # The counterparty contact is derived as a signer.
    assert any(s["email"] == "cp@acme.com" for s in result.signers)
    # Envelope state is persisted on the matter.
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    signature = stored[docusign_workflow.SIGNATURE_FIELD]
    assert signature["envelope_id"] == result.envelope_id
    assert signature["status"] == "sent"
    assert stored["awaiting_signature"] is True
    assert stored["board_column"] == "sent"


def test_send_for_signature_defaults_to_parallel_signing(matter_with_reviewed, in_memory_matters):
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient()
    docusign_workflow.send_for_signature(
        matter,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=fake,
        signers=[{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@y.com"}],
    )
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    orders = [s["routing_order"] for s in stored[docusign_workflow.SIGNATURE_FIELD]["signers"]]
    assert orders == [1, 1]


def test_full_flow_send_sign_sync_stores_signed_artifact(matter_with_reviewed, in_memory_matters):
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient()
    send = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    envelope_id = send.envelope_id

    # Not yet completed: sync reports the live status, no signed artifact.
    fake.advance(envelope_id)  # -> delivered
    mid_sync = docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    assert mid_sync.completed is False
    assert mid_sync.signed_artifact_id == ""

    # Counterparty signs -> completed -> sync captures the executed PDF.
    fake.advance(envelope_id)  # -> completed
    final = docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    assert final.completed is True
    assert final.status == "completed"
    assert final.signed_artifact_id

    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    signed = latest_artifact_for_role(stored, ROLE_SIGNED)
    assert signed is not None
    signed_bytes = artifact_service.get_artifact_bytes(
        matter_id, signed.id, repository=in_memory_matters, owner_user_id=OWNER
    )
    assert signed_bytes.startswith(b"%PDF-")
    # Lifecycle flipped to executed/fully-signed.
    assert stored["executed"] is True
    assert stored["executed_at"]
    assert stored["status"] == "fully_signed"
    assert stored["awaiting_signature"] is False


def test_sync_is_idempotent_on_completed(matter_with_reviewed, in_memory_matters):
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, OWNER, repository=in_memory_matters, client=fake)
    docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=in_memory_matters, client=fake)
    docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=in_memory_matters, client=fake)
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    signed = [a for a in stored.get("artifacts", []) if a.get("role") == ROLE_SIGNED]
    # Exactly one signed artifact survives (lifecycle_signed replaces, never duplicates).
    assert len(signed) == 1


def test_send_without_signable_document_raises(in_memory_matters):
    matter = in_memory_matters.create_matter(
        source_filename="",
        document_bytes=b"",
        extracted_text="",
        review_result={},
        triage={},
        owner_user_id=OWNER,
        intake_metadata={"reply_to": "cp@acme.com"},
    )
    # Drop the stored source bytes so there is genuinely nothing signable.
    matter_id = matter["id"]
    with pytest.raises(docusign_workflow.NoSignableDocumentError):
        docusign_workflow.send_for_signature(
            in_memory_matters.get_matter(matter_id, owner_user_id=OWNER),
            matter_id,
            OWNER,
            repository=in_memory_matters,
            client=FakeDocuSignClient(),
        )


def test_send_without_resolvable_signers_raises(in_memory_matters):
    matter = in_memory_matters.create_matter(
        source_filename="nda.docx",
        document_bytes=b"original",
        extracted_text="text",
        review_result={},
        triage={},
        owner_user_id=OWNER,
        intake_metadata={},  # no reply_to/sender -> no counterparty signer
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
    with pytest.raises(docusign_workflow.SignerResolutionError):
        docusign_workflow.send_for_signature(
            in_memory_matters.get_matter(matter_id, owner_user_id=OWNER),
            matter_id,
            OWNER,
            repository=in_memory_matters,
            client=FakeDocuSignClient(),
        )


def test_sync_without_envelope_raises(matter_with_reviewed, in_memory_matters):
    _matter, matter_id = matter_with_reviewed
    with pytest.raises(docusign_workflow.DocuSignWorkflowError):
        docusign_workflow.sync_signature_status(
            None, matter_id, OWNER, repository=in_memory_matters, client=FakeDocuSignClient()
        )


def test_send_for_missing_matter_raises(in_memory_matters):
    with pytest.raises(docusign_workflow.MatterNotFoundError):
        docusign_workflow.send_for_signature(
            None, "matter_missing", OWNER, repository=in_memory_matters, client=FakeDocuSignClient()
        )


def test_executed_capture_failure_does_not_block_completion(matter_with_reviewed, in_memory_matters):
    """A download error during completion still flips the matter to completed."""
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, OWNER, repository=in_memory_matters, client=fake)

    class _BrokenDownload(FakeDocuSignClient):
        def download_completed(self, envelope_id):
            raise docusign_workflow.docusign_integration.DocuSignError("download exploded")

    broken = _BrokenDownload()
    # Re-point the broken client's store to the live envelope by re-reading status.
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    envelope_id = stored[docusign_workflow.SIGNATURE_FIELD]["envelope_id"]

    # Make the broken client know about a completed envelope of the same id.
    broken._envelopes[envelope_id] = fake._envelopes[envelope_id]  # type: ignore[attr-defined]

    result = docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=broken
    )
    assert result.completed is True
    # No signed artifact captured (download failed) but the matter is executed.
    assert result.signed_artifact_id == ""
    refreshed = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert refreshed["status"] == "fully_signed"
