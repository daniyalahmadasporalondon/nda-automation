"""Cross-path convergence contract for the EXECUTED transition.

Three independent paths flip a matter to executed:

* the DocuSign ``completed`` sync (:func:`docusign_workflow.sync_signature_status`),
* the MANUAL mark (:func:`lifecycle_signed.mark_matter_executed`),
* the SIGNED-PDF upload (:func:`lifecycle_signed.handle_signed_upload`, which also
  routes through ``mark_matter_executed``).

The bug these tests guard: the DocuSign branch used to INLINE its own three-field
executed write instead of routing through the shared
``lifecycle_signed.mark_matter_executed`` primitive, so it SKIPPED two side-effects
the primitive owns -- clearing a stale ``workflow_error`` and appending the single
``type="executed"`` timeline event. A DocuSign-executed NDA therefore diverged from
a manually-marked / uploaded one: it could carry a contradictory failed-send marker
and had NO executed audit entry.

After the fix all three paths produce an IDENTICAL executed state:
the triad (``executed`` / ``executed_at`` / ``status=fully_signed``), a CLEARED
``workflow_error``, and EXACTLY ONE ``type="executed"`` timeline event.

Plus the P2 idempotency guard: a re-sync of an already-executed envelope must not
re-churn the signed artifact (no v1->v2), append a second executed event, or
re-archive to Drive.
"""

from __future__ import annotations

from nda_automation import (
    artifact_service,
    docusign_workflow,
    lifecycle_signed,
    workflow,
)
from nda_automation.artifact_registry import (
    ACTOR_HUMAN,
    ROLE_REVIEWED,
    ROLE_SIGNED,
    SOURCE_GENERATED,
    latest_artifact_for_role,
)
from nda_automation.docusign_test_double import FakeDocuSignClient

OWNER = "google:converge"
PDF_BYTES = b"%PDF-1.4 reviewed nda body"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _make_matter(repo, *, with_reviewed_pdf: bool = True) -> str:
    matter = repo.create_matter(
        source_filename="acme-nda.docx",
        document_bytes=b"original",
        extracted_text="text",
        review_result={},
        triage={},
        owner_user_id=OWNER,
        intake_metadata={
            "reply_to": "cp@acme.com",
            "sender": "cp@acme.com",
            "subject": "Acme NDA",
        },
    )
    matter_id = matter["id"]
    if with_reviewed_pdf:
        artifact_service.add_artifact(
            matter_id,
            source=SOURCE_GENERATED,
            actor=ACTOR_HUMAN,
            role=ROLE_REVIEWED,
            document_bytes=PDF_BYTES,
            repository=repo,
            owner_user_id=OWNER,
        )
    return matter_id


def _executed_events(matter: dict) -> list[dict]:
    timeline = matter.get("matter_timeline") or []
    return [e for e in timeline if isinstance(e, dict) and e.get("type") == "executed"]


def _triad(matter: dict) -> tuple:
    return (matter.get("executed"), matter.get("status"), bool(matter.get("executed_at")))


def _drive_to_completed(repo, matter_id) -> tuple[FakeDocuSignClient, str]:
    """Send + auto-advance an envelope to ``completed`` (not yet synced)."""
    fake = FakeDocuSignClient(auto_complete=True)
    send = docusign_workflow.send_for_signature(
        repo.get_matter(matter_id, owner_user_id=OWNER),
        matter_id,
        OWNER,
        repository=repo,
        client=fake,
    )
    return fake, send.envelope_id


# --------------------------------------------------------------------------- #
# P1a -- all three paths produce the IDENTICAL executed triad + cleared error +
#        exactly one executed timeline event.
# --------------------------------------------------------------------------- #
def test_docusign_completed_produces_the_same_executed_state_as_manual(in_memory_matters):
    repo = in_memory_matters

    # Path 1: DocuSign completed sync.
    ds_id = _make_matter(repo)
    fake, _env = _drive_to_completed(repo, ds_id)
    docusign_workflow.sync_signature_status(None, ds_id, OWNER, repository=repo, client=fake)
    ds_matter = repo.get_matter(ds_id, owner_user_id=OWNER)

    # Path 2: manual mark.
    mn_id = _make_matter(repo)
    lifecycle_signed.mark_matter_executed(repo, mn_id, OWNER, actor="alice@aspora.com")
    mn_matter = repo.get_matter(mn_id, owner_user_id=OWNER)

    # IDENTICAL triad.
    assert _triad(ds_matter) == _triad(mn_matter) == (True, "fully_signed", True)
    # IDENTICAL "is executed" verdict for the board/corpus readers.
    assert workflow._is_executed(ds_matter) is workflow._is_executed(mn_matter) is True
    # EXACTLY ONE executed timeline event on each.
    assert len(_executed_events(ds_matter)) == 1
    assert len(_executed_events(mn_matter)) == 1


