"""Admin garble-backfill endpoint + detection/heal service.

Covers the full safety contract against a REAL on-disk fixture store:

* detection hits the pre-fix glyph-garbled fixture (generated from the parent
  fix's regression PDF with the extractor demotion disabled) and misses a
  normal matter, a DOCX matter with garble-shaped text, a numbered-list run of
  short paragraphs, and a single spaced-out letterhead heading;
* dry-run (the default) reports would_reextract and writes NOTHING — the whole
  store is byte-compared before/after;
* execute re-extracts through the FIXED extractor: blocks become coherent
  ('CEO', 'Moorwand Limited', no shards), the untouched review_result is
  flagged stale by the EXISTING contract
  (routes/matters._matter_review_text_changed -> "matter_text_changed"), the
  stale corpus content_fingerprint is dropped, and other matters stay
  byte-identical;
* missing original bytes -> skip + report, record untouched;
* non-admin -> 403 (all three endpoints); starting an execute without
  "confirm": true -> 400 (string "true" included), nothing mutated;
* the EXECUTE path is the background /start route (+ GET /status): driven here
  with the daemon thread patched to run INLINE for determinism; the bare
  endpoint's synchronous execute is REMOVED (dry_run:false -> 400);
* the in-store-lock veto (reject_when) stops an approval landing DURING the
  re-extraction window from being overwritten (excluded_executed_late).

The route body is driven through a fake handler (the same pattern
test_bulk_archive / test_admin_manager use); the store is the REAL matter store
rooted at a per-test tmp dir.
"""

from __future__ import annotations

import copy
import json
import types
from io import BytesIO
from pathlib import Path

import pytest

pytest.importorskip("pypdf")

from nda_automation import (
    artifact_registry,
    garble_backfill,
    ingestion_service,
    matter_store,
    pdf_ingest_conversion,
    pdf_text,
)
from nda_automation.review_result_contract import extracted_text_from_paragraphs
from nda_automation.routes import admin as admin_routes
from nda_automation.routes import matters as matters_routes

from test_pdf_text import (
    make_pdf,
    make_pdf_glyph_fragmented_signature_page,
    make_pdf_shard_fragmented,
)

OWNER = "google:111"
ADMIN_USER = {"id": "google:999", "provider": "google", "email": "admin@example.com", "name": "Admin"}
NON_ADMIN_USER = {"id": "google:123", "provider": "google", "email": "user@example.com", "name": "User"}


# --- fixture PDFs / texts -----------------------------------------------------
GARBLED_PDF_BYTES = make_pdf_glyph_fragmented_signature_page()
NORMAL_PDF_BYTES = make_pdf("The parties agree to keep all Confidential Information secret at all times.")


def _legacy_join_line_chunks(bucket):
    """The PRE-D4 line-chunk join: always a single space between chunks, so a
    per-glyph line explodes into ``M o r w a n d`` (the historical garbled shape)."""
    return " ".join(" ".join(chunk[3].split()) for chunk in bucket if chunk[3].split())


def _pre_fix_extracted_text(pdf_bytes: bytes) -> str:
    """The text the PRE-FIX extractor stored for these bytes: run the current
    extractor with BOTH garble suppressors disabled -- the per-glyph demotion
    (``_GLYPH_FRAGMENT_RUN_MIN``) AND the D4 adjacency join (``_join_line_chunks``)
    -- reproducing the historical garbled stored shape this tool exists to heal.
    Both must be neutralized: D4 fixed garble at a different layer than the
    demotion toggle, so disabling only the demotion now yields clean text."""
    original = pdf_text._GLYPH_FRAGMENT_RUN_MIN
    original_join = pdf_text._join_line_chunks
    pdf_text._GLYPH_FRAGMENT_RUN_MIN = 10**9
    pdf_text._join_line_chunks = _legacy_join_line_chunks
    try:
        paragraphs = pdf_text.extract_pdf_paragraphs(pdf_bytes)
    finally:
        pdf_text._GLYPH_FRAGMENT_RUN_MIN = original
        pdf_text._join_line_chunks = original_join
    return extracted_text_from_paragraphs(paragraphs)


GARBLED_TEXT = _pre_fix_extracted_text(GARBLED_PDF_BYTES)

# SHARD-fragment garble (the SECOND class). Its stored text is simply the current
# extractor's output for the shard fixture (flag OFF) — a long run of <= 2-char
# shard paragraphs — so re-extraction with the flag OFF reproduces it (still
# garbled), while the DEFAULT-OFF reflow heals it.
SHARD_PDF_BYTES = make_pdf_shard_fragmented()
SHARD_TEXT = extracted_text_from_paragraphs(pdf_text.extract_pdf_paragraphs(SHARD_PDF_BYTES))


# --- store fixtures -----------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
    monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")
    matter_store._invalidate_list_cache()
    # Real (non-loopback) admin gate: env-root admin only.
    monkeypatch.setenv("NDA_REQUIRE_AUTH", "1")
    monkeypatch.setenv("NDA_ADMIN_USERS", ADMIN_USER["id"])
    yield tmp_path
    matter_store._invalidate_list_cache()


class _InlineThread:
    """threading.Thread stand-in that runs the target synchronously on start()."""

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


@pytest.fixture(autouse=True)
def _inline_backfill_thread(monkeypatch):
    """Deterministic execute runs: patch ONLY garble_backfill's thread spawn to
    run inline (start_garble_backfill_async resolves ``threading.Thread`` at call
    time through its module global), and reset the module run/status state."""
    monkeypatch.setattr(garble_backfill, "threading", types.SimpleNamespace(Thread=_InlineThread))
    monkeypatch.setattr(garble_backfill, "_RUNNING", False)
    with garble_backfill._STATUS_LOCK:
        garble_backfill._LAST_STATUS.clear()
    yield
    with garble_backfill._STATUS_LOCK:
        garble_backfill._LAST_STATUS.clear()


class _FakeServer:
    def __init__(self, host):
        self.server_address = (host, 0)


class _FakeHandler:
    def __init__(self, *, user=ADMIN_USER, payload=None, host="app.example.com",
                 path="/api/admin/matters/garble-backfill"):
        self.current_user = user
        self.current_user_id = (user or {}).get("id", "")
        self.path = path
        self._payload = payload
        self.status = None
        self.response = None
        self.server = _FakeServer(host)

    def _read_json_payload(self):
        return self._payload

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.response = payload


_MATTER_SEQ = 0


def _ai_review_snapshot(extracted_text):
    """An AI-first review shape carrying the extracted_text snapshot the real
    engine records (attach_document_source) — what the staleness contract reads."""
    return {
        "clauses": [],
        "extracted_text": extracted_text,
        "ai_first_review": {"status": "completed", "provider": "openrouter"},
        "active_review_engine": {
            "selected_engine": "ai_first",
            "executed_engine": "ai_first",
            "engine": "ai_first",
            "source": "settings",
            "status": "completed",
        },
    }


