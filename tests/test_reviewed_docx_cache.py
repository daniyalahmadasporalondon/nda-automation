"""Defect B: the composed reviewed-DOCX cache + ETag/304 + cheap HEAD.

The composition (build_reviewed_docx -> redline compose -> open-health -> coverage
gate, then accept + image-normalize) ran on EVERY GET/HEAD, per view mode, and for
the FE's fallback fetch. These tests prove:

* a second GET for the same fingerprint hits the cache (the composer is not called),
* an ETag round-trip (If-None-Match) yields 304,
* HEAD never invokes the composer (cold OR warm cache),
* changing the matter text / reviewer draft / view mode changes the fingerprint
  (cache miss -> recompose),
* the bounded disk cache evicts LRU within the shared render-cache budget.
"""
from __future__ import annotations

import copy
from io import BytesIO

import pytest
from docx import Document

from nda_automation import (
    approval,
    artifact_registry,
    artifact_service,
    matter_document_artifacts,
    matter_store,
    reviewed_docx_cache,
)
from nda_automation.routes import approval as approval_routes

# Reuse the approved-matter-with-redline seeding + paragraph fixtures.
from tests.test_reviewed_docx_changes_mode import (
    NDA_PARAGRAPHS,
    _seed_approved_matter_with_redline,
)


class _CachingHandler:
    """Route double that also carries request headers (for If-None-Match)."""

    def __init__(self, *, current_user_id="owner-1", path="", if_none_match=None):
        self.current_user_id = current_user_id
        self.current_user = {"id": current_user_id} if current_user_id else None
        self.path = path
        self.status = None
        self.json = None
        self.json_headers = None
        self.download = None
        self.download_headers = None
        self.headers = {}
        if if_none_match is not None:
            self.headers["If-None-Match"] = if_none_match

    def _send_json(self, payload, status=200, headers=None, *, send_body=True):
        self.status = status
        self.json = payload
        self.json_headers = headers or {}

    def _send_download(self, data, filename, content_type, headers=None, *, send_body=True):
        self.status = 200
        self.download = {
            "data": data,
            "filename": filename,
            "content_type": content_type,
            "send_body": send_body,
        }
        self.download_headers = headers or {}