def test_docusign_completed_appends_exactly_one_executed_event_with_actor(in_memory_matters):
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake, _env = _drive_to_completed(repo, matter_id)

    docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=repo, client=fake)

    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    events = _executed_events(stored)
    # The bug left timeline_event=None -> ZERO executed events. Now: exactly one.
    assert len(events) == 1
    # It carries an actor (the DocuSign path), matching the manual path's contract.
    assert events[0].get("actor") == "DocuSign"


def test_docusign_completed_clears_stale_workflow_error(in_memory_matters):
    """A matter that hit a send failure (workflow_error set) and is then executed
    via DocuSign must have that stale failed-send marker CLEARED -- otherwise the
    board reads it as done while the detail card/corpus read it as a live failure.
    The inline DocuSign write skipped this; routing through mark_matter_executed
    fixes it."""
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake, _env = _drive_to_completed(repo, matter_id)
    # Simulate a prior failed send leaving a stale error on the matter.
    repo.set_matter_workflow_error(
        matter_id, {"phase": "sent", "message": "send failed"}, owner_user_id=OWNER
    )
    assert repo.get_matter(matter_id, owner_user_id=OWNER).get("workflow_error")

    docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=repo, client=fake)

    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert not stored.get("workflow_error")
    # Both readers now agree: executed/done, NOT needs-attention.
    state = workflow.workflow_state(stored)
    assert state["phase"] == workflow.PHASE_EXECUTED
    assert state["status"] == workflow.STATUS_FULLY_SIGNED
    assert state["needs_attention"] is False


# --------------------------------------------------------------------------- #
# P1b -- the FIRST completion still captures the signed PDF + archives exactly once.
# --------------------------------------------------------------------------- #
def test_first_completion_still_captures_signed_pdf(in_memory_matters):
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake, _env = _drive_to_completed(repo, matter_id)

    result = docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=repo, client=fake)

    assert result.completed is True
    assert result.signed_artifact_id
    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    signed = latest_artifact_for_role(stored, ROLE_SIGNED)
    assert signed is not None
    body = artifact_service.get_artifact_bytes(matter_id, signed.id, repository=repo, owner_user_id=OWNER)
    assert body.startswith(b"%PDF-")


def test_first_completion_archives_to_drive_exactly_once(in_memory_matters):
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake, _env = _drive_to_completed(repo, matter_id)
    calls: list[dict] = []

    def spy_archiver(**kwargs):
        calls.append(kwargs)

    docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=repo, client=fake, drive_sync=spy_archiver
    )
    assert len(calls) == 1
    assert calls[0]["signed_via"] == "docusign"


# --------------------------------------------------------------------------- #
# P2 -- idempotency: a re-sync of an ALREADY-executed envelope must not churn.
# --------------------------------------------------------------------------- #
def test_resync_does_not_churn_artifact_or_duplicate_event_or_rearchive(in_memory_matters):
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake, _env = _drive_to_completed(repo, matter_id)
    calls: list[dict] = []

    def spy_archiver(**kwargs):
        calls.append(kwargs)

    # First completion sync: captures + archives + appends the executed event.
    docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=repo, client=fake, drive_sync=spy_archiver
    )
    after_first = repo.get_matter(matter_id, owner_user_id=OWNER)
    signed_first = latest_artifact_for_role(after_first, ROLE_SIGNED)
    first_artifact_id = signed_first.id
    first_version = signed_first.version
    first_executed_at = after_first["executed_at"]
    assert len(calls) == 1
    assert len(_executed_events(after_first)) == 1

    # Re-sync the same already-completed envelope.
    second = docusign_workflow.sync_signature_status(
        None, matter_id, OWNER, repository=repo, client=fake, drive_sync=spy_archiver
    )
    after_second = repo.get_matter(matter_id, owner_user_id=OWNER)
    signed_second = latest_artifact_for_role(after_second, ROLE_SIGNED)

    # The status-refresh still reports completed and re-points to the SAME artifact.
    assert second.completed is True
    assert second.signed_artifact_id == first_artifact_id
    # No artifact churn: same id, same version (no v1->v2 re-capture).
    assert signed_second.id == first_artifact_id
    assert signed_second.version == first_version
    # Exactly ONE signed artifact survives (no duplicate appended).
    signed_all = [a for a in (after_second.get("artifacts") or []) if a.get("role") == ROLE_SIGNED]
    assert len(signed_all) == 1
    # No second executed event, executed_at unchanged (no re-flip).
    assert len(_executed_events(after_second)) == 1
    assert after_second["executed_at"] == first_executed_at
    # No re-archive on the re-sync (still exactly one archive call total).
    assert len(calls) == 1


