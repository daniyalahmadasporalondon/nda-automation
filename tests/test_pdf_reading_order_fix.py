"""Behavioural gate for the PDF region-first reading-order fix.

Every test here feeds REAL PDF BYTES (the committed fixture corpus) through the
REAL ``extract_pdf_document`` extractor — nothing mocks the chunk list, so a pass
means the actual pypdf visitor -> region partition -> paragraph pipeline behaves.

Split of responsibility with ``test_pdf_reading_order_corpus.py``: that file is the
byte-identity gate (negatives must not drift); this file asserts the POSITIVE
behaviour the fix is supposed to produce (columns read in order, overlays split,
CTM composed, garble flagged) plus the confidence contract.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from nda_automation.pdf_text import (
    _compose_translation,
    _letterspaced_garble_run,
    extract_pdf_document,
)
from tests import pdf_reading_order_harness as harness

PYPDF_AVAILABLE = importlib.util.find_spec("pypdf") is not None
pytestmark = pytest.mark.skipif(not PYPDF_AVAILABLE, reason="pypdf is not installed")

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "pdf_reading_order")


def _extract(name: str):
    # Route through the harness path resolver so real-document anchors (which live
    # outside the generated-fixture dir) are found too.
    path = harness._fixture_path(name)
    with open(path, "rb") as fh:
        return extract_pdf_document(fh.read())


def _joined(doc) -> str:
    return " || ".join(p["text"] for p in doc.paragraphs)


def _first_index(doc, needle: str) -> int:
    for i, p in enumerate(doc.paragraphs):
        if needle in p["text"]:
            return i
    raise AssertionError(f"{needle!r} not found in paragraphs: {_joined(doc)}")


# --------------------------------------------------------------------------
# Defect 3: CTM composition, with the multiply-order regression guard.
# --------------------------------------------------------------------------

def _mult_translation(m1, m2):
    """Translation row of (m1 x m2) under pypdf's row-vector convention."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def test_ctm_compose_uses_tm_times_cm_not_the_reverse():
    tm = [1, 0, 0, 1, 10, 20]        # text placed at (10, 20)
    cm = [2, 0, 0, 3, 0, 0]          # graphics-state scale 2x horizontal, 3x vertical
    x, y, rotated = _compose_translation(tm, cm)
    # Correct order tm x cm: (10*2, 20*3) = (20, 60).
    assert (round(x, 6), round(y, 6)) == (20.0, 60.0)
    assert rotated is False
    # The WRONG order (cm x tm) gives a different answer on a SCALING cm — this is
    # exactly the input class where a silently-wrong order corrupts overlays.
    wrong = _mult_translation(cm, tm)
    assert (round(wrong[0], 6), round(wrong[1], 6)) != (20.0, 60.0)
    assert _mult_translation(tm, cm) == (x, y)


def test_ctm_order_is_silent_for_translation_only_cm():
    # The danger: for a translation-only cm both orders agree, so a wrong order
    # hides until a scaling/rotating cm appears. Lock that this is the benign case.
    tm = [1, 0, 0, 1, 10, 20]
    cm = [1, 0, 0, 1, 5, 7]
    assert _mult_translation(tm, cm) == _mult_translation(cm, tm) == (15.0, 27.0)


def test_ctm_rotation_is_detected():
    x, y, rotated = _compose_translation([0, 1, -1, 0, 0, 0], [1, 0, 0, 1, 0, 0])
    assert rotated is True


def test_cm_translated_block_renders_below_not_merged():
    # pos_ctm_translated_overlap: Block B is drawn with a cm that translates it far
    # DOWN the page; tm-only extraction collapses it onto Block A's baseline and
    # interleaves. Composed coordinates must place Block B below Block A.
    doc = _extract("pos_ctm_translated_overlap")
    assert _first_index(doc, "Block A line one") < _first_index(doc, "Block B line one")
    # No paragraph may fuse an A line and a B line together.
    for p in doc.paragraphs:
        assert not ("Block A" in p["text"] and "Block B" in p["text"]), _joined(doc)


# --------------------------------------------------------------------------
# Defects 1 & 2: two-column reading order + stamped-overlay separation.
# --------------------------------------------------------------------------

def test_two_column_clean_reads_left_column_fully_then_right():
    doc = _extract("pos_two_column_clean")
    i1 = _first_index(doc, "1. Confidential Information")
    i2 = _first_index(doc, "2. The Receiving Party")
    i3 = _first_index(doc, "3. The obligations")
    i4 = _first_index(doc, "4. Nothing in this Agreement")
    assert i1 < i2 < i3 < i4, _joined(doc)
    # The interleave defect: no single paragraph may contain both a left-column and
    # a right-column clause.
    for p in doc.paragraphs:
        assert not ("means any" in p["text"] and "obligations of" in p["text"])
    ro = doc.quality["reading_order"]
    assert ro["columns_detected"] == 2
    assert ro["reorder_applied"] is True
    assert "column_reconstructed" in ro["reasons"]


def test_two_column_unbalanced_reads_full_left_then_short_right():
    doc = _extract("pos_two_column_unbalanced_last")
    # Left column carries clauses 1-4, right column 5-6; every left clause precedes
    # every right clause despite the right column being much shorter.
    for left in ("1.", "2.", "3.", "4."):
        for right in ("5. This Agreement", "6. Neither party"):
            assert _first_index(doc, f"{left} ") < _first_index(doc, right), (
                left, right, _joined(doc)
            )
    assert doc.quality["reading_order"]["columns_detected"] == 2


