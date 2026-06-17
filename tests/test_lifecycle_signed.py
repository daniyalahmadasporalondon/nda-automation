"""Tests for the SIGNED lifecycle hook (terminal executed-copy capture)."""
from __future__ import annotations

import base64

import pytest

from nda_automation import artifact_service, lifecycle_signed
from nda_automation.artifact_registry import (
    ACTOR_HUMAN,
    ROLE_ORIGINAL,
    ROLE_SIGNED,
    SOURCE_UPLOAD,
    latest_artifact_for_role,
)
from nda_automation.matter_repository import InMemoryMatterRepository

OWNER = "owner-signed"
OTHER_OWNER = "owner-other"
SIGNED_BYTES = b"%PDF-1.7 executed copy bytes"


class _FakeHandler:
    """Minimal stand-in for the HTTP handler the route body expects."""

    def __init__(self, payload: dict | None = None, *, owner: str = OWNER):
        self._payload = payload
        self.current_user_id = owner
        self.current_user = None
        self.status = 200
        self.response = None

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, *, status=200, send_body=True, headers=None):
        self.status = status
        self.response = payload


def _create_matter(repo: InMemoryMatterRepository, *, owner: str = OWNER) -> dict:
    return repo.create_matter(
        source_filename="Mutual NDA.docx",
        document_bytes=b"PK\x03\x04 source docx",
        extracted_text="This Agreement is mutual.",
        review_result={"clauses": []},
        triage={"triage_status": "ready_to_sign"},
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id=owner,
    )


# --- capture_signed_artifact ----------------------------------------------
def test_capture_registers_terminal_signed_artifact_with_retrievable_bytes():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo)

    artifact = lifecycle_signed.capture_signed_artifact(
        repo, matter["id"], OWNER, SIGNED_BYTES, "Executed NDA.pdf"
    )

    assert artifact is not None
    assert artifact.role == ROLE_SIGNED
    assert artifact.actor == ACTOR_HUMAN
    assert artifact.source == SOURCE_UPLOAD
    assert artifact.ext == "pdf"
    # Terminal one-shot stage -> no version suffix in the Drive name.
    assert artifact.name.endswith("_signed.pdf")
    assert "_v" not in artifact.name

    # The executed bytes are retrievable through the registry service.
    fetched = artifact_service.get_artifact_bytes(
        matter["id"], artifact.id, repository=repo, owner_user_id=OWNER
    )
    assert fetched == SIGNED_BYTES


def test_capture_anchors_lineage_to_latest_in_flight_artifact():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo)
    original = artifact_service.add_artifact(
        matter["id"],
        source=SOURCE_UPLOAD,
        actor="counterparty",
        role=ROLE_ORIGINAL,
        stored_filename=matter["stored_filename"],
        repository=repo,
        owner_user_id=OWNER,
    )

    artifact = lifecycle_signed.capture_signed_artifact(
        repo, matter["id"], OWNER, SIGNED_BYTES, "Executed NDA.pdf"
    )

    assert artifact is not None
    assert artifact.based_on_artifact_id == original.id
    signed = latest_artifact_for_role(
        repo.get_matter(matter["id"], owner_user_id=OWNER), ROLE_SIGNED
    )
    assert signed is not None and signed.id == artifact.id


def test_second_signed_upload_replaces_rather_than_appends():
    """SIGNED is terminal: a second executed PDF REPLACES the first.

    A matter always carries exactly ONE signed artifact (the latest), and the
    retrievable bytes are the second upload's — not the first's.
    """
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo)
    first_bytes = b"%PDF-1.7 first executed copy"
    second_bytes = b"%PDF-1.7 SECOND executed copy (corrected)"

    first = lifecycle_signed.capture_signed_artifact(
        repo, matter["id"], OWNER, first_bytes, "Executed NDA.pdf"
    )
    second = lifecycle_signed.capture_signed_artifact(
        repo, matter["id"], OWNER, second_bytes, "Executed NDA v2.pdf"
    )

    assert first is not None and second is not None
    assert first.id != second.id

    # Exactly one signed artifact remains, and it is the second one.
    signed_artifacts = [
        a
        for a in artifact_service.list_artifacts(matter["id"], repository=repo, owner_user_id=OWNER)
        if a.role == ROLE_SIGNED
    ]
    assert len(signed_artifacts) == 1
    assert signed_artifacts[0].id == second.id

    # The first signed artifact is gone (no dangling record), and the retrievable
    # signed bytes are the second upload's — not the first's.
    assert (
        artifact_service.get_artifact_bytes(
            matter["id"], first.id, repository=repo, owner_user_id=OWNER
        )
        is None
    )
    assert (
        artifact_service.get_artifact_bytes(
            matter["id"], second.id, repository=repo, owner_user_id=OWNER
        )
        == second_bytes
    )

    # The current pointer follows the surviving (latest) signed artifact.
    current = repo.get_matter(matter["id"], owner_user_id=OWNER).get("current_artifact_id")
    assert current == second.id


