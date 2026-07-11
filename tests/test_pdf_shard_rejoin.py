"""SHARD-fragment garble: the second class the per-glyph fix does not reach.

A PDF whose body is positioned a few characters at a time on drifting baselines
extracts (on the current extractor) as a long run of one/two-character "shard"
paragraphs with ``exploded_count == 0`` — so ``_chunks_are_glyph_fragmented``'s
single-char run test never fires and the flat pypdf fallback that heals the
per-glyph class is never taken.

These tests prove, through the REAL pypdf pipeline:

* current code (flag OFF) SHARDS the fixture and the backfill fingerprint calls it
  garbled but re-extraction can never heal it;
* the DEFAULT-OFF ``NDA_PDF_SHARD_REJOIN`` reflow HEALS it into coherent lines;
* flag OFF is byte-identical to before on BOTH the shard fixture (stays garbled)
  and a normal clean PDF (unchanged);
* the safety guards: a genuine vertical short-token column (numbered-list gutter)
  and a two-cell table are NOT merged even with the flag ON, and the existing
  per-glyph signature page is unaffected (its own detector wins first).
"""

from __future__ import annotations

import re
import unittest
from io import BytesIO

from nda_automation import pdf_text
from nda_automation.pdf_text import GeoLine
from nda_automation.pdf_text import (
    extract_pdf_paragraphs,
    shard_rejoin_enabled,
    shard_rejoin_forced,
)
from nda_automation.garble_backfill import garble_fingerprint, stored_paragraph_blocks

from test_pdf_text import (
    PYPDF_AVAILABLE,
    _HELVETICA_WIDTHS,
    _escape_pdf_text,
    _pdf_package,
    make_pdf_glyph_fragmented_signature_page,
    make_pdf_lines,
    make_pdf_shard_fragmented,
)

requires_pypdf = unittest.skipUnless(PYPDF_AVAILABLE, "pypdf is not installed")


def _texts(pdf_bytes):
    return [str(p["text"]) for p in extract_pdf_paragraphs(pdf_bytes)]


def _fingerprint(pdf_bytes):
    return garble_fingerprint(stored_paragraph_blocks("\n\n".join(_texts(pdf_bytes))))


def _positioned_pdf(operations):
    stream = " ".join(["BT"] + operations + ["ET"]) + "\n"
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >> endobj\n",
        f"4 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n",
        "5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    return _pdf_package(objects)


def _vertical_short_column(items, x0=72, y_top=700, step=-16):
    """A genuine vertical column of short tokens (a numbered-list gutter): every
    item shares one x, so the reflow must leave each on its own line (the safety
    case the strict-reduction guard rejects)."""
    ops = []
    y = y_top
    for item in items:
        ops.append(f"/F1 10 Tf 1 0 0 1 {x0} {round(y, 2)} Tm ({_escape_pdf_text(item)}) Tj")
        y += step
    return _positioned_pdf(ops)


def _two_cell_table(rows, x_left=72, x_right=320, y_top=700, step=-16):
    ops = []
    y = y_top
    for left, right in rows:
        ops.append(f"/F1 10 Tf 1 0 0 1 {x_left} {round(y, 2)} Tm ({_escape_pdf_text(left)}) Tj")
        ops.append(f"/F1 10 Tf 1 0 0 1 {x_right} {round(y, 2)} Tm ({_escape_pdf_text(right)}) Tj")
        y += step
    return _positioned_pdf(ops)


def _x_advancing_outline(markers, x0=72, indent=18, y_top=700, step=-14):
    """Outline markers each on their OWN line, x ADVANCING to the right per line
    (indented sub-levels) and y descending a full line — the adversarial case where
    a pure x-advancement rule would wrongly collapse separate lines into one. The
    per-line y-step equals a true shard line's per-fragment y-step, so only the
    horizontal NON-contiguity (an indent gap, not edge-to-edge) keeps them split."""
    ops = []
    x = x0
    y = y_top
    for marker in markers:
        ops.append(f"/F1 10 Tf 1 0 0 1 {round(x, 2)} {round(y, 2)} Tm ({_escape_pdf_text(marker)}) Tj")
        x += indent
        y += step
    return _positioned_pdf(ops)