def _store_matter(*, filename, extracted_text, document_bytes=None, review_result=None, **overrides):
    global _MATTER_SEQ
    _MATTER_SEQ += 1
    matter_id = overrides.pop("id", f"matter_garble{_MATTER_SEQ:04d}")
    stored_filename = f"{matter_id}-{filename.replace(' ', '-')}"
    matter = {
        "id": matter_id,
        "created_at": "2026-06-10T00:00:00+00:00",
        "updated_at": "2026-06-10T00:00:01+00:00",
        "source_type": "gmail_inbound",
        "source_filename": filename,
        "stored_filename": stored_filename,
        "document_title": Path(filename).stem,
        "status": "active",
        "board_column": "gmail_demo",
        "owner_user_id": OWNER,
        "extracted_text": extracted_text,
        "review_result": review_result,
    }
    matter.update(overrides)
    if document_bytes is not None:
        matter_store.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        (matter_store.UPLOADS_DIR / stored_filename).write_bytes(document_bytes)
    matter_store._save_matter_record(matter)
    matter_store._invalidate_list_cache()
    return matter


def _garbled_matter(**overrides):
    return _store_matter(
        filename="Signature NDA.pdf",
        extracted_text=GARBLED_TEXT,
        document_bytes=GARBLED_PDF_BYTES,
        review_result=_ai_review_snapshot(GARBLED_TEXT),
        **overrides,
    )


def _normal_matter(**overrides):
    return _store_matter(
        filename="Clean NDA.pdf",
        extracted_text="The parties agree to keep all Confidential Information secret at all times.",
        document_bytes=NORMAL_PDF_BYTES,
        review_result=_ai_review_snapshot(
            "The parties agree to keep all Confidential Information secret at all times."
        ),
        **overrides,
    )


def _run(payload, *, user=ADMIN_USER):
    handler = _FakeHandler(user=user, payload=payload)
    admin_routes.handle_matters_garble_backfill(handler)
    return handler


def _execute(payload=None, *, user=ADMIN_USER):
    """Drive the EXECUTE path end to end: POST .../start (thread runs inline via
    the autouse fixture), then return (start_handler, final report from the
    status snapshot — None when the run never started)."""
    handler = _FakeHandler(
        user=user,
        payload={"confirm": True, **(payload or {})},
        path="/api/admin/matters/garble-backfill/start",
    )
    admin_routes.handle_matters_garble_backfill_start(handler)
    status = garble_backfill.garble_backfill_status()
    report = status.get("report") if isinstance(status, dict) else None
    return handler, report


def _status(*, user=ADMIN_USER):
    handler = _FakeHandler(user=user, payload=None, path="/api/admin/matters/garble-backfill/status")
    admin_routes.handle_matters_garble_backfill_status(handler)
    return handler


def _store_snapshot(tmp_path):
    """Byte-exact snapshot of every DATA file under the store (records + uploads).

    ``*.lock`` files are excluded: they are the empty flock coordination files
    that any locked READ creates (matter store, app settings via the admin
    gate) — infrastructure, never data.
    """
    return {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file() and path.suffix != ".lock"
    }


def _record(matter_id):
    path = matter_store._matter_records_dir() / f"{matter_id}.json"
    return json.loads(path.read_text())


# --- (a) detection ------------------------------------------------------------
def test_prefix_fixture_text_really_is_garbled_shape():
    """Sanity-pin the historical stored shape this whole tool exists for."""
    blocks = garble_backfill.stored_paragraph_blocks(GARBLED_TEXT)
    assert "C" in blocks and "E" in blocks and "O" in blocks
    assert any(block.startswith("M o r w a n d") for block in blocks)


def test_detection_hits_garbled_pdf_matter():
    matter = _garbled_matter()
    assessment = garble_backfill.matter_garble_assessment(matter)
    assert assessment["candidate"] is True
    fingerprint = assessment["fingerprint"]
    assert fingerprint["garbled"] is True
    assert fingerprint["longest_shard_run"] >= 3  # 'C','E','O'
    assert fingerprint["exploded_count"] >= 2  # 'M o r w a n d ...' et al.


def test_detection_misses_normal_pdf_matter():
    assessment = garble_backfill.matter_garble_assessment(_normal_matter())
    assert assessment["candidate"] is False
    assert assessment["skip_reason"] == "not_garbled"
    assert assessment["fingerprint"]["garbled"] is False


def test_docx_matter_is_never_a_candidate_even_with_garble_shaped_text():
    matter = _store_matter(filename="Word NDA.docx", extracted_text=GARBLED_TEXT)
    assessment = garble_backfill.matter_garble_assessment(matter)
    assert assessment["candidate"] is False
    assert assessment["skip_reason"] == "not_pdf_source"


@pytest.mark.parametrize(
    "text",
    [
        # A numbered list yielding standalone short clause-number paragraphs:
        # a shard run of 3 with no exploded corroboration must NOT match.
        "1\n\nConfidentiality obligations survive.\n\n2\n\n3\n\n4\n\nGoverning law is England.",
        # ONE legitimately spaced-out letterhead heading must NOT match alone.
        "C O N F I D E N T I A L\n\nThe parties agree to keep all information secret.",
        # Empty text is not garble.
        "",
    ],
)
def test_detection_fails_safe_on_plausible_legitimate_shapes(text):
    fingerprint = garble_backfill.garble_fingerprint(
        garble_backfill.stored_paragraph_blocks(text)
    )
    assert fingerprint["garbled"] is False


def test_detection_alone_never_mutates(_isolated_store):
    matter = _garbled_matter()
    before = _store_snapshot(_isolated_store)
    assessment = garble_backfill.matter_garble_assessment(
        matter_store.get_matter(matter["id"], owner_user_id="")
    )
    assert assessment["candidate"] is True
    assert _store_snapshot(_isolated_store) == before


# --- (b) dry-run --------------------------------------------------------------
def test_dry_run_is_default_reports_and_writes_nothing(_isolated_store):
    garbled = _garbled_matter()
    _normal_matter()
    before = _store_snapshot(_isolated_store)

    handler = _run({})
    assert handler.status == 200
    body = handler.response
    assert body["dry_run"] is True
    assert body["scanned"] == 2
    assert body["garbled_matched"] == 1
    assert body["selected"] == 1
    entry = body["matters"][0]
    assert entry["id"] == garbled["id"]
    assert entry["document"] == "Signature NDA.pdf"
    assert entry["action"] == "would_reextract"
    assert entry["fingerprint"]["garbled"] is True
    assert entry["fingerprint"]["shard_count"] >= 3
    assert body["healed"] == 0

    # NO writes of any kind: the store is byte-identical.
    assert _store_snapshot(_isolated_store) == before