def test_capture_returns_none_without_bytes():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo)

    assert (
        lifecycle_signed.capture_signed_artifact(repo, matter["id"], OWNER, b"", "x.pdf")
        is None
    )
    # Nothing registered.
    assert latest_artifact_for_role(
        repo.get_matter(matter["id"], owner_user_id=OWNER), ROLE_SIGNED
    ) is None


def test_capture_ownership_enforced_other_owner_cannot_capture():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo, owner=OWNER)

    # A different tenant cannot see the matter, so capture is a safe no-op.
    artifact = lifecycle_signed.capture_signed_artifact(
        repo, matter["id"], OTHER_OWNER, SIGNED_BYTES, "Executed NDA.pdf"
    )
    assert artifact is None
    # And no signed artifact leaked onto the real owner's matter.
    assert latest_artifact_for_role(
        repo.get_matter(matter["id"], owner_user_id=OWNER), ROLE_SIGNED
    ) is None


# --- handle_signed_upload (route body) ------------------------------------
@pytest.fixture
def route_repo(monkeypatch):
    """Back the route's hardcoded DiskMatterRepository() with an in-memory one."""
    repo = InMemoryMatterRepository()
    monkeypatch.setattr(lifecycle_signed, "DiskMatterRepository", lambda: repo)
    return repo


def test_route_uploads_signed_pdf_and_registers_artifact(route_repo):
    matter = _create_matter(route_repo)
    payload = {
        "filename": "Executed NDA.pdf",
        "content_base64": base64.b64encode(SIGNED_BYTES).decode("ascii"),
    }
    handler = _FakeHandler(payload, owner=OWNER)

    lifecycle_signed.handle_signed_upload(handler, f"/api/matters/{matter['id']}/signed")

    assert handler.status == 201
    artifact_id = handler.response["artifact_id"]
    assert handler.response["matter"]["id"] == matter["id"]
    fetched = artifact_service.get_artifact_bytes(
        matter["id"], artifact_id, repository=route_repo, owner_user_id=OWNER
    )
    assert fetched == SIGNED_BYTES


def test_route_rejects_non_pdf_filename(route_repo):
    matter = _create_matter(route_repo)
    payload = {
        "filename": "Executed NDA.docx",
        "content_base64": base64.b64encode(SIGNED_BYTES).decode("ascii"),
    }
    handler = _FakeHandler(payload, owner=OWNER)

    lifecycle_signed.handle_signed_upload(handler, f"/api/matters/{matter['id']}/signed")

    assert handler.status == 400
    assert lifecycle_signed.SIGNED_FILENAME_MESSAGE == handler.response["error"]


def test_route_enforces_ownership_returns_404_for_other_tenant(route_repo):
    matter = _create_matter(route_repo, owner=OWNER)
    payload = {
        "filename": "Executed NDA.pdf",
        "content_base64": base64.b64encode(SIGNED_BYTES).decode("ascii"),
    }
    handler = _FakeHandler(payload, owner=OTHER_OWNER)

    lifecycle_signed.handle_signed_upload(handler, f"/api/matters/{matter['id']}/signed")

    assert handler.status == 404
    # No signed artifact was registered on the real owner's matter.
    assert latest_artifact_for_role(
        route_repo.get_matter(matter["id"], owner_user_id=OWNER), ROLE_SIGNED
    ) is None


def test_route_rejects_undecodable_content(route_repo):
    matter = _create_matter(route_repo)
    handler = _FakeHandler(
        {"filename": "Executed NDA.pdf", "content_base64": "!!!not-base64!!!"}, owner=OWNER
    )

    lifecycle_signed.handle_signed_upload(handler, f"/api/matters/{matter['id']}/signed")

    assert handler.status == 400
    assert handler.response["error"] == lifecycle_signed.DECODE_FAILED_MESSAGE


# --- mark_matter_executed (the MANUAL "mark executed" path) ----------------
def _executed_fields(matter: dict) -> tuple:
    return (matter.get("executed"), matter.get("status"), matter.get("executed_at"))


def test_mark_executed_sets_the_three_shared_fields():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo)
    assert _executed_fields(matter) != (True, "fully_signed", matter.get("executed_at"))

    updated = lifecycle_signed.mark_matter_executed(
        repo, matter["id"], OWNER, actor="alice@aspora.com"
    )

    assert updated is not None
    assert updated["executed"] is True
    assert updated["status"] == "fully_signed"
    assert updated["executed_at"]
    # The board/corpus contract reads through workflow._is_executed.
    from nda_automation import workflow

    assert workflow._is_executed(updated) is True


