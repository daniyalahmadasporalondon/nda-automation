# PDF reading-order fixture corpus

The golden corpus behind the `nda_automation/pdf_text.py` reading-order fix. It
pins, per document shape, exactly what text the AI reviewer is handed today so a
fix can be proven to reorder the broken shapes **without** disturbing the shapes
that already read correctly.

## Why this exists

`pdf_text.py` groups text chunks into lines by baseline (y) only, so two-column
NDAs interleave across the gutter, table cells fuse, cm-translated blocks
collapse, and letter-spaced / kern-pair text garbles — all **silently**. The
asymmetry that governs the fix:

- **False negative** (miss a two-column doc): status quo, tolerable.
- **False positive** (split a single-column doc with wide margins, a centered
  title, a two-cell party/signature row, a hanging-indent list, a right-aligned
  page number, or justified text): **catastrophic** — it corrupts documents that
  work today.

So the gate is asymmetric: NEGATIVE/TRAP fixtures must extract **byte-identically**
forever; POSITIVE fixtures are allowed (expected) to change once the fix lands.

## Files

- `generate_fixtures.py` — deterministic, dependency-free builder (hand-built PDF
  content streams; no reportlab, no timestamps, no random ids). Regenerating
  yields byte-identical PDFs. Run: `python -m tests.fixtures.pdf_reading_order.generate_fixtures`.
- `*.pdf` — the committed corpus (16 synthetic fixtures).
- `baselines/*.json` — the f2af53a9 golden snapshot for every fixture: the exact
  paragraphs the reviewer sees + deterministic quality/confidence signals.
- `manifest.json` — name → category → gate → intended behavior.
- `../inbound_nda_sample.pdf` — the one real PDF in the repo, folded in as
  `real_inbound_nda_sample` (a byte-identity anchor).

Harness + gate live one level up:

- `tests/pdf_reading_order_harness.py` — `snapshot`, `capture_baselines`,
  `diff_against_baselines`. Run `python -m tests.pdf_reading_order_harness` for a
  diff report, `--capture` to (re)write baselines.
- `tests/test_pdf_reading_order_corpus.py` — the pytest gate.

## Categories

| category        | gate                              | meaning |
|-----------------|-----------------------------------|---------|
| `positive`      | documented to change after fix    | reads WRONG today; fix must reorder |
| `garble_open_a` | documented to change after fix    | letter-spaced; extract_text fallback re-spaces (gap a) |
| `garble_open_b` | documented to change after fix    | kern-pair chunks under the 6-glyph threshold, undetected (gap b) |
| `garble_fixed`  | documented to change after fix    | per-glyph overlay; already fixed on main — regression anchor |
| `negative`      | **must stay byte-identical**      | reads correctly today; splitting it is the catastrophic FP |
| `garble_trap`   | **must stay byte-identical**      | legit `N D A` spaced title — must NOT be flagged garble |
| `real_negative` | **must stay byte-identical**      | the one real repo PDF |

## The definitions-table decision (documented)

`neg_definitions_table_term_definition` (term column | definition column) is
**deliberately** classified NEGATIVE. It is visually indistinguishable from the
party/address and signature two-cell traps and reads as coherent English when
joined (`"Confidential Information" means …`). Splitting it would fire on every
definitions section and every party table — the catastrophic false positive. A
true multi-column *clause* layout (the positive fixtures) is distinguished by a
full column of independent baselines on **both** sides, not a single term paired
with its own definition.

## Extending

Add real NDAs as byte-identity anchors in `REAL_ANCHORS` in the harness, then
`python -m tests.pdf_reading_order_harness --capture`. When the fix lands,
re-capture the positive/garble_open baselines to the corrected output and add the
"must improve" assertions in the fix's own test.