def _two_level_bullets(x_top=72, x_sub=110, y_top=700, step=-14):
    """A two-level list: each top item followed by two deeper-indented sub-items,
    every entry on its own line. A sub-item sits a large gap to the RIGHT of the
    top item above it, so an x-only rule would append it to the top item's line."""
    ops = []
    y = y_top
    seq = [
        ("A", x_top), ("a1", x_sub), ("a2", x_sub),
        ("B", x_top), ("b1", x_sub), ("b2", x_sub),
        ("C", x_top), ("c1", x_sub), ("c2", x_sub),
        ("D", x_top), ("d1", x_sub), ("d2", x_sub),
    ]
    for text, x in seq:
        ops.append(f"/F1 10 Tf 1 0 0 1 {x} {round(y, 2)} Tm ({_escape_pdf_text(text)}) Tj")
        y += step
    return _positioned_pdf(ops)


def _helvetica_width(text, font_size):
    return sum(font_size * _HELVETICA_WIDTHS[c] / 1000.0 for c in text)


def _shard_line_ops(text, x0, y_top, *, frag_len=2, ystep=-4.0, font_size=10, scale=1.0):
    """Draw ``text`` as a smeared shard line: ``frag_len``-char fragments, one Tm+Tj
    each, x advancing at ``scale`` x the true Helvetica advance (``scale`` < 1 = a
    NARROWER real font than the digest assumes) and y drifting DOWN by ``ystep``
    (> _SAME_LINE_Y_TOLERANCE so the baseline grouping shards it). Returns
    ``(ops, last_x, last_frag, y_after)``."""
    ops = []
    cursor = float(x0)
    y = float(y_top)
    last_x = cursor
    last_frag = ""
    index = 0
    while index < len(text):
        frag = text[index:index + frag_len]
        if frag.strip():
            ops.append(
                f"/F1 {font_size} Tf 1 0 0 1 {round(cursor, 3)} {round(y, 3)} Tm "
                f"({_escape_pdf_text(frag)}) Tj"
            )
            last_x = cursor
            last_frag = frag
        cursor += scale * _helvetica_width(frag, font_size)
        y += ystep
        index += frag_len
    return ops, last_x, last_frag, y


def _bottom_up_shard_page(lines, *, frag_len=2, ystep=-4.0, band_gap=24, y_top=700, font_size=10):
    """Stack ``lines`` top-to-bottom as smeared shard lines, but EMIT them bottom-up
    (last visual line drawn first). A drawing-order reflow would reassemble them
    reversed; the reading-order emit must recover the correct top-to-bottom order."""
    tops = []
    y = float(y_top)
    for line in lines:
        nfrags = (len(line) + frag_len - 1) // frag_len
        tops.append(y)
        y -= abs(ystep) * max(0, nfrags - 1) + band_gap
    ops = []
    for line, top in reversed(list(zip(lines, tops))):  # bottom (lowest top) first
        line_ops, _lx, _lf, _y = _shard_line_ops(
            line, 72, top, frag_len=frag_len, ystep=ystep, font_size=font_size
        )
        ops += line_ops
    return _positioned_pdf(ops)


def _fused_word_page(word, *, frag_len=2, ystep=-4.0, font_size=10, y_top=700):
    """A single SPACELESS word drawn edge-to-edge in Helvetica shard fragments. The
    reflow would weld it into one spaceless token; when ``word`` is long enough that
    token is an implausible 'megaword' the readability backstop must reject."""
    ops, _lx, _lf, _y = _shard_line_ops(word, 72, y_top, frag_len=frag_len, ystep=ystep, font_size=font_size)
    return _positioned_pdf(ops)