def test_stamped_overlay_is_split_from_the_sentence_it_sits_on():
    doc = _extract("pos_stamped_executed_overlay")
    # The oversized "EXECUTED" stamp shares a baseline with a body line; it must not
    # splice into that sentence.
    for p in doc.paragraphs:
        assert not ("full force" in p["text"] and "EXECUTED" in p["text"]), _joined(doc)
    assert any(p["text"].strip() == "EXECUTED" for p in doc.paragraphs), _joined(doc)
    ro = doc.quality["reading_order"]
    assert "stamped_overlay_order_unknown" in ro["reasons"]
    assert ro["degraded"] is True


def test_borderless_table_stays_row_major_and_unflagged():
    doc = _extract("pos_table_3col_5row")
    # A borderless table already reads row-major under baseline bucketing; the fix
    # must NOT column-split it (that would linearise it column-major = wrong).
    assert _first_index(doc, "Clause Requirement Status") == 0
    ro = doc.quality["reading_order"]
    assert ro["reorder_applied"] is False
    assert ro["garbled"] is False


# --------------------------------------------------------------------------
# Defect 4: letter-spaced / kern-ligature garble detection.
# --------------------------------------------------------------------------

def test_letterspaced_run_helper_catches_both_gaps_and_spares_a_spaced_title():
    assert _letterspaced_garble_run("I N W I T N E S S W H E R E O F") >= 6
    assert _letterspaced_garble_run("IN WI TN ES S WH ER EO F the par ties") >= 6
    # A legitimate spaced "N D A" title is only a run of 3 among normal prose.
    assert _letterspaced_garble_run("N D A This Non-Disclosure Agreement sets out") < 6
    # Ordinary prose with its scattered 1-2 letter words never chains a long run.
    assert _letterspaced_garble_run(
        "The Receiving Party shall use it only for the Purpose as set out"
    ) < 6


@pytest.mark.parametrize("name", ["garble_letter_spaced_tracking", "garble_ligature_kern_pairs"])
def test_letterspaced_and_ligature_garble_is_flagged(name):
    doc = _extract(name)
    ro = doc.quality["reading_order"]
    assert ro["garbled"] is True
    assert ro["degraded"] is True
    types = {w["type"] for w in doc.quality["warnings"]}
    assert "pdf_fragmented_text" in types, doc.quality["warnings"]


def test_spaced_nda_title_is_not_flagged_as_garble():
    doc = _extract("garble_spaced_nda_title_legit")
    ro = doc.quality["reading_order"]
    assert ro["garbled"] is False
    assert ro["degraded"] is False
    types = {w["type"] for w in doc.quality["warnings"]}
    assert "pdf_fragmented_text" not in types


def test_already_fixed_per_glyph_page_is_not_reflagged():
    doc = _extract("garble_per_glyph_normal_advance")
    assert "Moorwand Limited" in _joined(doc)
    assert doc.quality["reading_order"]["garbled"] is False


# --------------------------------------------------------------------------
# Confidence contract + conservatism.
# --------------------------------------------------------------------------

MUST_NOT_DRIFT = {
    "neg_definitions_table_term_definition",
    "neg_justified_large_interword",
    "neg_numbered_list_far_left_number",
    "neg_right_aligned_page_number",
    "neg_signature_name_left_date_right",
    "neg_single_col_wide_margins_centered_title",
    "neg_two_cell_party_table",
    "garble_spaced_nda_title_legit",
    "real_inbound_nda_sample",
}


@pytest.mark.parametrize("name", sorted(MUST_NOT_DRIFT))
def test_no_false_positive_reorder_on_clean_documents(name):
    doc = _extract(name)
    ro = doc.quality["reading_order"]
    # A clean single-column document must never be reordered or flagged.
    assert ro["reorder_applied"] is False, (name, _joined(doc))
    assert ro["columns_detected"] == 1
    assert ro["degraded"] is False
    assert ro["reading_order_confidence"] == 1.0


@pytest.mark.parametrize("name", sorted(MUST_NOT_DRIFT))
def test_clean_documents_are_byte_identical_to_golden(name):
    diffs = harness.diff_against_baselines()
    assert diffs[name] is None, diffs[name]


def test_confidence_is_lower_for_a_reordered_page_than_a_clean_one():
    clean = _extract("neg_single_col_wide_margins_centered_title")
    two_col = _extract("pos_two_column_clean")
    assert clean.quality["reading_order"]["reading_order_confidence"] == 1.0
    assert (
        two_col.quality["reading_order"]["reading_order_confidence"]
        < clean.quality["reading_order"]["reading_order_confidence"]
    )


def test_reading_order_contract_shape_is_stable_and_serializable():
    import json

    doc = _extract("pos_two_column_clean")
    ro = doc.quality["reading_order"]
    assert set(ro.keys()) == {
        "reading_order_confidence",
        "columns_detected",
        "reorder_applied",
        "garbled",
        "degraded",
        "reasons",
    }
    assert isinstance(ro["reading_order_confidence"], float)
    assert isinstance(ro["columns_detected"], int)
    assert isinstance(ro["reorder_applied"], bool)
    assert isinstance(ro["degraded"], bool)
    assert isinstance(ro["reasons"], list)
    json.dumps(ro)  # must be JSON-serializable for the visibility layer


def test_source_index_is_sequential_in_reading_order():
    # Reordering changes the sequence the AI sees; source_index must stay a per-page
    # sequential counter in READING order (no downstream consumer may assume it
    # tracks visual draw order).
    doc = _extract("pos_two_column_clean")
    indices = [p["source_index"] for p in doc.paragraphs]
    assert indices == list(range(1, len(indices) + 1))
