"""Tests for the artifact registry: the thin metadata layer over matter documents."""
from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from nda_automation import artifact_registry, artifact_service
from nda_automation.artifact_registry import (
    Artifact,
    ArtifactRegistryError,
    artifact_name,
    hash_bytes,
)
from nda_automation.artifact_service import ArtifactMatterNotFoundError
from nda_automation.ingestion_service import create_matter_from_document
from nda_automation.matter_repository import InMemoryMatterRepository

_NDA_PARAGRAPHS = [
    "This Mutual Non-Disclosure Agreement is entered into by both parties.",
    "Each party agrees to keep the other party's Confidential Information secret.",
    "This Agreement shall be governed by the laws of England and Wales.",
    "The confidentiality obligations survive for three years from disclosure.",
]


def _docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _seed_matter(repo, **overrides):
    kwargs = dict(
        source_filename="Mutual NDA.docx",
        document_bytes=b"PK\x03\x04 original nda bytes",
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": [{"id": "mutuality", "decision": "pass"}]},
        triage={"triage_status": "review", "headline": "Mutual NDA"},
        source_type="gmail_demo",
        board_column="in_review",
    )
    kwargs.update(overrides)
    return repo.create_matter(**kwargs)


# --- naming grammar --------------------------------------------------------
def test_auto_naming_grammar():
    # {NN}_{stage}[_v{N}]: stage derives from (role, actor). A counterparty
    # original is the inbound "received" paper (one-shot, no version). A redline
    # is the versioned "ai_redline" stage.
    assert artifact_name(1, "counterparty", "original", 1, "docx") == "01_received.docx"
    assert artifact_name(2, "ai", "redline", 2, "docx") == "02_ai_redline_v2.docx"


def test_auto_naming_slugifies_and_pads():
    # Sequence zero-padded; ext normalised. A non-counterparty original (our org
    # authored it) reads as the one-shot "draft" stage.
    assert artifact_name(10, "Aspora Tech", "Original", 1, "PDF") == "10_draft.pdf"
    # Unknown extension falls back to docx.
    assert artifact_name(1, "ai", "redline", 1, "exe") == "01_ai_redline_v1.docx"


def test_stage_mapping_covers_full_lifecycle():
    # received <- counterparty original; draft <- our original/generated.
    assert artifact_registry.stage_for("original", "counterparty") == "received"
    assert artifact_registry.stage_for("original", "aspora_tech") == "draft"
    assert artifact_registry.stage_for("generated", "aspora") == "draft"
    assert artifact_registry.stage_for("redline", "ai") == "ai_redline"
    assert artifact_registry.stage_for("reviewed", "human") == "legal_review"
    assert artifact_registry.stage_for("sent", "human") == "sent"
    assert artifact_registry.stage_for("counter", "counterparty") == "counter"
    assert artifact_registry.stage_for("signed", "human") == "signed"
    # One-shot stages get no version suffix; repeatable stages do (from v1).
    assert artifact_name(1, "counterparty", "original", 1, "docx") == "01_received.docx"
    assert artifact_name(8, "human", "signed", 1, "pdf") == "08_signed.pdf"
    assert artifact_name(4, "human", "sent", 1, "docx") == "04_sent_v1.docx"
    assert artifact_name(5, "counterparty", "counter", 2, "docx") == "05_counter_v2.docx"


# --- creation + provenance -------------------------------------------------
def test_register_artifact_sets_provenance_and_current_pointer():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)

    artifact = artifact_service.add_artifact(
        matter["id"],
        source=artifact_registry.SOURCE_GMAIL,
        actor="counterparty",
        role="original",
        stored_filename=matter["stored_filename"],
        repository=repo,
    )

    assert artifact.id.startswith("artifact_")
    assert artifact.matter_id == matter["id"]
    assert artifact.source == "gmail"
    assert artifact.actor == "counterparty"
    assert artifact.role == "original"
    assert artifact.version == 1
    assert artifact.name == "01_received.docx"
    assert artifact.created_at  # stamped

    stored = repo.get_matter(matter["id"])
    assert stored["current_artifact_id"] == artifact.id
    assert len(stored["artifacts"]) == 1
    assert stored["artifacts"][0]["id"] == artifact.id