def test_dry_run_respects_limit():
    for _ in range(3):
        _garbled_matter()
    handler = _run({"limit": 2})
    assert handler.status == 200
    assert handler.response["garbled_matched"] == 3
    assert handler.response["selected"] == 2


def test_invalid_limit_is_400():
    handler = _run({"limit": 0})
    assert handler.status == 400
    handler = _run({"limit": "many"})
    assert handler.status == 400
    handler = _run({"limit": garble_backfill.GARBLE_BACKFILL_MAX_LIMIT + 1})
    assert handler.status == 400


# --- (c) mutation -------------------------------------------------------------
def test_execute_heals_blocks_flags_review_stale_and_leaves_others_untouched(_isolated_store):
    garbled = _garbled_matter()
    normal = _normal_matter()
    review_before = copy.deepcopy(garbled["review_result"])
    normal_record_before = (matter_store._matter_records_dir() / f"{normal['id']}.json").read_bytes()

    # BEFORE: the untouched matter/review pair is NOT text-stale.
    stored = matter_store.get_matter(garbled["id"], owner_user_id="")
    _may, reasons = matters_routes._review_may_be_stale(stored, playbook_stale=False)
    assert "matter_text_changed" not in reasons

    handler, body = _execute()
    assert handler.status == 202
    assert handler.response["started"] is True
    assert body["dry_run"] is False
    assert body["healed"] == 1
    assert body["failed"] == 0
    assert body["matters"][0]["action"] == "healed"

    healed = matter_store.get_matter(garbled["id"], owner_user_id="")
    blocks = garble_backfill.stored_paragraph_blocks(healed["extracted_text"])
    # Blocks are coherent: the signature page reassembled, no one/two-char shards.
    assert "CEO" in blocks
    assert "Moorwand Limited" in blocks
    assert [b for b in blocks if len(b) <= 2] == []
    assert garble_backfill.garble_fingerprint(blocks)["garbled"] is False

    # STALENESS, NOT REPAIR: review_result is byte-for-byte untouched...
    assert healed["review_result"] == review_before
    # ...and the EXISTING staleness contract now flags the text drift by itself.
    assert matters_routes._matter_review_text_changed(healed, healed["review_result"]) is True
    may_be_stale, reasons = matters_routes._review_may_be_stale(healed, playbook_stale=False)
    assert may_be_stale is True
    assert "matter_text_changed" in reasons

    # The stale corpus fingerprint (pure function of the old text) was dropped.
    assert "content_fingerprint" not in healed

    # Other matters: byte-identical record on disk.
    normal_record_after = (matter_store._matter_records_dir() / f"{normal['id']}.json").read_bytes()
    assert normal_record_after == normal_record_before


def test_execute_run_is_idempotent():
    _garbled_matter()
    _first_handler, first_report = _execute()
    assert first_report["healed"] == 1
    second_handler, second_report = _execute()
    assert second_handler.status == 202
    assert second_report["garbled_matched"] == 0
    assert second_report["healed"] == 0


def test_status_endpoint_serves_progress_and_final_report():
    garbled = _garbled_matter()
    # Before any run: empty snapshot.
    empty = _status()
    assert empty.status == 200
    assert empty.response == {"status": {}}

    _handler, report = _execute()
    status_handler = _status()
    assert status_handler.status == 200
    snapshot = status_handler.response["status"]
    assert snapshot["state"] == "done"
    assert snapshot["run_id"].startswith("garble-backfill-")
    assert snapshot["healed"] == 1
    assert snapshot["report"] == report
    assert snapshot["report"]["matters"][0]["id"] == garbled["id"]


def test_start_while_a_run_is_in_flight_is_409(_isolated_store, monkeypatch):
    garbled = _garbled_matter()
    before = _store_snapshot(_isolated_store)
    monkeypatch.setattr(garble_backfill, "_RUNNING", True)
    handler, report = _execute()
    assert handler.status == 409
    assert handler.response["already_running"] is True
    assert report is None
    # Nothing ran, nothing written.
    assert _store_snapshot(_isolated_store) == before
    assert matter_store.get_matter(garbled["id"], owner_user_id="")["extracted_text"] == GARBLED_TEXT


def test_dry_run_never_reads_document_bytes(monkeypatch):
    """The dry-run is detection-only — record reads, zero byte reads — which is
    why it may stay synchronous on the request thread."""
    _garbled_matter()

    def _boom(matter):
        raise AssertionError("dry-run must not read document bytes")

    monkeypatch.setattr(garble_backfill.matter_store, "get_source_document_bytes", _boom)
    handler = _run({})
    assert handler.status == 200
    assert handler.response["dry_run"] is True
    assert handler.response["selected"] == 1


def test_execute_does_not_write_when_reextraction_is_still_garbled(_isolated_store, monkeypatch):
    """If the fixed extractor somehow still yields garble, swapping garble for
    garble is refused (report-only)."""
    garbled = _garbled_matter()
    before = (matter_store._matter_records_dir() / f"{garbled['id']}.json").read_bytes()
    # Force the re-extraction to reproduce the garbled shape (as pre-fix code would).
    monkeypatch.setattr(pdf_text, "_GLYPH_FRAGMENT_RUN_MIN", 10**9)
    handler, report = _execute()
    assert handler.status == 202
    assert report["healed"] == 0
    # 'unchanged' when the reproduction is byte-identical, else 'still_garbled':
    # either way NOTHING was written.
    assert report["matters"][0]["action"] in ("unchanged", "still_garbled")
    assert (matter_store._matter_records_dir() / f"{garbled['id']}.json").read_bytes() == before


# --- (d) missing bytes --------------------------------------------------------
def test_missing_source_bytes_skips_and_reports(_isolated_store):
    garbled = _garbled_matter()
    (matter_store.UPLOADS_DIR / garbled["stored_filename"]).unlink()
    before = _store_snapshot(_isolated_store)

    handler, body = _execute()
    assert handler.status == 202
    assert body["healed"] == 0
    assert body["skipped_missing_bytes"] == 1
    assert body["matters"][0]["action"] == "skipped_missing_bytes"
    # Fail-soft: nothing was written anywhere.
    assert _store_snapshot(_isolated_store) == before


def test_one_matters_failure_does_not_abort_the_run(monkeypatch):
    broken = _garbled_matter()
    healthy = _garbled_matter()

    real_extract = garble_backfill.matter_store.get_source_document_bytes

    def _bytes(matter):
        if matter.get("id") == broken["id"]:
            raise OSError("disk went away")
        return real_extract(matter)

    monkeypatch.setattr(garble_backfill.matter_store, "get_source_document_bytes", _bytes)
    handler, body = _execute()
    assert handler.status == 202
    assert body["healed"] == 1
    assert body["failed"] == 1
    assert body["errors"] and body["errors"][0]["id"] == broken["id"]
    by_id = {entry["id"]: entry for entry in body["matters"]}
    assert by_id[broken["id"]]["action"] == "failed"
    assert by_id[healthy["id"]]["action"] == "healed"