def test_resync_does_not_redownload_the_signed_pdf(in_memory_matters):
    """The P2 churn guard must short-circuit BEFORE re-downloading the executed
    PDF. We count download_completed calls on the client to prove the re-sync is a
    cheap status refresh, not a re-capture."""
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake, _env = _drive_to_completed(repo, matter_id)

    download_calls = {"n": 0}
    original_download = fake.download_completed

    def counting_download(envelope_id):
        download_calls["n"] += 1
        return original_download(envelope_id)

    fake.download_completed = counting_download  # type: ignore[assignment]

    docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=repo, client=fake)
    assert download_calls["n"] == 1  # captured on first completion
    docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=repo, client=fake)
    assert download_calls["n"] == 1  # NOT re-downloaded on re-sync


# --------------------------------------------------------------------------- #
# Must-not-break: declined / voided / re-send-clears-flag / 1-of-2 partial.
# --------------------------------------------------------------------------- #
def test_declined_branch_intact_not_executed(in_memory_matters):
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake = FakeDocuSignClient()
    send = docusign_workflow.send_for_signature(
        repo.get_matter(matter_id, owner_user_id=OWNER), matter_id, OWNER, repository=repo, client=fake
    )
    fake.decline_envelope(send.envelope_id, "cp@acme.com")

    docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=repo, client=fake)

    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert stored.get("signature_declined") is True
    assert stored.get("executed") in (None, False)
    assert stored.get("awaiting_signature") is False
    # No executed event on a declined matter.
    assert _executed_events(stored) == []


def test_voided_branch_intact_resendable(in_memory_matters):
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake = FakeDocuSignClient()
    send = docusign_workflow.send_for_signature(
        repo.get_matter(matter_id, owner_user_id=OWNER), matter_id, OWNER, repository=repo, client=fake
    )
    fake.void_envelope(send.envelope_id, "reissue")

    docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=repo, client=fake)

    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert stored.get("signature_voided") is True
    assert stored.get("executed") in (None, False)
    assert _executed_events(stored) == []


def test_resend_after_void_still_clears_the_stale_flag(in_memory_matters):
    """The just-shipped re-send-clears-flag fix must stay intact alongside the
    convergence change."""
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake = FakeDocuSignClient()
    send = docusign_workflow.send_for_signature(
        repo.get_matter(matter_id, owner_user_id=OWNER), matter_id, OWNER, repository=repo, client=fake
    )
    fake.void_envelope(send.envelope_id, "reissue")
    docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=repo, client=fake)
    assert repo.get_matter(matter_id, owner_user_id=OWNER).get("signature_voided") is True

    resend = docusign_workflow.send_for_signature(
        repo.get_matter(matter_id, owner_user_id=OWNER), matter_id, OWNER, repository=repo, client=fake
    )
    assert resend.envelope_id != send.envelope_id
    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert stored.get("signature_voided") is False
    assert stored.get("signature_voided_at") is None


def test_partial_one_of_two_signed_is_not_executed(in_memory_matters):
    """A 1/2 partial (one recipient signed, envelope not completed) must remain
    awaiting -- not executed, no executed event, no artifact captured."""
    repo = in_memory_matters
    matter_id = _make_matter(repo)
    fake = FakeDocuSignClient()
    send = docusign_workflow.send_for_signature(
        repo.get_matter(matter_id, owner_user_id=OWNER),
        matter_id,
        OWNER,
        repository=repo,
        client=fake,
        signers=[{"name": "A", "email": "a@x.com"}, {"name": "B", "email": "b@y.com"}],
    )
    # Only one of two signs -> envelope advances but is NOT completed.
    fake.sign_recipient(send.envelope_id, "a@x.com")
    assert fake.get_envelope_status(send.envelope_id) != "completed"

    result = docusign_workflow.sync_signature_status(None, matter_id, OWNER, repository=repo, client=fake)

    assert result.completed is False
    stored = repo.get_matter(matter_id, owner_user_id=OWNER)
    assert stored.get("executed") in (None, False)
    assert latest_artifact_for_role(stored, ROLE_SIGNED) is None
    assert _executed_events(stored) == []