def _narrow_word_then_separate(*, scale=0.6, font_size=10, ystep=-4.0, x0=72, y_top=700, gap_extra=17.0):
    """A smeared word laid out at a NARROWER advance than Helvetica (``scale``),
    followed by a GENUINELY-SEPARATE short element ('SEP') placed a real gap away.
    The gap is tuned to fall in the trap window: the font-agnostic Helvetica digest
    OVER-estimates the word's right edge and would MERGE 'SEP' onto the word's line,
    but the observed-scale calibration keeps them separate. Returns
    ``(pdf, last_word_x, last_word_frag, sep_x, font_size)``."""
    word_units = ["aa"] * 10 + ["mm"]
    ops = []
    cursor = float(x0)
    y = float(y_top)
    last_x = cursor
    last_frag = ""
    for frag in word_units:
        ops.append(
            f"/F1 {font_size} Tf 1 0 0 1 {round(cursor, 3)} {round(y, 3)} Tm "
            f"({_escape_pdf_text(frag)}) Tj"
        )
        last_x = cursor
        last_frag = frag
        cursor += scale * _helvetica_width(frag, font_size)
        y += ystep
    sep_x = last_x + gap_extra
    ops.append(
        f"/F1 {font_size} Tf 1 0 0 1 {round(sep_x, 3)} {round(y, 3)} Tm ({_escape_pdf_text('SEP')}) Tj"
    )
    return _positioned_pdf(ops), last_x, last_frag, sep_x, font_size


NORMAL_PDF = make_pdf_lines([
    "1. CONFIDENTIALITY",
    "The receiving party shall keep all Confidential Information secret at all times and forever.",
    "2. TERM",
    "This Agreement remains in force for two years from the date it is signed by both parties.",
])


@requires_pypdf
class ShardFragmentDetectionTests(unittest.TestCase):
    def test_chunk_run_detector_fires_only_on_a_long_short_chunk_run(self):
        detect = pdf_text._chunks_are_shard_fragmented

        def chunk(text):
            return (72.0, 700.0, 10.0, text)

        # Whole-line chunks (normal document): never shard-fragmented.
        self.assertFalse(detect([chunk("The parties agree to keep it secret."), chunk("2. TERM")]))
        # A spaced 'N D A' title / a few short markers: below the run minimum.
        self.assertFalse(detect([chunk("N"), chunk("D"), chunk("A")]))
        self.assertFalse(detect([chunk(f) for f in ["Th", "is", "Ag"]]))
        # Ten consecutive short fragments — the shard shape.
        self.assertTrue(detect([chunk(f) for f in ["Th", "is", "Ag", "re", "em", "en", "ta", "ti", "on", "ok"]]))
        # A long word chunk in the middle resets the run.
        self.assertFalse(
            detect([chunk(f) for f in ["Th", "is", "Ag", "re", "em"]]
                   + [chunk("Confidentiality obligations survive termination.")]
                   + [chunk(f) for f in ["en", "ta", "ti", "on", "ok"]])
        )


@requires_pypdf
class ShardReflowAdoptionGateTests(unittest.TestCase):
    """The adoption gate rejects a reflow that is not a confident improvement."""

    def _lines(self, *texts):
        return [GeoLine(text=t, left_x=None, y=None, font_size=None) for t in texts]

    def test_rejected_when_short_line_count_not_reduced(self):
        baseline = self._lines("a", "b", "c")
        reflowed = self._lines("x", "y", "z")  # same short-line count
        self.assertFalse(pdf_text._reflow_is_adopted(reflowed, baseline))

    def test_rejected_when_a_line_is_implausibly_long(self):
        baseline = self._lines(*(["ab"] * 20))
        giant = "x" * (pdf_text._SHARD_REFLOW_MAX_LINE_CHARS + 1)
        self.assertFalse(pdf_text._reflow_is_adopted(self._lines(giant), baseline))

    def test_rejected_when_glyph_multiset_differs(self):
        baseline = self._lines("ab", "cd", "ef", "gh")
        # Fewer short lines and short enough, but a glyph was dropped -> reject.
        self.assertFalse(pdf_text._reflow_is_adopted(self._lines("abcdef"), baseline))

    def test_adopted_when_reduces_and_preserves(self):
        baseline = self._lines("ab", "cd", "ef", "gh")
        self.assertTrue(pdf_text._reflow_is_adopted(self._lines("abcdefgh"), baseline))

    def test_rejected_when_reading_order_is_reversed(self):
        # Fewer short lines and the SAME glyph multiset, but the reflow emitted its
        # content out of reading order: the sequence check (baseline is in -y,x
        # order) rejects it. The order-blind multiset gate would have ADOPTED this.
        baseline = self._lines("ab", "cd", "ef", "gh")  # reading order: ab cd ef gh
        self.assertFalse(pdf_text._reflow_is_adopted(self._lines("ghefcdab"), baseline))
        # The correctly-ordered reflow of the same glyphs is adopted.
        self.assertTrue(pdf_text._reflow_is_adopted(self._lines("abcdefgh"), baseline))

    def test_rejected_when_a_word_is_fused_into_a_megaword(self):
        # Reduces the short-line count AND preserves the reading sequence, but a
        # dropped inter-word space welded a spaceless run longer than the plausible
        # word cap. The whitespace-insensitive sequence check cannot see it; the
        # megaword backstop must reject.
        fused = "a" * (pdf_text._SHARD_MAX_WORD_CHARS + 1)
        baseline = self._lines(*(["a"] * len(fused)))
        self.assertFalse(pdf_text._reflow_is_adopted(self._lines(fused), baseline))
        # The same characters split at a plausible word boundary are adopted.
        spaced = self._lines("a" * 10, "a" * 10, "a" * (len(fused) - 20))
        self.assertTrue(pdf_text._reflow_is_adopted(spaced, baseline))