def test_mark_executed_leaves_review_state_untouched():
    repo = InMemoryMatterRepository()
    review_result = {"clauses": [{"id": "c1", "status": "pass"}], "active_review_engine": {"executed_engine": "ai_first"}}
    matter = repo.create_matter(
        source_filename="Mutual NDA.docx",
        document_bytes=b"PK\x03\x04 source docx",
        extracted_text="This Agreement is mutual.",
        review_result=review_result,
        triage={"triage_status": "ready_to_sign"},
        source_type="manual_upload",
        board_column="in_review",
        owner_user_id=OWNER,
    )
    before = repo.get_matter(matter["id"], owner_user_id=OWNER)

    updated = lifecycle_signed.mark_matter_executed(repo, matter["id"], OWNER, actor="a")

    assert updated is not None
    # The mark flips only the executed contract -- the review payload + its AI-ran
    # provenance are byte-for-byte unchanged.
    assert updated.get("review_result") == before.get("review_result") == review_result


def test_mark_executed_records_who_when_timeline_event():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo)

    updated = lifecycle_signed.mark_matter_executed(
        repo, matter["id"], OWNER, actor="alice@aspora.com"
    )

    timeline = updated.get("matter_timeline") or []
    executed_events = [e for e in timeline if e.get("type") == "executed"]
    assert len(executed_events) == 1
    assert executed_events[0]["actor"] == "alice@aspora.com"


def test_mark_executed_is_idempotent_no_duplicate_event():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo)

    first = lifecycle_signed.mark_matter_executed(repo, matter["id"], OWNER, actor="a")
    first_at = first["executed_at"]
    second = lifecycle_signed.mark_matter_executed(repo, matter["id"], OWNER, actor="b")

    # Second call is a no-op: the executed_at is unchanged and there is exactly one
    # executed timeline event (the guard returns the matter untouched).
    assert second["executed_at"] == first_at
    executed_events = [e for e in (second.get("matter_timeline") or []) if e.get("type") == "executed"]
    assert len(executed_events) == 1


def test_mark_executed_returns_none_for_other_tenant():
    repo = InMemoryMatterRepository()
    matter = _create_matter(repo, owner=OWNER)

    result = lifecycle_signed.mark_matter_executed(repo, matter["id"], OTHER_OWNER, actor="x")

    assert result is None
    # No fields flipped on the real owner's matter.
    owner_matter = repo.get_matter(matter["id"], owner_user_id=OWNER)
    assert owner_matter.get("executed") in (None, False)


# --- handle_mark_executed (route body) ------------------------------------
def test_mark_executed_route_flips_fields_and_returns_200(route_repo):
    matter = _create_matter(route_repo)
    handler = _FakeHandler({}, owner=OWNER)
    handler.current_user = {"email": "alice@aspora.com"}

    lifecycle_signed.handle_mark_executed(handler, f"/api/matters/{matter['id']}/mark-executed")

    assert handler.status == 200
    body = handler.response
    assert body["matter"]["id"] == matter["id"]
    assert body["executed_by"] == "alice@aspora.com"
    assert body["executed_at"]
    stored = route_repo.get_matter(matter["id"], owner_user_id=OWNER)
    assert stored["executed"] is True
    assert stored["status"] == "fully_signed"


def test_mark_executed_route_409s_already_executed(route_repo):
    matter = _create_matter(route_repo)
    lifecycle_signed.mark_matter_executed(route_repo, matter["id"], OWNER, actor="a")
    handler = _FakeHandler({}, owner=OWNER)

    lifecycle_signed.handle_mark_executed(handler, f"/api/matters/{matter['id']}/mark-executed")

    assert handler.status == 409
    assert handler.response["error"] == lifecycle_signed.ALREADY_EXECUTED_MESSAGE


def test_mark_executed_route_404s_other_tenant(route_repo):
    matter = _create_matter(route_repo, owner=OWNER)
    handler = _FakeHandler({}, owner=OTHER_OWNER)

    lifecycle_signed.handle_mark_executed(handler, f"/api/matters/{matter['id']}/mark-executed")

    assert handler.status == 404
    assert route_repo.get_matter(matter["id"], owner_user_id=OWNER).get("executed") in (None, False)


# --- signed-upload reconciliation (auto-marks executed) --------------------
def test_signed_upload_route_also_marks_matter_executed(route_repo):
    matter = _create_matter(route_repo)
    payload = {
        "filename": "Executed NDA.pdf",
        "content_base64": base64.b64encode(SIGNED_BYTES).decode("ascii"),
    }
    handler = _FakeHandler(payload, owner=OWNER)

    lifecycle_signed.handle_signed_upload(handler, f"/api/matters/{matter['id']}/signed")

    assert handler.status == 201
    # The signed artifact landed AND the executed contract flipped (reconciliation).
    assert handler.response["matter"]["executed"] is True
    assert handler.response["matter"]["status"] == "fully_signed"
    stored = route_repo.get_matter(matter["id"], owner_user_id=OWNER)
    assert stored["executed"] is True
    assert latest_artifact_for_role(stored, ROLE_SIGNED) is not None