def test_register_artifact_hashes_supplied_bytes():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    body = b"PK\x03\x04 generated nda"

    artifact = artifact_service.add_artifact(
        matter["id"],
        source=artifact_registry.SOURCE_GENERATED,
        actor="aspora",
        role="generated",
        document_bytes=body,
        repository=repo,
    )

    assert artifact.content_hash == hash_bytes(body)
    # Bytes are retrievable through the registry.
    assert artifact_service.get_artifact_bytes(matter["id"], artifact.id, repository=repo) == body


def test_register_artifact_reuses_existing_original_bytes_without_duplicating():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo, document_bytes=b"PK\x03\x04 the original")

    artifact = artifact_service.add_artifact(
        matter["id"],
        source=artifact_registry.SOURCE_GMAIL,
        actor="counterparty",
        role="original",
        stored_filename=matter["stored_filename"],  # reuse, no new bytes
        repository=repo,
    )
    # Stored filename points at the original document already in storage.
    assert artifact.stored_filename == matter["stored_filename"]
    assert artifact_service.get_artifact_bytes(matter["id"], artifact.id, repository=repo) == b"PK\x03\x04 the original"


def test_register_rejects_unknown_source_role_and_missing_actor():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    base = dict(actor="ai", role="redline", repository=repo)

    with pytest.raises(ArtifactRegistryError):
        artifact_service.add_artifact(matter["id"], source="ftp", **base)
    with pytest.raises(ArtifactRegistryError):
        artifact_service.add_artifact(matter["id"], source="gmail", actor="", role="redline", repository=repo)
    with pytest.raises(ArtifactRegistryError):
        artifact_service.add_artifact(matter["id"], source="gmail", actor="ai", role="bogus", repository=repo)


def test_add_artifact_to_missing_matter_raises():
    repo = InMemoryMatterRepository()
    with pytest.raises(ArtifactMatterNotFoundError):
        artifact_service.add_artifact(
            "matter_does_not_exist", source="upload", actor="human", role="original", repository=repo
        )


# --- versioning ------------------------------------------------------------
def test_versions_increment_per_role():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)

    first = artifact_service.add_artifact(
        matter["id"], source="generated", actor="ai", role="redline", document_bytes=b"r1", repository=repo
    )
    second = artifact_service.add_artifact(
        matter["id"], source="generated", actor="ai", role="redline", document_bytes=b"r2", repository=repo
    )
    assert first.version == 1
    assert second.version == 2
    assert second.name == "02_ai_redline_v2.docx"  # versioned stage
    # Each version owns its OWN bytes: the v2 storage key must not overwrite v1's
    # (the version-aware provisional-storage-key regression guard).
    assert artifact_service.get_artifact_bytes(matter["id"], first.id, repository=repo) == b"r1"
    assert artifact_service.get_artifact_bytes(matter["id"], second.id, repository=repo) == b"r2"


# --- lineage ---------------------------------------------------------------
def test_based_on_lineage_links_to_existing_artifact():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)

    original = artifact_service.add_artifact(
        matter["id"], source="gmail", actor="counterparty", role="original",
        stored_filename=matter["stored_filename"], repository=repo,
    )
    redline = artifact_service.add_artifact(
        matter["id"], source="generated", actor="ai", role="redline",
        document_bytes=b"redline bytes", based_on_artifact_id=original.id, repository=repo,
    )
    assert redline.based_on_artifact_id == original.id


def test_based_on_unknown_artifact_rejected():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    with pytest.raises(ArtifactRegistryError):
        artifact_service.add_artifact(
            matter["id"], source="generated", actor="ai", role="redline",
            document_bytes=b"x", based_on_artifact_id="artifact_ghost", repository=repo,
        )


# --- current pointer -------------------------------------------------------
def test_set_current_artifact_moves_pointer():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    original = artifact_service.add_artifact(
        matter["id"], source="gmail", actor="counterparty", role="original",
        stored_filename=matter["stored_filename"], repository=repo,
    )
    redline = artifact_service.add_artifact(
        matter["id"], source="generated", actor="ai", role="redline",
        document_bytes=b"r", based_on_artifact_id=original.id, make_current=False, repository=repo,
    )
    # make_current=False left the original as current.
    assert repo.get_matter(matter["id"])["current_artifact_id"] == original.id

    artifact_service.set_current_artifact(matter["id"], redline.id, repository=repo)
    assert repo.get_matter(matter["id"])["current_artifact_id"] == redline.id


