"""REAL-converter regression tests for the garble working-DOCX rebuild.

Every other docx-rebuild test drives the conversion through STUB converters.
These tests exercise the REAL machinery end-to-end: the actual pdf2docx
reconstruction (real child subprocess with its RLIMIT/process-group guards,
launched from ``sys.executable``) over the per-glyph signature fixtures from
``test_pdf_text`` — the exact rendering class (one glyph per positioned text op,
baseline jitter/drift) that garbled the pypdf visitor path in prod.

EMPIRICAL FINDING these tests pin down (2026-07-10 adversarial runtime probe):
pdf2docx does NOT garble the per-glyph class. It reads text through PyMuPDF's
span assembly, which reassembles hand-positioned glyphs from their true
geometry — unlike pypdf's ``visitor_text`` (stale ``tm`` translations for
back-to-back single-glyph ops), which is what produced the
'M o r w a n d L i m i t e d o' / stacked 'C'/'E'/'O' stored shape. So the
rebuilt working DOCX comes out coherent: full character conservation, zero
one/two-char shard paragraphs, zero exploded (spaced-single-letter) lines.

SKIPPED (not failed) where the real engine is absent: pdf2docx + PyMuPDF are
the ``[pdf]`` extra; environments without them skip via ``importorskip``.
Each real conversion is subsecond on these small fixtures.
"""

from __future__ import annotations

from collections import Counter

import pytest

pytest.importorskip("pypdf")
pytest.importorskip("fitz")
pytest.importorskip("pdf2docx")

from nda_automation import garble_backfill, matter_store, pdf_text
from nda_automation import pdf_ingest_conversion
from nda_automation.review_document import (
    ParagraphAlignmentError,
    align_document_paragraphs,
)
from nda_automation.review_result_contract import extracted_text_from_paragraphs

from test_pdf_text import (
    make_pdf_glyph_fragmented_signature_page,
    make_pdf_normal_body_plus_glyph_signature_page,
    make_pdf_pages,
)

# The text the per-glyph fixture draws (see _signature_block_ops in test_pdf_text).
SIGNATURE_LINES = [
    "Moorwand Limited",
    "Signed: _______________",
    "Briane Lucas",
    "CEO",
    "Luc Guerand",
    "Authorised Signatory Name Position/Title",
    "Date",
]
MIXED_PAGE_BODY_LINES = [
    "The parties agree to keep all Confidential Information secret and secure at all",
    "times during the term of this Agreement.",
]
TWO_PAGE_FIRST_PAGE_LINES = [
    "1. CONFIDENTIALITY",
    "The receiving party shall keep information confidential at all times.",
]


# --- quality metrics ----------------------------------------------------------
def _longest_single_char_token_run(text: str) -> int:
    run = best = 0
    for token in text.split():
        if len(token) == 1:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _longest_shard_run(texts: list[str]) -> int:
    """Longest run of consecutive <=2-char paragraphs (the stacked 'C'/'E'/'O' shape)."""
    run = best = 0
    for text in texts:
        if len(text.strip()) <= garble_backfill.GARBLE_SHARD_MAX_CHARS:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _missing_characters(expected_lines: list[str], got_texts: list[str]) -> Counter:
    """Character-multiset conservation: drawn glyphs that never made the output."""
    expected = Counter(c for line in expected_lines for c in line if not c.isspace())
    got = Counter(c for text in got_texts for c in text if not c.isspace())
    return expected - got


def _assert_texts_coherent(texts: list[str], expected_lines: list[str], label: str) -> None:
    texts = [t for t in texts if t.strip()]
    assert texts, f"{label}: no non-empty paragraphs"
    exploded = [t for t in texts if _longest_single_char_token_run(t)
                >= garble_backfill.GARBLE_EXPLODED_TOKEN_RUN_MIN]
    assert not exploded, f"{label}: exploded per-glyph lines survived: {exploded!r}"
    shard_run = _longest_shard_run(texts)
    # 'CEO' is a legitimate 3-char paragraph; a RUN of shards is the garble shape.
    assert shard_run < garble_backfill.GARBLE_SHARD_RUN_CORROBORATING, (
        f"{label}: shard run of {shard_run} one/two-char paragraphs: {texts!r}"
    )
    missing = _missing_characters(expected_lines, texts)
    assert not missing, f"{label}: drawn characters lost in conversion: {dict(missing)!r}"
    fingerprint = garble_backfill.garble_fingerprint(
        garble_backfill.stored_paragraph_blocks("\n\n".join(texts))
    )
    assert not fingerprint["garbled"], f"{label}: output carries the garble fingerprint: {fingerprint}"