# --- executed/approved exclusion ------------------------------------------------
# The mark-executed triad lifecycle_signed.mark_matter_executed stamps, plus the
# partial legacy variants workflow.is_matter_executed also treats as executed.
_EXECUTED_VARIANTS = [
    {"executed": True, "executed_at": "2026-06-20T00:00:00+00:00", "status": "fully_signed"},
    {"executed_at": "2026-06-20T00:00:00+00:00"},
    {"executed": True},
    # BARE status=fully_signed (legacy/partial stamp): the product treats this
    # status alone as signed elsewhere (drive_integration's signed filter,
    # corpus_index._SIGNED_TRUE_STATUSES), so it must exclude here too.
    {"status": "fully_signed"},
]
# The approve-transition triad matter_store.record_matter_approval stamps, plus
# the partial signals docusign_workflow.matter_cleared_for_signature keys on.
_APPROVED_VARIANTS = [
    {"status": "approved", "approver": "counsel@example.com", "approved_at": "2026-06-20T00:00:00+00:00"},
    {"status": "approved"},
    {"approved_at": "2026-06-20T00:00:00+00:00"},
]


@pytest.mark.parametrize("overrides", _EXECUTED_VARIANTS + _APPROVED_VARIANTS)
def test_executed_or_approved_matter_is_detected_reported_but_never_written(
    _isolated_store, overrides
):
    protected = _garbled_matter(**overrides)
    healable = _garbled_matter()
    record_path = matter_store._matter_records_dir() / f"{protected['id']}.json"
    protected_before = record_path.read_bytes()

    # Dry-run LISTS it with the distinct status (the owner must know it exists)
    # and it never consumes a heal slot.
    dry = _run({})
    assert dry.status == 200
    assert dry.response["garbled_matched"] == 2
    assert dry.response["excluded_executed"] == 1
    assert dry.response["selected"] == 1
    dry_by_id = {entry["id"]: entry for entry in dry.response["matters"]}
    assert dry_by_id[protected["id"]]["action"] == "excluded_executed"
    assert dry_by_id[protected["id"]]["fingerprint"]["garbled"] is True
    assert dry_by_id[healable["id"]]["action"] == "would_reextract"

    # Execute: still listed excluded, never written — byte-identical record —
    # while the unprotected sibling heals normally.
    handler, body = _execute()
    assert handler.status == 202
    assert body["excluded_executed"] == 1
    assert body["healed"] == 1
    by_id = {entry["id"]: entry for entry in body["matters"]}
    assert by_id[protected["id"]]["action"] == "excluded_executed"
    assert by_id[healable["id"]]["action"] == "healed"
    assert record_path.read_bytes() == protected_before
    protected_after = matter_store.get_matter(protected["id"], owner_user_id="")
    assert protected_after["extracted_text"] == GARBLED_TEXT


def test_matter_executed_between_selection_and_write_is_not_written(monkeypatch):
    """The exclusion is re-checked on the FRESH record right before healing."""
    garbled = _garbled_matter()
    record_path = matter_store._matter_records_dir() / f"{garbled['id']}.json"

    real_get = garble_backfill.matter_store.get_matter

    def _get_and_execute(matter_id, owner_user_id=""):
        fresh = real_get(matter_id, owner_user_id=owner_user_id)
        if isinstance(fresh, dict):
            # A DocuSign completion landing mid-run: the re-read sees it executed.
            return {**fresh, "executed": True, "executed_at": "2026-06-20T00:00:00+00:00"}
        return fresh

    monkeypatch.setattr(garble_backfill.matter_store, "get_matter", _get_and_execute)
    before = record_path.read_bytes()
    handler, report = _execute()
    assert handler.status == 202
    assert report["healed"] == 0
    assert report["excluded_executed"] == 1
    assert report["matters"][0]["action"] == "excluded_executed"
    assert record_path.read_bytes() == before


def test_approval_landing_during_reextraction_is_vetoed_inside_the_store_lock(monkeypatch):
    """TOCTOU closer: the run loop's exclusion pre-check runs BEFORE the
    seconds-long re-extraction. An approval landing in THAT window does not touch
    extracted_text (so the expected-text guard alone would let the write
    through) — the writer's in-lock ``reject_when`` re-evaluation must veto it,
    reported distinctly as excluded_executed_late."""
    garbled = _garbled_matter()

    real_bytes = garble_backfill.matter_store.get_source_document_bytes

    def _bytes_then_approve(matter):
        data = real_bytes(matter)
        # Approval lands AFTER the pre-write exclusion re-check (which wraps the
        # get_matter re-read), DURING the byte-read/re-extraction window, via the
        # REAL approve transition writer.
        matter_store.record_matter_approval(
            str(matter.get("id") or ""),
            approver="counsel@example.com",
            approved_at="2026-06-20T00:00:00+00:00",
            timeline_event={
                "type": "matter_approved",
                "actor": "counsel@example.com",
                "at": "2026-06-20T00:00:00+00:00",
            },
        )
        return data

    monkeypatch.setattr(
        garble_backfill.matter_store, "get_source_document_bytes", _bytes_then_approve
    )
    handler, report = _execute()
    assert handler.status == 202
    assert report["healed"] == 0
    assert report["excluded_executed_late"] == 1
    assert report["matters"][0]["action"] == "excluded_executed_late"
    after = matter_store.get_matter(garbled["id"], owner_user_id="")
    # The approval survived intact and the garbled text was NEVER overwritten.
    assert after["status"] == "approved"
    assert after["approver"] == "counsel@example.com"
    assert after["extracted_text"] == GARBLED_TEXT
    assert after["review_result"] == garbled["review_result"]


def test_human_reviewed_alone_is_not_excluded():
    """Predicate boundary (deliberate): the board's 'mark reviewed' is a human
    sign-off on the REVIEW, not an approval/execution — such a matter still heals
    (its garble should be fixed BEFORE any approval bakes it into an artifact)."""
    matter = _garbled_matter(human_reviewed=True)
    assert garble_backfill.matter_is_executed_or_approved(matter) is False
    _handler, report = _execute()
    assert report["healed"] == 1
    assert report["excluded_executed"] == 0


# --- (e) gates ----------------------------------------------------------------
def test_non_admin_is_403_on_all_three_endpoints(_isolated_store):
    _garbled_matter()
    before = _store_snapshot(_isolated_store)

    dry = _run({}, user=NON_ADMIN_USER)
    assert dry.status == 403

    start, report = _execute(user=NON_ADMIN_USER)
    assert start.status == 403
    assert report is None

    status = _status(user=NON_ADMIN_USER)
    assert status.status == 403

    assert _store_snapshot(_isolated_store) == before