def test_set_current_unknown_artifact_rejected():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    with pytest.raises(ArtifactRegistryError):
        artifact_service.set_current_artifact(matter["id"], "artifact_ghost", repository=repo)


# --- backfill --------------------------------------------------------------
def test_backfill_registers_original_for_existing_matter():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo, source_type="gmail_demo", document_bytes=b"PK original")

    artifacts = artifact_service.backfill_matter(matter, repository=repo)

    assert len(artifacts) == 1
    original = artifacts[0]
    assert original.role == "original"
    assert original.version == 1
    assert original.source == "gmail"
    assert original.actor == "counterparty"
    assert original.stored_filename == matter["stored_filename"]

    stored = repo.get_matter(matter["id"])
    assert stored["current_artifact_id"] == original.id


def test_backfill_registers_redline_when_draft_present_with_lineage():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    repo.update_redline_draft(matter["id"], {"export_redline_edits": [{"id": "r1"}]})
    matter = repo.get_matter(matter["id"])

    artifacts = artifact_service.backfill_matter(matter, repository=repo)

    assert [a.role for a in artifacts] == ["original", "redline"]
    original, redline = artifacts
    assert redline.based_on_artifact_id == original.id
    assert redline.actor == "ai"
    assert redline.source == "generated"
    # Redline is the version that matters now.
    assert repo.get_matter(matter["id"])["current_artifact_id"] == redline.id


def test_backfill_is_idempotent():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)

    first = artifact_service.backfill_matter(matter, repository=repo)
    refreshed = repo.get_matter(matter["id"])
    second = artifact_service.backfill_matter(refreshed, repository=repo)

    assert [a.id for a in first] == [a.id for a in second]
    assert len(repo.get_matter(matter["id"])["artifacts"]) == 1


def test_backfill_infers_upload_source():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo, source_type="manual_upload")
    artifacts = artifact_service.backfill_matter(matter, repository=repo)
    assert artifacts[0].source == "upload"


def test_backfill_all_matters_summary():
    repo = InMemoryMatterRepository()
    already = _seed_matter(repo)
    artifact_service.backfill_matter(already, repository=repo)  # pre-registered
    _seed_matter(repo, source_filename="Second.docx")  # un-registered

    summary = artifact_service.backfill_all_matters(repository=repo)

    assert summary["scanned"] == 2
    assert summary["registered"] == 1
    assert summary["skipped_already_registered"] == 1
    assert summary["artifacts_created"] == 1


# --- (de)serialisation round-trip -----------------------------------------
def test_artifact_round_trips_through_dict():
    artifact = Artifact(
        id="artifact_abc",
        matter_id="matter_1",
        source="gmail",
        actor="counterparty",
        role="original",
        version=1,
        name="01_received.docx",
        content_hash="deadbeef",
        based_on_artifact_id="",
        stored_filename="matter_1-orig.docx",
        ext="docx",
        created_at="2026-06-06T00:00:00+00:00",
        metadata={"k": "v"},
    )
    assert Artifact.from_dict(artifact.to_dict()) == artifact


def test_artifacts_persist_across_repository_reads():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    artifact_service.add_artifact(
        matter["id"], source="gmail", actor="counterparty", role="original",
        stored_filename=matter["stored_filename"], repository=repo,
    )
    listed = artifact_service.list_artifacts(matter["id"], repository=repo)
    assert len(listed) == 1
    assert listed[0].role == "original"


# --- intake auto-registration ----------------------------------------------
def test_intake_auto_registers_original_artifact():
    repo = InMemoryMatterRepository()
    docx = _docx(_NDA_PARAGRAPHS)
    matter = create_matter_from_document(
        filename="mutual-nda.docx",
        document_bytes=docx,
        source_type="manual_upload",
        board_column="intake",
        repository=repo,
    )
    artifacts = artifact_service.list_artifacts(matter["id"], repository=repo)
    assert len(artifacts) == 1
    original = artifacts[0]
    assert original.role == "original"
    assert original.version == 1
    assert original.source == "upload"  # inferred from manual_upload source_type
    assert original.actor == "counterparty"
    assert original.content_hash == hash_bytes(docx)
    # The original is the current version, and its bytes are the source document.
    stored = repo.get_matter(matter["id"])
    assert stored["current_artifact_id"] == original.id
    assert artifact_service.get_artifact_bytes(matter["id"], original.id, repository=repo) == docx