def _pre_fix_paragraphs(pdf_bytes: bytes) -> list[dict]:
    """The garbled paragraphs the PRE-FIX extractor stored (per-glyph demotion off)."""
    original = pdf_text._GLYPH_FRAGMENT_RUN_MIN
    pdf_text._GLYPH_FRAGMENT_RUN_MIN = 10**9
    try:
        return pdf_text.extract_pdf_paragraphs(pdf_bytes)
    finally:
        pdf_text._GLYPH_FRAGMENT_RUN_MIN = original


def _real_conversion(pdf_bytes: bytes, name: str) -> pdf_ingest_conversion.PdfWorkingDocument:
    """The REAL pipeline conversion: default converter => Pdf2DocxConverter
    subprocess with RLIMIT/process-group guards. No stubs anywhere."""
    healed = pdf_text.extract_pdf_paragraphs(pdf_bytes)
    return pdf_ingest_conversion.convert_pdf_matter_to_docx(pdf_bytes, name, healed)


# --- real-converter baseline: output quality per fixture -----------------------
class TestRealPdf2DocxReconstructionQuality:
    def test_mixed_page_body_plus_per_glyph_signature_is_coherent(self):
        # The class most likely in real NDAs: normal body + per-glyph signature
        # block on the SAME page. pdf2docx must not garble the page its own way.
        pdf_bytes = make_pdf_glyph_fragmented_signature_page()
        working = _real_conversion(pdf_bytes, "mixed.pdf")
        body_texts = [t for (_i, t, _n) in pdf_ingest_conversion.reconstructed_body_index(working.docx_bytes)]
        _assert_texts_coherent(
            body_texts, MIXED_PAGE_BODY_LINES + SIGNATURE_LINES, "reconstructed DOCX body (mixed page)"
        )
        # The signature names must survive as words, not spaced glyphs.
        joined = "\n".join(body_texts)
        for phrase in ("Moorwand Limited", "Briane Lucas", "CEO", "Luc Guerand"):
            assert phrase in joined, f"{phrase!r} not reassembled in: {joined!r}"
        # And the stored working paragraphs (healed pypdf text, re-keyed) are clean.
        _assert_texts_coherent(
            [str(p.get("text") or "") for p in working.paragraphs],
            MIXED_PAGE_BODY_LINES + SIGNATURE_LINES,
            "stored working_docx_paragraphs (mixed page)",
        )
        assert working.mapped_count > 0

    def test_two_page_normal_then_per_glyph_signature_page_is_coherent(self):
        pdf_bytes = make_pdf_normal_body_plus_glyph_signature_page()
        working = _real_conversion(pdf_bytes, "two-page.pdf")
        body_texts = [t for (_i, t, _n) in pdf_ingest_conversion.reconstructed_body_index(working.docx_bytes)]
        _assert_texts_coherent(
            body_texts,
            TWO_PAGE_FIRST_PAGE_LINES + SIGNATURE_LINES,
            "reconstructed DOCX body (normal page + per-glyph sig page)",
        )
        assert "Moorwand Limited" in "\n".join(body_texts)
        assert working.mapped_count > 0

    def test_control_normal_multi_paragraph_pdf_converts_cleanly(self):
        lines_page_one = [
            "1. CONFIDENTIALITY",
            "The receiving party shall keep all Confidential Information secret and",
            "secure at all times during the term of this Agreement.",
            "2. TERM",
            "This Agreement shall remain in force for a period of two years from the",
            "date of execution by both parties.",
        ]
        lines_page_two = [
            "3. GOVERNING LAW",
            "This Agreement shall be governed by the laws of England and Wales and",
            "the parties submit to the exclusive jurisdiction of its courts.",
        ]
        pdf_bytes = make_pdf_pages([lines_page_one, lines_page_two])
        working = _real_conversion(pdf_bytes, "control.pdf")
        body_texts = [t for (_i, t, _n) in pdf_ingest_conversion.reconstructed_body_index(working.docx_bytes)]
        _assert_texts_coherent(
            body_texts, lines_page_one + lines_page_two, "reconstructed DOCX body (control)"
        )
        assert working.mapped_count > 0