@pytest.mark.parametrize("confirm", [None, False, "true", 1, "yes"])
def test_start_without_boolean_confirm_true_is_400(_isolated_store, confirm):
    _garbled_matter()
    before = _store_snapshot(_isolated_store)
    payload = {} if confirm is None else {"confirm": confirm}
    handler = _FakeHandler(payload=payload, path="/api/admin/matters/garble-backfill/start")
    admin_routes.handle_matters_garble_backfill_start(handler)
    assert handler.status == 400
    assert "confirm" in handler.response["error"]
    # Nothing started, nothing mutated.
    assert garble_backfill.garble_backfill_status() == {}
    assert _store_snapshot(_isolated_store) == before


def test_synchronous_execute_path_is_removed(_isolated_store):
    """The bare endpoint is dry-run ONLY: dry_run:false is a 400 pointing at the
    background /start route — even with confirm — and nothing mutates."""
    _garbled_matter()
    before = _store_snapshot(_isolated_store)
    handler = _run({"dry_run": False, "confirm": True})
    assert handler.status == 400
    assert "/start" in handler.response["error"]
    assert _store_snapshot(_isolated_store) == before


def test_dry_run_must_be_boolean():
    handler = _run({"dry_run": "false"})
    assert handler.status == 400


def test_invalid_limit_on_start_is_400():
    handler = _FakeHandler(
        payload={"confirm": True, "limit": 0},
        path="/api/admin/matters/garble-backfill/start",
    )
    admin_routes.handle_matters_garble_backfill_start(handler)
    assert handler.status == 400
    assert garble_backfill.garble_backfill_status() == {}


# --- (f) post-heal working-DOCX rebuild ----------------------------------------
# A retro-converted pre-fix matter persisted the OLD garbled pypdf paragraphs as
# working_docx_paragraphs. Healing extracted_text alone leaves them garbled
# FOREVER (the retro conversion is idempotent), so the execute path must
# force-rebuild the working DOCX through the SAME reconstruction machinery a
# fresh import uses (garble_backfill._rebuild_working_docx ->
# ingestion_service.rebuild_pdf_working_docx), fail-soft per matter: a rebuild
# failure never rolls back the healed text and never fails the heal.

def _make_body_docx(paragraph_texts) -> bytes:
    from docx import Document  # noqa: PLC0415 - python-docx, product core dep

    document = Document()
    for text in paragraph_texts:
        document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class _StubPdfConverter:
    """pdf2docx stand-in: 'reconstructs' the fixture PDF to a DOCX whose body is
    the given paragraphs. The real engine (RLIMIT subprocess etc.) is covered by
    tests/test_pdf_docx_reconstruction*.py; here everything AROUND the engine is
    real — mapping, re-keying, empty-body guard, persistence, artifact
    registration — only the subprocess converter is swapped."""

    name = "stub-pdf2docx"

    def __init__(self, paragraphs):
        self._paragraphs = list(paragraphs)

    def is_available(self):
        return True

    def convert_pdf_to_docx(self, source_path, output_path):
        output_path.write_bytes(_make_body_docx(self._paragraphs))


def _healed_paragraph_texts():
    """The texts the FIXED extractor yields for the fixture PDF — what the heal
    stores, and what a faithful reconstruction's body carries."""
    return [str(p["text"]) for p in pdf_text.extract_pdf_paragraphs(GARBLED_PDF_BYTES)]


def _pre_fix_working_paragraphs():
    """``working_docx_paragraphs`` as the retro conversion persisted them for a
    pre-fix matter: the GARBLED pypdf paragraphs, re-keyed by body index."""
    original = pdf_text._GLYPH_FRAGMENT_RUN_MIN
    pdf_text._GLYPH_FRAGMENT_RUN_MIN = 10**9
    try:
        paragraphs = pdf_text.extract_pdf_paragraphs(GARBLED_PDF_BYTES)
    finally:
        pdf_text._GLYPH_FRAGMENT_RUN_MIN = original
    return [
        {"id": f"wp{index}", "text": str(p["text"]), "source_index": index}
        for index, p in enumerate(paragraphs)
    ]


def _garbled_working_matter(**overrides):
    """A pre-fix matter in the exact shape this feature exists for: garbled
    stored text AND garbled persisted working paragraphs."""
    return _garbled_matter(
        working_docx_paragraphs=_pre_fix_working_paragraphs(), **overrides
    )


def _route_rebuild_conversion_through_stub(monkeypatch, spy):
    """Route the shared conversion seam through the REAL
    ``convert_pdf_matter_to_docx`` with the stub engine, counting invocations —
    ``spy['n']`` is how a test asserts the rebuild machinery ran (or did NOT)."""
    real_convert = pdf_ingest_conversion.convert_pdf_matter_to_docx

    def _counting_convert(pdf_bytes, source_filename, paragraphs, **_):
        spy["n"] += 1
        return real_convert(
            pdf_bytes,
            source_filename,
            paragraphs,
            converter=_StubPdfConverter(_healed_paragraph_texts()),
        )

    monkeypatch.setattr(
        ingestion_service.pdf_ingest_conversion,
        "convert_pdf_matter_to_docx",
        _counting_convert,
    )


def test_fixture_working_paragraphs_really_are_garbled():
    """Sanity-pin the working-paragraph fixture shape + the detector's contract:
    True on the pre-fix shape, False on healed texts, None when absent."""
    garbled = {"working_docx_paragraphs": _pre_fix_working_paragraphs()}
    assert garble_backfill.working_docx_paragraphs_garbled(garbled) is True
    healthy = {
        "working_docx_paragraphs": [
            {"id": f"wp{i}", "text": text, "source_index": i}
            for i, text in enumerate(_healed_paragraph_texts())
        ]
    }
    assert garble_backfill.working_docx_paragraphs_garbled(healthy) is False
    assert garble_backfill.working_docx_paragraphs_garbled({}) is None
    assert garble_backfill.working_docx_paragraphs_garbled(
        {"working_docx_paragraphs": []}
    ) is None


