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
* non-admin -> 403; execute without "confirm": true -> 400 (string "true"
  included), nothing mutated.

The route body is driven through a fake handler (the same pattern
test_bulk_archive / test_admin_manager use); the store is the REAL matter store
rooted at a per-test tmp dir.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("pypdf")

from nda_automation import garble_backfill, matter_store, pdf_text
from nda_automation.review_result_contract import extracted_text_from_paragraphs
from nda_automation.routes import admin as admin_routes
from nda_automation.routes import matters as matters_routes

from test_pdf_text import make_pdf, make_pdf_glyph_fragmented_signature_page

OWNER = "google:111"
ADMIN_USER = {"id": "google:999", "provider": "google", "email": "admin@example.com", "name": "Admin"}
NON_ADMIN_USER = {"id": "google:123", "provider": "google", "email": "user@example.com", "name": "User"}


# --- fixture PDFs / texts -----------------------------------------------------
GARBLED_PDF_BYTES = make_pdf_glyph_fragmented_signature_page()
NORMAL_PDF_BYTES = make_pdf("The parties agree to keep all Confidential Information secret at all times.")


def _pre_fix_extracted_text(pdf_bytes: bytes) -> str:
    """The text the PRE-FIX extractor stored for these bytes: run the current
    extractor with the per-glyph demotion disabled (the exact code path old
    imports took), reproducing the historical garbled stored shape."""
    original = pdf_text._GLYPH_FRAGMENT_RUN_MIN
    pdf_text._GLYPH_FRAGMENT_RUN_MIN = 10**9
    try:
        paragraphs = pdf_text.extract_pdf_paragraphs(pdf_bytes)
    finally:
        pdf_text._GLYPH_FRAGMENT_RUN_MIN = original
    return extracted_text_from_paragraphs(paragraphs)


GARBLED_TEXT = _pre_fix_extracted_text(GARBLED_PDF_BYTES)


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

    handler = _run({"dry_run": False, "confirm": True})
    assert handler.status == 200
    body = handler.response
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
    first = _run({"dry_run": False, "confirm": True})
    assert first.response["healed"] == 1
    second = _run({"dry_run": False, "confirm": True})
    assert second.status == 200
    assert second.response["garbled_matched"] == 0
    assert second.response["healed"] == 0


def test_execute_does_not_write_when_reextraction_is_still_garbled(_isolated_store, monkeypatch):
    """If the fixed extractor somehow still yields garble, swapping garble for
    garble is refused (report-only)."""
    garbled = _garbled_matter()
    before = (matter_store._matter_records_dir() / f"{garbled['id']}.json").read_bytes()
    # Force the re-extraction to reproduce the garbled shape (as pre-fix code would).
    monkeypatch.setattr(pdf_text, "_GLYPH_FRAGMENT_RUN_MIN", 10**9)
    handler = _run({"dry_run": False, "confirm": True})
    assert handler.status == 200
    assert handler.response["healed"] == 0
    # 'unchanged' when the reproduction is byte-identical, else 'still_garbled':
    # either way NOTHING was written.
    assert handler.response["matters"][0]["action"] in ("unchanged", "still_garbled")
    assert (matter_store._matter_records_dir() / f"{garbled['id']}.json").read_bytes() == before


# --- (d) missing bytes --------------------------------------------------------
def test_missing_source_bytes_skips_and_reports(_isolated_store):
    garbled = _garbled_matter()
    (matter_store.UPLOADS_DIR / garbled["stored_filename"]).unlink()
    before = _store_snapshot(_isolated_store)

    handler = _run({"dry_run": False, "confirm": True})
    assert handler.status == 200
    body = handler.response
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
    handler = _run({"dry_run": False, "confirm": True})
    assert handler.status == 200
    body = handler.response
    assert body["healed"] == 1
    assert body["failed"] == 1
    assert body["errors"] and body["errors"][0]["id"] == broken["id"]
    by_id = {entry["id"]: entry for entry in body["matters"]}
    assert by_id[broken["id"]]["action"] == "failed"
    assert by_id[healthy["id"]]["action"] == "healed"


# --- (e) gates ----------------------------------------------------------------
def test_non_admin_is_403(_isolated_store):
    _garbled_matter()
    before = _store_snapshot(_isolated_store)
    handler = _run({"dry_run": False, "confirm": True}, user=NON_ADMIN_USER)
    assert handler.status == 403
    assert _store_snapshot(_isolated_store) == before


@pytest.mark.parametrize("confirm", [None, False, "true", 1, "yes"])
def test_execute_without_boolean_confirm_true_is_400(_isolated_store, confirm):
    _garbled_matter()
    before = _store_snapshot(_isolated_store)
    payload = {"dry_run": False}
    if confirm is not None:
        payload["confirm"] = confirm
    handler = _run(payload)
    assert handler.status == 400
    assert "confirm" in handler.response["error"]
    assert _store_snapshot(_isolated_store) == before


def test_dry_run_must_be_boolean():
    handler = _run({"dry_run": "false"})
    assert handler.status == 400
