"""Unit tests for the DocuSign send-for-signature workflow.

Exercises the full flow with the test double injected: send -> advance/sign ->
completed -> signed artifact stored + lifecycle transitioned. Document bytes are
seeded as PDF so the DOCX->PDF converter path is skipped (no LibreOffice needed).
"""

from __future__ import annotations

import pytest

from nda_automation import artifact_service, docusign_integration, docusign_workflow
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
    # Mark the matter human-reviewed so it clears the send-for-signature
    # review/approval gate (matter_cleared_for_signature). The empty review_result
    # alone reads as "needs human review"; a genuinely sendable matter must have
    # been reviewed/approved, which is what this fixture represents.
    in_memory_matters.update_matter_fields(
        matter_id, {"human_reviewed": True}, owner_user_id=OWNER
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


def test_send_for_signature_defaults_to_sequential_signing(matter_with_reviewed, in_memory_matters):
    """No explicit signing_order -> the SEQUENTIAL default routes 1,2 (matching the
    FE radio default). The signers persist their ranked routing order."""
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
    block = stored[docusign_workflow.SIGNATURE_FIELD]
    orders = [s["routing_order"] for s in block["signers"]]
    assert orders == [1, 2]
    assert block["signing_order"] == "sequential"


def test_send_for_signature_sequential_routes_one_two_not_collapsed(
    matter_with_reviewed, in_memory_matters
):
    """Regression for the sequential-collapse bug: picking "sequential" must produce
    routingOrder 1,2 on the ENVELOPE DEFINITION (the authoritative wire contract).

    Before the fix, ``_resolve_signers`` normalized to parallel FIRST (stamping
    routing_order=1 on every signer), and the second, mode-aware normalize inside
    ``create_envelope`` saw an order already set and left it — so a "sequential"
    request shipped BOTH recipients at routingOrder=1 (parallel). This asserts the
    real envelope body the workflow sends now ranks them 1,2.
    """
    matter, matter_id = matter_with_reviewed
    client = _RecordingClient()
    docusign_workflow.send_for_signature(
        matter,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=client,
        signers=[{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@y.com"}],
        signing_order="sequential",
    )
    # The envelope DEFINITION built from exactly the signers the workflow sent.
    definition = docusign_integration.build_envelope_definition(
        b"%PDF-1.4 body", "nda.pdf", client.last_signers
    )
    routing = [r["routingOrder"] for r in definition["recipients"]["signers"]]
    assert routing == ["1", "2"], f"sequential collapsed to {routing}"


def test_send_for_signature_parallel_routes_all_one(matter_with_reviewed, in_memory_matters):
    """Picking "parallel" shares routingOrder 1 across recipients (the envelope
    definition carries 1,1)."""
    matter, matter_id = matter_with_reviewed
    client = _RecordingClient()
    docusign_workflow.send_for_signature(
        matter,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=client,
        signers=[{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@y.com"}],
        signing_order="parallel",
    )
    definition = docusign_integration.build_envelope_definition(
        b"%PDF-1.4 body", "nda.pdf", client.last_signers
    )
    routing = [r["routingOrder"] for r in definition["recipients"]["signers"]]
    assert routing == ["1", "1"]


def test_send_for_signature_honors_explicit_reorder_aspora_first(
    matter_with_reviewed, in_memory_matters
):
    """An explicit per-signer routing_order from the request (the UI "who signs
    first" reorder) is AUTHORITATIVE: putting Aspora at order 1 routes Aspora=1,
    counterparty=2 on the envelope definition — the chosen order, not row order."""
    matter, matter_id = matter_with_reviewed
    client = _RecordingClient()
    docusign_workflow.send_for_signature(
        matter,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=client,
        # Aspora listed second but explicitly pinned to sign FIRST via routing_order.
        signers=[
            {"name": "Counterparty", "email": "cp@acme.com", "role": "counterparty", "routing_order": 2},
            {"name": "Aspora", "email": "signer@aspora.com", "role": "aspora", "routing_order": 1},
        ],
        signing_order="sequential",
    )
    definition = docusign_integration.build_envelope_definition(
        b"%PDF-1.4 body", "nda.pdf", client.last_signers
    )
    by_email = {r["email"]: r["routingOrder"] for r in definition["recipients"]["signers"]}
    assert by_email["signer@aspora.com"] == "1"
    assert by_email["cp@acme.com"] == "2"


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
    # Clear the review/approval gate so the test reaches the no-document path.
    in_memory_matters.update_matter_fields(matter_id, {"human_reviewed": True}, owner_user_id=OWNER)
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
    # Clear the review/approval gate so the test reaches the signer-resolution path.
    in_memory_matters.update_matter_fields(matter_id, {"human_reviewed": True}, owner_user_id=OWNER)
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


# --------------------------------------------------------------------------- #
# P0: review/approval gate on send-for-signature.
# A generated NDA is created defer_ai_review=True, so generate -> send would
# otherwise dispatch a real envelope on a never-reviewed document.
# --------------------------------------------------------------------------- #
def _generated_unreviewed_matter(in_memory_matters):
    """A freshly-generated, never-reviewed matter: a signable generated document
    but NO approval/human-review (exactly what nda_generation_workflow creates)."""
    from nda_automation.artifact_registry import ROLE_GENERATED  # noqa: PLC0415

    matter = in_memory_matters.create_matter(
        source_filename="Generated NDA.docx",
        document_bytes=b"original",
        extracted_text="text",
        review_result={},
        triage={},
        source_type="generated",
        board_column="generated",
        owner_user_id=OWNER,
        intake_metadata={"reply_to": "cp@acme.com", "sender": "cp@acme.com", "subject": "Gen NDA"},
    )
    matter_id = matter["id"]
    artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_GENERATED,
        document_bytes=PDF_BYTES,
        repository=in_memory_matters,
        owner_user_id=OWNER,
    )
    return in_memory_matters.get_matter(matter_id, owner_user_id=OWNER), matter_id


def test_send_for_signature_blocks_unreviewed_generated_matter(in_memory_matters):
    """P0 PROOF (non-vacuity): a generated, never-reviewed NDA must NOT be sent.

    The document is signable and DocuSign would accept it, so the ONLY thing
    refusing the send is the review/approval gate. No envelope is persisted.
    """
    matter, matter_id = _generated_unreviewed_matter(in_memory_matters)
    assert not docusign_workflow.matter_cleared_for_signature(matter)
    with pytest.raises(docusign_workflow.MatterNotApprovedError):
        docusign_workflow.send_for_signature(
            matter, matter_id, OWNER, repository=in_memory_matters, client=FakeDocuSignClient()
        )
    # No envelope was ever created on the unreviewed matter.
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert docusign_workflow.SIGNATURE_FIELD not in stored


def test_send_for_signature_allows_matter_approved_by_status(in_memory_matters):
    """The legitimate path: a matter approved via status=="approved" still sends."""
    matter, matter_id = _generated_unreviewed_matter(in_memory_matters)
    # Approve it the canonical way (status + approved_at), matching the reviewed-DOCX
    # export gate and matter_lifecycle._matter_review_block_resolved.
    in_memory_matters.update_matter_fields(
        matter_id,
        {"status": "approved", "approved_at": "2026-06-21T00:00:00+00:00"},
        owner_user_id=OWNER,
    )
    approved = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert docusign_workflow.matter_cleared_for_signature(approved)
    result = docusign_workflow.send_for_signature(
        approved, matter_id, OWNER, repository=in_memory_matters, client=FakeDocuSignClient()
    )
    assert result.envelope_id
    assert result.status == "sent"
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert stored[docusign_workflow.SIGNATURE_FIELD]["envelope_id"] == result.envelope_id


# --------------------------------------------------------------------------- #
# Per-recipient signature status — Aspora vs counterparty, 0/2 -> 1/2 -> 2/2
# --------------------------------------------------------------------------- #

_TWO_PARTY_SIGNERS = [
    {"name": "Daniyal Ahmad", "email": "daniyal.ahmad@aspora.com", "role": "aspora"},
    {"name": "Acme Corp", "email": "cp@acme.com", "role": "counterparty"},
]


def _send_two_party(matter, matter_id, in_memory_matters, fake):
    return docusign_workflow.send_for_signature(
        matter,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=fake,
        signers=list(_TWO_PARTY_SIGNERS),
    ).envelope_id


def _stored_signers_by_role(in_memory_matters, matter_id):
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    signature = stored[docusign_workflow.SIGNATURE_FIELD]
    return {s.get("role"): s for s in signature["signers"]}


def test_sync_records_per_recipient_status_zero_of_two(matter_with_reviewed, in_memory_matters):
    """Sent, neither party signed -> both recipients read 'awaiting' (0/2)."""
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient()
    envelope_id = _send_two_party(matter, matter_id, in_memory_matters, fake)
    fake.advance(envelope_id)  # -> delivered (still out for signature)

    docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    by_role = _stored_signers_by_role(in_memory_matters, matter_id)
    assert by_role["aspora"]["signature_status"] == "awaiting"
    assert by_role["counterparty"]["signature_status"] == "awaiting"
    assert by_role["aspora"]["signed_at"] == ""


def test_sync_records_per_recipient_status_one_of_two_aspora(matter_with_reviewed, in_memory_matters):
    """Only Aspora has signed -> Aspora 'signed' (with a date), counterparty 'awaiting' (1/2)."""
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient()
    envelope_id = _send_two_party(matter, matter_id, in_memory_matters, fake)
    fake.sign_recipient(envelope_id, "daniyal.ahmad@aspora.com")

    docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    by_role = _stored_signers_by_role(in_memory_matters, matter_id)
    assert by_role["aspora"]["signature_status"] == "signed"
    assert by_role["aspora"]["signed_at"]  # a signed timestamp is surfaced
    assert by_role["counterparty"]["signature_status"] == "awaiting"


def test_sync_records_per_recipient_status_one_of_two_counterparty(matter_with_reviewed, in_memory_matters):
    """Only the counterparty has signed -> counterparty 'signed', Aspora 'awaiting' (1/2)."""
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient()
    envelope_id = _send_two_party(matter, matter_id, in_memory_matters, fake)
    fake.sign_recipient(envelope_id, "cp@acme.com")

    docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    by_role = _stored_signers_by_role(in_memory_matters, matter_id)
    assert by_role["counterparty"]["signature_status"] == "signed"
    assert by_role["aspora"]["signature_status"] == "awaiting"


def test_sync_records_per_recipient_status_two_of_two(matter_with_reviewed, in_memory_matters):
    """Envelope completed -> both parties 'signed' (2/2, fully executed)."""
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient(auto_complete=True)
    _send_two_party(matter, matter_id, in_memory_matters, fake)

    final = docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    assert final.completed is True
    by_role = _stored_signers_by_role(in_memory_matters, matter_id)
    assert by_role["aspora"]["signature_status"] == "signed"
    assert by_role["counterparty"]["signature_status"] == "signed"
    assert by_role["aspora"]["signed_at"]
    assert by_role["counterparty"]["signed_at"]


def test_sync_tolerates_client_without_recipient_support(matter_with_reviewed, in_memory_matters):
    """A client lacking get_envelope_recipients leaves signers unenriched (no crash)."""

    class _NoRecipientsClient(FakeDocuSignClient):
        get_envelope_recipients = None  # not callable -> best-effort skip

    matter, matter_id = matter_with_reviewed
    fake = _NoRecipientsClient()
    _send_two_party(matter, matter_id, in_memory_matters, fake)

    # Must not raise; the per-party fields are simply absent.
    docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    by_role = _stored_signers_by_role(in_memory_matters, matter_id)
    assert "signature_status" not in by_role["aspora"]
    assert by_role["aspora"]["role"] == "aspora"


# --------------------------------------------------------------------------- #
# Signature anchoring — each signer's tabs anchor to its party's token
# --------------------------------------------------------------------------- #


class _RecordingClient(FakeDocuSignClient):
    """A fake that also remembers the signer list create_envelope was called with."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_signers = []
        self.last_signing_order = ""

    def create_envelope(self, document_bytes, filename, signers, **kwargs):
        self.last_signers = list(signers)
        self.last_signing_order = kwargs.get("signing_order", "")
        return super().create_envelope(document_bytes, filename, signers, **kwargs)


@pytest.fixture
def generated_matter(in_memory_matters, monkeypatch):
    """A matter whose source IS a generated NDA (carries the per-party anchors).

    Built through the real generation engine so the document genuinely contains
    the anchor tokens and the artifact carries the generation manifest (entity id).
    The registry's Aspora signatory is a placeholder with no email, so we give the
    entity a routable signatory here to exercise the Aspora signer + its anchor.
    """
    from nda_automation import entity_registry, nda_generation

    # Make the chosen entity's signatory routable so the Aspora signer is emitted.
    real_get_entity = entity_registry.get_entity

    def routable_get_entity(entity_id):
        bundle = real_get_entity(entity_id)
        if isinstance(bundle, dict) and entity_id == "aspora_technology":
            bundle = dict(bundle)
            bundle["signatory"] = {"name": "Priya Nair", "title": "Director", "email": "priya@aspora.com"}
        return bundle

    monkeypatch.setattr(entity_registry, "get_entity", routable_get_entity)
    # The workflow looks the entity up via its own module reference too.
    monkeypatch.setattr(docusign_workflow.entity_registry, "get_entity", routable_get_entity)

    matter = in_memory_matters.create_matter(
        source_filename="NDA - Acme.docx",
        document_bytes=b"PK placeholder",
        extracted_text="placeholder",
        review_result={},
        triage={},
        source_type="generated",
        owner_user_id=OWNER,
        intake_metadata={"reply_to": "cp@acme.com", "sender": "cp@acme.com", "subject": "Acme NDA"},
    )
    matter_id = matter["id"]

    intake = nda_generation.CounterpartyIntake(
        company_name="Acme Innovations Pvt Ltd",
        registered_office="42 MG Road, Bengaluru 560001",
        jurisdiction_of_incorporation="India",
        business_description="payments technology",
        purpose="a commercial partnership",
        term_years=3,
    )
    result, _artifact = nda_generation.generate_and_save_nda(
        "aspora_technology", intake, matter_id, repository=in_memory_matters, owner_user_id=OWNER
    )
    # Cleared for the send-for-signature review/approval gate: these tests exercise
    # anchor/signer behavior on a SENT envelope, so the matter must be reviewed.
    in_memory_matters.update_matter_fields(matter_id, {"human_reviewed": True}, owner_user_id=OWNER)
    return in_memory_matters.get_matter(matter_id, owner_user_id=OWNER), matter_id, result


def test_generated_nda_signers_anchor_to_their_party_token(generated_matter, in_memory_matters):
    from nda_automation import nda_generation

    matter, matter_id, _result = generated_matter
    client = _RecordingClient()
    result = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=client
    )

    by_role = {s["role"]: s for s in result.signers}
    # Both parties resolved as signers.
    assert "counterparty" in by_role
    assert "aspora" in by_role
    # Each signer carries ITS party's anchor token (not the other party's).
    assert by_role["counterparty"]["anchor"] == nda_generation.SIGNATURE_ANCHOR_COUNTERPARTY
    assert by_role["aspora"]["anchor"] == nda_generation.SIGNATURE_ANCHOR_ASPORA


def test_generated_nda_envelope_tabs_carry_the_right_anchor_per_recipient(
    generated_matter, in_memory_matters
):
    from nda_automation import docusign_integration, nda_generation

    matter, matter_id, _result = generated_matter
    client = _RecordingClient()
    docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=client
    )

    # Build the real envelope body from the exact signers the workflow sent.
    definition = docusign_integration.build_envelope_definition(
        b"%PDF-1.4 body", "nda.pdf", client.last_signers
    )
    recipients = {r["email"]: r for r in definition["recipients"]["signers"]}

    cp = recipients["cp@acme.com"]
    aspora = recipients["priya@aspora.com"]
    # signHere AND dateSigned for each recipient anchor to that party's token.
    assert cp["tabs"]["signHereTabs"][0]["anchorString"] == nda_generation.SIGNATURE_ANCHOR_COUNTERPARTY
    assert cp["tabs"]["dateSignedTabs"][0]["anchorString"] == nda_generation.SIGNATURE_ANCHOR_COUNTERPARTY
    assert aspora["tabs"]["signHereTabs"][0]["anchorString"] == nda_generation.SIGNATURE_ANCHOR_ASPORA
    assert aspora["tabs"]["dateSignedTabs"][0]["anchorString"] == nda_generation.SIGNATURE_ANCHOR_ASPORA


def test_sent_generated_document_actually_contains_the_anchor_strings(
    generated_matter, in_memory_matters
):
    """The anchor is only useful if DocuSign can FIND it: the bytes the envelope
    carries must contain both party tokens. We assert on the document the workflow
    actually sends (captured from the recording client).

    The workflow converts the generated DOCX to a PDF when LibreOffice is
    available (``_as_pdf``) and otherwise sends the DOCX bytes unchanged, so the
    captured bytes are PDF in a soffice/prod environment and DOCX without it.
    Extract the text with the matching extractor for whichever format was sent;
    both extractors recover the literal anchor tokens, so the assertion stays
    live in either environment instead of breaking on the format it didn't expect.
    """
    from nda_automation import nda_generation
    from nda_automation.docx_text import extract_docx_text
    from nda_automation.pdf_text import extract_pdf_text

    matter, matter_id, _result = generated_matter
    client = _RecordingClient()
    docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=client
    )
    # The fake stored the exact document bytes it was sent.
    sent_bytes = next(iter(client._envelopes.values())).document_bytes  # type: ignore[attr-defined]
    if sent_bytes[:5] == b"%PDF-":
        text = extract_pdf_text(sent_bytes)
    else:
        text = extract_docx_text(sent_bytes)
    assert nda_generation.SIGNATURE_ANCHOR_COUNTERPARTY in text
    assert nda_generation.SIGNATURE_ANCHOR_ASPORA in text


def test_received_paper_matter_does_not_get_generated_anchors(matter_with_reviewed, in_memory_matters):
    """Scoping guard: a NON-generated matter (received counterparty paper, no
    generation manifest) must NOT get the generated-NDA anchor tokens — those
    strings are not in its document. The tabs fall back to the signer name."""
    from nda_automation import nda_generation

    matter, matter_id = matter_with_reviewed
    client = _RecordingClient()
    result = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=client
    )
    for signer in result.signers:
        assert signer["anchor"] != nda_generation.SIGNATURE_ANCHOR_ASPORA
        assert signer["anchor"] != nda_generation.SIGNATURE_ANCHOR_COUNTERPARTY


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


# --------------------------------------------------------------------------- #
# Single default Aspora signatory (NDA_DOCUSIGN_ASPORA_SIGNER_NAME/EMAIL)
#
# The per-entity registry signatory is a "[Authorised Signatory]" placeholder
# with no email, so DocuSign cannot route Aspora's copy from it. One configured
# default identity stands in for EVERY Aspora entity, making Aspora a routable
# signer on every generated NDA. These tests deliberately DO NOT make the registry
# signatory routable, so the only thing that can produce the Aspora signer is the
# config default.
# --------------------------------------------------------------------------- #

ASPORA_DEFAULT_NAME = "Rahul Mehta"
ASPORA_DEFAULT_EMAIL = "signatory@aspora.com"


@pytest.fixture
def placeholder_generated_matter(in_memory_matters):
    """A generated-NDA matter whose entity keeps its unroutable registry signatory.

    Unlike ``generated_matter`` this does NOT monkeypatch a routable signatory onto
    the entity, so the registry signatory stays the ``[Authorised Signatory]``
    placeholder with no email. Any Aspora signer that appears therefore comes from
    the config default, not the registry.
    """
    from nda_automation import nda_generation

    matter = in_memory_matters.create_matter(
        source_filename="NDA - Globex.docx",
        document_bytes=b"PK placeholder",
        extracted_text="placeholder",
        review_result={},
        triage={},
        source_type="generated",
        owner_user_id=OWNER,
        intake_metadata={"reply_to": "cp@globex.com", "sender": "cp@globex.com", "subject": "Globex NDA"},
    )
    matter_id = matter["id"]

    intake = nda_generation.CounterpartyIntake(
        company_name="Globex Innovations Pvt Ltd",
        registered_office="1 Park Avenue, Mumbai 400001",
        jurisdiction_of_incorporation="India",
        business_description="payments technology",
        purpose="a commercial partnership",
        term_years=3,
    )
    nda_generation.generate_and_save_nda(
        "aspora_technology", intake, matter_id, repository=in_memory_matters, owner_user_id=OWNER
    )
    # Cleared for the send-for-signature review/approval gate (tests exercise the
    # SENT envelope's signer/anchor behavior, not the gate).
    in_memory_matters.update_matter_fields(matter_id, {"human_reviewed": True}, owner_user_id=OWNER)
    return in_memory_matters.get_matter(matter_id, owner_user_id=OWNER), matter_id


def _set_aspora_default(monkeypatch, name=ASPORA_DEFAULT_NAME, email=ASPORA_DEFAULT_EMAIL):
    from nda_automation import docusign_connection

    monkeypatch.setenv(docusign_connection.ASPORA_SIGNER_NAME_ENV, name)
    monkeypatch.setenv(docusign_connection.ASPORA_SIGNER_EMAIL_ENV, email)


def test_default_aspora_signer_used_for_any_entity_when_configured(
    placeholder_generated_matter, in_memory_matters, monkeypatch
):
    """With both env vars set, ANY Aspora entity gets the single default identity
    as the Aspora recipient — even though its registry signatory has no email."""
    from nda_automation import nda_generation

    _set_aspora_default(monkeypatch)
    matter, matter_id = placeholder_generated_matter
    result = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=FakeDocuSignClient()
    )

    by_role = {s["role"]: s for s in result.signers}
    assert "aspora" in by_role
    aspora = by_role["aspora"]
    assert aspora["name"] == ASPORA_DEFAULT_NAME
    assert aspora["email"] == ASPORA_DEFAULT_EMAIL
    # It is a routable generated-NDA signer, so it still carries Aspora's anchor.
    assert aspora["anchor"] == nda_generation.SIGNATURE_ANCHOR_ASPORA


def test_default_aspora_signer_omitted_when_env_unset(
    placeholder_generated_matter, in_memory_matters, monkeypatch
):
    """Backward compatible: with the env vars unset and only the placeholder
    registry signatory, the Aspora signer is omitted (current behaviour)."""
    from nda_automation import docusign_connection

    monkeypatch.delenv(docusign_connection.ASPORA_SIGNER_NAME_ENV, raising=False)
    monkeypatch.delenv(docusign_connection.ASPORA_SIGNER_EMAIL_ENV, raising=False)
    matter, matter_id = placeholder_generated_matter
    result = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=FakeDocuSignClient()
    )
    roles = {s["role"] for s in result.signers}
    assert "aspora" not in roles
    # The counterparty still signs — only Aspora is omitted.
    assert "counterparty" in roles


def test_default_aspora_signer_requires_both_name_and_email(
    placeholder_generated_matter, in_memory_matters, monkeypatch
):
    """A half-set config (email only, no name) is NOT routable and is ignored."""
    from nda_automation import docusign_connection

    monkeypatch.delenv(docusign_connection.ASPORA_SIGNER_NAME_ENV, raising=False)
    monkeypatch.setenv(docusign_connection.ASPORA_SIGNER_EMAIL_ENV, ASPORA_DEFAULT_EMAIL)
    matter, matter_id = placeholder_generated_matter
    result = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=FakeDocuSignClient()
    )
    assert "aspora" not in {s["role"] for s in result.signers}


def test_explicit_signer_override_still_wins_over_default(
    placeholder_generated_matter, in_memory_matters, monkeypatch
):
    """A per-send explicit signer list takes precedence over the configured
    default — the override is used verbatim and the default is not consulted."""
    _set_aspora_default(monkeypatch)
    matter, matter_id = placeholder_generated_matter
    result = docusign_workflow.send_for_signature(
        matter,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=FakeDocuSignClient(),
        signers=[{"name": "Override Person", "email": "override@aspora.com"}],
    )
    emails = {s["email"] for s in result.signers}
    assert emails == {"override@aspora.com"}
    # The configured default identity does not appear.
    assert ASPORA_DEFAULT_EMAIL not in emails


# ---------------------------------------------------------------------------
# Drive auto-archive on the "completed"/executed transition (Option A)
# ---------------------------------------------------------------------------
def test_completion_fires_drive_sync_with_signed_artifact(matter_with_reviewed, in_memory_matters):
    """On completion the injected Drive archiver is invoked with the executed
    matter — which already carries the captured signed artifact."""
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, OWNER, repository=in_memory_matters, client=fake)

    calls: list[dict] = []

    def _spy_drive_sync(**kwargs):
        calls.append(kwargs)

    result = docusign_workflow.sync_signature_status(
        None,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=fake,
        drive_sync=_spy_drive_sync,
    )

    assert result.completed is True
    assert result.signed_artifact_id  # the signed PDF was captured
    # The archiver fired exactly once with the executed matter + ids.
    assert len(calls) == 1
    archived_matter = calls[0]["matter"]
    assert calls[0]["matter_id"] == matter_id
    assert calls[0]["owner_user_id"] == OWNER
    assert archived_matter["executed"] is True
    assert archived_matter["status"] == "fully_signed"
    # The executed matter handed to Drive carries the signed artifact.
    signed = latest_artifact_for_role(archived_matter, ROLE_SIGNED)
    assert signed is not None
    assert signed.id == result.signed_artifact_id


def test_drive_sync_not_fired_before_completion(matter_with_reviewed, in_memory_matters):
    """A non-completed sync must NOT trigger the Drive archive."""
    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient()
    send = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    fake.advance(send.envelope_id)  # -> delivered, not completed

    calls: list[dict] = []
    result = docusign_workflow.sync_signature_status(
        None,
        matter_id,
        OWNER,
        repository=in_memory_matters,
        client=fake,
        drive_sync=lambda **kw: calls.append(kw),
    )
    assert result.completed is False
    assert calls == []


def test_drive_down_is_swallowed_and_executed_transition_completes(
    matter_with_reviewed, in_memory_matters
):
    """A Drive outage during completion is swallowed: the matter still flips to
    executed/fully-signed and the signed artifact is intact.

    Drives the REAL ``_archive_to_drive`` (default archiver) with ``drive_connected``
    forced True / auto-intake on, but ``sync_matter_folder`` raising — proving the
    best-effort guard inside the archiver, not just an injected no-op."""
    from nda_automation import app_settings, drive_integration

    matter, matter_id = matter_with_reviewed
    fake = FakeDocuSignClient(auto_complete=True)
    docusign_workflow.send_for_signature(matter, matter_id, OWNER, repository=in_memory_matters, client=fake)

    orig_connected = drive_integration.drive_connected
    orig_auto = app_settings.drive_auto_intake_enabled
    orig_settings = app_settings.drive_settings
    orig_sync = drive_integration.sync_matter_folder
    try:
        drive_integration.drive_connected = lambda owner_user_id="": True
        app_settings.drive_auto_intake_enabled = lambda: True
        app_settings.drive_settings = lambda: {"folder_id": "root123"}

        def _boom(**kwargs):
            raise drive_integration.DriveIntegrationError("Drive is down")

        drive_integration.sync_matter_folder = _boom

        # Must NOT raise even though the Drive sync explodes.
        result = docusign_workflow.sync_signature_status(
            None, matter_id, OWNER, repository=in_memory_matters, client=fake
        )
    finally:
        drive_integration.drive_connected = orig_connected
        app_settings.drive_auto_intake_enabled = orig_auto
        app_settings.drive_settings = orig_settings
        drive_integration.sync_matter_folder = orig_sync

    # The executed transition completed regardless of the Drive failure.
    assert result.completed is True
    assert result.status == "completed"
    assert result.signed_artifact_id
    refreshed = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert refreshed["executed"] is True
    assert refreshed["status"] == "fully_signed"
    assert refreshed["awaiting_signature"] is False
    # No partial/garbage drive pointer was written (the sync raised before write-back).
    assert "drive" not in refreshed or not refreshed.get("drive", {}).get("matter_folder_id")


# ---------------------------------------------------------------------------
# Override role-stamping: an override never lets the Aspora party persist as a
# blank-role (or, worse, leading) signer that the matter view could read as the
# counterparty. We stamp roles at the SOURCE in _resolve_signers.
# ---------------------------------------------------------------------------
def test_override_stamps_aspora_role_by_domain_when_listed_first():
    """An override that lists the aspora.com signer FIRST with NO role gets the
    Aspora party stamped role="aspora" and the external party "counterparty",
    so the recorded signer set is never read as Aspora-is-the-counterparty."""
    signers = docusign_workflow._resolve_signers(
        {},
        [
            {"name": "Daniyal Ahmad", "email": "daniyal.ahmad@aspora.com"},
            {"name": "Pranav Sharma", "email": "pranav@acme.com"},
        ],
    )
    # _resolve_signers returns RAW signer dicts now (normalization is deferred to
    # the single create_envelope pass), so read the dict keys.
    by_email = {s["email"]: s.get("role") for s in signers}
    assert by_email["daniyal.ahmad@aspora.com"] == "aspora"
    assert by_email["pranav@acme.com"] == "counterparty"
    # Order / who-receives is preserved; only the role label is stamped.
    assert [s["email"] for s in signers] == [
        "daniyal.ahmad@aspora.com",
        "pranav@acme.com",
    ]


def test_override_stamps_aspora_role_by_configured_email(monkeypatch):
    """When a default Aspora signer email is configured, an override signer at
    that exact address is stamped role="aspora" even off-domain."""
    _set_aspora_default(monkeypatch, email="ops@example.org")
    signers = docusign_workflow._resolve_signers(
        {},
        [
            {"name": "Ops Person", "email": "ops@example.org"},
            {"name": "Pranav", "email": "pranav@acme.com"},
        ],
    )
    by_email = {s["email"]: s.get("role") for s in signers}
    assert by_email["ops@example.org"] == "aspora"
    assert by_email["pranav@acme.com"] == "counterparty"


def test_override_preserves_explicit_non_blank_roles():
    """An override that already carries deliberate roles is left untouched (we
    never relabel a non-blank role); blank roles on non-aspora signers default
    to counterparty."""
    signers = docusign_workflow._resolve_signers(
        {},
        [
            {"name": "Pranav", "email": "pranav@acme.com", "role": "signer1"},
            {"name": "Daniyal", "email": "daniyal.ahmad@aspora.com", "role": "aspora"},
        ],
    )
    by_email = {s["email"]: s.get("role") for s in signers}
    assert by_email["pranav@acme.com"] == "signer1"
    assert by_email["daniyal.ahmad@aspora.com"] == "aspora"


def test_override_blank_role_non_aspora_defaults_to_counterparty():
    """A single non-aspora override signer with no role becomes counterparty."""
    signers = docusign_workflow._resolve_signers(
        {},
        [{"name": "Pranav", "email": "pranav@acme.com"}],
    )
    assert signers[0].get("role") == "counterparty"
    assert signers[0]["email"] == "pranav@acme.com"


# --------------------------------------------------------------------------- #
# P0: a reviewed/edited matter can NEVER sign the un-redlined ORIGINAL or a
# stale reviewed copy — it signs the CURRENT, coverage-verified reviewed doc or
# it FAILS LOUD. These exercise _resolve_signable_document directly.
# --------------------------------------------------------------------------- #
from nda_automation import matter_document_artifacts, redline_export_service  # noqa: E402
from nda_automation.artifact_registry import ROLE_ORIGINAL  # noqa: E402
from nda_automation.matter_document_artifacts import ReviewedDocx  # noqa: E402
from nda_automation.redline_export_service import RedlineExport  # noqa: E402

ORIGINAL_PDF = b"%PDF-1.4 THE UNREDLINED ORIGINAL"
REVIEWED_PDF = b"%PDF-1.4 the reviewed redlined document"


def _edited_matter(in_memory_matters, *, source_filename="acme-nda.docx", source_bytes=b"orig"):
    """A matter that HAS reviewer edits (accepted clause with a matching server
    redline) and only an ORIGINAL artifact registered — the dangerous shape where a
    naive precedence walk would sign the original and drop the reviewer's edits."""
    matter = in_memory_matters.create_matter(
        source_filename=source_filename,
        document_bytes=source_bytes,
        extracted_text="Clause one.\n\nClause two.",
        review_result={"redline_edits": [{"id": "r1", "clause_id": "c1", "paragraph_id": "p1", "action": "replace_paragraph"}]},
        triage={},
        owner_user_id=OWNER,
        intake_metadata={"reply_to": "cp@acme.com", "sender": "cp@acme.com", "subject": "Acme NDA"},
    )
    matter_id = matter["id"]
    # Register the ORIGINAL artifact with distinctive bytes so a wrong fallback is
    # detectable (signing these bytes == the bug).
    artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_ORIGINAL,
        document_bytes=ORIGINAL_PDF,
        repository=in_memory_matters,
        owner_user_id=OWNER,
    )
    # An accepted decision on the flagged clause => non-empty export_redline_edits
    # => _matter_has_reviewer_edits is True. Use the canonical decision API (a plain
    # update_matter_fields drops the reviewer_decisions field via the store
    # whitelist). Mark human_reviewed so the send gate (matter_cleared_for_signature)
    # is satisfied for the full-send tests.
    in_memory_matters.set_clause_reviewer_decision(
        matter_id, "c1", {"action": "accept", "actor": "reviewer"}, owner_user_id=OWNER
    )
    in_memory_matters.update_matter_fields(
        matter_id, {"human_reviewed": True}, owner_user_id=OWNER
    )
    return in_memory_matters.get_matter(matter_id, owner_user_id=OWNER), matter_id


def test_edited_matter_resolves_reviewed_not_original(in_memory_matters, monkeypatch):
    """An edited matter signs the freshly-built REVIEWED doc, never the ORIGINAL."""
    matter, matter_id = _edited_matter(in_memory_matters)
    assert docusign_workflow._matter_has_reviewer_edits(matter) is True

    def fake_build(mid, m, *, repository=None, owner_user_id="", persist=True):
        # Reuse-the-same-path contract: persist=True at send.
        assert persist is True
        return ReviewedDocx(
            export=RedlineExport(data=REVIEWED_PDF, filename="acme-nda-redlined.pdf"),
            artifact=None,
            payload={},
        )

    monkeypatch.setattr(matter_document_artifacts, "build_reviewed_docx", fake_build)
    data, filename = docusign_workflow._resolve_signable_document(
        matter, matter_id, OWNER, in_memory_matters
    )
    assert data == REVIEWED_PDF
    assert data != ORIGINAL_PDF
    assert filename.endswith(".pdf")


def test_edited_matter_reviewed_unavailable_raises_never_signs_original(in_memory_matters, monkeypatch):
    """Edits exist but the reviewed build fails => RAISE, never fall back to original."""
    matter, matter_id = _edited_matter(in_memory_matters)

    def boom(*args, **kwargs):
        raise RuntimeError("reviewed build blew up")

    monkeypatch.setattr(matter_document_artifacts, "build_reviewed_docx", boom)
    with pytest.raises(docusign_workflow.ReviewedDocumentUnavailableError):
        docusign_workflow._resolve_signable_document(
            matter, matter_id, OWNER, in_memory_matters
        )


def test_edited_matter_stale_after_edit_raises(in_memory_matters, monkeypatch):
    """A stale review (playbook drift / re-edit) => the guarded build raises
    StaleMatterReviewError, so the send RAISES rather than signing a stale doc."""
    matter, matter_id = _edited_matter(in_memory_matters)

    def stale(*args, **kwargs):
        raise redline_export_service.StaleMatterReviewError(
            {"stale_reasons": ["playbook_changed"], "stale": True}
        )

    monkeypatch.setattr(matter_document_artifacts, "build_reviewed_docx", stale)
    with pytest.raises(docusign_workflow.ReviewedDocumentUnavailableError):
        docusign_workflow._resolve_signable_document(
            matter, matter_id, OWNER, in_memory_matters
        )


def test_edited_pdf_source_reconstruction_shortfall_fails_closed(in_memory_matters, monkeypatch):
    """PDF-source WITH edits whose reconstruction would drop redlines => the build
    raises PdfSourceRedlineUnavailableError, so the send fails closed and NEVER
    signs the original PDF."""
    matter, matter_id = _edited_matter(
        in_memory_matters, source_filename="acme-nda.pdf", source_bytes=ORIGINAL_PDF
    )

    def pdf_unavailable(*args, **kwargs):
        raise redline_export_service.PdfSourceRedlineUnavailableError(
            "reconstruction dropped a redline", source_filename="acme-nda.pdf"
        )

    monkeypatch.setattr(matter_document_artifacts, "build_reviewed_docx", pdf_unavailable)
    with pytest.raises(docusign_workflow.ReviewedDocumentUnavailableError):
        docusign_workflow._resolve_signable_document(
            matter, matter_id, OWNER, in_memory_matters
        )


def test_edited_matter_build_collapsing_to_original_passthrough_fails_closed(in_memory_matters, monkeypatch):
    """Belt-and-suspenders: if the reviewed build (impossibly, for an edited matter)
    returned the ORIGINAL passthrough tagged X-Export-Original, we refuse it rather
    than sign the original under a 'reviewed' guise."""
    matter, matter_id = _edited_matter(in_memory_matters)

    def original_passthrough(*args, **kwargs):
        return ReviewedDocx(
            export=RedlineExport(
                data=ORIGINAL_PDF,
                filename="acme-nda.pdf",
                headers={
                    redline_export_service.ORIGINAL_EXPORT_MARKER_HEADER:
                        redline_export_service.ORIGINAL_UNCHANGED_EXPORT_HEADER
                },
            ),
            artifact=None,
            payload={},
        )

    monkeypatch.setattr(matter_document_artifacts, "build_reviewed_docx", original_passthrough)
    with pytest.raises(docusign_workflow.ReviewedDocumentUnavailableError):
        docusign_workflow._resolve_signable_document(
            matter, matter_id, OWNER, in_memory_matters
        )


def test_no_edits_matter_resolves_original(in_memory_matters):
    """A matter with NO reviewer edits (nothing to redline) signs the ORIGINAL — the
    correct document, not a degraded fallback. The reviewed builder is never invoked."""
    matter = in_memory_matters.create_matter(
        source_filename="clean-nda.pdf",
        document_bytes=b"src",
        extracted_text="text",
        review_result={},
        triage={},
        owner_user_id=OWNER,
        intake_metadata={"reply_to": "cp@acme.com", "sender": "cp@acme.com"},
    )
    matter_id = matter["id"]
    artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_ORIGINAL,
        document_bytes=ORIGINAL_PDF,
        repository=in_memory_matters,
        owner_user_id=OWNER,
    )
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert docusign_workflow._matter_has_reviewer_edits(stored) is False
    data, filename = docusign_workflow._resolve_signable_document(
        stored, matter_id, OWNER, in_memory_matters
    )
    assert data == ORIGINAL_PDF
    assert filename.endswith(".pdf")


def test_generated_matter_resolves_generated_artifact(in_memory_matters):
    """A generated-but-unreviewed NDA has no edits => precedence resolves the
    GENERATED artifact (not a reviewed build, not the raw source)."""
    matter, matter_id = _generated_unreviewed_matter(in_memory_matters)
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert docusign_workflow._matter_has_reviewer_edits(stored) is False
    data, filename = docusign_workflow._resolve_signable_document(
        stored, matter_id, OWNER, in_memory_matters
    )
    # The generated artifact's PDF bytes (PDF_BYTES) resolve, not the raw b"original".
    assert data == PDF_BYTES
    assert filename.endswith(".pdf")


def test_send_for_edited_matter_signs_reviewed_bytes_end_to_end(in_memory_matters, monkeypatch):
    """Full send: the envelope document is the REVIEWED bytes, and the persisted
    filename is the reviewed one — the original bytes never reach DocuSign."""
    matter, matter_id = _edited_matter(in_memory_matters)

    def fake_build(mid, m, *, repository=None, owner_user_id="", persist=True):
        return ReviewedDocx(
            export=RedlineExport(data=REVIEWED_PDF, filename="acme-nda-redlined.pdf"),
            artifact=None,
            payload={},
        )

    monkeypatch.setattr(matter_document_artifacts, "build_reviewed_docx", fake_build)
    fake = FakeDocuSignClient()
    result = docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=fake
    )
    assert result.envelope_id
    # The document DocuSign received is the reviewed doc, never the original.
    sent_bytes = fake._envelopes[result.envelope_id].document_bytes
    assert sent_bytes == REVIEWED_PDF
    assert sent_bytes != ORIGINAL_PDF


def test_no_edits_branch_never_signs_stale_reviewed_artifact(in_memory_matters):
    """BACKSTOP (P1 stale-after-reversal): a matter whose CURRENT decisions yield NO
    edits, yet still carries a lingering role=reviewed artifact (minted against a
    since-reversed decision), must NOT sign that reviewed copy — it falls through to
    the ORIGINAL. Signing the reviewed artifact would ship a change the reviewer
    undid."""
    matter = in_memory_matters.create_matter(
        source_filename="acme-nda.pdf",
        document_bytes=b"src",
        extracted_text="text",
        review_result={},
        triage={},
        owner_user_id=OWNER,
        intake_metadata={"reply_to": "cp@acme.com", "sender": "cp@acme.com"},
    )
    matter_id = matter["id"]
    # The ORIGINAL and a STALE reviewed artifact both exist; current decisions are
    # empty (no edits) — the reviewed artifact is contradictory and must be ignored.
    artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_ORIGINAL,
        document_bytes=ORIGINAL_PDF,
        repository=in_memory_matters,
        owner_user_id=OWNER,
    )
    artifact_service.add_artifact(
        matter_id,
        source=SOURCE_GENERATED,
        actor=ACTOR_HUMAN,
        role=ROLE_REVIEWED,
        document_bytes=REVIEWED_PDF,  # the stale, reverted-edit reviewed copy
        repository=in_memory_matters,
        owner_user_id=OWNER,
    )
    stored = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert docusign_workflow._matter_has_reviewer_edits(stored) is False
    data, _filename = docusign_workflow._resolve_signable_document(
        stored, matter_id, OWNER, in_memory_matters
    )
    # Falls through to the ORIGINAL, never the stale reviewed artifact.
    assert data == ORIGINAL_PDF
    assert data != REVIEWED_PDF