@requires_pypdf
class ShardFragmentReproTests(unittest.TestCase):
    """The fixture SHARDS under current code and today's backfill can't heal it."""

    def test_current_code_shards_the_fixture(self):
        self.assertFalse(shard_rejoin_enabled())  # flag OFF by default
        fingerprint = _fingerprint(make_pdf_shard_fragmented())
        # The shard fingerprint: no exploded run, a long shard run, flagged garbled.
        self.assertEqual(fingerprint["exploded_count"], 0, fingerprint)
        self.assertGreaterEqual(fingerprint["longest_shard_run"], 8, fingerprint)
        self.assertTrue(fingerprint["garbled"], fingerprint)

    def test_reextraction_without_the_flag_stays_garbled(self):
        # The exact condition garble_backfill._heal_matter checks: re-extracting the
        # bytes through today's extractor (flag OFF) reproduces the shards, so the
        # heal would report "still_garbled" and write nothing.
        self.assertTrue(_fingerprint(make_pdf_shard_fragmented())["garbled"])


@requires_pypdf
class ShardRejoinHealTests(unittest.TestCase):
    def test_flag_on_heals_the_fixture(self):
        pdf = make_pdf_shard_fragmented()
        with shard_rejoin_forced(True):
            texts = _texts(pdf)
            fingerprint = garble_fingerprint(stored_paragraph_blocks("\n\n".join(texts)))
        self.assertFalse(fingerprint["garbled"], fingerprint)
        self.assertEqual(fingerprint["longest_shard_run"], 0, texts)
        # The reflow recovers the words (spacing may differ), not shards. Compare
        # the whitespace-stripped character multiset to the drawn body.
        drawn = "".join("".join(line.split()) for line in [
            "This Agreement is made between the parties for the protection of Confidential Information",
            "The receiving party shall not disclose any Confidential Information to third parties anywhere",
            "The obligations of confidentiality shall survive the termination of this Agreement entirely",
            "Each party shall use the same degree of care to protect the disclosing party information",
            "Nothing in this Agreement grants any license under any intellectual property rights of a party",
        ])
        got = "".join("".join(t.split()) for t in texts)
        self.assertEqual(sorted(drawn), sorted(got))
        # The reflow restores READING ORDER: with whitespace removed the healed text
        # equals the drawn body with whitespace removed (word spacing may still carry
        # the occasional spurious space after a capital — the reconstruction recovers
        # every character in order, which is the reviewable win over 200+ shards).
        self.assertEqual(got, drawn)
        for word in ("Agreement", "Confidential", "confidentiality", "termination"):
            self.assertIn(word, got, texts)

    def test_healed_lines_carry_no_geometry(self):
        # A reconstructed line must not feed the pdf_confident trust tier.
        pdf = make_pdf_shard_fragmented()
        with shard_rejoin_forced(True):
            paragraphs = extract_pdf_paragraphs(pdf)
        self.assertFalse(any("pdf_geometry" in p for p in paragraphs), paragraphs)