def test_intake_gmail_source_registers_gmail_original():
    repo = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="mutual-nda.docx",
        document_bytes=_docx(_NDA_PARAGRAPHS),
        source_type="gmail_demo",
        board_column="gmail_demo",
        repository=repo,
    )
    [original] = artifact_service.list_artifacts(matter["id"], repository=repo)
    assert original.source == "gmail"


def test_intake_registration_does_not_double_register_on_redundant_backfill():
    repo = InMemoryMatterRepository()
    matter = create_matter_from_document(
        filename="mutual-nda.docx",
        document_bytes=_docx(_NDA_PARAGRAPHS),
        repository=repo,
    )
    # Re-running backfill over an already-registered matter is a no-op.
    artifact_service.backfill_matter(repo.get_matter(matter["id"]), repository=repo)
    assert len(artifact_service.list_artifacts(matter["id"], repository=repo)) == 1


def test_intake_registration_failure_does_not_break_intake(monkeypatch):
    repo = InMemoryMatterRepository()

    def _boom(*args, **kwargs):
        raise ArtifactRegistryError("registry exploded")

    monkeypatch.setattr(artifact_service, "backfill_matter", _boom)
    # Intake still succeeds even though artifact registration raised.
    matter = create_matter_from_document(
        filename="mutual-nda.docx",
        document_bytes=_docx(_NDA_PARAGRAPHS),
        repository=repo,
    )
    assert repo.get_matter(matter["id"]) is not None


# --- public_matter artifact view -------------------------------------------
def test_public_matter_exposes_artifact_view_and_current_pointer():
    from nda_automation.matter_view import public_matter

    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    original = artifact_service.add_artifact(
        matter["id"], source="gmail", actor="counterparty", role="original",
        stored_filename=matter["stored_filename"], repository=repo,
    )
    redline = artifact_service.add_artifact(
        matter["id"], source="generated", actor="ai", role="redline",
        document_bytes=b"r", based_on_artifact_id=original.id, repository=repo,
    )
    public = public_matter(repo.get_matter(matter["id"]))

    assert public["current_artifact_id"] == redline.id
    view = public["artifacts"]
    assert [a["role"] for a in view] == ["original", "redline"]
    # Compact projection: provenance present, storage internals omitted.
    redline_view = next(a for a in view if a["role"] == "redline")
    assert redline_view["id"] == redline.id
    assert redline_view["version"] == 1
    assert redline_view["name"] == "02_ai_redline_v1.docx"  # versioned stage
    assert redline_view["based_on_artifact_id"] == original.id
    assert redline_view["is_current"] is True
    assert "content_hash" not in redline_view
    assert "stored_filename" not in redline_view
    # The earlier original is not current.
    assert next(a for a in view if a["role"] == "original")["is_current"] is False
    # The lineage fields the dashboard "Relationships" view orders/labels by are all
    # present on every node: role, version, based_on_artifact_id, actor, created_at.
    for node in view:
        for key in ("role", "version", "based_on_artifact_id", "actor", "created_at"):
            assert key in node, key
    assert redline_view["actor"] == "ai"
    assert isinstance(redline_view["created_at"], str)


def test_public_matter_counterparty_prefers_generation_manifest():
    from nda_automation.matter_view import public_matter

    repo = InMemoryMatterRepository()
    # Subject says one thing; the generation manifest is the authoritative name.
    matter = _seed_matter(repo, intake_metadata={"subject": "RE: some thread"})
    artifact_service.add_artifact(
        matter["id"], source="generated", actor="ai", role="generated",
        document_bytes=b"gen", repository=repo,
        metadata={"generation": {"counterparty_name": "Acme Robotics Ltd"}},
    )
    public = public_matter(repo.get_matter(matter["id"]))
    # The generated NDA's manifest name is the EXACT counterparty (beats the subject).
    assert public["counterparty"] == "Acme Robotics Ltd"


def test_public_matter_counterparty_derives_from_subject_for_inbound():
    from nda_automation.matter_view import public_matter

    repo = InMemoryMatterRepository()
    # Inbound matter (no generation manifest) -> best-effort subject-derived name.
    matter = _seed_matter(repo, intake_metadata={"subject": "NDA from Globex Ltd"})
    public = public_matter(repo.get_matter(matter["id"]))
    assert public["counterparty"] == "NDA from Globex Ltd"


