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

import unittest
from io import BytesIO

from nda_automation import pdf_text
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

    def test_two_cell_table_rows_are_not_merged(self):
        pdf = _two_cell_table([(f"A{n}", f"B{n}") for n in range(1, 10)])
        off = _texts(pdf)
        with shard_rejoin_forced(True):
            on = _texts(pdf)
        self.assertEqual(off, on)

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


if __name__ == "__main__":
    unittest.main()