@requires_pypdf
class ShardRejoinByteIdentityTests(unittest.TestCase):
    """Flag OFF must be byte-identical to before, everywhere."""

    def test_flag_off_shard_fixture_unchanged_and_still_garbled(self):
        pdf = make_pdf_shard_fragmented()
        baseline = _texts(pdf)
        self.assertTrue(garble_fingerprint(stored_paragraph_blocks("\n\n".join(baseline)))["garbled"])
        # Idempotent + unaffected by an explicit forced-OFF override.
        self.assertEqual(baseline, _texts(pdf))
        with shard_rejoin_forced(False):
            self.assertEqual(baseline, _texts(pdf))

    def test_flag_off_normal_pdf_unchanged(self):
        baseline = _texts(NORMAL_PDF)
        with shard_rejoin_forced(False):
            self.assertEqual(baseline, _texts(NORMAL_PDF))

    def test_flag_on_normal_pdf_is_byte_identical(self):
        # A clean whole-line page never trips the shard detector, so the flag ON
        # output equals the flag OFF output exactly.
        baseline = _texts(NORMAL_PDF)
        with shard_rejoin_forced(True):
            self.assertEqual(baseline, _texts(NORMAL_PDF))


@requires_pypdf
class ShardRejoinSafetyTests(unittest.TestCase):
    """The reflow must not merge LEGITIMATE short-token sequences."""

    def test_vertical_numbered_list_column_is_not_merged(self):
        pdf = _vertical_short_column([f"{n}." for n in range(1, 15)])
        off = _texts(pdf)
        with shard_rejoin_forced(True):
            on = _texts(pdf)
        # Same output with the flag on: the column's non-advancing x means the
        # reflow keeps every item on its own line, the short-line count does not
        # drop, and the strict-reduction guard rejects the reflow.
        self.assertEqual(off, on)
        self.assertIn("1.", on)
        self.assertIn("14.", on)

    def test_x_advancing_outline_markers_stay_separate_lines(self):
        # The headline regression: markers whose x ADVANCES down the page (and whose
        # y-step equals a true shard line's) must NOT collapse into one line.
        markers = ["1.", "a.", "i.", "A.", "(1)", "2.", "b.", "ii.", "B.", "(2)", "3.", "c."]
        pdf = _x_advancing_outline(markers)
        off = _texts(pdf)
        with shard_rejoin_forced(True):
            on = _texts(pdf)
        self.assertEqual(off, on)
        # Each marker survives as its own line; none were run together.
        self.assertEqual(len(on), len(markers), on)
        self.assertNotIn(" ", "".join(on).replace(".", "").replace("(", "").replace(")", ""))

    def test_two_level_bullet_list_top_and_sub_items_stay_separate(self):
        pdf = _two_level_bullets()
        off = _texts(pdf)
        with shard_rejoin_forced(True):
            on = _texts(pdf)
        self.assertEqual(off, on)
        # No top item absorbed its deeper-indented sub-item (e.g. 'A a1').
        self.assertNotIn("A a1", on)
        self.assertIn("a1", on)
        self.assertIn("A", on)

    def test_two_cell_table_rows_are_not_merged(self):
        pdf = _two_cell_table([(f"A{n}", f"B{n}") for n in range(1, 10)])
        off = _texts(pdf)
        with shard_rejoin_forced(True):
            on = _texts(pdf)
        self.assertEqual(off, on)

    def test_healed_shard_line_does_not_run_separate_words_together(self):
        # A smeared prose line whose word boundaries carry a real space must heal
        # WITHOUT fusing adjacent words. The reflow may over-split WITHIN a word (the
        # accepted under-heal), which only ever ADDS spaces — so no maximal alphabetic
        # run can ever equal two drawn words concatenated. If a boundary space were
        # dropped, that fused run WOULD appear; we assert it never does.
        words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
        pdf = make_pdf_shard_fragmented(lines=[" ".join(words)])
        with shard_rejoin_forced(True):
            texts = _texts(pdf)
        runs = set(re.findall(r"[A-Za-z]+", " ".join(texts)))
        for first, second in zip(words, words[1:]):
            self.assertNotIn(first + second, runs, texts)  # e.g. 'alphabravo'

    def test_per_glyph_signature_page_is_unaffected(self):
        # The per-glyph detector fires FIRST (flat pypdf fallback), so the shard
        # branch is never reached; the flag changes nothing here.
        pdf = make_pdf_glyph_fragmented_signature_page()
        off = _texts(pdf)
        with shard_rejoin_forced(True):
            on = _texts(pdf)
        self.assertEqual(off, on)
        self.assertIn("CEO", on)
        self.assertEqual([t for t in on if len(t) <= 2], [])


