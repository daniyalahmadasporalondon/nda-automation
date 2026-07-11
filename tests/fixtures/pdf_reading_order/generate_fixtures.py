"""Deterministic generator for the PDF reading-order fixture corpus.

Every fixture is a hand-built PDF content stream (no reportlab dependency, no
timestamps, no random ids) so the bytes are byte-for-byte reproducible: running
this module twice, or on any machine, yields identical files. That determinism is
what lets the golden-baseline / byte-identity gate mean anything.

The primitives below (``_pdf_package``, ``_escape_pdf_text``, ``_per_glyph_ops``,
the Helvetica advance-width table) are copied verbatim from
``tests/test_pdf_text.py`` so the fixtures are drawn exactly the way the real
suite already draws its PDFs.

Run:  python -m tests.fixtures.pdf_reading_order.generate_fixtures
      (writes every *.pdf next to this file)

Each fixture's category and INTENDED post-fix behavior is recorded in FIXTURES
below and mirrored into ``manifest.json`` by the harness.
"""

from __future__ import annotations

import os
from io import BytesIO

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA_W, MEDIA_H = 612, 792


# --------------------------------------------------------------------------- #
# Low-level PDF assembly (verbatim from tests/test_pdf_text.py)
# --------------------------------------------------------------------------- #
def _escape_pdf_text(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_package_bytes(objects):
    with BytesIO() as output:
        output.write(b"%PDF-1.4\n")
        offsets = [0]
        for pdf_object in objects:
            offsets.append(output.tell())
            output.write(pdf_object)
        xref_offset = output.tell()
        output.write(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
        output.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
        output.write(
            f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n".encode("latin-1")
        )
        return output.getvalue()


def _pdf_package(objects):
    return _pdf_package_bytes([o.encode("latin-1") for o in objects])


# Helvetica AFM advance widths (per mille of the font size).
_HELVETICA_WIDTHS = {
    " ": 278, ":": 278, "_": 556, "/": 278, ".": 278, ",": 278, "-": 333, "(": 333, ")": 333,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556, "7": 556, "8": 556, "9": 556,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722, "I": 278,
    "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778, "P": 667, "Q": 778, "R": 722,
    "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556, "i": 222,
    "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556, "p": 556, "q": 556, "r": 333,
    "s": 500, "t": 278, "u": 556, "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
}


def _advance(text, font_size):
    return sum(font_size * _HELVETICA_WIDTHS.get(c, 556) / 1000.0 for c in text)


def _per_glyph_ops(text, x, y, font_size, jitter=None, extra_tracking=0.0):
    """One Tm+Tj per glyph at true Helvetica advances, plus optional per-glyph
    baseline jitter and EXTRA inter-glyph tracking (points added after every glyph
    advance). Wide extra_tracking is what makes pypdf's own extract_text re-insert
    spaces between letters -- the letter-spacing garble case."""
    ops = []
    cursor = x
    jitter = jitter or [0.0]
    for index, char in enumerate(text):
        offset = jitter[index % len(jitter)]
        if char != " ":
            ops.append(
                f"/F1 {font_size} Tf 1 0 0 1 {round(cursor, 2)} {round(y + offset, 2)} Tm "
                f"({_escape_pdf_text(char)}) Tj"
            )
        cursor += font_size * _HELVETICA_WIDTHS.get(char, 556) / 1000.0 + extra_tracking
    return ops


def _line_op(text, x, y, font_size):
    """A single whole-line Tm+Tj op (word-processor style)."""
    return f"/F1 {font_size} Tf 1 0 0 1 {x} {y} Tm ({_escape_pdf_text(text)}) Tj"


def _page_obj(page_no, content_no, font_no):
    return (
        f"{page_no} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 {MEDIA_W} {MEDIA_H}] "
        f"/Resources << /Font << /F1 {font_no} 0 R >> >> /Contents {content_no} 0 R >> endobj\n"
    )


def _build(pages_ops):
    """Assemble a PDF from a list of pages; each page is a list of content-stream
    operators (already string ops, WITHOUT the enclosing BT/ET -- we add them)."""
    font_no = 3 + len(pages_ops) * 2
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(len(pages_ops)))
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Kids [{kids}] /Count {len(pages_ops)} >> endobj\n",
    ]
    for i, ops in enumerate(pages_ops):
        page_no = 3 + i * 2
        content_no = page_no + 1
        stream = "BT " + " ".join(ops) + " ET\n"
        objects.append(_page_obj(page_no, content_no, font_no))
        objects.append(
            f"{content_no} 0 obj << /Length {len(stream.encode('latin-1'))} >> "
            f"stream\n{stream}endstream endobj\n"
        )
    objects.append(f"{font_no} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    return _pdf_package(objects)


def _build_raw(pages_streams):
    """Like _build but each page supplies its FULL content stream (so it may use
    graphics operators such as q/Q/cm that must live outside BT/ET)."""
    font_no = 3 + len(pages_streams) * 2
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(len(pages_streams)))
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Kids [{kids}] /Count {len(pages_streams)} >> endobj\n",
    ]
    for i, stream in enumerate(pages_streams):
        page_no = 3 + i * 2
        content_no = page_no + 1
        objects.append(_page_obj(page_no, content_no, font_no))
        objects.append(
            f"{content_no} 0 obj << /Length {len(stream.encode('latin-1'))} >> "
            f"stream\n{stream}endstream endobj\n"
        )
    objects.append(f"{font_no} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    return _pdf_package(objects)


# --------------------------------------------------------------------------- #
# Layout helpers
# --------------------------------------------------------------------------- #
COL1_X = 72
COL2_X = 330
FS = 11
LEADING = 15


def _column_block(lines, x, y0, font_size=FS, leading=LEADING):
    ops = []
    y = y0
    for line in lines:
        ops.append(_line_op(line, x, y, font_size))
        y -= leading
    return ops


# --------------------------------------------------------------------------- #
# POSITIVE fixtures -- reading order is currently WRONG; a correct fix must
# reorder them (baseline captures the buggy order).
# --------------------------------------------------------------------------- #
def two_column_clean():
    """2-page two-column NDA. Clauses 1-2 fill the left column, 3-4 the right
    column at the SAME baselines. Baseline-only bucketing interleaves them
    across the gutter ('1. ... 3. ...'). Correct reading order is 1,2,3,4."""
    p1_left = [
        "1. Confidential Information means any",
        "and all non-public information disclosed",
        "by one party to the other, whether oral",
        "or written, marked confidential.",
        "2. The Receiving Party shall hold the",
        "Confidential Information in strict",
        "confidence and not disclose it to any",
        "third party without prior written consent.",
    ]
    p1_right = [
        "3. The obligations of confidentiality",
        "shall survive termination of this",
        "Agreement for a period of five years",
        "from the date of disclosure.",
        "4. Nothing in this Agreement grants the",
        "Receiving Party any licence or right in",
        "the Confidential Information other than",
        "as expressly set out herein.",
    ]
    p2_left = [
        "5. Each party shall promptly notify the",
        "other of any unauthorised disclosure of",
        "the Confidential Information of which it",
        "becomes aware.",
        "6. This Agreement shall be governed by",
        "and construed in accordance with the",
        "laws of England and Wales.",
    ]
    p2_right = [
        "7. The parties submit to the exclusive",
        "jurisdiction of the courts of England",
        "and Wales in respect of any dispute",
        "arising out of this Agreement.",
        "8. This Agreement constitutes the entire",
        "agreement between the parties relating",
        "to its subject matter.",
    ]
    page1 = _column_block(p1_left, COL1_X, 720) + _column_block(p1_right, COL2_X, 720)
    page2 = _column_block(p2_left, COL1_X, 720) + _column_block(p2_right, COL2_X, 720)
    return _build([page1, page2])


def two_column_unbalanced_last():
    """Two-column, last page's left column is FULL while the right column is only
    half-height (col 2 runs out of clauses). The trailing left-only baselines have
    no right-column partner -- a splitter must not fabricate one."""
    left = [
        "1. Confidential Information means any",
        "non-public information disclosed by the",
        "Disclosing Party to the Receiving Party.",
        "2. The Receiving Party shall use the",
        "Confidential Information solely for the",
        "Purpose and for no other purpose.",
        "3. The Receiving Party shall protect the",
        "Confidential Information using no less",
        "than a reasonable degree of care.",
        "4. Upon request the Receiving Party shall",
        "return or destroy all Confidential",
        "Information in its possession.",
    ]
    right = [
        "5. This Agreement shall remain in force",
        "for a period of three years from the",
        "Effective Date.",
        "6. Neither party shall be liable for any",
        "indirect or consequential loss.",
    ]
    page = _column_block(left, COL1_X, 720) + _column_block(right, COL2_X, 720)
    return _build([page])


def table_3col_5row():
    """A 3-column, 5-row table of NDA-ish content. Same-baseline cells are joined
    with ' ' into one line ('Clause Requirement Status ...'), fusing columns."""
    rows = [
        ("Clause", "Requirement", "Status"),
        ("Definition", "Mark all disclosures confidential", "Agreed"),
        ("Term", "Three years from Effective Date", "Agreed"),
        ("Governing Law", "England and Wales", "Agreed"),
        ("Non-solicit", "Twelve month restriction", "Rejected"),
    ]
    ops = []
    y = 700
    xs = (72, 220, 470)
    for row in rows:
        for x, cell in zip(xs, row):
            ops.append(_line_op(cell, x, y, FS))
        y -= 40
    return _build([ops])


def stamped_executed_overlay():
    """A body paragraph with a horizontal 'EXECUTED' stamp drawn on the SAME
    baseline as one of the body lines. Same-baseline join splices the stamp into
    the sentence ('...this Agreement EXECUTED as of the date...')."""
    body = [
        "This Agreement is entered into by the parties and",
        "shall be binding upon their successors and assigns",
        "as of the date first written above and remains in",
        "full force until terminated in accordance herewith.",
    ]
    ops = _column_block(body, 72, 700)
    # Stamp sits at the same baseline as the third body line (y = 700 - 2*15 = 670).
    ops.append(_line_op("EXECUTED", 430, 670, 20))
    return _build([ops])


def ctm_translated_overlap():
    """One text block positioned by the text matrix (Tm) at y=600, and a second
    block pushed DOWN by a cm current-transformation-matrix translation while its
    Tm still reads y=600. The visitor records tm[5]=600 for BOTH, ignoring the cm,
    so the two physically separate blocks collapse onto one baseline."""
    untranslated = " ".join(
        [_line_op(t, 72, 600 - i * 15, FS) for i, t in enumerate([
            "Block A line one at the true top of the page.",
            "Block A line two immediately below it.",
        ])]
    )
    # cm translates the coordinate system down by 200pt; Tm still says y=600/585,
    # so the visitor mis-reports these as overlapping Block A.
    translated_ops = " ".join(
        [_line_op(t, 72, 600 - i * 15, FS) for i, t in enumerate([
            "Block B line one that actually renders lower down.",
            "Block B line two that actually renders lower down.",
        ])]
    )
    stream = (
        f"BT {untranslated} ET\n"
        f"q 1 0 0 1 0 -200 cm BT {translated_ops} ET Q\n"
    )
    return _build_raw([stream])


# --------------------------------------------------------------------------- #
# NEGATIVE / TRAP fixtures -- these work correctly TODAY. Extraction output must
# be BYTE-IDENTICAL before and after the fix. Splitting any of them is the
# catastrophic false positive.
# --------------------------------------------------------------------------- #
def single_col_wide_margins_centered_title():
    """Single column with generous margins and a centered title. Wide left margin
    + centered heading must NOT be read as two columns."""
    title = "MUTUAL NON-DISCLOSURE AGREEMENT"
    title_x = (MEDIA_W - _advance(title, 16)) / 2.0
    ops = [_line_op(title, round(title_x, 2), 730, 16)]
    body = [
        "This Mutual Non-Disclosure Agreement is entered into between the parties",
        "identified below for the purpose of exploring a potential business",
        "relationship. Each party may disclose confidential information to the other.",
        "The parties agree to protect such information and to use it solely for the",
        "stated purpose and for no other purpose whatsoever.",
    ]
    ops += _column_block(body, 130, 690, font_size=12, leading=18)
    return _build([ops])


def signature_block_name_left_date_right():
    """A signature line: 'Name: ____' on the left and 'Date: ____' on the right at
    the SAME baseline. This is ONE logical line and SHOULD stay merged."""
    ops = [
        _line_op("Signed for and on behalf of the Disclosing Party:", 72, 700, FS),
        _line_op("Name: ______________________", 72, 660, FS),
        _line_op("Date: ______________", 380, 660, FS),
        _line_op("Signature: __________________", 72, 630, FS),
        _line_op("Title: ______________", 380, 630, FS),
    ]
    return _build([ops])


def two_cell_party_table():
    """A two-cell party row: party name (left) | registered address (right) on the
    same baseline. A single conceptual row that today reads as one line."""
    ops = [
        _line_op("Disclosing Party:", 72, 700, FS),
        _line_op("Acme Holdings Limited", 72, 680, FS),
        _line_op("Registered Office:", 340, 700, FS),
        _line_op("1 High Street, London EC1A 1AA", 340, 680, FS),
    ]
    return _build([ops])


def numbered_list_far_left_number():
    """A numbered list whose marker sits far to the left of the wrapped text
    (hanging indent). The lone left-most number must not be read as a column."""
    ops = [
        _line_op("1.", 72, 700, FS),
        _line_op("The Receiving Party shall keep the Confidential Information", 108, 700, FS),
        _line_op("secret and shall not disclose it to any third party.", 108, 685, FS),
        _line_op("2.", 72, 655, FS),
        _line_op("The Receiving Party shall use the Confidential Information", 108, 655, FS),
        _line_op("only for the Purpose described in this Agreement.", 108, 640, FS),
    ]
    return _build([ops])


def right_aligned_page_number_footer():
    """A footer line on the left and a right-aligned page number on the SAME
    baseline. Both belong to the page footer; they read as one line today."""
    ops = _column_block(
        [
            "This Agreement may be executed in counterparts, each of which shall",
            "be deemed an original and all of which together constitute one instrument.",
        ],
        72, 700,
    )
    footer_left = "Mutual NDA -- Confidential"
    ops.append(_line_op(footer_left, 72, 72, 9))
    page_no = "Page 1 of 1"
    px = MEDIA_W - 72 - _advance(page_no, 9)
    ops.append(_line_op(page_no, round(px, 2), 72, 9))
    return _build([ops])


def justified_large_interword():
    """Fully justified prose whose inter-word gaps are stretched wide (drawn word
    by word with large gaps). Big inter-word whitespace must NOT be read as a
    column gutter."""
    words = [
        "The", "Receiving", "Party", "shall", "hold", "the", "Confidential",
        "Information", "in", "strict", "confidence", "and", "shall", "not",
        "use", "it", "except", "for", "the", "Purpose.",
    ]
    ops = []
    # First justified line: spread words across the full text width with big gaps.
    x = 72
    gap = 34  # wide inter-word gap typical of justified short lines
    for w in words[:8]:
        ops.append(_line_op(w, round(x, 2), 700, FS))
        x += _advance(w, FS) + gap
    x = 72
    for w in words[8:16]:
        ops.append(_line_op(w, round(x, 2), 682, FS))
        x += _advance(w, FS) + gap
    x = 72
    for w in words[16:]:
        ops.append(_line_op(w, round(x, 2), 664, FS))
        x += _advance(w, FS) + 6
    return _build([ops])


def definitions_table_term_definition():
    """A definitions table: a narrow TERM column and a wide DEFINITION column.

    DOCUMENTED DECISION: this is deliberately ambiguous. It is classified as a
    NEGATIVE/TRAP -- extraction must stay byte-identical. Rationale: the two-cell
    'term | definition' shape is visually indistinguishable from the two-cell
    party/address and signature-block traps above, and reads as coherent English
    when joined ('"Confidential Information" means ...'). Splitting it risks the
    catastrophic false positive on every definitions section and every party
    table. The asymmetry says: when a two-cell same-baseline row reads as prose,
    leave it. A true multi-column CLAUSE layout (the positive fixtures) is
    distinguished by a full column of independent baselines on BOTH sides, not a
    single term paired with its own definition."""
    rows = [
        ('"Confidential Information"', "means any non-public information disclosed"),
        ('"Purpose"', "means the evaluation of a potential transaction"),
        ('"Effective Date"', "means the date of last signature below"),
        ('"Representatives"', "means directors, officers, employees and advisers"),
    ]
    ops = []
    y = 700
    for term, definition in rows:
        ops.append(_line_op(term, 72, y, FS))
        ops.append(_line_op(definition, 240, y, FS))
        y -= 30
    return _build([ops])


# --------------------------------------------------------------------------- #
# GARBLE fixtures
# --------------------------------------------------------------------------- #
_SIGNATURE_JITTER = [0.0, 1.4, -1.6, 0.8, -1.1, 1.9, -0.4]


def garble_per_glyph_normal_advance():
    """The DocuSign-style signature overlay: a normal body line plus a per-glyph
    signature block at true advances with baseline jitter. Already handled on main
    by the glyph-fragment detector -> extract_text fallback. Included as the
    regression anchor for the case that IS fixed."""
    body = [
        _line_op("The parties agree to keep all Confidential Information secret at all", 72, 720, FS),
        _line_op("times during the term of this Agreement.", 72, 705, FS),
    ]
    sig = []
    sig += _per_glyph_ops("Moorwand Limited", 72, 640, 10, jitter=_SIGNATURE_JITTER)
    sig += _per_glyph_ops("Signed: _______________", 72, 620, 10, jitter=_SIGNATURE_JITTER)
    sig += _per_glyph_ops("Briane Lucas", 300, 620, 10, jitter=_SIGNATURE_JITTER)
    sig += _per_glyph_ops("CEO", 300, 590, 10, jitter=[0.0, -3.2, -6.4])
    sig += _per_glyph_ops("Authorised Signatory", 72, 560, 10, jitter=_SIGNATURE_JITTER)
    return _build([body + sig])


def garble_letter_spaced_tracking():
    """Expanded-tracking title: 'IN WITNESS WHEREOF' drawn per-glyph with WIDE
    extra inter-glyph tracking, so pypdf's own extract_text re-inserts spaces
    between the letters ('I N  W I T N E S S ...'). This is gap (a): the
    glyph-fragment fallback reproduces the garble. A long single-glyph run is
    present, so it routes to the extract_text fallback today."""
    ops = _per_glyph_ops("IN WITNESS WHEREOF the parties have executed", 72, 700, 12,
                         extra_tracking=4.0)
    # A little following body so the page is not garble-only.
    ops += [_line_op("this Agreement as of the Effective Date.", 72, 670, 12)]
    return _build([ops])


def garble_ligature_kern_pairs():
    """'IN WITNESS WHEREOF' drawn as 2-3 character chunks (kern pairs / ligature
    clusters) with wide inter-chunk tracking. Because no run of 6 CONSECUTIVE
    single glyphs occurs, the glyph-fragment detector never fires and the chunks
    route to the ' '.join path -- producing spaced garble that today goes
    UNDETECTED. This is gap (b)."""
    chunks = ["IN", "WI", "TN", "ES", "S", "WH", "ER", "EO", "F", "the", "par", "ties"]
    ops = []
    x = 72
    for ch in chunks:
        ops.append(_line_op(ch, round(x, 2), 700, 12))
        x += _advance(ch, 12) + 10  # wide inter-chunk gap
    ops += [_line_op("have duly executed this Agreement on the date shown.", 72, 675, 12)]
    return _build([ops])


def garble_spaced_nda_title_legit():
    """A legitimately letter-spaced title 'N D A' plus normal body. The spaced
    title is real typographic tracking, NOT garble, and must NOT be flagged. The
    guardrail against over-eager garble detection."""
    ops = [
        _line_op("N D A", 72, 720, 18),
        _line_op("This Non-Disclosure Agreement sets out the terms on which the parties", 72, 690, 12),
        _line_op("will exchange confidential information for the stated Purpose.", 72, 672, 12),
    ]
    return _build([ops])


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
# name -> (builder, category, expected_behavior)
FIXTURES = {
    # POSITIVE -- fix must reorder; baseline captures today's scrambled output.
    "pos_two_column_clean": (
        two_column_clean, "positive",
        "2-page two-column NDA; correct order is clauses 1..8 down each column "
        "then across. Baseline interleaves across the gutter.",
    ),
    "pos_two_column_unbalanced_last": (
        two_column_unbalanced_last, "positive",
        "Two-column, left column full / right column half. Fix must read left "
        "column fully then right, without fabricating a right partner for the "
        "trailing left-only lines.",
    ),
    "pos_table_3col_5row": (
        table_3col_5row, "positive",
        "3-col x 5-row table; fix must keep cells from fusing across columns into "
        "one line.",
    ),
    "pos_stamped_executed_overlay": (
        stamped_executed_overlay, "positive",
        "'EXECUTED' stamp on a body baseline; fix must not splice the stamp into "
        "the sentence.",
    ),
    "pos_ctm_translated_overlap": (
        ctm_translated_overlap, "positive",
        "cm-translated block vs untranslated block; fix must honor the CTM so the "
        "two blocks do not collapse onto one baseline.",
    ),
    # NEGATIVE / TRAP -- extraction output MUST stay byte-identical.
    "neg_single_col_wide_margins_centered_title": (
        single_col_wide_margins_centered_title, "negative",
        "Single column, wide margins, centered title. Must NOT be split.",
    ),
    "neg_signature_name_left_date_right": (
        signature_block_name_left_date_right, "negative",
        "'Name: ___' left / 'Date: ___' right on one baseline. One logical line; "
        "must stay merged.",
    ),
    "neg_two_cell_party_table": (
        two_cell_party_table, "negative",
        "Party name | registered address two-cell row. Must stay as-is.",
    ),
    "neg_numbered_list_far_left_number": (
        numbered_list_far_left_number, "negative",
        "Hanging-indent numbered list; lone left number must not be read as a "
        "column.",
    ),
    "neg_right_aligned_page_number": (
        right_aligned_page_number_footer, "negative",
        "Footer text left + right-aligned page number on one baseline. Must stay "
        "as-is.",
    ),
    "neg_justified_large_interword": (
        justified_large_interword, "negative",
        "Justified prose with wide inter-word gaps. Big whitespace must not read "
        "as a gutter.",
    ),
    "neg_definitions_table_term_definition": (
        definitions_table_term_definition, "negative",
        "Term | definition table -- DELIBERATELY classified negative (see builder "
        "docstring). Must stay byte-identical.",
    ),
    # GARBLE
    "garble_per_glyph_normal_advance": (
        garble_per_glyph_normal_advance, "garble_fixed",
        "Per-glyph signature overlay -- already handled on main (glyph-fragment "
        "detector). Regression anchor.",
    ),
    "garble_letter_spaced_tracking": (
        garble_letter_spaced_tracking, "garble_open_a",
        "Expanded-tracking 'IN WITNESS WHEREOF'; extract_text fallback re-inserts "
        "spaces -> garble. Confidence signal must fire (gap a).",
    ),
    "garble_ligature_kern_pairs": (
        garble_ligature_kern_pairs, "garble_open_b",
        "2-3 char kern-pair chunks under the 6-single-glyph threshold; routes to "
        "' '.join, undetected today. Confidence signal must fire (gap b).",
    ),
    "garble_spaced_nda_title_legit": (
        garble_spaced_nda_title_legit, "garble_trap",
        "Legit 'N D A' spaced title. Must NOT be flagged as garble (guardrail).",
    ),
}


def write_all(target_dir=HERE):
    written = {}
    for name, (builder, _cat, _beh) in FIXTURES.items():
        data = builder()
        path = os.path.join(target_dir, f"{name}.pdf")
        with open(path, "wb") as fh:
            fh.write(data)
        written[name] = path
    return written


if __name__ == "__main__":
    paths = write_all()
    for name in sorted(paths):
        print(f"wrote {os.path.relpath(paths[name], HERE)}")