def test_public_matter_counterparty_unknown_fallback():
    from nda_automation.matter_view import public_matter

    # A matter dict with neither a generation manifest nor a usable subject collapses
    # to the honest "Unknown Counterparty" fallback. (A repository-built matter always
    # back-fills a subject from the filename, so the empty case is exercised at the
    # raw-dict contract level — that's where the fallback actually fires.)
    public = public_matter({"id": "m_unknown", "subject": ""})
    assert public["counterparty"] == "Unknown Counterparty"


# --- derive_counterparty precedence (generation > verified AI > normalized subject) ---
def test_derive_counterparty_prefers_verified_review_over_subject():
    # A VERIFIED AI extraction beats the (mangled) raw subject.
    matter = {
        "id": "m1",
        "subject": "Fwd: Air India <> Aspora",
        "intake_metadata": {
            "counterparty": {
                "name": "Air India Limited",
                "confidence": 0.99,
                "verified": True,
                "first_party": "Aspora",
                "second_party": "Air India Limited",
                "source": "preamble",
            }
        },
    }
    assert artifact_registry.derive_counterparty(matter) == "Air India Limited"


def test_derive_counterparty_unverified_falls_through_to_normalized_subject():
    # An UNVERIFIED (and below-threshold) AI value is ignored; the deterministic
    # subject normalizer drops the 'Aspora' side and the 'Fwd:' prefix.
    matter = {
        "id": "m2",
        "subject": "Fwd: Air India <> Aspora",
        "intake_metadata": {
            "counterparty": {
                "name": "Guessed Co",
                "confidence": 0.40,
                "verified": False,
            }
        },
    }
    assert artifact_registry.derive_counterparty(matter) == "Air India"


def test_derive_counterparty_high_confidence_without_verified_flag_is_used():
    # When 'verified' is absent, confidence >= 0.75 is treated as usable.
    matter = {
        "id": "m3",
        "subject": "Fwd: Air India <> Aspora",
        "intake_metadata": {
            "counterparty": {"name": "Air India Pvt Ltd", "confidence": 0.80}
        },
    }
    assert artifact_registry.derive_counterparty(matter) == "Air India Pvt Ltd"


def test_derive_counterparty_low_confidence_without_verified_flag_falls_through():
    matter = {
        "id": "m4",
        "subject": "Fwd: Aspora <> Coverstack",
        "intake_metadata": {
            "counterparty": {"name": "Maybe Co", "confidence": 0.50}
        },
    }
    assert artifact_registry.derive_counterparty(matter) == "Coverstack"


def test_derive_counterparty_normalizes_subject_when_no_stored_value():
    # No stored AI value at all -> the deterministic normalizer cleans the subject.
    matter = {"id": "m5", "subject": "Fwd: Stark Industries / Aspora"}
    # The '/' connector only survives because normalize runs before the Drive
    # sanitizer turns '/' into a space.
    assert artifact_registry.derive_counterparty(matter) == "Stark Industries"


def test_derive_counterparty_generation_manifest_still_wins():
    # The generation manifest is the most authoritative; it beats even a verified
    # AI review extraction.
    repo = InMemoryMatterRepository()
    matter = _seed_matter(
        repo,
        intake_metadata={
            "subject": "Fwd: Air India <> Aspora",
            "counterparty": {"name": "Air India Limited", "verified": True},
        },
    )
    artifact_service.add_artifact(
        matter["id"], source="generated", actor="ai", role="generated",
        document_bytes=b"gen", repository=repo,
        metadata={"generation": {"counterparty_name": "Acme Robotics Ltd"}},
    )
    stored = repo.get_matter(matter["id"])
    assert artifact_registry.derive_counterparty(stored) == "Acme Robotics Ltd"


def test_counterparty_from_review_defensive_on_missing_or_bad_shape():
    # Missing key, non-dict intake_metadata, and non-dict counterparty all -> "".
    assert artifact_registry.counterparty_from_review({"id": "x"}) == ""
    assert artifact_registry.counterparty_from_review({"intake_metadata": "nope"}) == ""
    assert (
        artifact_registry.counterparty_from_review(
            {"intake_metadata": {"counterparty": "nope"}}
        )
        == ""
    )
    # Verified-but-empty-name -> "".
    assert (
        artifact_registry.counterparty_from_review(
            {"intake_metadata": {"counterparty": {"name": "", "verified": True}}}
        )
        == ""
    )