@requires_pypdf
class ShardReflowGateFixTests(unittest.TestCase):
    """The four confirmed gate holes, each proven through the REAL pypdf pipeline."""

    def test_fix1_bottom_up_page_heals_in_reading_order(self):
        # A page whose visual lines are DRAWN bottom-up must reassemble top-to-bottom
        # (reading order), not in drawing order. Distinct leading markers pin the order.
        lines = [
            "AAAA alpha bravo charlie delta",
            "BBBB echo foxtrot golf hotel",
            "CCCC india juliet kilo lima",
        ]
        pdf = _bottom_up_shard_page(lines)
        with shard_rejoin_forced(True):
            texts = _texts(pdf)
        joined = " ".join(texts)
        # Healed (the shard fingerprint is gone)...
        self.assertFalse(_fingerprint_from_texts(texts)["garbled"], texts)
        # ...AND in the correct reading order A, then B, then C.
        idx_a, idx_b, idx_c = joined.index("AAAA"), joined.index("BBBB"), joined.index("CCCC")
        self.assertLess(idx_a, idx_b, texts)
        self.assertLess(idx_b, idx_c, texts)

    def test_fix2_word_fusion_produces_no_spaceless_megaword(self):
        # A spaceless word drawn edge-to-edge would weld into a >30-char megaword.
        # The readability backstop rejects that reflow (flag ON == flag OFF), so no
        # fused megaword ever reaches the review (accepted under-heal).
        word = "confidentialinformationprotectionobligations"  # 44 chars, no spaces
        self.assertGreater(len(word), pdf_text._SHARD_MAX_WORD_CHARS)
        pdf = _fused_word_page(word)
        off = _texts(pdf)
        with shard_rejoin_forced(True):
            on = _texts(pdf)
        # Rejected: identical to flag-off, and no spaceless megaword persisted.
        self.assertEqual(off, on)
        self.assertFalse(pdf_text.text_has_implausible_megaword("\n\n".join(on)), on)

    def test_fix3_narrow_font_separate_element_is_not_merged(self):
        # A narrower-than-Helvetica word followed by a genuinely-separate element,
        # at a gap the font-agnostic digest would BRIDGE but the observed-scale
        # calibration keeps split.
        pdf, last_x, last_frag, sep_x, font_size = _narrow_word_then_separate()
        # Document the trap: the Helvetica estimate's right edge is close enough that
        # the OLD (uncalibrated) continuation test would have merged 'SEP'.
        est_right = last_x + pdf_text._estimate_text_width(last_frag, font_size)
        budget = pdf_text._SHARD_HGAP_MAX_EM * font_size
        self.assertLessEqual(sep_x - est_right, budget)  # a raw digest WOULD merge
        with shard_rejoin_forced(True):
            texts = _texts(pdf)
        merged_word = "a" * 20 + "mm"
        self.assertIn(merged_word, texts)  # the narrow word still heals as one line
        self.assertIn("SEP", texts)  # the separate element kept its own line
        # 'SEP' is never glued onto another line (no false merge).
        self.assertFalse(any("SEP" in t and t != "SEP" for t in texts), texts)

    def test_fix4_true_shard_fixture_still_heals_under_the_new_gate(self):
        # The whole point: the real shard fixture must STILL heal through the
        # rebuilt (reading-order + word-spacing) adoption gate.
        pdf = make_pdf_shard_fragmented()
        off = _texts(pdf)
        self.assertTrue(_fingerprint_from_texts(off)["garbled"])  # garbled with flag off
        with shard_rejoin_forced(True):
            on = _texts(pdf)
        self.assertFalse(_fingerprint_from_texts(on)["garbled"], on)  # healed with flag on
        # Characters recovered IN reading order (whitespace-insensitive: the reflow
        # may add the occasional spurious space after a capital, which never fuses).
        recovered = "".join("".join(t.split()) for t in on)
        for word in ("Agreement", "Confidential", "confidentiality", "termination"):
            self.assertIn(word, recovered, on)


def _fingerprint_from_texts(texts):
    return garble_fingerprint(stored_paragraph_blocks("\n\n".join(texts)))


if __name__ == "__main__":
    unittest.main()