def _spy_composer(monkeypatch):
    """Wrap build_reviewed_docx with a call counter (delegating to the real one)."""
    calls = {"n": 0}
    real = matter_document_artifacts.build_reviewed_docx

    def _wrapper(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(matter_document_artifacts, "build_reviewed_docx", _wrapper)
    return calls


def _get(matter_id, *, changes="tracked", if_none_match=None, send_body=True):
    path = f"/api/matters/{matter_id}/reviewed-docx?changes={changes}"
    handler = _CachingHandler(path=path, if_none_match=if_none_match)
    approval_routes.handle_matter_reviewed_docx(
        handler, f"/api/matters/{matter_id}/reviewed-docx", send_body=send_body
    )
    return handler


# --------------------------------------------------------------------------- #
# Route: second GET hits the cache
# --------------------------------------------------------------------------- #
def test_second_get_hits_cache_and_skips_composer(monkeypatch):
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    first = _get(matter_id)
    assert first.status == 200
    assert calls["n"] == 1
    first_bytes = first.download["data"]
    etag = first.download_headers["ETag"]
    assert etag

    second = _get(matter_id)
    assert second.status == 200
    # Composer NOT invoked again: served from the cache.
    assert calls["n"] == 1
    assert second.download["data"] == first_bytes
    assert second.download_headers["ETag"] == etag


def test_etag_round_trip_yields_304(monkeypatch):
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    first = _get(matter_id)
    etag = first.download_headers["ETag"]
    assert calls["n"] == 1

    conditional = _get(matter_id, if_none_match=etag)
    assert conditional.status == 304
    assert conditional.download is None
    assert conditional.json_headers.get("ETag") == etag
    # A 304 composes nothing.
    assert calls["n"] == 1


def test_head_does_not_invoke_composer_cold_cache(monkeypatch):
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    head = _get(matter_id, send_body=False)
    assert head.status == 200
    # Cold HEAD must NOT run the full build.
    assert calls["n"] == 0
    assert head.download["send_body"] is False
    assert head.download["data"] == b""
    # Defect P2: a cold HEAD must NOT advertise a validator for bytes it never
    # composed. No ETag until the body-bearing GET produces + stores the bytes.
    assert head.download_headers.get("ETag") is None


def test_head_does_not_invoke_composer_warm_cache(monkeypatch):
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    _get(matter_id)  # populate
    assert calls["n"] == 1

    head = _get(matter_id, send_body=False)
    assert head.status == 200
    assert head.download["send_body"] is False
    # Warm HEAD serves from the cache; still no additional build.
    assert calls["n"] == 1


def test_changing_view_mode_misses_cache(monkeypatch):
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    _get(matter_id, changes="tracked")
    assert calls["n"] == 1
    _get(matter_id, changes="accepted")
    # Different mode -> different fingerprint -> recompose.
    assert calls["n"] == 2
    # Each mode is independently cached now.
    _get(matter_id, changes="tracked")
    _get(matter_id, changes="accepted")
    assert calls["n"] == 2


def test_changing_reviewer_draft_misses_cache(monkeypatch):
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    _get(matter_id)
    assert calls["n"] == 1

    # Mutate the reviewer draft: add a comment on some clause. This flows into
    # reviewed_docx_payload -> the fingerprint.
    review_result = matter_store.get_matter(matter_id, owner_user_id="owner-1")["review_result"]
    clause_id = str(review_result["clauses"][0]["id"])
    matter_store.set_clause_reviewer_decision(
        matter_id,
        clause_id,
        approval.normalize_reviewer_decision(
            {"action": "comment", "comment": "please revisit"}, actor="reviewer"
        ),
    )

    _get(matter_id)
    # Draft changed -> fingerprint changed -> recompose.
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# Defect P1: cache poisoning by OMISSION. The composer reads the raw source bytes
# fresh (or SUBSTITUTES the role="working" DOCX for a PDF matter). Neither is
# captured by extracted_text/source_filename, so a rebuilt working DOCX or edited
# source used to keep the SAME fingerprint -> composer never re-run -> stale bytes
# served. The fingerprint now folds in the composition SOURCE IDENTITY.
# --------------------------------------------------------------------------- #
def _working_docx_with_title(paragraphs, title):
    """A byte-DIFFERENT working DOCX with IDENTICAL body text: only a core-property
    changes, so the redline still anchors/covers exactly (recompose succeeds) while
    the artifact content hash changes (the composed output bytes differ). This is the
    gate's scenario -- a working DOCX rebuilt in place, extracted_text untouched."""
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    document.core_properties.title = title
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_reregistering_a_different_working_docx_misses_and_recomposes(monkeypatch):
    # The gate's EXACT repro: PDF matter with a working DOCX. GET composes + stores.
    # Then re-register a DIFFERENT working DOCX (register_working_docx replaces the
    # working artifact) WITHOUT touching extracted_text / review_result / source_filename.
    matter_id = _seed_approved_matter_with_redline(source_docx=False, working_docx=True)
    calls = _spy_composer(monkeypatch)

    first = _get(matter_id)
    assert first.status == 200, first.json
    assert calls["n"] == 1
    first_bytes = first.download["data"]

    new_working = _working_docx_with_title(NDA_PARAGRAPHS, "healed-v2")
    registered = artifact_service.register_working_docx(matter_id, new_working, owner_user_id="owner-1")
    assert registered is not None  # different bytes -> a real new working artifact

    second = _get(matter_id)
    assert second.status == 200, second.json
    # The working bytes changed -> fingerprint changed -> MISS -> recompose.
    assert calls["n"] == 2
    # And the freshly composed bytes reflect the new working DOCX (not the stale entry).
    assert second.download["data"] != first_bytes


def test_reregistering_identical_working_docx_still_hits(monkeypatch):
    # Re-registering the SAME working bytes must NOT spuriously invalidate: the
    # content-hash key is byte-derived, and register_working_docx is idempotent on it.
    matter_id = _seed_approved_matter_with_redline(source_docx=False, working_docx=True)
    calls = _spy_composer(monkeypatch)

    first = _get(matter_id)
    assert first.status == 200, first.json
    assert calls["n"] == 1

    # Re-register the EXACT current working bytes.
    stored = matter_store.get_matter(matter_id, owner_user_id="owner-1")
    working = artifact_registry.latest_artifact_for_role(stored, artifact_registry.ROLE_WORKING)
    same_bytes = artifact_service.get_artifact_bytes(matter_id, working.id, owner_user_id="owner-1")
    assert artifact_service.register_working_docx(matter_id, same_bytes, owner_user_id="owner-1") is None

    second = _get(matter_id)
    assert second.status == 200, second.json
    # Same bytes -> same content hash -> same fingerprint -> HIT (no recompose).
    assert calls["n"] == 1
    assert second.download["data"] == first.download["data"]


def test_changing_source_document_bytes_misses(monkeypatch):
    # Native DOCX matter (no working artifact): the composer reads the raw source
    # bytes. Overwriting them (body text unchanged, so the redline still composes)
    # must MISS -- the source-identity hash moves.
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    first = _get(matter_id)
    assert first.status == 200, first.json
    assert calls["n"] == 1

    matter = matter_store.get_matter(matter_id, owner_user_id="owner-1")
    source_path = matter_store.source_document_path(matter)
    assert source_path is not None
    source_path.write_bytes(_working_docx_with_title(NDA_PARAGRAPHS, "source-edited"))

    second = _get(matter_id)
    assert second.status == 200, second.json
    # Source bytes changed -> fingerprint changed -> MISS -> recompose.
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# Defect P2: 304 / cold-HEAD must never advertise a validator for bytes never
# composed. The ETag derives from fingerprint INPUTS, so a match alone does not
# prove the bytes exist -- the route must gate 304 (and any advertised validator)
# on load() actually returning a representation.
# --------------------------------------------------------------------------- #
def test_cold_head_issues_no_etag_and_conditional_get_composes_not_304(monkeypatch):
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    head = _get(matter_id, send_body=False)
    assert head.status == 200
    assert calls["n"] == 0
    assert head.download_headers.get("ETag") is None  # cold HEAD advertises nothing

    # Even if a client fabricated the input-derived validator, nothing is cached, so
    # the conditional GET must NOT 304 -- it composes and returns 200 with real bytes.
    matter = matter_store.get_matter(matter_id, owner_user_id="owner-1")
    fp = reviewed_docx_cache.reviewed_docx_fingerprint(
        matter_id, matter, changes_mode="tracked", owner_user_id="owner-1"
    )
    forged_etag = reviewed_docx_cache.etag_for(fp)
    conditional = _get(matter_id, if_none_match=forged_etag)
    assert conditional.status == 200, conditional.json
    assert conditional.download is not None and conditional.download["data"][:2] == b"PK"
    assert calls["n"] == 1  # it composed rather than short-circuiting to 304


def test_warm_head_advertises_etag_and_conditional_get_304s(monkeypatch):
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    _get(matter_id)  # compose + store
    assert calls["n"] == 1

    head = _get(matter_id, send_body=False)
    assert head.status == 200
    assert calls["n"] == 1  # warm HEAD serves from cache, no build
    etag = head.download_headers.get("ETag")
    assert etag  # warm HEAD DOES advertise the validator

    conditional = _get(matter_id, if_none_match=etag)
    assert conditional.status == 304
    assert conditional.download is None
    assert calls["n"] == 1


def test_no_304_when_cache_lacks_the_representation(monkeypatch):
    # A fingerprint match with the bytes GONE (evicted / file deleted) must recompose,
    # never 304 with an empty body.
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    calls = _spy_composer(monkeypatch)

    first = _get(matter_id)
    assert first.status == 200, first.json
    etag = first.download_headers["ETag"]
    assert calls["n"] == 1

    # Delete the .bin behind the entry so load() misses despite the fingerprint match.
    matter = matter_store.get_matter(matter_id, owner_user_id="owner-1")
    fp = reviewed_docx_cache.reviewed_docx_fingerprint(
        matter_id, matter, changes_mode="tracked", owner_user_id="owner-1"
    )
    (reviewed_docx_cache.cache_root() / f"{fp}.bin").unlink()
    assert reviewed_docx_cache.load(fp) is None

    conditional = _get(matter_id, if_none_match=etag)
    # No representation held -> NOT 304 -> recompose + 200 with bytes.
    assert conditional.status == 200, conditional.json
    assert conditional.download is not None and conditional.download["data"][:2] == b"PK"
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# Fingerprint invalidation (pure, build-independent)
# --------------------------------------------------------------------------- #
def _fp(matter, *, changes_mode="tracked"):
    return reviewed_docx_cache.reviewed_docx_fingerprint(
        str(matter.get("id")), matter, changes_mode=changes_mode, owner_user_id="owner-1"
    )


def test_fingerprint_stable_for_identical_inputs():
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    matter = matter_store.get_matter(matter_id, owner_user_id="owner-1")
    assert _fp(matter) == _fp(copy.deepcopy(matter))


def test_fingerprint_changes_on_text_draft_review_and_mode():
    matter_id = _seed_approved_matter_with_redline(source_docx=True)
    base = matter_store.get_matter(matter_id, owner_user_id="owner-1")
    base_fp = _fp(base)

    # 1) view mode
    assert _fp(base, changes_mode="accepted") != base_fp

    # 2) matter source text
    text_changed = copy.deepcopy(base)
    text_changed["extracted_text"] = (base.get("extracted_text") or "") + "\n\nExtra clause added."
    assert _fp(text_changed) != base_fp

    # 3) review result
    review_changed = copy.deepcopy(base)
    review_changed["review_result"]["redline_edits"][0]["replacement_text"] = "totally different text"
    assert _fp(review_changed) != base_fp

    # 4) reviewer draft (decisions)
    draft_changed = copy.deepcopy(base)
    draft_changed.setdefault("reviewer_decisions", {})
    # A new decision on some clause changes reviewed_docx_payload.
    clause_id = str(base["review_result"]["clauses"][0]["id"])
    draft_changed["reviewer_decisions"][clause_id] = approval.normalize_reviewer_decision(
        {"action": "comment", "comment": "draft note"}, actor="reviewer"
    )
    assert _fp(draft_changed) != base_fp


def test_fingerprint_does_not_collide_across_matters():
    matter_id_a = _seed_approved_matter_with_redline(source_docx=True)
    matter_id_b = _seed_approved_matter_with_redline(source_docx=True)
    a = matter_store.get_matter(matter_id_a, owner_user_id="owner-1")
    b = matter_store.get_matter(matter_id_b, owner_user_id="owner-1")
    assert _fp(a) != _fp(b)


# --------------------------------------------------------------------------- #
# Cache module: round-trip + bounded eviction
# --------------------------------------------------------------------------- #
def test_cache_store_load_round_trip():
    fp = "a" * 64
    reviewed_docx_cache.store(
        fp,
        b"hello-docx-bytes",
        filename="reviewed.docx",
        content_type=reviewed_docx_cache.document_rendering.DOCX_CONTENT_TYPE,
        headers={"X-Reviewed-Changes": "tracked"},
    )
    got = reviewed_docx_cache.load(fp)
    assert got is not None
    assert got.data == b"hello-docx-bytes"
    assert got.filename == "reviewed.docx"
    assert got.headers.get("X-Reviewed-Changes") == "tracked"
    reviewed_docx_cache.invalidate(fp)
    assert reviewed_docx_cache.load(fp) is None


def test_cache_is_bounded_and_evicts_lru(monkeypatch):
    monkeypatch.setattr(
        reviewed_docx_cache.document_rendering, "MAX_RENDER_CACHE_ENTRIES", 4
    )
    fingerprints = [f"{i:064x}" for i in range(1, 9)]
    for fp in fingerprints:
        reviewed_docx_cache.store(fp, b"x", filename="r.docx", content_type="", headers={})
    root = reviewed_docx_cache.cache_root()
    remaining = [p for p in root.iterdir() if p.suffix == ".bin"]
    assert len(remaining) <= 4