def test_public_matter_omits_artifact_keys_when_no_registry():
    from nda_automation.matter_view import public_matter

    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    # A matter with no artifacts registered yet exposes neither key.
    public = public_matter(repo.get_matter(matter["id"]))
    assert "artifacts" not in public
    assert "current_artifact_id" not in public


# --- reviewed-DOCX registration (eager-at-approval wrapper) -----------------
def _matter_with_original_and_redline(repo):
    matter = _seed_matter(repo)
    original = artifact_service.add_artifact(
        matter["id"], source="gmail", actor="counterparty", role="original",
        stored_filename=matter["stored_filename"], repository=repo,
    )
    redline = artifact_service.add_artifact(
        matter["id"], source="generated", actor="ai", role="redline",
        document_bytes=b"redline bytes", based_on_artifact_id=original.id, repository=repo,
    )
    return matter, original, redline


def test_register_reviewed_docx_registers_with_human_actor_and_redline_lineage():
    repo = InMemoryMatterRepository()
    matter, _original, redline = _matter_with_original_and_redline(repo)

    reviewed = artifact_service.register_reviewed_docx(
        matter["id"], b"reviewed docx bytes", review_version_hash="hash-v1", repository=repo,
    )

    assert reviewed is not None
    assert reviewed.role == "reviewed"
    assert reviewed.actor == "human"
    assert reviewed.source == "generated"
    assert reviewed.based_on_artifact_id == redline.id  # lineage prefers the redline
    assert reviewed.metadata["review_version_hash"] == "hash-v1"
    assert reviewed.metadata["materialized_at"] == "approval"
    # Reviewed is the version that matters now.
    assert repo.get_matter(matter["id"])["current_artifact_id"] == reviewed.id


def test_register_reviewed_docx_falls_back_to_original_lineage_without_redline():
    repo = InMemoryMatterRepository()
    matter = _seed_matter(repo)
    original = artifact_service.add_artifact(
        matter["id"], source="gmail", actor="counterparty", role="original",
        stored_filename=matter["stored_filename"], repository=repo,
    )
    reviewed = artifact_service.register_reviewed_docx(matter["id"], b"reviewed", repository=repo)
    assert reviewed.based_on_artifact_id == original.id


def test_register_reviewed_docx_is_idempotent_on_identical_bytes():
    repo = InMemoryMatterRepository()
    matter, _original, _redline = _matter_with_original_and_redline(repo)

    first = artifact_service.register_reviewed_docx(matter["id"], b"reviewed v1", repository=repo)
    # Re-approval with unchanged reviewer decisions -> byte-identical -> skipped.
    second = artifact_service.register_reviewed_docx(matter["id"], b"reviewed v1", repository=repo)

    assert first is not None
    assert second is None
    reviewed = [a for a in artifact_service.list_artifacts(matter["id"], repository=repo) if a.role == "reviewed"]
    assert len(reviewed) == 1


def test_register_reviewed_docx_new_version_when_bytes_change():
    repo = InMemoryMatterRepository()
    matter, _original, _redline = _matter_with_original_and_redline(repo)

    first = artifact_service.register_reviewed_docx(matter["id"], b"reviewed v1", repository=repo)
    # A re-review changes reviewer decisions -> different reviewed bytes -> new version.
    second = artifact_service.register_reviewed_docx(matter["id"], b"reviewed v2 (re-reviewed)", repository=repo)

    assert first is not None and second is not None
    assert second.version == first.version + 1
    assert repo.get_matter(matter["id"])["current_artifact_id"] == second.id
    reviewed = [a for a in artifact_service.list_artifacts(matter["id"], repository=repo) if a.role == "reviewed"]
    assert len(reviewed) == 2
    # The re-review must NOT have overwritten v1's stored bytes: each reviewed
    # version retrieves its own distinct content (the corruption this guards).
    assert artifact_service.get_artifact_bytes(matter["id"], first.id, repository=repo) == b"reviewed v1"
    assert (
        artifact_service.get_artifact_bytes(matter["id"], second.id, repository=repo)
        == b"reviewed v2 (re-reviewed)"
    )


def test_register_reviewed_docx_missing_matter_raises():
    repo = InMemoryMatterRepository()
    with pytest.raises(ArtifactMatterNotFoundError):
        artifact_service.register_reviewed_docx("matter_ghost", b"x", repository=repo)
