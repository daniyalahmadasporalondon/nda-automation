"""Tests for the COUNTER lifecycle stage (counterparty revision capture).

Drives ``nda_automation.lifecycle_counter`` directly with a temp-dir matter store
and a fake handler -- so the tests exercise the versioned COUNTER artifact
registration, the byte storage (so the Drive sync can read the counter), and the
owner-scoping, all without a live server.
"""
from __future__ import annotations

import base64
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from nda_automation import artifact_service, lifecycle_counter, matter_store
from nda_automation.artifact_registry import ROLE_COUNTER, latest_artifact_for_role
from nda_automation.matter_repository import DiskMatterRepository


def _make_docx(text: str = "Counterparty revision.") -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


class _FakeHandler:
    def __init__(self, payload: dict | None = None, *, current_user_id: str = ""):
        self._payload = payload
        self.current_user_id = current_user_id
        self.current_user = None
        self.status = 200
        self.response = None

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, *, status=200, send_body=True):
        self.status = status
        self.response = payload


@pytest.fixture
def isolated_matter_store(monkeypatch):
    with tempfile.TemporaryDirectory() as data_dir:
        data_path = Path(data_dir)
        monkeypatch.setattr(matter_store, "DATA_DIR", data_path)
        monkeypatch.setattr(matter_store, "MATTERS_PATH", data_path / "matters.json")
        monkeypatch.setattr(matter_store, "UPLOADS_DIR", data_path / "uploads")
        yield


def _create_matter(*, owner_user_id: str = "") -> dict:
    return matter_store.create_matter(
        source_filename="Mutual NDA.docx",
        document_bytes=_make_docx("Original NDA."),
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": [{"id": "mutuality", "decision": "pass"}]},
        triage={
            "triage_status": "ready_to_sign",
            "issue_count": 0,
            "requirements_passed": 1,
            "requirements_needs_review": 0,
            "requirements_failed": 0,
        },
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id=owner_user_id,
    )


def _counter_payload(text: str) -> dict:
    return {
        "filename": "Counterparty Revision.docx",
        "content_base64": base64.b64encode(_make_docx(text)).decode("ascii"),
    }


# --- capture_counter_artifact ---------------------------------------------
def test_capture_registers_counter_v1_and_stores_bytes(isolated_matter_store):
    matter = _create_matter()
    repository = DiskMatterRepository()
    counter_bytes = _make_docx("Counter 1.")

    artifact = lifecycle_counter.capture_counter_artifact(
        repository,
        matter["id"],
        "",
        counter_bytes,
        "Counterparty Revision.docx",
    )

    assert artifact is not None
    assert artifact.role == ROLE_COUNTER
    assert artifact.version == 1
    # The stage filename carries the versioned ``counter`` stage from v1.
    assert artifact.name.endswith("_counter_v1.docx")
    # The bytes are retrievable through the service the Drive sync uses.
    stored = artifact_service.get_artifact_bytes(matter["id"], artifact.id, repository=repository)
    assert stored == counter_bytes


def test_capture_second_counter_registers_v2(isolated_matter_store):
    matter = _create_matter()
    repository = DiskMatterRepository()

    counter_v1_bytes = _make_docx("Counter 1.")
    counter_v2_bytes = _make_docx("Counter 2.")
    first = lifecycle_counter.capture_counter_artifact(
        repository, matter["id"], "", counter_v1_bytes, "c1.docx"
    )
    second = lifecycle_counter.capture_counter_artifact(
        repository, matter["id"], "", counter_v2_bytes, "c2.docx"
    )

    assert first.version == 1
    assert second.version == 2
    assert second.name.endswith("_counter_v2.docx")
    latest = latest_artifact_for_role(
        repository.get_matter(matter["id"]), ROLE_COUNTER
    )
    assert latest.id == second.id
    assert latest.version == 2

    # Each version keeps its OWN bytes: a v2 must NOT have overwritten v1's
    # stored bytes (the version-aware storage-key regression guard).
    assert counter_v1_bytes != counter_v2_bytes
    assert (
        artifact_service.get_artifact_bytes(matter["id"], first.id, repository=repository)
        == counter_v1_bytes
    )
    assert (
        artifact_service.get_artifact_bytes(matter["id"], second.id, repository=repository)
        == counter_v2_bytes
    )


