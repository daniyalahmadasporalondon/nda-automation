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


# --------------------------------------------------------------------------- #
# Signature anchoring — each signer's tabs anchor to its party's token
# --------------------------------------------------------------------------- #


class _RecordingClient(FakeDocuSignClient):
    """A fake that also remembers the signer list create_envelope was called with."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_signers = []

    def create_envelope(self, document_bytes, filename, signers, **kwargs):
        self.last_signers = list(signers)
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
    actually sends (captured from the recording client)."""
    from nda_automation import nda_generation
    from nda_automation.docx_text import extract_docx_text

    matter, matter_id, _result = generated_matter
    client = _RecordingClient()
    docusign_workflow.send_for_signature(
        matter, matter_id, OWNER, repository=in_memory_matters, client=client
    )
    # The fake stored the exact document bytes it was sent.
    sent_bytes = next(iter(client._envelopes.values())).document_bytes  # type: ignore[attr-defined]
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