def test_execute_rebuilds_garbled_working_docx_after_heal(monkeypatch):
    matter = _garbled_working_matter()
    spy = {"n": 0}
    _route_rebuild_conversion_through_stub(monkeypatch, spy)

    handler, report = _execute()
    assert handler.status == 202
    assert report["healed"] == 1
    assert report["docx_rebuilt"] == 1
    assert report["docx_rebuild_failed"] == 0
    entry = report["matters"][0]
    assert entry["action"] == "healed"
    assert entry["docx_rebuild"] == "rebuilt"
    assert entry["working_docx_paragraphs_garbled"] is False
    assert spy["n"] == 1

    fresh = matter_store.get_matter(matter["id"], owner_user_id="")
    working = fresh["working_docx_paragraphs"]
    assert isinstance(working, list) and working
    blocks = garble_backfill.stored_paragraph_blocks(
        extracted_text_from_paragraphs(working)
    )
    # Coherent working paragraphs: no one/two-char shard runs, fingerprint clean.
    assert [b for b in blocks if len(b) <= 2] == []
    assert garble_backfill.garble_fingerprint(blocks)["garbled"] is False
    # Re-keyed exactly as a fresh import: DOCX body index anchors, the
    # source_part="pdf" marker dropped.
    assert all(isinstance(p.get("source_index"), int) for p in working)
    assert all(p.get("source_part") != "pdf" for p in working)
    # Same provenance a fresh import records: a role="working" artifact (actor=ai)
    # and the converted status stamp.
    artifact = artifact_registry.latest_artifact_for_role(
        fresh, artifact_registry.ROLE_WORKING
    )
    assert artifact is not None
    assert fresh.get("working_docx_status") == "converted"
    # And the heal itself is intact: coherent stored text, stale-review contract.
    text_blocks = garble_backfill.stored_paragraph_blocks(fresh["extracted_text"])
    assert garble_backfill.garble_fingerprint(text_blocks)["garbled"] is False
    assert matters_routes._matter_review_text_changed(fresh, fresh["review_result"]) is True


def test_rebuild_failure_keeps_healed_text_and_garbled_flag(monkeypatch):
    """FAIL-SOFT: a reconstruction failure never rolls back the healed text,
    never turns the heal into 'failed', and leaves the flag pointing at the
    matter for the next run/human."""
    matter = _garbled_working_matter()
    working_before = copy.deepcopy(matter["working_docx_paragraphs"])

    def _boom(*_args, **_kwargs):
        raise RuntimeError("pdf2docx exploded")

    monkeypatch.setattr(
        ingestion_service.pdf_ingest_conversion, "convert_pdf_matter_to_docx", _boom
    )

    handler, report = _execute()
    assert handler.status == 202
    assert report["healed"] == 1
    assert report["failed"] == 0
    assert report["docx_rebuilt"] == 0
    assert report["docx_rebuild_failed"] == 1
    entry = report["matters"][0]
    assert entry["action"] == "healed"
    assert entry["docx_rebuild"] == "failed"
    assert entry["working_docx_paragraphs_garbled"] is True

    fresh = matter_store.get_matter(matter["id"], owner_user_id="")
    # Healed TEXT kept...
    blocks = garble_backfill.stored_paragraph_blocks(fresh["extracted_text"])
    assert garble_backfill.garble_fingerprint(blocks)["garbled"] is False
    # ...working paragraphs untouched (still the old garbled ones, flag persists),
    # and no working artifact was registered by the failed rebuild.
    assert fresh["working_docx_paragraphs"] == working_before
    assert garble_backfill.working_docx_paragraphs_garbled(fresh) is True
    assert (
        artifact_registry.latest_artifact_for_role(fresh, artifact_registry.ROLE_WORKING)
        is None
    )


def test_dry_run_reports_garbled_working_flag_without_bytes_or_rebuild(monkeypatch):
    """The dry-run stays detection-only even for a garbled-working matter: the
    flag is surfaced, but zero byte reads and zero reconstruction attempts."""
    _garbled_working_matter()

    def _boom(matter):
        raise AssertionError("dry-run must not read document bytes")

    monkeypatch.setattr(garble_backfill.matter_store, "get_source_document_bytes", _boom)
    spy = {"n": 0}
    _route_rebuild_conversion_through_stub(monkeypatch, spy)

    handler = _run({})
    assert handler.status == 200
    assert handler.response["dry_run"] is True
    entry = handler.response["matters"][0]
    assert entry["action"] == "would_reextract"
    assert entry["working_docx_paragraphs_garbled"] is True
    assert "docx_rebuild" not in entry
    assert spy["n"] == 0


@pytest.mark.parametrize("overrides", [_EXECUTED_VARIANTS[0], _APPROVED_VARIANTS[0]])
def test_executed_or_approved_matter_is_fully_excluded_from_the_rebuild(
    _isolated_store, monkeypatch, overrides
):
    """The executed/approved exclusion covers the rebuild too — it is a mutation:
    the protected record stays byte-identical and the reconstruction machinery is
    never even invoked."""
    protected = _garbled_working_matter(**overrides)
    record_path = matter_store._matter_records_dir() / f"{protected['id']}.json"
    before = record_path.read_bytes()
    spy = {"n": 0}
    _route_rebuild_conversion_through_stub(monkeypatch, spy)

    handler, report = _execute()
    assert handler.status == 202
    assert report["excluded_executed"] == 1
    assert report["docx_rebuilt"] == 0
    assert report["docx_rebuild_failed"] == 0
    assert report["matters"][0]["action"] == "excluded_executed"
    assert spy["n"] == 0
    assert record_path.read_bytes() == before


def test_healthy_working_docx_is_never_rebuilt(monkeypatch):
    """A matter whose working paragraphs are already coherent heals its text
    only — the rebuild is not invoked and the working representation is
    byte-identically preserved."""
    healthy_working = [
        {"id": f"wp{i}", "text": text, "source_index": i}
        for i, text in enumerate(_healed_paragraph_texts())
    ]
    matter = _garbled_matter(working_docx_paragraphs=copy.deepcopy(healthy_working))
    spy = {"n": 0}
    _route_rebuild_conversion_through_stub(monkeypatch, spy)

    handler, report = _execute()
    assert handler.status == 202
    assert report["healed"] == 1
    assert report["docx_rebuilt"] == 0
    assert report["docx_rebuild_failed"] == 0
    entry = report["matters"][0]
    assert entry["action"] == "healed"
    assert "docx_rebuild" not in entry
    assert entry["working_docx_paragraphs_garbled"] is False
    assert spy["n"] == 0

    fresh = matter_store.get_matter(matter["id"], owner_user_id="")
    assert fresh["working_docx_paragraphs"] == healthy_working


def test_in_lock_heal_veto_also_prevents_the_rebuild(monkeypatch):
    """Mid-run approval (excluded_executed_late): when the writer's in-lock
    reject_when vetoes the HEAL, the rebuild never runs either — no
    reconstruction attempt, working paragraphs untouched."""
    matter = _garbled_working_matter()
    working_before = copy.deepcopy(matter["working_docx_paragraphs"])
    spy = {"n": 0}
    _route_rebuild_conversion_through_stub(monkeypatch, spy)

    real_bytes = garble_backfill.matter_store.get_source_document_bytes

    def _bytes_then_approve(m):
        data = real_bytes(m)
        matter_store.record_matter_approval(
            str(m.get("id") or ""),
            approver="counsel@example.com",
            approved_at="2026-06-20T00:00:00+00:00",
            timeline_event={
                "type": "matter_approved",
                "actor": "counsel@example.com",
                "at": "2026-06-20T00:00:00+00:00",
            },
        )
        return data

    monkeypatch.setattr(
        garble_backfill.matter_store, "get_source_document_bytes", _bytes_then_approve
    )

    handler, report = _execute()
    assert handler.status == 202
    assert report["healed"] == 0
    assert report["excluded_executed_late"] == 1
    assert report["docx_rebuilt"] == 0
    assert report["docx_rebuild_failed"] == 0
    entry = report["matters"][0]
    assert entry["action"] == "excluded_executed_late"
    assert "docx_rebuild" not in entry
    assert spy["n"] == 0

    fresh = matter_store.get_matter(matter["id"], owner_user_id="")
    assert fresh["status"] == "approved"
    assert fresh["extracted_text"] == GARBLED_TEXT
    assert fresh["working_docx_paragraphs"] == working_before


