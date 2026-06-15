"""Tests for the SENT lifecycle hook (``nda_automation.lifecycle_sent``).

A send registers a SENT artifact with the exact emailed bytes (retrievable via
``artifact_service.get_artifact_bytes``), versioned via the registry; a second
send to the same matter registers ``sent`` v2.
"""
from __future__ import annotations

from nda_automation import artifact_service, lifecycle_sent
from nda_automation.artifact_registry import ROLE_SENT, hash_bytes
from nda_automation.matter_repository import InMemoryMatterRepository


def _make_matter(repo: InMemoryMatterRepository) -> dict:
    return repo.create_matter(
        source_filename="Mutual NDA.docx",
        document_bytes=b"original-source",
        extracted_text="Text",
        review_result={"clauses": []},
        triage={"triage_status": "ready_to_sign"},
    )


def test_send_registers_sent_artifact_with_sent_bytes():
    repo = InMemoryMatterRepository()
    matter = _make_matter(repo)
    matter_id = str(matter["id"])
    sent_bytes = b"the-exact-emailed-docx-bytes"

    artifact = lifecycle_sent.capture_sent_artifact(
        repo,
        matter_id,
        "",
        sent_bytes,
        "04_sent.docx",
        "counsel@counterparty.example",
    )

    assert artifact is not None
    assert artifact.role == ROLE_SENT
    assert artifact.version == 1
    assert artifact.name == "01_sent_v1.docx"  # versioned stage, first chronological slot
    assert artifact.content_hash == hash_bytes(sent_bytes)
    assert artifact.metadata.get("recipient") == "counsel@counterparty.example"
    assert artifact.metadata.get("sent_filename") == "04_sent.docx"

    # The Drive sync reads bytes back through this seam — they must match exactly.
    stored = artifact_service.get_artifact_bytes(matter_id, artifact.id, repository=repo)
    assert stored == sent_bytes

    artifacts = artifact_service.list_artifacts(matter_id, repository=repo)
    assert [a.role for a in artifacts] == [ROLE_SENT]


def test_second_send_registers_sent_v2():
    repo = InMemoryMatterRepository()
    matter = _make_matter(repo)
    matter_id = str(matter["id"])

    first = lifecycle_sent.capture_sent_artifact(
        repo, matter_id, "", b"first-emailed-bytes", "sent.docx", "a@b.example"
    )
    second = lifecycle_sent.capture_sent_artifact(
        repo, matter_id, "", b"second-emailed-bytes", "sent.docx", "a@b.example"
    )

    assert first is not None and second is not None
    assert first.version == 1
    assert second.version == 2
    assert first.name == "01_sent_v1.docx"
    assert second.name == "02_sent_v2.docx"

    # Each version stores its own bytes, retrievable independently.
    assert artifact_service.get_artifact_bytes(matter_id, first.id, repository=repo) == b"first-emailed-bytes"
    assert artifact_service.get_artifact_bytes(matter_id, second.id, repository=repo) == b"second-emailed-bytes"

    sent_artifacts = [a for a in artifact_service.list_artifacts(matter_id, repository=repo) if a.role == ROLE_SENT]
    assert [a.version for a in sent_artifacts] == [1, 2]


def test_capture_is_noop_without_bytes_or_matter():
    repo = InMemoryMatterRepository()
    matter = _make_matter(repo)
    matter_id = str(matter["id"])

    # No bytes -> nothing to capture.
    assert lifecycle_sent.capture_sent_artifact(repo, matter_id, "", b"", "sent.docx", "a@b.example") is None
    # Unknown matter -> nothing to capture, no raise.
    assert lifecycle_sent.capture_sent_artifact(repo, "matter_missing", "", b"bytes", "sent.docx", "a@b.example") is None
    # Empty matter id -> nothing to capture.
    assert lifecycle_sent.capture_sent_artifact(repo, "", "", b"bytes", "sent.docx", "a@b.example") is None

    assert artifact_service.list_artifacts(matter_id, repository=repo) == []


def test_sent_lineage_points_at_latest_upstream():
    repo = InMemoryMatterRepository()
    matter = _make_matter(repo)
    matter_id = str(matter["id"])

    # Seed an upstream redline so the SENT artifact records lineage to it.
    redline = artifact_service.add_artifact(
        matter_id,
        source="generated",
        actor="ai",
        role="redline",
        document_bytes=b"redline-bytes",
        repository=repo,
    )

    sent = lifecycle_sent.capture_sent_artifact(
        repo, matter_id, "", b"emailed-bytes", "sent.docx", "a@b.example"
    )
    assert sent is not None
    assert sent.based_on_artifact_id == redline.id