def test_capture_returns_none_for_empty_bytes(isolated_matter_store):
    matter = _create_matter()
    artifact = lifecycle_counter.capture_counter_artifact(
        DiskMatterRepository(), matter["id"], "", b"", "empty.docx"
    )
    assert artifact is None


def test_capture_is_owner_scoped(isolated_matter_store):
    matter = _create_matter(owner_user_id="owner_alice")

    # A different authenticated user cannot capture a counter on Alice's matter.
    intruder = lifecycle_counter.capture_counter_artifact(
        DiskMatterRepository(),
        matter["id"],
        "intruder_bob",
        _make_docx("Sneaky counter."),
        "sneaky.docx",
    )
    assert intruder is None
    # No counter artifact leaked onto the matter.
    assert latest_artifact_for_role(
        matter_store.get_matter(matter["id"]), ROLE_COUNTER
    ) is None

    # The real owner can capture it.
    owned = lifecycle_counter.capture_counter_artifact(
        DiskMatterRepository(),
        matter["id"],
        "owner_alice",
        _make_docx("Real counter."),
        "real.docx",
    )
    assert owned is not None
    assert owned.version == 1


# --- handle_counter_upload (route body) -----------------------------------
def test_route_uploads_counter_v1_then_v2(isolated_matter_store):
    matter = _create_matter()

    first_handler = _FakeHandler(_counter_payload("Counter 1."))
    lifecycle_counter.handle_counter_upload(first_handler, f"/api/matters/{matter['id']}/counter")
    assert first_handler.status == 201
    assert first_handler.response["version"] == 1
    assert first_handler.response["artifact_name"].endswith("_counter_v1.docx")
    assert "matter" in first_handler.response

    second_handler = _FakeHandler(_counter_payload("Counter 2."))
    lifecycle_counter.handle_counter_upload(second_handler, f"/api/matters/{matter['id']}/counter")
    assert second_handler.status == 201
    assert second_handler.response["version"] == 2
    assert second_handler.response["artifact_name"].endswith("_counter_v2.docx")

    counters = [
        artifact
        for artifact in artifact_service.list_artifacts(matter["id"])
        if artifact.role == ROLE_COUNTER
    ]
    assert sorted(artifact.version for artifact in counters) == [1, 2]


def test_route_rejects_non_docx(isolated_matter_store):
    matter = _create_matter()
    handler = _FakeHandler(
        {
            "filename": "counter.pdf",
            "content_base64": base64.b64encode(b"not a docx").decode("ascii"),
        }
    )
    lifecycle_counter.handle_counter_upload(handler, f"/api/matters/{matter['id']}/counter")
    assert handler.status == 400
    assert "docx" in handler.response["error"]


def test_route_rejects_undecodable_content(isolated_matter_store):
    matter = _create_matter()
    handler = _FakeHandler({"filename": "counter.docx", "content_base64": "!!!not base64!!!"})
    lifecycle_counter.handle_counter_upload(handler, f"/api/matters/{matter['id']}/counter")
    assert handler.status == 400
    assert "decoded" in handler.response["error"]


def test_route_is_owner_scoped(isolated_matter_store):
    matter = _create_matter(owner_user_id="owner_alice")

    handler = _FakeHandler(_counter_payload("Sneaky."), current_user_id="intruder_bob")
    lifecycle_counter.handle_counter_upload(handler, f"/api/matters/{matter['id']}/counter")

    assert handler.status == 404
    assert latest_artifact_for_role(
        matter_store.get_matter(matter["id"]), ROLE_COUNTER
    ) is None


def test_route_404_for_unknown_matter(isolated_matter_store):
    handler = _FakeHandler(_counter_payload("Counter."))
    lifecycle_counter.handle_counter_upload(handler, "/api/matters/matter_missing/counter")
    assert handler.status == 404