def test_approval_landing_during_reconstruction_vetoes_the_rebuild_write(monkeypatch):
    """TOCTOU closer for the rebuild itself: the heal committed BEFORE the
    approval (correct order), but an approval landing DURING the seconds-long
    reconstruction window must veto the rebuild's writes — no paragraphs, no
    artifact, not even a status stamp — reported as docx_rebuild failed."""
    matter = _garbled_working_matter()
    working_before = copy.deepcopy(matter["working_docx_paragraphs"])
    real_convert = pdf_ingest_conversion.convert_pdf_matter_to_docx
    spy = {"n": 0}

    def _convert_then_approve(pdf_bytes, source_filename, paragraphs, **_):
        spy["n"] += 1
        result = real_convert(
            pdf_bytes,
            source_filename,
            paragraphs,
            converter=_StubPdfConverter(_healed_paragraph_texts()),
        )
        # Approval lands via the REAL approve-transition writer while the
        # reconstruction is (conceptually) still in flight.
        matter_store.record_matter_approval(
            matter["id"],
            approver="counsel@example.com",
            approved_at="2026-06-20T00:00:00+00:00",
            timeline_event={
                "type": "matter_approved",
                "actor": "counsel@example.com",
                "at": "2026-06-20T00:00:00+00:00",
            },
        )
        return result

    monkeypatch.setattr(
        ingestion_service.pdf_ingest_conversion,
        "convert_pdf_matter_to_docx",
        _convert_then_approve,
    )

    handler, report = _execute()
    assert handler.status == 202
    assert report["healed"] == 1
    assert report["docx_rebuilt"] == 0
    assert report["docx_rebuild_failed"] == 1
    entry = report["matters"][0]
    assert entry["action"] == "healed"
    assert entry["docx_rebuild"] == "failed"
    assert entry["working_docx_paragraphs_garbled"] is True
    assert spy["n"] == 1

    fresh = matter_store.get_matter(matter["id"], owner_user_id="")
    # The approval survived; the vetoed rebuild wrote NOTHING.
    assert fresh["status"] == "approved"
    assert fresh["approver"] == "counsel@example.com"
    assert fresh["working_docx_paragraphs"] == working_before
    assert (
        artifact_registry.latest_artifact_for_role(fresh, artifact_registry.ROLE_WORKING)
        is None
    )
    assert "working_docx_status" not in fresh
    # The heal itself stands (it committed before the approval landed).
    blocks = garble_backfill.stored_paragraph_blocks(fresh["extracted_text"])
    assert garble_backfill.garble_fingerprint(blocks)["garbled"] is False


# --- SHARD-REJOIN dry-run measurement hook (writes NOTHING) -------------------
def _shard_garbled_matter(**overrides):
    """A PDF-source matter whose STORED text is shard-garbled and whose retained
    bytes re-extract as shards under the flag OFF, but heal under the reflow."""
    return _store_matter(
        filename="Shard NDA.pdf",
        extracted_text=SHARD_TEXT,
        document_bytes=SHARD_PDF_BYTES,
        review_result=_ai_review_snapshot(SHARD_TEXT),
        **overrides,
    )


def test_shard_matter_is_detected_as_a_candidate():
    matter = _shard_garbled_matter()
    fingerprint = garble_backfill.garble_fingerprint(
        garble_backfill.stored_paragraph_blocks(matter["extracted_text"])
    )
    assert fingerprint["exploded_count"] == 0
    assert fingerprint["longest_shard_run"] >= 8
    assert garble_backfill.matter_garble_assessment(matter)["candidate"] is True


def test_measure_only_with_shard_rejoin_reports_would_heal_and_writes_nothing(_isolated_store):
    _shard_garbled_matter()
    before = _store_snapshot(_isolated_store)
    report = garble_backfill.run_garble_backfill(
        dry_run=True, measure_only=True, shard_rejoin=True
    )
    # The measurement re-extracts with the reflow ON and reports a would-heal.
    assert report["measure_only"] is True
    assert report["shard_rejoin"] is True
    assert report["would_heal"] == 1
    assert report["measure_still_garbled"] == 0
    entry = next(m for m in report["matters"] if m.get("action") == "would_heal")
    assert entry["fingerprint_after"]["garbled"] is False
    # NOTHING was written: the store is byte-identical.
    assert _store_snapshot(_isolated_store) == before


def test_measure_only_without_shard_rejoin_does_not_heal(_isolated_store):
    _shard_garbled_matter()
    report = garble_backfill.run_garble_backfill(
        dry_run=True, measure_only=True, shard_rejoin=False
    )
    # Re-extraction WITHOUT the reflow reproduces the stored shards (identical to
    # the stored text here): no would-heal. It lands as unchanged/still-garbled,
    # never healed.
    assert report["would_heal"] == 0
    assert report["measure_unchanged"] + report["measure_still_garbled"] == 1


def test_execute_default_does_not_heal_shards_and_leaves_the_record(_isolated_store):
    # The DEFAULT execute path (shard_rejoin False) is unchanged: re-extraction
    # reproduces the stored shards, so it heals NOTHING and writes nothing.
    matter = _shard_garbled_matter()
    before = _store_snapshot(_isolated_store)
    report = garble_backfill.run_garble_backfill(dry_run=False)
    assert report["healed"] == 0
    assert report["unchanged"] + report["still_garbled"] == 1
    assert _store_snapshot(_isolated_store) == before
    fresh = matter_store.get_matter(matter["id"], owner_user_id="")
    assert fresh["extracted_text"] == SHARD_TEXT


def test_endpoint_shard_rejoin_true_kicks_measure_run_and_writes_nothing(_isolated_store):
    # POST .../garble-backfill with shard_rejoin:true starts a background measure
    # run (thread patched inline) and mutates nothing; the report carries would_heal.
    _shard_garbled_matter()
    before = _store_snapshot(_isolated_store)
    handler = _run({"shard_rejoin": True})
    assert handler.status == 202
    assert handler.response["started"] is True
    assert handler.response["measure_only"] is True
    assert handler.response["shard_rejoin"] is True
    status = garble_backfill.garble_backfill_status()
    assert status["state"] == "done"
    assert status["report"]["would_heal"] == 1
    assert status["report"]["measure_only"] is True
    assert _store_snapshot(_isolated_store) == before