# --- end-to-end: real heal + real rebuild over a real on-disk store ------------
@pytest.fixture()
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(matter_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(matter_store, "MATTERS_PATH", tmp_path / "matters.json")
    monkeypatch.setattr(matter_store, "UPLOADS_DIR", tmp_path / "uploads")
    matter_store._invalidate_list_cache()
    yield tmp_path
    matter_store._invalidate_list_cache()


class TestGarbleBackfillEndToEndWithRealConverter:
    def test_execute_heals_text_rebuilds_working_docx_and_alignment_succeeds(
        self, _isolated_store
    ):
        pdf_bytes = make_pdf_glyph_fragmented_signature_page()
        garbled_paragraphs = _pre_fix_paragraphs(pdf_bytes)
        garbled_text = extracted_text_from_paragraphs(garbled_paragraphs)
        # Sanity: the stored pre-fix shape really is the garble class.
        assert garble_backfill.garble_fingerprint(
            garble_backfill.stored_paragraph_blocks(garbled_text)
        )["garbled"]
        # Working paragraphs as the pre-fix retro conversion left them: the
        # garbled pypdf paragraphs re-keyed to the reconstruction.
        garbled_working = []
        for index, paragraph in enumerate(garbled_paragraphs):
            re_keyed = dict(paragraph)
            re_keyed["source_index"] = index
            re_keyed.pop("source_part", None)
            garbled_working.append(re_keyed)

        matter_id = "matter_real_rebuild"
        stored_filename = f"{matter_id}-Signature-NDA.pdf"
        matter = {
            "id": matter_id,
            "created_at": "2026-06-10T00:00:00+00:00",
            "updated_at": "2026-06-10T00:00:01+00:00",
            "source_type": "gmail_inbound",
            "source_filename": "Signature NDA.pdf",
            "stored_filename": stored_filename,
            "document_title": "Signature NDA",
            "status": "active",
            "owner_user_id": "google:111",
            "extracted_text": garbled_text,
            "working_docx_paragraphs": garbled_working,
            "review_result": None,
        }
        matter_store.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        (matter_store.UPLOADS_DIR / stored_filename).write_bytes(pdf_bytes)
        matter_store._save_matter_record(matter)
        matter_store._invalidate_list_cache()
        assert garble_backfill.working_docx_paragraphs_garbled(matter) is True

        # REAL execute body (the exact function the background thread runs),
        # REAL pdf2docx subprocess — no stubs anywhere on the path.
        report = garble_backfill.run_garble_backfill(dry_run=False, limit=10)

        assert report["healed"] == 1
        assert report["docx_rebuilt"] == 1
        assert report["docx_rebuild_failed"] == 0
        assert report["failed"] == 0
        (entry,) = [e for e in report["matters"] if e["id"] == matter_id]
        assert entry["action"] == "healed"
        assert entry["docx_rebuild"] == "rebuilt"
        assert entry["working_docx_paragraphs_garbled"] is False

        fresh = matter_store.get_matter(matter_id, owner_user_id="")
        healed_text = str(fresh.get("extracted_text") or "")
        assert "Moorwand Limited" in healed_text
        assert "M o r w a n d" not in healed_text
        _assert_texts_coherent(
            garble_backfill.stored_paragraph_blocks(healed_text),
            MIXED_PAGE_BODY_LINES + SIGNATURE_LINES,
            "healed extracted_text",
        )
        working = fresh.get("working_docx_paragraphs")
        assert isinstance(working, list) and working
        _assert_texts_coherent(
            [str(p.get("text") or "") for p in working],
            MIXED_PAGE_BODY_LINES + SIGNATURE_LINES,
            "rebuilt working_docx_paragraphs",
        )
        assert garble_backfill.working_docx_paragraphs_garbled(fresh) is False
        # A role="working" artifact was registered by the real persistence path.
        roles = [a.get("role") for a in fresh.get("artifacts") or []]
        assert "working" in roles

        # THE decisive contract: the review worker aligns the working paragraphs
        # against the matter's extracted_text before anchoring tracked redlines.
        aligned = align_document_paragraphs(list(working), healed_text)
        assert len(aligned) == len(
            [p for p in working if str(p.get("text") or "").strip()]
        )
        # Counterfactual: WITHOUT the rebuild the old garbled working paragraphs
        # can never align against the healed text — reviews would permanently
        # degrade to text-only anchoring. This is what the rebuild fixes.
        with pytest.raises(ParagraphAlignmentError):
            align_document_paragraphs(garbled_working, healed_text)
