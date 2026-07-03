"""Admin bulk-archive endpoint + store primitive for auto-imported Gmail noise.

Covers the safety contract end to end:

* the fail-closed selection predicate (every exclusion rule individually, plus a
  pristine gmail import that IS selected);
* dry-run never deletes and its selection hash is stable/selection-sensitive;
* execute demands the confirm hash of the CURRENT selection (409 otherwise,
  nothing deleted), archives to pruned-matters/ before deleting, marks the
  per-owner Gmail processed ledger (the mandatory re-import guard), forgets the
  render coordinator entry, and writes one JSON audit line;
* a second execute selects nothing (idempotency);
* other tenants' matters are never touched even when the window matches.

The route body is driven through a fake handler (the same pattern
test_admin_manager uses); the store is the REAL on-disk matter store rooted at a
per-test tmp dir.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest

from nda_automation import document_rendering, gmail_processed_ledger, matter_store, telemetry
from nda_automation.routes import admin as admin_routes

OWNER = "google:111"
OTHER_OWNER = "google:222"
ADMIN_USER = {"id": "google:999", "provider": "google", "email": "admin@example.com", "name": "Admin"}
NON_ADMIN_USER = {"id": "google:123", "provider": "google", "email": "user@example.com", "name": "User"}

WINDOW_AFTER = "2026-06-01T00:00:00+00:00"
WINDOW_BEFORE = "2026-06-30T00:00:00+00:00"
IN_WINDOW = "2026-06-10T00:00:00+00:00"

_AFTER_DT = datetime.fromisoformat(WINDOW_AFTER)
_BEFORE_DT = datetime.fromisoformat(WINDOW_BEFORE)


# --- fixtures ---------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
    monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")
    matter_store._invalidate_list_cache()
    # Real (non-loopback) admin gate: env-root admin only.
    monkeypatch.setenv("NDA_REQUIRE_AUTH", "1")
    monkeypatch.setenv("NDA_ADMIN_USERS", ADMIN_USER["id"])
    # Gmail polling PAUSED via the env kill switch, so execute-mode tests pass
    # the ledger-race guard by default; the polling tests flip this explicitly.
    monkeypatch.setenv("NDA_GMAIL_SYNC_ENABLED", "off")
    yield
    matter_store._invalidate_list_cache()


@pytest.fixture
def render_recorder(monkeypatch):
    """Record render-coordinator forgets + cache purges the batch performs."""
    forgotten: list[str] = []
    purged: list[str] = []

    class _Coordinator:
        def forget(self, matter_id):
            forgotten.append(matter_id)

    monkeypatch.setattr(document_rendering, "matter_render_coordinator", lambda: _Coordinator())
    monkeypatch.setattr(
        document_rendering,
        "purge_render_cache_for_source",
        lambda source_bytes, **kwargs: purged.append(kwargs.get("source_filename", "")) or 0,
    )
    return {"forgotten": forgotten, "purged": purged}


class _FakeServer:
    def __init__(self, host):
        self.server_address = (host, 0)


class _FakeHandler:
    def __init__(self, *, user=ADMIN_USER, payload=None, host="app.example.com", path="/api/admin/matters/bulk-archive"):
        self.current_user = user
        self.current_user_id = (user or {}).get("id", "")
        self.path = path
        self._payload = payload
        self.status = None
        self.response = None
        self.download = None
        self.server = _FakeServer(host)

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.response = payload

    def _send_download(self, data, filename, content_type, headers=None, *, send_body=True):
        self.status = 200
        self.download = (data, filename, content_type)


# --- matter builders --------------------------------------------------------
_MATTER_SEQ = 0


def _pristine_matter(**overrides):
    """A full matter record exactly as a deferred Gmail poll import persists it.

    Shape VERIFIED against the real ingestion path
    (``ingestion_service.create_matter_from_document`` + ``complete_intake``):
    a pristine import carries ONE system-registered ``original`` artifact with
    ``current_artifact_id`` pointing at it, TWO system-actor timeline events,
    and an ``updated_at`` a few ms AFTER ``created_at`` (the intake artifact +
    timeline writes bump it) — so the predicate must select exactly this shape.
    """
    global _MATTER_SEQ
    _MATTER_SEQ += 1
    matter_id = overrides.pop("id", f"matter_bulk{_MATTER_SEQ:04d}")
    document_bytes = overrides.pop("document_bytes", f"nda bytes {matter_id}".encode())
    artifact_id = f"artifact_{matter_id}"
    matter = {
        "id": matter_id,
        "created_at": IN_WINDOW,
        # Real imports: intake hooks bump updated_at AFTER create (verified).
        "updated_at": IN_WINDOW.replace("T00:00:00", "T00:00:01"),
        "source_type": "gmail_inbound",
        "source_filename": "Inbound NDA.docx",
        "stored_filename": f"{matter_id}-Inbound-NDA.docx",
        "document_title": "Inbound NDA",
        "status": "active",
        "board_column": "gmail_demo",
        "sender": "counterparty@example.com",
        "subject": "NDA attached",
        "received_at": IN_WINDOW,
        "message_snippet": "Please review the attached NDA.",
        "attachment_filename": "Inbound NDA.docx",
        "gmail_message_id": f"msg-{matter_id}",
        "gmail_attachment_id": f"att-{matter_id}",
        "gmail_attachment_sha256": hashlib.sha256(document_bytes).hexdigest(),
        "owner_user_id": OWNER,
        "extracted_text": "This Agreement is mutual.",
        "review_result": None,
        # System intake backfill: exactly one original artifact, current.
        "artifacts": [{
            "id": artifact_id,
            "role": "original",
            "version": 1,
            "source": "gmail_inbound",
            "actor": "counterparty",
            "stored_filename": f"{matter_id}-Inbound-NDA.docx",
        }],
        "current_artifact_id": artifact_id,
        # System intake timeline: created + review_completed, both actor=system.
        "matter_timeline": [
            {"type": "created", "actor": "system", "at": IN_WINDOW},
            {"type": "review_completed", "actor": "system", "at": IN_WINDOW},
        ],
    }
    matter.update(overrides)
    matter["_document_bytes"] = document_bytes
    return matter


def _store_matter(matter):
    matter = dict(matter)
    document_bytes = matter.pop("_document_bytes", b"nda bytes")
    stored_filename = str(matter.get("stored_filename") or "")
    if stored_filename:
        matter_store.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        (matter_store.UPLOADS_DIR / stored_filename).write_bytes(document_bytes)
    matter_store._save_matter_record(matter)
    matter_store._invalidate_list_cache()
    # Hand the bytes back to the test (never persisted on the record itself).
    return {**matter, "_document_bytes": document_bytes}


def _deterministic_review():
    return {
        "clauses": [],
        "active_review_engine": {
            "selected_engine": "deterministic",
            "executed_engine": "deterministic",
            "engine": "deterministic",
            "source": "forced",
            "status": "completed",
        },
    }


def _ai_first_review():
    return {
        "clauses": [],
        "ai_first_review": {"status": "completed", "provider": "openrouter"},
        "active_review_engine": {
            "selected_engine": "ai_first",
            "executed_engine": "ai_first",
            "engine": "ai_first",
            "source": "settings",
            "status": "completed",
        },
    }


def _reason(matter):
    return admin_routes._bulk_archive_exclusion_reason(
        matter,
        owner_user_id=OWNER,
        created_after=_AFTER_DT,
        created_before=_BEFORE_DT,
    )


def _payload(**overrides):
    payload = {
        "owner_user_id": OWNER,
        "created_after": WINDOW_AFTER,
        "created_before": WINDOW_BEFORE,
    }
    payload.update(overrides)
    return payload


def _run(payload, *, user=ADMIN_USER):
    handler = _FakeHandler(user=user, payload=payload)
    admin_routes.handle_matters_bulk_archive(handler)
    return handler


def _record_path(matter_id):
    return matter_store._matter_records_dir() / f"{matter_id}.json"


# --- predicate: every exclusion rule individually ----------------------------
def test_pristine_gmail_import_in_window_is_selected():
    assert _reason(_pristine_matter()) is None


def test_real_import_shape_with_system_writes_is_still_selected():
    """F1 verification, encoded as a regression guard.

    Probing the REAL deferred import path (ingestion_service.create_matter_from_
    document + complete_intake) shows every pristine import carries
    updated_at != created_at (intake artifact backfill + two timeline appends
    bump it), one system-registered original artifact, and two system timeline
    events; the corpus build later adds content_fingerprint via
    update_matter_fields. The predicate must SELECT this shape — a naive
    ``updated_at != created_at`` rule would exclude every import.
    """
    matter = _pristine_matter()
    assert matter["updated_at"] != matter["created_at"]
    assert [artifact["role"] for artifact in matter["artifacts"]] == ["original"]
    assert matter["current_artifact_id"] == matter["artifacts"][0]["id"]
    assert all(event["actor"] == "system" for event in matter["matter_timeline"])
    assert _reason(matter) is None
    # The lazy corpus-fingerprint system write must not read as a human touch.
    assert _reason(_pristine_matter(content_fingerprint={"version": 1, "shingles": []})) is None
    # A fail-soft intake whose artifact backfill errored (no artifacts at all)
    # is also a valid pristine shape.
    assert _reason(_pristine_matter(artifacts=[], current_artifact_id="")) is None


def test_deterministic_import_time_review_is_still_selected():
    assert _reason(_pristine_matter(review_result=_deterministic_review())) is None


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"source_type": "manual_upload"}, "not_gmail_inbound"),
        ({"gmail_message_id": ""}, "missing_gmail_message_id"),
        ({"owner_user_id": OTHER_OWNER}, "owner_mismatch"),
        ({"owner_user_id": ""}, "owner_mismatch"),
        ({"created_at": "not-a-date"}, "created_at_invalid"),
        ({"created_at": ""}, "created_at_invalid"),
        ({"created_at": "2026-05-01T00:00:00+00:00"}, "outside_window"),
        ({"created_at": "2026-07-15T00:00:00+00:00"}, "outside_window"),
        ({"status": "closed"}, "status_not_active"),
        ({"status": "approved"}, "status_not_active"),
        ({"board_column": "in_review"}, "board_column_moved"),
        ({"board_column": "sent"}, "board_column_moved"),
        ({"human_reviewed": True}, "human_reviewed"),
        ({"reviewer_decisions": {"c1": {"decision": "accept"}}}, "reviewer_decisions_present"),
        ({"approved_at": "2026-06-11T00:00:00+00:00", "approver": "x"}, "approval_present"),
        ({"artifacts": [{"id": "a1"}]}, "artifacts_present"),
        ({"artifacts": [{"id": "a1", "role": "redline"}]}, "artifacts_present"),
        (
            {"artifacts": [
                {"id": "a1", "role": "original"},
                {"id": "a2", "role": "redline"},
            ]},
            "artifacts_present",
        ),
        ({"current_artifact_id": "a1"}, "artifacts_present"),
        (
            {"matter_timeline": [
                {"type": "created", "actor": "system", "at": IN_WINDOW},
                {"type": "approved", "actor": "admin@example.com", "at": IN_WINDOW},
            ]},
            "non_system_timeline_event",
        ),
        (
            {"matter_timeline": [{"type": "approval_reset", "at": IN_WINDOW}]},
            "non_system_timeline_event",
        ),
        ({"signed_artifact_id": "a1"}, "signed_artifact_present"),
        ({"redline_draft": {"edits": [{"action": "replace_paragraph"}]}}, "redline_edits_present"),
        ({"redline_edits": [{"action": "replace_paragraph"}]}, "redline_edits_present"),
        ({"pdf_annotations": [{"id": "ann1"}]}, "pdf_annotations_present"),
        ({"last_outbound_at": "2026-06-11T00:00:00+00:00"}, "outbound_send_present"),
        ({"last_outbound_message_id": "out-1"}, "outbound_send_present"),
        ({"sent_at": "2026-06-11T00:00:00+00:00"}, "outbound_send_present"),
        ({"executed": True}, "signature_activity_present"),
        ({"executed_at": "2026-06-11T00:00:00+00:00"}, "signature_activity_present"),
        ({"signed_at": "2026-06-11T00:00:00+00:00"}, "signature_activity_present"),
        ({"awaiting_signature": True}, "signature_activity_present"),
        ({"signature_declined": True}, "signature_activity_present"),
        ({"docusign": {"signature": {"envelope_id": "e1"}}}, "docusign_present"),
        ({"review_status": "in_progress"}, "review_status_present"),
        ({"review_status": "completed"}, "review_status_present"),
        ({"review_status": "failed"}, "review_status_present"),
    ],
)
def test_exclusion_rules_fail_closed(overrides, expected_reason):
    assert _reason(_pristine_matter(**overrides)) == expected_reason


def test_ai_first_review_anywhere_excludes():
    assert _reason(_pristine_matter(review_result=_ai_first_review())) == "review_engine_not_deterministic"
    # ai_first trace with an otherwise deterministic top-level engine still excludes.
    mixed = _deterministic_review()
    mixed["ai_first_review"] = {"status": "partial"}
    assert _reason(_pristine_matter(review_result=mixed)) == "review_engine_not_deterministic"
    # Unknown / missing engine metadata on a non-empty review fails closed.
    assert _reason(_pristine_matter(review_result={"clauses": []})) == "review_engine_not_deterministic"
    assert _reason(_pristine_matter(review_result="weird")) == "review_engine_not_deterministic"


def test_human_counterparty_override_excludes():
    human = _pristine_matter(
        intake_metadata={"counterparty": {"name": "Acme", "source": "human", "verified": True}}
    )
    assert _reason(human) == "counterparty_human_override"
    ai_extracted = _pristine_matter(
        intake_metadata={"counterparty": {"name": "Acme", "source": "ai_review_preamble"}}
    )
    assert _reason(ai_extracted) is None


# --- dry run -----------------------------------------------------------------
def test_dry_run_is_default_and_deletes_nothing():
    pristine = _store_matter(_pristine_matter())
    touched = _store_matter(_pristine_matter(human_reviewed=True))

    handler = _run(_payload())
    assert handler.status == 200
    body = handler.response
    assert body["dry_run"] is True
    assert body["selected_count"] == 1
    assert body["excluded_count"] == 1
    assert [m["id"] for m in body["matters"]] == [pristine["id"]]
    assert body["excluded_samples"] == [{"id": touched["id"], "reason": "human_reviewed"}]
    assert body["archived"] == 0
    assert body["ledger_marked"] is False
    # Nothing was deleted: records and source docs are all still on disk.
    for matter in (pristine, touched):
        assert _record_path(matter["id"]).is_file()
        assert (matter_store.UPLOADS_DIR / matter["stored_filename"]).is_file()


def test_selection_hash_stable_then_changes_with_selection():
    _store_matter(_pristine_matter())
    first = _run(_payload()).response
    second = _run(_payload()).response
    assert first["selection_hash"] == second["selection_hash"]

    _store_matter(_pristine_matter())
    third = _run(_payload()).response
    assert third["selected_count"] == 2
    assert third["selection_hash"] != first["selection_hash"]


# --- execute -----------------------------------------------------------------
def test_execute_with_wrong_or_missing_confirm_is_409_and_deletes_nothing():
    pristine = _store_matter(_pristine_matter())

    missing = _run(_payload(dry_run=False))
    assert missing.status == 409
    wrong = _run(_payload(dry_run=False, confirm="deadbeef" * 8))
    assert wrong.status == 409
    # The 409 returns the FRESH hash of the current selection.
    current_hash = _run(_payload()).response["selection_hash"]
    assert wrong.response["selection_hash"] == current_hash
    assert _record_path(pristine["id"]).is_file()
    assert (matter_store.UPLOADS_DIR / pristine["stored_filename"]).is_file()


def test_execute_archives_deletes_marks_ledger_and_audits(render_recorder):
    pristine = _store_matter(_pristine_matter())
    kept = _store_matter(_pristine_matter(board_column="in_review"))
    counters_before = telemetry.snapshot()["counters"].get("bulk_archive_matters_removed", 0)

    selection_hash = _run(_payload()).response["selection_hash"]
    handler = _run(_payload(dry_run=False, confirm=selection_hash))
    assert handler.status == 200
    body = handler.response
    assert body["dry_run"] is False
    assert body["archived"] == 1
    assert [m["id"] for m in body["matters"]] == [pristine["id"]]
    assert body["ledger_marked"] is True
    assert body["audit_written"] is True

    # Record + stored source document are gone; the untouched matter remains.
    assert not _record_path(pristine["id"]).is_file()
    assert not (matter_store.UPLOADS_DIR / pristine["stored_filename"]).is_file()
    assert _record_path(kept["id"]).is_file()

    # Archived record + source bytes landed in pruned-matters/ BEFORE deletion.
    archive_dir = matter_store.DATA_DIR / matter_store.PRUNED_ARCHIVE_DIRNAME
    archived_record = json.loads((archive_dir / f"{pristine['id']}.json").read_text())
    assert archived_record["id"] == pristine["id"]
    assert archived_record["archived_source_document"]["present"] is True
    archived_source = archive_dir / archived_record["archived_source_document"]["archive_path"]
    assert archived_source.read_bytes() == pristine["_document_bytes"]

    # MANDATORY re-import guard: the gmail message id is in the processed ledger,
    # exactly as the poll path checks it.
    assert gmail_processed_ledger.is_message_processed(pristine["gmail_message_id"], OWNER)
    assert pristine["gmail_message_id"] in gmail_processed_ledger.load_processed_message_ids(OWNER)

    # Render coordinator forgot the matter and the render cache purge ran.
    assert pristine["id"] in render_recorder["forgotten"]
    assert render_recorder["purged"] == [pristine["source_filename"]]

    # One JSON audit line, ids only (no subjects/filenames).
    audit_path = archive_dir / admin_routes.BULK_ARCHIVE_AUDIT_FILENAME
    audit_lines = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert len(audit_lines) == 1
    entry = audit_lines[0]
    assert entry["deleted_ids"] == [pristine["id"]]
    assert entry["owner"] == OWNER
    assert entry["admin_user"] == ADMIN_USER["email"]
    assert entry["selection_hash"] == selection_hash
    assert entry["ledger_marked"] is True
    assert entry["window"] == {"created_after": WINDOW_AFTER, "created_before": WINDOW_BEFORE}
    serialized = json.dumps(entry)
    assert pristine["subject"] not in serialized
    assert pristine["stored_filename"] not in serialized

    counters_after = telemetry.snapshot()["counters"].get("bulk_archive_matters_removed", 0)
    assert counters_after - counters_before == 1


def test_second_execute_is_idempotent(render_recorder):
    _store_matter(_pristine_matter())
    selection_hash = _run(_payload()).response["selection_hash"]
    first = _run(_payload(dry_run=False, confirm=selection_hash))
    assert first.response["archived"] == 1

    rerun_dry = _run(_payload()).response
    assert rerun_dry["selected_count"] == 0
    second = _run(_payload(dry_run=False, confirm=rerun_dry["selection_hash"]))
    assert second.status == 200
    assert second.response["archived"] == 0
    # The stale first-run hash is now rejected outright.
    stale = _run(_payload(dry_run=False, confirm=selection_hash))
    assert stale.status == 409


def test_other_owners_matters_are_never_touched(render_recorder):
    mine = _store_matter(_pristine_matter())
    theirs = _store_matter(_pristine_matter(owner_user_id=OTHER_OWNER))

    dry = _run(_payload()).response
    assert [m["id"] for m in dry["matters"]] == [mine["id"]]

    handler = _run(_payload(dry_run=False, confirm=dry["selection_hash"]))
    assert handler.response["archived"] == 1
    assert not _record_path(mine["id"]).is_file()
    assert _record_path(theirs["id"]).is_file()
    assert (matter_store.UPLOADS_DIR / theirs["stored_filename"]).is_file()
    # And the other tenant's message id was NOT marked processed.
    assert not gmail_processed_ledger.is_message_processed(theirs["gmail_message_id"], OTHER_OWNER)


# --- request validation + gating ---------------------------------------------
def test_non_admin_is_403():
    _store_matter(_pristine_matter())
    handler = _run(_payload(), user=NON_ADMIN_USER)
    assert handler.status == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"created_after": WINDOW_AFTER, "created_before": WINDOW_BEFORE},  # no owner
        _payload(owner_user_id="   "),
        _payload(created_after=""),
        _payload(created_before="not-a-date"),
        _payload(created_after=WINDOW_BEFORE, created_before=WINDOW_AFTER),  # inverted
        _payload(dry_run="yes"),
        _payload(limit=0),
        _payload(limit="many"),
        _payload(limit=10**6),
    ],
)
def test_invalid_requests_are_400(payload):
    handler = _run(payload)
    assert handler.status == 400


def test_limit_caps_the_batch(render_recorder):
    for _ in range(3):
        _store_matter(_pristine_matter())
    dry = _run(_payload(limit=2)).response
    assert dry["selected_count"] == 2
    handler = _run(_payload(dry_run=False, confirm=dry["selection_hash"], limit=2))
    assert handler.response["archived"] == 2
    remaining = _run(_payload()).response
    assert remaining["selected_count"] == 1


# --- F2: ledger-race guard (execute refuses while Gmail polling is enabled) ---
def test_execute_refuses_while_gmail_polling_enabled(monkeypatch, render_recorder):
    pristine = _store_matter(_pristine_matter())
    dry = _run(_payload()).response

    # Flip the env kill switch back ON: default admin settings (sync_enabled +
    # inbound_enabled true) then make polling ACTIVE.
    monkeypatch.setenv("NDA_GMAIL_SYNC_ENABLED", "1")

    # Dry-run stays available while polling runs.
    assert _run(_payload()).status == 200

    handler = _run(_payload(dry_run=False, confirm=dry["selection_hash"]))
    assert handler.status == 409
    assert handler.response["polling_paused_verified"] is False
    assert "Pause Gmail polling" in handler.response["error"]
    # Nothing was deleted and the ledger was not touched.
    assert _record_path(pristine["id"]).is_file()
    assert not gmail_processed_ledger.is_message_processed(pristine["gmail_message_id"], OWNER)

    # Pausing via the env kill switch lets the same confirm proceed.
    monkeypatch.setenv("NDA_GMAIL_SYNC_ENABLED", "off")
    ok = _run(_payload(dry_run=False, confirm=dry["selection_hash"]))
    assert ok.status == 200
    assert ok.response["archived"] == 1
    assert ok.response["polling_paused_verified"] is True


def test_execute_honours_admin_polling_settings(monkeypatch, render_recorder):
    pristine = _store_matter(_pristine_matter())
    dry = _run(_payload()).response
    # Env switch open; the admin settings now decide.
    monkeypatch.setenv("NDA_GMAIL_SYNC_ENABLED", "")

    monkeypatch.setattr(
        admin_routes.app_settings,
        "gmail_settings",
        lambda: {"sync_enabled": True, "inbound_enabled": True},
    )
    refused = _run(_payload(dry_run=False, confirm=dry["selection_hash"]))
    assert refused.status == 409
    assert _record_path(pristine["id"]).is_file()

    # Master pause (sync_enabled false) => polling paused => execute proceeds.
    monkeypatch.setattr(
        admin_routes.app_settings,
        "gmail_settings",
        lambda: {"sync_enabled": False, "inbound_enabled": True},
    )
    ok = _run(_payload(dry_run=False, confirm=dry["selection_hash"]))
    assert ok.status == 200
    assert ok.response["archived"] == 1


def test_unknown_polling_state_fails_closed(monkeypatch):
    pristine = _store_matter(_pristine_matter())
    dry = _run(_payload()).response
    monkeypatch.setenv("NDA_GMAIL_SYNC_ENABLED", "")

    def _boom():
        raise RuntimeError("settings store unreachable")

    monkeypatch.setattr(admin_routes.app_settings, "gmail_settings", _boom)
    handler = _run(_payload(dry_run=False, confirm=dry["selection_hash"]))
    assert handler.status == 409
    assert _record_path(pristine["id"]).is_file()


# --- F3: delete the CONFIRMED set, never the raw predicate set -----------------
def test_store_confirmed_ids_bound_the_deletion(render_recorder):
    confirmed = _store_matter(_pristine_matter())
    unconfirmed = _store_matter(_pristine_matter())
    report = matter_store.bulk_archive_gmail_matters(
        OWNER,
        lambda matter: True,
        confirmed_matter_ids=frozenset({confirmed["id"]}),
    )
    assert [matter["id"] for matter in report["deleted_matters"]] == [confirmed["id"]]
    assert not _record_path(confirmed["id"]).is_file()
    assert _record_path(unconfirmed["id"]).is_file()
    assert (matter_store.UPLOADS_DIR / unconfirmed["stored_filename"]).is_file()


def test_matter_imported_between_confirm_and_execute_is_not_deleted(monkeypatch, render_recorder):
    """A fresh qualifying import landing AFTER the confirm-hash check is spared.

    Simulated by wrapping the store call: the wrapper inserts a new pristine
    matter (inside the window) just before delegating, i.e. after the handler
    validated the confirm hash but before the store lock — the exact race F3
    describes. Only the confirmed matter is deleted; the newcomer surfaces in
    the next dry-run instead.
    """
    confirmed = _store_matter(_pristine_matter())
    dry = _run(_payload()).response
    real_bulk_archive = matter_store.bulk_archive_gmail_matters
    late_import = {}

    def racing_bulk_archive(owner_user_id, predicate, limit=200, **kwargs):
        late_import.update(_store_matter(_pristine_matter()))
        return real_bulk_archive(owner_user_id, predicate, limit=limit, **kwargs)

    monkeypatch.setattr(matter_store, "bulk_archive_gmail_matters", racing_bulk_archive)
    handler = _run(_payload(dry_run=False, confirm=dry["selection_hash"]))
    assert handler.status == 200
    assert handler.response["archived"] == 1
    assert [m["id"] for m in handler.response["matters"]] == [confirmed["id"]]
    # The unreviewed newcomer QUALIFIES for the predicate but was never confirmed.
    assert _record_path(late_import["id"]).is_file()
    assert not _record_path(confirmed["id"]).is_file()
    # It surfaces in the next dry-run for a fresh review + confirm cycle.
    next_dry = _run(_payload()).response
    assert [m["id"] for m in next_dry["matters"]] == [late_import["id"]]


# --- store primitive guard rails ----------------------------------------------
def test_store_bulk_archive_requires_explicit_owner():
    with pytest.raises(matter_store.MatterStoreError):
        matter_store.bulk_archive_gmail_matters("", lambda matter: True)


def test_store_bulk_archive_keeps_matters_when_archive_fails(monkeypatch, render_recorder):
    pristine = _store_matter(_pristine_matter())
    monkeypatch.setattr(
        matter_store,
        "_archive_pruned_matters",
        lambda matters, **kwargs: False,
    )
    report = matter_store.bulk_archive_gmail_matters(OWNER, lambda matter: True)
    assert report["archive_failed"] is True
    assert report["deleted_matters"] == []
    assert _record_path(pristine["id"]).is_file()
    assert (matter_store.UPLOADS_DIR / pristine["stored_filename"]).is_file()


def test_store_bulk_archive_reevaluates_predicate_under_lock(render_recorder):
    """The execute predicate runs against the CURRENT record, not a snapshot."""
    pristine = _store_matter(_pristine_matter())
    selection_hash = _run(_payload()).response["selection_hash"]
    # A human touches the matter AFTER the dry run (moves it out of gmail_demo).
    touched = dict(matter_store.get_matter(pristine["id"]))
    touched["board_column"] = "in_review"
    matter_store._save_matter_record(touched)
    matter_store._invalidate_list_cache()
    # The stale confirm no longer matches the (now empty) selection: 409, kept.
    handler = _run(_payload(dry_run=False, confirm=selection_hash))
    assert handler.status == 409
    assert _record_path(pristine["id"]).is_file()
    # And the STORE primitive itself re-reads each record when deciding: a
    # predicate keyed on the live board_column now excludes the touched matter,
    # even though a stale snapshot of it would have passed.
    report = matter_store.bulk_archive_gmail_matters(
        OWNER,
        lambda matter: matter.get("board_column") == "gmail_demo",
    )
    assert report["deleted_matters"] == []
    assert _record_path(pristine["id"]).is_file()


# --- admin backup owner override ----------------------------------------------
def test_backup_owner_override_scopes_to_requested_user():
    mine = _store_matter(_pristine_matter())
    theirs = _store_matter(_pristine_matter(owner_user_id=OTHER_OWNER))

    handler = _FakeHandler(path=f"/api/matters/export?owner={OTHER_OWNER}")
    admin_routes.handle_matter_backup(handler)
    assert handler.status == 200
    backup = json.loads(handler.download[0].decode("utf-8"))
    backed_up_ids = {matter["id"] for matter in backup["matters"]}
    assert backed_up_ids == {theirs["id"]}
    assert mine["id"] not in backed_up_ids


def test_backup_without_override_still_scopes_to_caller():
    _store_matter(_pristine_matter())
    handler = _FakeHandler(path="/api/matters/export")
    admin_routes.handle_matter_backup(handler)
    assert handler.status == 200
    backup = json.loads(handler.download[0].decode("utf-8"))
    # Admin caller owns no matters; no override => their own (empty) scope.
    assert backup["matters"] == []


def test_backup_owner_override_is_still_admin_gated():
    _store_matter(_pristine_matter())
    handler = _FakeHandler(user=NON_ADMIN_USER, path=f"/api/matters/export?owner={OWNER}")
    admin_routes.handle_matter_backup(handler)
    assert handler.status == 403
    assert handler.download is None