def test_endpoint_shard_rejoin_must_be_boolean():
    handler = _run({"shard_rejoin": "yes"})
    assert handler.status == 400
    assert "shard_rejoin" in handler.response["error"]


def test_persisting_run_with_shard_rejoin_is_structurally_refused(_isolated_store):
    # STRUCTURAL guard (not a route convention): the reflow is measurement-only, so
    # any PERSISTING run (measure_only False) with shard_rejoin True must raise
    # BEFORE it can write, regardless of dry_run — UNLESS the explicit persist
    # opt-in is set (tested separately below).
    _shard_garbled_matter()
    before = _store_snapshot(_isolated_store)
    with pytest.raises(ValueError):
        garble_backfill.run_garble_backfill(dry_run=False, measure_only=False, shard_rejoin=True)
    with pytest.raises(ValueError):
        garble_backfill.run_garble_backfill(dry_run=True, measure_only=False, shard_rejoin=True)
    # Nothing was written by the refused calls.
    assert _store_snapshot(_isolated_store) == before


# --- SHARD heal-PERSIST path (default-OFF, doubly gated) ----------------------
def test_reflow_healed_text_is_readable_gate():
    """Gate (b) unit: readable iff NOT garbled AND no fused megaword; empty is not."""
    good = "The parties shall keep all Confidential Information secret at all times."
    assert garble_backfill._reflow_healed_text_is_readable(good) is True
    # A fused spaceless 'megaword' longer than the plausible word cap -> not readable.
    bad = "The parties " + ("x" * (pdf_text._SHARD_MAX_WORD_CHARS + 5))
    assert garble_backfill._reflow_healed_text_is_readable(bad) is False
    # Still shard-garbled text -> not readable.
    still_garbled = "\n\n".join(["C", "E", "O"] * 4)
    assert garble_backfill._reflow_healed_text_is_readable(still_garbled) is False
    # Empty -> not readable.
    assert garble_backfill._reflow_healed_text_is_readable("") is False


def test_persist_opt_in_refuses_inconsistent_combinations(_isolated_store):
    """The persist opt-in must be paired with shard_rejoin, exclude measure_only,
    and require dry_run False — every inconsistent combination raises before scan."""
    _shard_garbled_matter()
    before = _store_snapshot(_isolated_store)
    with pytest.raises(ValueError):  # opt-in without the reflow
        garble_backfill.run_garble_backfill(dry_run=False, persist_shard_rejoin=True)
    with pytest.raises(ValueError):  # opt-in cannot combine with measurement
        garble_backfill.run_garble_backfill(
            dry_run=False, measure_only=True, shard_rejoin=True, persist_shard_rejoin=True
        )
    with pytest.raises(ValueError):  # persisting is a mutation: dry_run must be False
        garble_backfill.run_garble_backfill(
            dry_run=True, shard_rejoin=True, persist_shard_rejoin=True
        )
    assert _store_snapshot(_isolated_store) == before


def test_persist_shard_rejoin_heals_and_writes_when_readable(_isolated_store):
    """Opted-in AND readability-gated: the shard matter is healed and PERSISTED,
    the review is flagged stale by the existing contract, others untouched."""
    matter = _shard_garbled_matter()
    normal = _normal_matter()
    normal_before = (matter_store._matter_records_dir() / f"{normal['id']}.json").read_bytes()

    report = garble_backfill.run_garble_backfill(
        dry_run=False, shard_rejoin=True, persist_shard_rejoin=True
    )
    assert report["persist_shard_rejoin"] is True
    assert report["healed"] == 1
    assert report["reflow_unreadable"] == 0
    assert report["matters"][0]["action"] == "healed"

    fresh = matter_store.get_matter(matter["id"], owner_user_id="")
    blocks = garble_backfill.stored_paragraph_blocks(fresh["extracted_text"])
    assert garble_backfill.garble_fingerprint(blocks)["garbled"] is False
    assert fresh["extracted_text"] != SHARD_TEXT
    # No fused megaword slipped through the persist gate.
    assert not pdf_text.text_has_implausible_megaword(fresh["extracted_text"])
    # The untouched review is flagged stale by the EXISTING contract.
    assert matters_routes._matter_review_text_changed(fresh, fresh["review_result"]) is True
    # Other matters byte-identical.
    assert (matter_store._matter_records_dir() / f"{normal['id']}.json").read_bytes() == normal_before


def test_persist_refuses_write_when_readability_backstop_fails(_isolated_store, monkeypatch):
    """Gate (b) end-to-end: the reflow HEALS (adopted in-extractor), but force the
    persist-time readability backstop to reject the healed text; the run must write
    NOTHING (action reflow_unreadable). Patching the garble_backfill-layer gate (not
    the megaword primitive) isolates the persist gate from the adoption gate — which
    also uses the megaword check, defence in depth."""
    matter = _shard_garbled_matter()
    before = _store_snapshot(_isolated_store)
    monkeypatch.setattr(garble_backfill, "_reflow_healed_text_is_readable", lambda new_text: False)

    report = garble_backfill.run_garble_backfill(
        dry_run=False, shard_rejoin=True, persist_shard_rejoin=True
    )
    assert report["healed"] == 0
    assert report["reflow_unreadable"] == 1
    assert report["matters"][0]["action"] == "reflow_unreadable"
    # Nothing written: store byte-identical, stored text still the garbled shards.
    assert _store_snapshot(_isolated_store) == before
    assert matter_store.get_matter(matter["id"], owner_user_id="")["extracted_text"] == SHARD_TEXT


def test_persist_shard_rejoin_still_excludes_executed_or_approved(_isolated_store):
    """The executed/approved exclusion holds on the persist path too: a protected
    shard matter is listed but never written, while an unprotected sibling heals."""
    protected = _shard_garbled_matter(
        status="approved", approver="counsel@example.com", approved_at="2026-06-20T00:00:00+00:00"
    )
    healable = _shard_garbled_matter()
    protected_before = (matter_store._matter_records_dir() / f"{protected['id']}.json").read_bytes()

    report = garble_backfill.run_garble_backfill(
        dry_run=False, shard_rejoin=True, persist_shard_rejoin=True
    )
    assert report["excluded_executed"] == 1
    assert report["healed"] == 1
    by_id = {entry["id"]: entry for entry in report["matters"]}
    assert by_id[protected["id"]]["action"] == "excluded_executed"
    assert by_id[healable["id"]]["action"] == "healed"
    assert (matter_store._matter_records_dir() / f"{protected['id']}.json").read_bytes() == protected_before