def test_post_approval_decision_change_unclears_matter(in_memory_matters):
    """PRIMARY (P1): reversing a clause decision AFTER approval un-clears the matter
    (back to in_review, approved_at/human_reviewed cleared) so it must be
    re-approved — the send gate then blocks until re-approval re-mints the correct
    reviewed artifact."""
    matter, matter_id = _edited_matter(in_memory_matters)
    # Simulate approval (the pre-flight/lifecycle would set these).
    in_memory_matters.update_matter_fields(
        matter_id,
        {"status": "approved", "approved_at": "2026-07-02T00:00:00+00:00"},
        owner_user_id=OWNER,
    )
    approved = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    assert docusign_workflow.matter_cleared_for_signature(approved) is True

    # Reviewer now REVERSES c1 (reject) post-approval.
    in_memory_matters.set_clause_reviewer_decision(
        matter_id, "c1", {"action": "reject", "actor": "reviewer"}, owner_user_id=OWNER
    )
    reverted = in_memory_matters.get_matter(matter_id, owner_user_id=OWNER)
    # Un-cleared: no longer approved / human_reviewed.
    assert reverted["status"] == "in_review"
    assert not reverted.get("approved_at")
    assert not reverted.get("human_reviewed")
    assert docusign_workflow.matter_cleared_for_signature(reverted) is False
    # The send gate now refuses until re-approval.
    with pytest.raises(docusign_workflow.MatterNotApprovedError):
        docusign_workflow.send_for_signature(
            reverted, matter_id, OWNER, repository=in_memory_matters, client=FakeDocuSignClient()
        )
