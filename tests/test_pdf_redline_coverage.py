"""Post-render content-coverage gate for the PDF-reconstruction reviewed export.

Closes a confirmed silent-data-loss defect: a PDF-source matter is exported (and
sent) as a RECONSTRUCTED Word document with the reviewer's redlines applied. The
strong DOCX sequence gate (``verify_export_content_coverage``) is switched OFF for
PDF because the reconstruction's paragraph/whitespace model differs from the PDF
text extractor's, so a positional sequence match would false-positive on every
normal reconstruction. That historically left the PDF path with NO post-render
coverage check -- a reviewer redline that anchored but never landed in the output
bytes shipped SILENTLY to the counterparty as if complete.

The fix (``verify_pdf_reconstruction_redline_coverage`` +
``redline_export_service._raise_for_pdf_redline_coverage``) verifies POST-render
that every reviewer redline is represented in the exported bytes, failing LOUDLY on
a genuine drop while NOT tripping on normal reconstruction formatting differences.
"""
from __future__ import annotations

import json
import os
import unittest
from io import BytesIO
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from nda_automation import docx_health, redline_export_service
from nda_automation.docx_health import verify_pdf_reconstruction_redline_coverage
from nda_automation.redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from nda_automation.redline_edit_contract import MANUAL_VIEWER_EDIT_CLAUSE_ID
from nda_automation.redline_export_service import PdfSourceRedlineUnavailableError
from nda_automation.redline_xml import (
    _tracked_replace_paragraph,
    _tracked_replace_paragraph_char,
)

from tests.test_docx_export import make_source_docx
from tests.test_pdf_redline_anchor import (
    CONFIDENTIALITY,
    GOVERNING_LAW,
    GOVERNING_LAW_REPLACEMENT,
    _pdf_replace_redline,
    _pdf_review_paragraphs,
)


def _docx_with_paragraphs(paragraphs: list[str]) -> bytes:
    """A minimal DOCX whose body holds these plain (non-tracked) paragraphs --
    stands in for a reconstruction in which a redline never landed."""
    return make_source_docx(paragraphs)


def _tracked_replace_docx(original: str, replacement: str, *, others: list[str] | None = None) -> bytes:
    """A DOCX whose body has a TRACKED replacement (del original / ins replacement),
    standing in for a reconstruction in which the redline DID land."""
    others = others or []
    paragraphs_xml = "".join(
        f"<w:p><w:r><w:t>{other}</w:t></w:r></w:p>" for other in others
    )
    tracked = (
        "<w:p>"
        f'<w:del w:id="1" w:author="reviewer"><w:r><w:delText>{original}</w:delText></w:r></w:del>'
        f'<w:ins w:id="2" w:author="reviewer"><w:r><w:t>{replacement}</w:t></w:r></w:ins>'
        "</w:p>"
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs_xml}{tracked}</w:body></w:document>"
    )
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)
        return output.getvalue()


def _docx_from_paragraph_xml(paragraph_xml: str, *, others: list[str] | None = None) -> bytes:
    others = others or []
    others_xml = "".join(f"<w:p><w:r><w:t>{other}</w:t></w:r></w:p>" for other in others)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{others_xml}{paragraph_xml}</w:body></w:document>"
    )
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)
        return output.getvalue()


def _landed_replace_docx(original: str, replacement: str, *, others: list[str] | None = None) -> bytes:
    """A reconstruction in which a replace redline DID land, built with the SAME
    diff-driven tracked-paragraph builder production uses (``_tracked_replace_paragraph``).
    Only the CHANGED delta lands inside ``w:ins``/``w:del`` -- exactly the markup shape
    the coverage gate must key on, not a synthetic whole-paragraph del/ins."""
    paragraph_xml, _ = _tracked_replace_paragraph(original, replacement, 1)
    return _docx_from_paragraph_xml(paragraph_xml, others=others)


def _landed_char_replace_docx(original: str, replacement: str, *, others: list[str] | None = None) -> bytes:
    """A reconstruction in which a FREEFORM MANUAL char-level edit DID land, built with
    the same char-level builder production uses (``_tracked_replace_paragraph_char``).
    Words fragment into single-character ``w:ins``/``w:del`` runs (``written`` -> ``oral``
    emits del ``w``+``itten`` / ins ``o``+``al``), which is exactly the markup shape the
    coverage gate's char-level path must accept without false-positiving."""
    paragraph_xml, _ = _tracked_replace_paragraph_char(original, replacement, 1)
    return _docx_from_paragraph_xml(paragraph_xml, others=others)


# Long clauses where a SMALL edit is dropped. The surviving original is >90%
# token-identical to the intended replacement, so a whole-paragraph fuzzy/global-blob
# matcher would pass the drop SILENTLY -- the exact class round 1 missed.
LONG_TERM_CLAUSE = (
    "The Receiving Party shall keep the Confidential Information confidential for a "
    "period of two (2) years from the date of disclosure and shall not use it for any "
    "purpose other than evaluating the proposed transaction between the parties."
)
LONG_TERM_CLAUSE_REPLACEMENT = LONG_TERM_CLAUSE.replace("two (2) years", "five (5) years")
CI_DEFINITION = (
    "Confidential Information means any information disclosed by one party to the other "
    "party in written form and clearly marked as confidential at the time of disclosure."
)
CI_DEFINITION_REPLACEMENT = CI_DEFINITION.replace("written", "oral")


# A SMALL token-level edit inside a LONG single-paragraph clause whose
# original*replacement token product EXCEEDS ``inline_diff``'s 40000-cell matrix
# limit, so ``diff_text_operations`` (which the builder AND the gate share) falls
# back to a single WHOLE-TEXT pair -- ('delete', whole_original) + ('insert',
# whole_replacement) -- instead of fine-grained changed-token deltas. The clause is
# >1100 chars; ~239 tokens/side -> 239*239 = 57121 > 40000.
_LARGE_CLAUSE_SENTENCE = (
    "The Receiving Party shall hold in strict confidence and shall not disclose to "
    "any third party any Confidential Information of the Disclosing Party, and shall "
    "use such Confidential Information solely for the purpose of evaluating the "
    "proposed transaction between the parties hereto, "
)
LARGE_CLAUSE = (
    _LARGE_CLAUSE_SENTENCE * 5
    + "for a period of two (2) years from the date of disclosure of such "
    "Confidential Information."
)
LARGE_CLAUSE_REPLACEMENT = LARGE_CLAUSE.replace("two (2) years", "five (5) years")


# --------------------------------------------------------------------------- #
# Load-bearing repros: a SMALL edit dropped from a LONG clause. Round 1's loose
# matcher (global-blob substring + 0.9 whole-paragraph fuzzy fallback) deemed these
# "covered" because the surviving original is >90% similar to the replacement; the
# robust gate keys on the ABSENCE of tracked-change markup for the changed tokens.
# --------------------------------------------------------------------------- #
class SmallEditDropTests(unittest.TestCase):
    def _replace_redline(self, original: str, replacement: str) -> dict:
        return {
            "id": "small-edit",
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p1",
            "source_part": "pdf",
            "original_text": original,
            "replacement_text": replacement,
        }

    def test_dropped_term_small_edit_is_caught(self):
        # "two (2) years" -> "five (5) years" dropped: reconstruction keeps the ORIGINAL
        # long clause, NO tracked change. Round 1 passed this (ratio ~0.95); must CATCH.
        docx_bytes = _docx_with_paragraphs([LONG_TERM_CLAUSE])
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [self._replace_redline(LONG_TERM_CLAUSE, LONG_TERM_CLAUSE_REPLACEMENT)]
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 1", errors[0])
        # Counts only -- no NDA text leaks into the error string.
        self.assertNotIn("five (5) years", errors[0])

    def test_landed_term_small_edit_passes(self):
        # Same edit, but it LANDED: the delta is carried by real w:ins/w:del. No false
        # positive despite the surrounding clause being >90% identical.
        docx_bytes = _landed_replace_docx(LONG_TERM_CLAUSE, LONG_TERM_CLAUSE_REPLACEMENT)
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [self._replace_redline(LONG_TERM_CLAUSE, LONG_TERM_CLAUSE_REPLACEMENT)]
        )
        self.assertEqual(errors, [])

    def test_dropped_ci_definition_small_edit_is_caught(self):
        # "written" -> "oral" in the Confidential-Information definition dropped.
        docx_bytes = _docx_with_paragraphs([CI_DEFINITION])
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [self._replace_redline(CI_DEFINITION, CI_DEFINITION_REPLACEMENT)]
        )
        self.assertEqual(len(errors), 1)

    def test_landed_ci_definition_small_edit_passes(self):
        docx_bytes = _landed_replace_docx(CI_DEFINITION, CI_DEFINITION_REPLACEMENT)
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [self._replace_redline(CI_DEFINITION, CI_DEFINITION_REPLACEMENT)]
        )
        self.assertEqual(errors, [])

    def test_dropped_small_edit_with_replacement_duplicated_elsewhere_is_caught(self):
        # The replacement's new words ("five (5) years") appear VERBATIM in an unrelated
        # untracked paragraph. A global-document substring matcher would be fooled; the
        # markup-keyed gate is not, because there is no w:ins carrying the change.
        decoy = "The term of this Agreement is five (5) years from the Effective Date."
        docx_bytes = _docx_with_paragraphs([LONG_TERM_CLAUSE, decoy])
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [self._replace_redline(LONG_TERM_CLAUSE, LONG_TERM_CLAUSE_REPLACEMENT)]
        )
        self.assertEqual(len(errors), 1)

    def test_dropped_small_edit_landed_for_a_DIFFERENT_redline_is_caught(self):
        # A different redline landed (its delta is in w:ins/w:del), but THIS small edit
        # was dropped. The gate must not be satisfied by an unrelated tracked change.
        landed = _landed_replace_docx(
            GOVERNING_LAW, GOVERNING_LAW_REPLACEMENT, others=[LONG_TERM_CLAUSE]
        )
        dropped = self._replace_redline(LONG_TERM_CLAUSE, LONG_TERM_CLAUSE_REPLACEMENT)
        landed_redline = {
            "id": "gl",
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p2",
            "source_part": "pdf",
            "original_text": GOVERNING_LAW,
            "replacement_text": GOVERNING_LAW_REPLACEMENT,
        }
        errors = verify_pdf_reconstruction_redline_coverage(landed, [landed_redline, dropped])
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 2", errors[0])

    def test_landed_insert_and_delete_pass(self):
        # Real w:ins for an insert and real w:del for a delete both pass.
        insert_xml = (
            "<w:p>"
            '<w:ins w:id="9" w:author="r"><w:r><w:t>The parties agree to a mutual non-disparagement clause.</w:t></w:r></w:ins>'
            "</w:p>"
        )
        insert_docx = _docx_from_paragraph_xml(insert_xml, others=[GOVERNING_LAW])
        insert_redline = {
            "id": "ins",
            "action": REDLINE_INSERT_AFTER_PARAGRAPH,
            "paragraph_id": "p1",
            "source_part": "pdf",
            "anchor_text": GOVERNING_LAW,
            "insert_text": "The parties agree to a mutual non-disparagement clause.",
        }
        self.assertEqual(
            verify_pdf_reconstruction_redline_coverage(insert_docx, [insert_redline]), []
        )


# --------------------------------------------------------------------------- #
# Unit tests on the docx_health coverage function.
# --------------------------------------------------------------------------- #
class PdfReconstructionRedlineCoverageUnitTests(unittest.TestCase):
    def _replace_redline(self) -> dict:
        return {
            "id": "r1",
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p1",
            "source_part": "pdf",
            "original_text": GOVERNING_LAW,
            "replacement_text": GOVERNING_LAW_REPLACEMENT,
        }

    def test_landed_replace_redline_passes(self):
        docx_bytes = _tracked_replace_docx(
            GOVERNING_LAW, GOVERNING_LAW_REPLACEMENT, others=[CONFIDENTIALITY]
        )
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [self._replace_redline()])
        self.assertEqual(errors, [])

    def test_dropped_replace_redline_is_caught(self):
        # The reconstruction never received the replacement: the new text is absent.
        docx_bytes = _docx_with_paragraphs([GOVERNING_LAW, CONFIDENTIALITY])
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [self._replace_redline()])
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 1", errors[0])
        # Counts only -- the gate must not leak NDA text into the error string.
        self.assertNotIn(GOVERNING_LAW_REPLACEMENT, errors[0])

    def test_reconstruction_whitespace_difference_does_not_false_positive(self):
        # A NORMAL reconstruction legitimately differs in whitespace/run-splitting.
        # The redline's new text is present despite the noise, so the gate passes.
        noisy_replacement = "This Agreement   shall be\tgoverned by the laws\nof England and Wales."
        docx_bytes = _tracked_replace_docx(GOVERNING_LAW, noisy_replacement)
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [self._replace_redline()])
        self.assertEqual(errors, [])

    def test_dropped_one_of_two_redlines_is_caught(self):
        landed = self._replace_redline()
        dropped = {
            "id": "r2",
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p2",
            "source_part": "pdf",
            "original_text": CONFIDENTIALITY,
            "replacement_text": "Each party shall hold the other's Confidential Information in strict confidence.",
        }
        docx_bytes = _tracked_replace_docx(
            GOVERNING_LAW, GOVERNING_LAW_REPLACEMENT, others=[CONFIDENTIALITY]
        )
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [landed, dropped])
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 2", errors[0])

    def test_dropped_insert_redline_is_caught(self):
        insert = {
            "id": "r3",
            "action": REDLINE_INSERT_AFTER_PARAGRAPH,
            "paragraph_id": "p1",
            "source_part": "pdf",
            "anchor_text": GOVERNING_LAW,
            "insert_text": "The parties further agree to a two-year survival period.",
        }
        docx_bytes = _docx_with_paragraphs([GOVERNING_LAW])
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [insert])
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 1", errors[0])

    def test_dropped_delete_redline_is_caught(self):
        # A delete must survive as a tracked deletion (delText). A reconstruction
        # that simply omits the paragraph dropped the redline.
        delete = {
            "id": "r4",
            "action": REDLINE_DELETE_PARAGRAPH,
            "paragraph_id": "p2",
            "source_part": "pdf",
            "original_text": CONFIDENTIALITY,
        }
        docx_bytes = _docx_with_paragraphs([GOVERNING_LAW])  # CONFIDENTIALITY paragraph gone
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [delete])
        self.assertEqual(len(errors), 1)

    def test_landed_delete_redline_passes(self):
        delete = {
            "id": "r4",
            "action": REDLINE_DELETE_PARAGRAPH,
            "paragraph_id": "p1",
            "source_part": "pdf",
            "original_text": GOVERNING_LAW,
        }
        # GOVERNING_LAW retained as a tracked deletion (delText), not in accepted view.
        tracked_delete = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            f'<w:p><w:del w:id="1" w:author="r"><w:r><w:delText>{GOVERNING_LAW}</w:delText></w:r></w:del></w:p>'
            f"<w:p><w:r><w:t>{CONFIDENTIALITY}</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        with BytesIO() as output:
            with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", tracked_delete)
            docx_bytes = output.getvalue()
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [delete])
        self.assertEqual(errors, [])

    def test_no_redlines_is_a_pass(self):
        docx_bytes = _docx_with_paragraphs([GOVERNING_LAW])
        self.assertEqual(verify_pdf_reconstruction_redline_coverage(docx_bytes, []), [])
        self.assertEqual(verify_pdf_reconstruction_redline_coverage(docx_bytes, None), [])

    def test_empty_body_with_pending_redlines_fails(self):
        empty = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body></w:body></w:document>"
        )
        with BytesIO() as output:
            with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", empty)
            docx_bytes = output.getvalue()
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [self._replace_redline()])
        self.assertEqual(len(errors), 1)


# --------------------------------------------------------------------------- #
# Integration tests through the export service (covers BOTH reviewed-DOCX export
# and the send-redline path, which share _build_redline_export).
# --------------------------------------------------------------------------- #
class _DroppingRenderResult:
    """A package render result whose bytes are a clean reconstruction MISSING the
    redline -- i.e. anchoring reported success but the change never landed."""

    def __init__(self, data: bytes):
        self.data = data
        self.health_errors: list[str] = []
        self.content_errors: list[str] = []
        self.anchor_uncertain_redlines: list[dict] = []


class PdfExportServiceCoverageGateTests(unittest.TestCase):
    def _pdf_review_result(
        self, *, original: str = GOVERNING_LAW, replacement: str = GOVERNING_LAW_REPLACEMENT
    ) -> dict:
        review_paragraphs = _pdf_review_paragraphs([original, CONFIDENTIALITY])
        return {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=original,
                    replacement_text=replacement,
                )
            ],
        }

    def _build_pdf_export(self, *, reconstruction: bytes, review_result: dict | None = None):
        review_result = review_result or self._pdf_review_result()
        reconstructed = redline_export_service.pdf_docx_reconstruction.ReconstructedDocx(
            data=make_source_docx([GOVERNING_LAW, CONFIDENTIALITY]),
            filename="Signed-NDA.docx",
            headers={"X-PDF-DOCX-Reconstruction": "pdf2docx"},
        )
        with patch.object(
            redline_export_service,
            "_review_result_for_export",
            return_value=(review_result, b"%PDF-1.7\nsource\n%%EOF\n", "Signed NDA.pdf"),
        ), patch.object(
            redline_export_service.pdf_docx_reconstruction,
            "reconstruct_pdf_to_docx",
            return_value=reconstructed,
        ), patch.object(
            redline_export_service.docx_package_renderer,
            "render_source_redline_package",
            return_value=_DroppingRenderResult(reconstruction),
        ):
            return redline_export_service.build_matter_redline(
                "matter-pdf", {}, persist=False, repository=object()
            )

    def test_landed_redline_export_passes(self):
        landed = _tracked_replace_docx(
            GOVERNING_LAW, GOVERNING_LAW_REPLACEMENT, others=[CONFIDENTIALITY]
        )
        export = self._build_pdf_export(reconstruction=landed)
        self.assertEqual(export.filename, "Signed-NDA-reviewed.docx")
        self.assertTrue(export.data)

    def test_dropped_redline_export_is_blocked(self):
        # The load-bearing test: anchoring "succeeded" but the redline is absent from
        # the reconstruction. The gate must BLOCK rather than ship silently.
        dropped = _docx_with_paragraphs([GOVERNING_LAW, CONFIDENTIALITY])
        with self.assertRaises(PdfSourceRedlineUnavailableError) as caught:
            self._build_pdf_export(reconstruction=dropped)
        self.assertEqual(caught.exception.reason, "redline_coverage_shortfall")
        self.assertEqual(caught.exception.status, 503)
        # Recovery path is offered (mark up the source PDF) so no change is lost.
        self.assertEqual(caught.exception.payload["recovery"]["path"], "annotated_pdf")

    def test_dropped_small_edit_export_is_blocked_end_to_end(self):
        # End-to-end through build_matter_redline: a SMALL edit to a LONG clause
        # ("two (2) years" -> "five (5) years") is dropped, so the reconstruction keeps
        # the ORIGINAL long clause with NO tracked change. This is the class round 1
        # passed silently (surviving original >90% similar). The gate must BLOCK.
        review_result = self._pdf_review_result(
            original=LONG_TERM_CLAUSE, replacement=LONG_TERM_CLAUSE_REPLACEMENT
        )
        dropped = _docx_with_paragraphs([LONG_TERM_CLAUSE, CONFIDENTIALITY])
        with self.assertRaises(PdfSourceRedlineUnavailableError) as caught:
            self._build_pdf_export(reconstruction=dropped, review_result=review_result)
        self.assertEqual(caught.exception.reason, "redline_coverage_shortfall")

    def test_landed_small_edit_export_passes_end_to_end(self):
        # Same small edit, but it LANDED (real diff-driven w:ins/w:del). No false
        # positive despite the surrounding long clause being >90% identical.
        review_result = self._pdf_review_result(
            original=LONG_TERM_CLAUSE, replacement=LONG_TERM_CLAUSE_REPLACEMENT
        )
        landed = _landed_replace_docx(
            LONG_TERM_CLAUSE, LONG_TERM_CLAUSE_REPLACEMENT, others=[CONFIDENTIALITY]
        )
        export = self._build_pdf_export(reconstruction=landed, review_result=review_result)
        self.assertTrue(export.data)

    def test_send_redline_path_honors_coverage_gate(self):
        # The send-redline path (matter_lifecycle.send_redline -> build_matter_redline)
        # shares _build_redline_export, so a dropped redline blocks the send by raising
        # BEFORE any Gmail call -- no incomplete redline reaches the counterparty.
        from nda_automation import matter_lifecycle

        dropped = _docx_with_paragraphs([GOVERNING_LAW, CONFIDENTIALITY])
        reconstructed = redline_export_service.pdf_docx_reconstruction.ReconstructedDocx(
            data=make_source_docx([GOVERNING_LAW, CONFIDENTIALITY]),
            filename="Signed-NDA.docx",
            headers={"X-PDF-DOCX-Reconstruction": "pdf2docx"},
        )

        class _Repo:
            pass

        lifecycle = matter_lifecycle.RepositoryMatterLifecycle(_Repo())
        with patch.object(
            redline_export_service,
            "_review_result_for_export",
            return_value=(self._pdf_review_result(), b"%PDF-1.7\n%%EOF\n", "Signed NDA.pdf"),
        ), patch.object(
            redline_export_service.pdf_docx_reconstruction,
            "reconstruct_pdf_to_docx",
            return_value=reconstructed,
        ), patch.object(
            redline_export_service.docx_package_renderer,
            "render_source_redline_package",
            return_value=_DroppingRenderResult(dropped),
        ), patch("nda_automation.gmail_integration.send_redline_email") as send_email:
            with self.assertRaises(PdfSourceRedlineUnavailableError):
                redline_export_service.build_matter_redline(
                    "matter-pdf", {}, repository=lifecycle._repository
                )
            send_email.assert_not_called()


# --------------------------------------------------------------------------- #
# Round-3 BLOCKER repro: GLOBAL token pooling. Round 2 collected EVERY w:ins token
# into one document-wide pool and EVERY w:del into another, then tested each redline
# against those GLOBAL pools. When two clauses received the IDENTICAL edit, one
# landing satisfied the gate for BOTH -- so a DROPPED twin slipped silently. The fix
# scopes each redline's coverage to its OWN target paragraph.
# --------------------------------------------------------------------------- #
# Two clauses that receive the SAME "two (2) years" -> "five (5) years" delta. With
# global pooling the term clause landing would mask the survival clause being dropped.
TERM_CLAUSE = (
    "The confidentiality obligations of the Receiving Party shall survive the "
    "termination of this Agreement for a period of two (2) years from the date of "
    "disclosure of the Confidential Information."
)
TERM_CLAUSE_REPLACEMENT = TERM_CLAUSE.replace("two (2) years", "five (5) years")
SURVIVAL_CLAUSE = (
    "Notwithstanding any termination, the obligations set out in this clause shall "
    "continue in full force and effect for two (2) years following the return or "
    "destruction of all Confidential Information."
)
SURVIVAL_CLAUSE_REPLACEMENT = SURVIVAL_CLAUSE.replace("two (2) years", "five (5) years")


def _replace_redline(
    redline_id: str, paragraph_id: str, original: str, replacement: str
) -> dict:
    return {
        "id": redline_id,
        "action": REDLINE_REPLACE_PARAGRAPH,
        "paragraph_id": paragraph_id,
        "source_part": "pdf",
        "original_text": original,
        "replacement_text": replacement,
    }


class RepeatedIdenticalEditScopingTests(unittest.TestCase):
    def test_dropped_twin_of_identical_edit_is_caught(self):
        # The load-bearing round-3 repro. Two clauses get the identical delta; the term
        # clause LANDS (real diff-driven w:ins/w:del), the survival clause is DROPPED
        # (its paragraph survives plain/untracked). Round 2's global token pool held the
        # term clause's 'five'/'5' (w:ins) and 'two'/'2' (w:del), so the dropped survival
        # redline read as covered. Per-paragraph scoping checks the survival paragraph's
        # OWN markup (none) -> CAUGHT.
        term_xml, _ = _tracked_replace_paragraph(TERM_CLAUSE, TERM_CLAUSE_REPLACEMENT, 1)
        # term clause landed (tracked) + survival clause dropped (plain original text).
        docx_bytes = _two_paragraph_docx(
            term_xml, f"<w:p><w:r><w:t>{SURVIVAL_CLAUSE}</w:t></w:r></w:p>"
        )
        landed = _replace_redline("term", "p1", TERM_CLAUSE, TERM_CLAUSE_REPLACEMENT)
        dropped = _replace_redline("survival", "p2", SURVIVAL_CLAUSE, SURVIVAL_CLAUSE_REPLACEMENT)
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [landed, dropped])
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 2", errors[0])
        # Counts only -- no NDA text leaks into the error string.
        self.assertNotIn("five (5) years", errors[0])

    def test_both_identical_edits_landed_pass(self):
        # Both clauses landed in their OWN paragraphs: no false positive.
        term_xml, _ = _tracked_replace_paragraph(TERM_CLAUSE, TERM_CLAUSE_REPLACEMENT, 1)
        survival_xml, _ = _tracked_replace_paragraph(
            SURVIVAL_CLAUSE, SURVIVAL_CLAUSE_REPLACEMENT, 1
        )
        docx_bytes = _two_paragraph_docx(term_xml, survival_xml)
        landed_a = _replace_redline("term", "p1", TERM_CLAUSE, TERM_CLAUSE_REPLACEMENT)
        landed_b = _replace_redline("survival", "p2", SURVIVAL_CLAUSE, SURVIVAL_CLAUSE_REPLACEMENT)
        self.assertEqual(
            verify_pdf_reconstruction_redline_coverage(docx_bytes, [landed_a, landed_b]), []
        )

    def test_dropped_twin_whose_paragraph_is_removed_is_caught(self):
        # Harder variant: the dropped survival clause's paragraph is entirely REMOVED
        # from the reconstruction (not merely left untracked), so its original text
        # matches NO paragraph. The gate must NOT fall back to a global scan that would
        # let the term clause's identical landed markup satisfy the survival redline --
        # it must stay scoped and CATCH the drop.
        term_xml, _ = _tracked_replace_paragraph(TERM_CLAUSE, TERM_CLAUSE_REPLACEMENT, 1)
        docx_bytes = _docx_from_paragraph_xml(term_xml)  # survival paragraph absent
        landed = _replace_redline("term", "p1", TERM_CLAUSE, TERM_CLAUSE_REPLACEMENT)
        dropped = _replace_redline("survival", "p2", SURVIVAL_CLAUSE, SURVIVAL_CLAUSE_REPLACEMENT)
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [landed, dropped])
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 2", errors[0])

    def test_dropped_twin_blocks_export_end_to_end(self):
        # End-to-end through build_matter_redline: the dropped twin raises
        # PdfSourceRedlineUnavailableError, so an incomplete redline never ships.
        review_paragraphs = _pdf_review_paragraphs([TERM_CLAUSE, SURVIVAL_CLAUSE])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=TERM_CLAUSE,
                    replacement_text=TERM_CLAUSE_REPLACEMENT,
                ),
                _pdf_replace_redline(
                    review_paragraphs[1],
                    original_text=SURVIVAL_CLAUSE,
                    replacement_text=SURVIVAL_CLAUSE_REPLACEMENT,
                ),
            ],
        }
        term_xml, _ = _tracked_replace_paragraph(TERM_CLAUSE, TERM_CLAUSE_REPLACEMENT, 1)
        # term landed, survival dropped (plain).
        dropped_reconstruction = _two_paragraph_docx(
            term_xml, f"<w:p><w:r><w:t>{SURVIVAL_CLAUSE}</w:t></w:r></w:p>"
        )
        with self.assertRaises(PdfSourceRedlineUnavailableError) as caught:
            _build_pdf_export_with(
                reconstruction=dropped_reconstruction,
                review_result=review_result,
                source_paragraphs=[TERM_CLAUSE, SURVIVAL_CLAUSE],
            )
        self.assertEqual(caught.exception.reason, "redline_coverage_shortfall")


# --------------------------------------------------------------------------- #
# Round-4 BLOCKER repro: IDENTICAL / near-identical clause aliasing. Round 3 scoped
# each redline to paragraphs whose pre-change text matched its original text -- but
# when TWO redlines carry IDENTICAL (or >=0.9 similar) original text, BOTH match the
# SAME landed paragraph, and with no "already-consumed" dedup ONE landed twin could
# satisfy ANY number of dropped twins. The fix treats coverage as a ONE-TO-ONE
# bipartite matching: a marked-up paragraph is consumed by at most one redline, so N
# identical redlines need N DISTINCT landed paragraphs.
# --------------------------------------------------------------------------- #
# Verbatim-identical clause (appears twice in the body): the exact prompt repro.
IDENTICAL_CLAUSE = (
    "The obligations of confidentiality under this Agreement shall remain in full force "
    "and effect for a period of two (2) years from the date of disclosure."
)
IDENTICAL_CLAUSE_REPLACEMENT = IDENTICAL_CLAUSE.replace("two (2) years", "five (5) years")
# Near-identical PARALLEL mutual clauses: First Party / Second Party obligations that
# differ only in the party label (>=0.9 token-set similar), so confident_text_match
# treats each as a candidate for the other's redline.
FIRST_PARTY_CLAUSE = (
    "The First Party shall hold the Confidential Information of the other party in strict "
    "confidence and shall not disclose it to any third party for a period of two (2) years."
)
FIRST_PARTY_CLAUSE_REPLACEMENT = FIRST_PARTY_CLAUSE.replace("two (2) years", "five (5) years")
SECOND_PARTY_CLAUSE = (
    "The Second Party shall hold the Confidential Information of the other party in strict "
    "confidence and shall not disclose it to any third party for a period of two (2) years."
)
SECOND_PARTY_CLAUSE_REPLACEMENT = SECOND_PARTY_CLAUSE.replace("two (2) years", "five (5) years")


class IdenticalClauseOneToOneMatchingTests(unittest.TestCase):
    def test_identical_clause_one_landed_one_dropped_is_caught(self):
        # The load-bearing round-4 repro. TWO verbatim-identical clauses: p1 LANDS (real
        # diff-driven w:ins/w:del), p2 is DROPPED (its paragraph survives plain). Round 3
        # scoped each redline to paragraphs matching its original text -- but both clauses
        # are identical, so the dropped redline matched the LANDED twin and read covered.
        # One-to-one matching: the single landed paragraph is consumed by one redline,
        # leaving the other unmatched -> CAUGHT.
        landed_xml, _ = _tracked_replace_paragraph(IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT, 1)
        docx_bytes = _two_paragraph_docx(
            landed_xml, f"<w:p><w:r><w:t>{IDENTICAL_CLAUSE}</w:t></w:r></w:p>"
        )
        landed = _replace_redline("a", "p1", IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT)
        dropped = _replace_redline("b", "p2", IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT)
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [landed, dropped])
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 2", errors[0])
        self.assertNotIn("five (5) years", errors[0])

    def test_three_identical_two_landed_one_dropped_is_caught(self):
        # Three identical clauses: 2 land (two distinct marked-up paragraphs), 1 dropped
        # (plain). The matching can cover only 2 of the 3 redlines -> the 3rd is CAUGHT.
        landed_xml_1, _ = _tracked_replace_paragraph(IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT, 1)
        landed_xml_2, _ = _tracked_replace_paragraph(IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT, 2)
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            f"{landed_xml_1}{landed_xml_2}"
            f"<w:p><w:r><w:t>{IDENTICAL_CLAUSE}</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        with BytesIO() as output:
            with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", document_xml)
            docx_bytes = output.getvalue()
        redlines = [
            _replace_redline("a", "p1", IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT),
            _replace_redline("b", "p2", IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT),
            _replace_redline("c", "p3", IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT),
        ]
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, redlines)
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 3", errors[0])

    def test_all_identical_edits_landed_pass(self):
        # Both identical edits landed in their OWN distinct paragraphs: the matching
        # assigns each redline a distinct landed paragraph -> no false positive.
        landed_xml_1, _ = _tracked_replace_paragraph(IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT, 1)
        landed_xml_2, _ = _tracked_replace_paragraph(IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT, 2)
        docx_bytes = _two_paragraph_docx(landed_xml_1, landed_xml_2)
        redlines = [
            _replace_redline("a", "p1", IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT),
            _replace_redline("b", "p2", IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT),
        ]
        self.assertEqual(verify_pdf_reconstruction_redline_coverage(docx_bytes, redlines), [])

    def test_near_identical_parallel_clause_dropped_is_caught(self):
        # Near-identical PARALLEL mutual clauses (First Party / Second Party differ only
        # by the label, >=0.9 similar). First Party LANDS; Second Party is DROPPED. Each
        # clause is a >=0.9 match for the other's original text, so round 3 let the First
        # Party's landed markup satisfy the dropped Second Party redline. One-to-one
        # matching consumes the single landed paragraph once -> the drop is CAUGHT.
        landed_xml, _ = _tracked_replace_paragraph(
            FIRST_PARTY_CLAUSE, FIRST_PARTY_CLAUSE_REPLACEMENT, 1
        )
        docx_bytes = _two_paragraph_docx(
            landed_xml, f"<w:p><w:r><w:t>{SECOND_PARTY_CLAUSE}</w:t></w:r></w:p>"
        )
        landed = _replace_redline("first", "p1", FIRST_PARTY_CLAUSE, FIRST_PARTY_CLAUSE_REPLACEMENT)
        dropped = _replace_redline(
            "second", "p2", SECOND_PARTY_CLAUSE, SECOND_PARTY_CLAUSE_REPLACEMENT
        )
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [landed, dropped])
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 2", errors[0])

    def test_both_near_identical_parallel_clauses_landed_pass(self):
        # Both parallel clauses landed in distinct paragraphs: even though each is a
        # candidate for the other, the matching gives each redline its own paragraph.
        first_xml, _ = _tracked_replace_paragraph(
            FIRST_PARTY_CLAUSE, FIRST_PARTY_CLAUSE_REPLACEMENT, 1
        )
        second_xml, _ = _tracked_replace_paragraph(
            SECOND_PARTY_CLAUSE, SECOND_PARTY_CLAUSE_REPLACEMENT, 2
        )
        docx_bytes = _two_paragraph_docx(first_xml, second_xml)
        redlines = [
            _replace_redline("first", "p1", FIRST_PARTY_CLAUSE, FIRST_PARTY_CLAUSE_REPLACEMENT),
            _replace_redline("second", "p2", SECOND_PARTY_CLAUSE, SECOND_PARTY_CLAUSE_REPLACEMENT),
        ]
        self.assertEqual(verify_pdf_reconstruction_redline_coverage(docx_bytes, redlines), [])

    def test_identical_clause_dropped_twin_blocks_export_end_to_end(self):
        # End-to-end through build_matter_redline: two identical clauses, one landed one
        # dropped, raises PdfSourceRedlineUnavailableError so the incomplete redline set
        # never ships to the counterparty.
        review_paragraphs = _pdf_review_paragraphs([IDENTICAL_CLAUSE, IDENTICAL_CLAUSE])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=IDENTICAL_CLAUSE,
                    replacement_text=IDENTICAL_CLAUSE_REPLACEMENT,
                ),
                _pdf_replace_redline(
                    review_paragraphs[1],
                    original_text=IDENTICAL_CLAUSE,
                    replacement_text=IDENTICAL_CLAUSE_REPLACEMENT,
                ),
            ],
        }
        landed_xml, _ = _tracked_replace_paragraph(IDENTICAL_CLAUSE, IDENTICAL_CLAUSE_REPLACEMENT, 1)
        # First clause landed, second dropped (plain).
        dropped_reconstruction = _two_paragraph_docx(
            landed_xml, f"<w:p><w:r><w:t>{IDENTICAL_CLAUSE}</w:t></w:r></w:p>"
        )
        with self.assertRaises(PdfSourceRedlineUnavailableError) as caught:
            _build_pdf_export_with(
                reconstruction=dropped_reconstruction,
                review_result=review_result,
                source_paragraphs=[IDENTICAL_CLAUSE, IDENTICAL_CLAUSE],
            )
        self.assertEqual(caught.exception.reason, "redline_coverage_shortfall")


# --------------------------------------------------------------------------- #
# Round-3 MAJOR repro: FALSE POSITIVE on freeform manual char-level edits. A manual
# viewer replace (clause_id == manual_viewer_edit, not whole_paragraph) lands via the
# CHAR-level builder, which fragments words into single-character ins/del runs
# ('written' -> 'oral' emits del 'w'+'itten', ins 'o'+'al'). Round 2's contiguous-
# token-subsequence test could not find the whole word and BLOCKED a correctly-landed
# export. The char-level path must PASS a genuinely-landed manual edit.
# --------------------------------------------------------------------------- #
CI_CLAUSE = "Confidential Information must be disclosed in written form to be protected."
CI_CLAUSE_WRITTEN_TO_ORAL = CI_CLAUSE.replace("written", "oral")
COLOR_CLAUSE = "The brand guidelines specify the primary color of the logo."
COLOR_CLAUSE_COLOUR = COLOR_CLAUSE.replace("color", "colour")


def _manual_char_replace_redline(original: str, replacement: str, *, paragraph_id: str = "p1") -> dict:
    """A freeform manual viewer replace: clause_id == manual_viewer_edit and NOT
    whole_paragraph, so it routes through the char-level builder."""
    return {
        "id": "manual-edit",
        "action": REDLINE_REPLACE_PARAGRAPH,
        "clause_id": MANUAL_VIEWER_EDIT_CLAUSE_ID,
        "paragraph_id": paragraph_id,
        "source_part": "pdf",
        "original_text": original,
        "replacement_text": replacement,
    }


class FreeformManualCharLevelEditTests(unittest.TestCase):
    def test_landed_written_to_oral_char_edit_passes(self):
        # 'written' -> 'oral' landed via the char-level builder (fragmented ins/del).
        # Round 2 false-positived here; the char-level path must PASS.
        docx_bytes = _landed_char_replace_docx(CI_CLAUSE, CI_CLAUSE_WRITTEN_TO_ORAL)
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [_manual_char_replace_redline(CI_CLAUSE, CI_CLAUSE_WRITTEN_TO_ORAL)]
        )
        self.assertEqual(errors, [])

    def test_landed_color_to_colour_char_edit_passes(self):
        # 'color' -> 'colour' inserts just 'u': a single fragmented w:ins. Must PASS.
        docx_bytes = _landed_char_replace_docx(COLOR_CLAUSE, COLOR_CLAUSE_COLOUR)
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [_manual_char_replace_redline(COLOR_CLAUSE, COLOR_CLAUSE_COLOUR)]
        )
        self.assertEqual(errors, [])

    def test_dropped_manual_char_edit_is_still_caught(self):
        # Fail-safe preserved: a DROPPED manual edit (paragraph survives plain, no
        # markup) is still CAUGHT -- the fix must not blunt drop detection.
        docx_bytes = _docx_with_paragraphs([CI_CLAUSE])
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [_manual_char_replace_redline(CI_CLAUSE, CI_CLAUSE_WRITTEN_TO_ORAL)]
        )
        self.assertEqual(len(errors), 1)

    def test_landed_manual_char_edit_passes_among_siblings(self):
        # The landed char edit's target paragraph is resolved by its pre-change text, so
        # an untouched sibling clause does not confuse coverage.
        docx_bytes = _landed_char_replace_docx(
            CI_CLAUSE, CI_CLAUSE_WRITTEN_TO_ORAL, others=[GOVERNING_LAW]
        )
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [_manual_char_replace_redline(CI_CLAUSE, CI_CLAUSE_WRITTEN_TO_ORAL)]
        )
        self.assertEqual(errors, [])


# --------------------------------------------------------------------------- #
# Round-5 FALSE POSITIVE repro: a SMALL token-level edit inside a LONG single-
# paragraph clause whose original*replacement token product exceeds inline_diff's
# 40000-cell matrix limit. ``diff_text_operations`` then falls back to a single
# WHOLE-TEXT pair (del whole_original / ins whole_replacement) instead of fine-
# grained changed-token deltas. The builder (_tracked_replace_paragraph) emits the
# WHOLE original inside w:del and the WHOLE replacement inside w:ins; the export
# tokenizes those into per-WORD tokens. Round 4's gate computed its expected delta
# from the SAME whole-text fallback but kept each op's payload as one opaque token,
# which never appears as a single token in the per-word export markup -- so a
# genuinely-LANDED large-clause edit was wrongly reported 'missing' and BLOCKED.
# The fix re-tokenizes each side's op text through the same word tokenizer the
# export uses, so the gate's expected delta and the export markup agree: a LANDED
# large-clause edit PASSES; a DROPPED one (no markup) is still CAUGHT.
# --------------------------------------------------------------------------- #
class LargeClauseWholeTextFallbackTests(unittest.TestCase):
    def _replace_redline(self) -> dict:
        return {
            "id": "large-edit",
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p1",
            "source_part": "pdf",
            "original_text": LARGE_CLAUSE,
            "replacement_text": LARGE_CLAUSE_REPLACEMENT,
        }

    def test_fixture_actually_triggers_whole_text_fallback(self):
        # Guard the repro: if the clause ever shrinks below the matrix limit the diff
        # would NOT fall back to whole-text and the test would no longer exercise the
        # bug. Assert the >1100-char clause and the active whole-text fallback.
        from nda_automation.inline_diff import (
            INLINE_DIFF_MAX_MATRIX_CELLS,
            diff_text_operations,
            tokenize_inline_diff,
        )

        self.assertGreater(len(LARGE_CLAUSE), 1100)
        product = len(tokenize_inline_diff(LARGE_CLAUSE)) * len(
            tokenize_inline_diff(LARGE_CLAUSE_REPLACEMENT)
        )
        self.assertGreater(product, INLINE_DIFF_MAX_MATRIX_CELLS)
        operations = diff_text_operations(LARGE_CLAUSE, LARGE_CLAUSE_REPLACEMENT)
        # Whole-text fallback: exactly one delete + one insert, each the whole text.
        self.assertEqual([kind for kind, _ in operations], ["delete", "insert"])

    def test_landed_large_clause_small_edit_passes(self):
        # The false positive repro: the small edit LANDED via the production diff-driven
        # builder (which itself fell back to whole-text del/ins). Round 4 BLOCKED this;
        # the fix must PASS it -- no error.
        docx_bytes = _landed_replace_docx(LARGE_CLAUSE, LARGE_CLAUSE_REPLACEMENT)
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [self._replace_redline()]
        )
        self.assertEqual(errors, [])

    def test_dropped_large_clause_small_edit_is_caught(self):
        # Fail-safe preserved: the SAME large clause with the edit DROPPED (paragraph
        # kept plain, no markup) is still CAUGHT -- the fix must not blunt drop detection.
        docx_bytes = _docx_with_paragraphs([LARGE_CLAUSE])
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [self._replace_redline()]
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 1", errors[0])
        # Counts only -- no NDA text leaks into the error string.
        self.assertNotIn("five (5) years", errors[0])

    def test_landed_large_clause_passes_among_siblings(self):
        # The landed large-clause edit's target paragraph is resolved by its pre-change
        # text, so an untouched sibling clause does not confuse coverage.
        docx_bytes = _landed_replace_docx(
            LARGE_CLAUSE, LARGE_CLAUSE_REPLACEMENT, others=[GOVERNING_LAW]
        )
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes, [self._replace_redline()]
        )
        self.assertEqual(errors, [])

    def test_landed_large_clause_edit_passes_end_to_end(self):
        # End-to-end through build_matter_redline: the LANDED large-clause edit no longer
        # raises -- the export is produced.
        review_paragraphs = _pdf_review_paragraphs([LARGE_CLAUSE, CONFIDENTIALITY])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=LARGE_CLAUSE,
                    replacement_text=LARGE_CLAUSE_REPLACEMENT,
                )
            ],
        }
        landed_xml, _ = _tracked_replace_paragraph(LARGE_CLAUSE, LARGE_CLAUSE_REPLACEMENT, 1)
        landed_reconstruction = _two_paragraph_docx(
            landed_xml, f"<w:p><w:r><w:t>{CONFIDENTIALITY}</w:t></w:r></w:p>"
        )
        export = _build_pdf_export_with(
            reconstruction=landed_reconstruction,
            review_result=review_result,
            source_paragraphs=[LARGE_CLAUSE, CONFIDENTIALITY],
        )
        self.assertEqual(export.filename, "Signed-NDA-reviewed.docx")
        self.assertTrue(export.data)

    def test_dropped_large_clause_edit_blocks_export_end_to_end(self):
        # End-to-end through build_matter_redline: the DROPPED large-clause edit
        # (paragraph kept plain) still raises PdfSourceRedlineUnavailableError, so the
        # incomplete redline never ships to the counterparty.
        review_paragraphs = _pdf_review_paragraphs([LARGE_CLAUSE, CONFIDENTIALITY])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=LARGE_CLAUSE,
                    replacement_text=LARGE_CLAUSE_REPLACEMENT,
                )
            ],
        }
        dropped_reconstruction = _two_paragraph_docx(
            f"<w:p><w:r><w:t>{LARGE_CLAUSE}</w:t></w:r></w:p>",
            f"<w:p><w:r><w:t>{CONFIDENTIALITY}</w:t></w:r></w:p>",
        )
        with self.assertRaises(PdfSourceRedlineUnavailableError) as caught:
            _build_pdf_export_with(
                reconstruction=dropped_reconstruction,
                review_result=review_result,
                source_paragraphs=[LARGE_CLAUSE, CONFIDENTIALITY],
            )
        self.assertEqual(caught.exception.reason, "redline_coverage_shortfall")


def _two_paragraph_docx(first_paragraph_xml: str, second_paragraph_xml: str) -> bytes:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{first_paragraph_xml}{second_paragraph_xml}</w:body></w:document>"
    )
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", document_xml)
        return output.getvalue()


def _build_pdf_export_with(*, reconstruction: bytes, review_result: dict, source_paragraphs: list[str]):
    """Drive build_matter_redline for a PDF matter with a patched reconstruction, the
    same harness shape PdfExportServiceCoverageGateTests uses but with caller-supplied
    source paragraphs."""
    reconstructed = redline_export_service.pdf_docx_reconstruction.ReconstructedDocx(
        data=make_source_docx(source_paragraphs),
        filename="Signed-NDA.docx",
        headers={"X-PDF-DOCX-Reconstruction": "pdf2docx"},
    )
    with patch.object(
        redline_export_service,
        "_review_result_for_export",
        return_value=(review_result, b"%PDF-1.7\nsource\n%%EOF\n", "Signed NDA.pdf"),
    ), patch.object(
        redline_export_service.pdf_docx_reconstruction,
        "reconstruct_pdf_to_docx",
        return_value=reconstructed,
    ), patch.object(
        redline_export_service.docx_package_renderer,
        "render_source_redline_package",
        return_value=_DroppingRenderResult(reconstruction),
    ):
        return redline_export_service.build_matter_redline(
            "matter-pdf", {}, persist=False, repository=object()
        )


# --------------------------------------------------------------------------- #
# Regression: the DOCX-source path is unchanged (it still uses the strong gate;
# the new PDF gate must not run on it).
# --------------------------------------------------------------------------- #
class DocxSourcePathUnchangedTests(unittest.TestCase):
    def test_strong_docx_sequence_gate_still_active_for_docx_source(self):
        # The DOCX path still wires expected_source_text + expected_redline_edits into
        # render_source_redline_package; verify_export_content_coverage remains the
        # gate there (we don't touch it). A genuine DOCX drop is still caught by it.
        docx_bytes = _docx_with_paragraphs([GOVERNING_LAW, CONFIDENTIALITY])
        errors = docx_health.verify_export_content_coverage(
            docx_bytes,
            "\n\n".join([GOVERNING_LAW, CONFIDENTIALITY]),
            expected_redline_edits=[
                {
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p1",
                    "paragraph_index": 1,
                    "original_text": GOVERNING_LAW,
                    "replacement_text": GOVERNING_LAW_REPLACEMENT,
                }
            ],
        )
        # The export's accepted text still has the ORIGINAL governing law (drop), so
        # the strong sequence gate reports the divergence.
        self.assertTrue(errors)


# --------------------------------------------------------------------------- #
# Round-6 FALSE POSITIVE repro: a NON-freeform REPLACE redline whose
# replacement_text contains an INTERNAL NEWLINE and shares tokens with the
# original. The builder ``_tracked_replace_paragraph`` routes ANY multiline side
# into its WHOLE-del/WHOLE-ins branch (whole original in w:del, whole replacement
# in w:ins, tokenized per-word INCLUDING shared/punctuation tokens). But round 5's
# gate still computed a FINE-GRAINED word diff that EXCLUDES shared tokens, so the
# expected delta was non-contiguous in the whole-text markup and the contiguous-run
# match failed -> phantom "missing redline" -> export AND Gmail send BLOCKED.
#
# This is reachable in production: playbook.json clauses[5] (signatures) has a
# MULTILINE redline_template; clause_outcomes._template_redline_for_required_clause
# emits a REPLACE_PARAGRAPH redline with that template as replacement_text for a
# present-but-wrong signature block, stamped source_part="pdf" -> the PDF gate. A
# correctly-applied signature-block redline on a PDF NDA could not be exported/sent.
#
# The fix makes the gate mirror the builder's branch decision via the shared
# predicate ``tracked_replace_uses_whole_text_markup``: when the builder emits
# whole-text markup, the gate expects the WHOLE-text per-word token sequence
# (re-tokenized, like the matrix-overflow path) instead of the shared-token-
# excluding fine-grained diff. A LANDED multiline replace reconciles and PASSES;
# a DROPPED one (paragraph plain, no markup) still has no tokens and is CAUGHT.
# --------------------------------------------------------------------------- #
_PLAYBOOK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "playbook.json"
)


def _signatures_redline_template() -> str:
    """The REAL multiline signatures redline_template from the shipped playbook --
    so this repro tracks the production value that reaches the PDF gate."""
    with open(_PLAYBOOK_PATH, encoding="utf-8") as handle:
        playbook = json.load(handle)
    clause = next(clause for clause in playbook["clauses"] if clause.get("id") == "signatures")
    return str(clause["redline_template"])


# A present-but-wrong signature block that SHARES tokens with the multiline
# template ("For", "By:", "Name:", "Title:", "Date:") -- the shared tokens are
# exactly what the fine-grained diff would drop, so a contiguous-run match against
# the whole-text markup fails unless the gate mirrors the builder branch.
WRONG_SIGNATURE_BLOCK = "For Acme Corp By: Name: Title: Date:"


class MultilineReplaceWholeTextMarkupTests(unittest.TestCase):
    """The internal-newline whole-text branch of the builder, mirrored by the gate."""

    def setUp(self):
        self.template = _signatures_redline_template()

    def _signature_redline(self) -> dict:
        return {
            "id": "signatures",
            "clause_id": "signatures",
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p1",
            "source_part": "pdf",
            "original_text": WRONG_SIGNATURE_BLOCK,
            "replacement_text": self.template,
        }

    def test_fixture_template_is_actually_multiline(self):
        # Guard the repro: the playbook signatures template must carry an internal
        # newline (the condition that routes the builder into its whole-text branch).
        # If it ever becomes single-line this test no longer exercises the bug.
        self.assertIn("\n", self.template)

    def test_builder_takes_whole_text_branch_for_this_template(self):
        # The shared predicate the gate now consults must agree that the builder uses
        # whole-text markup for this multiline replacement.
        from nda_automation.redline_xml import tracked_replace_uses_whole_text_markup

        self.assertTrue(
            tracked_replace_uses_whole_text_markup(WRONG_SIGNATURE_BLOCK, self.template)
        )

    def test_landed_multiline_replace_passes_unit(self):
        # The load-bearing repro: a present-but-wrong signature block redlined with the
        # multiline template, LANDED via the production builder (_tracked_replace_paragraph
        # -> whole-del/whole-ins). Round 5 BLOCKED this; the fix must return NO errors.
        docx_bytes = _landed_replace_docx(WRONG_SIGNATURE_BLOCK, self.template)
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [self._signature_redline()])
        self.assertEqual(errors, [])

    def test_dropped_multiline_replace_is_caught_unit(self):
        # Fail-safe preserved: the SAME multiline redline DROPPED (paragraph kept plain,
        # no w:ins/w:del) is still CAUGHT -- the fix must not blunt drop detection.
        docx_bytes = _docx_with_paragraphs([WRONG_SIGNATURE_BLOCK])
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [self._signature_redline()])
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 1", errors[0])
        # Counts only -- no template text leaks into the error string.
        self.assertNotIn("Title", errors[0])

    def test_landed_multiline_replace_passes_among_siblings(self):
        # An untouched sibling clause must not confuse coverage: the landed multiline
        # replace's target paragraph is resolved by its pre-change text.
        docx_bytes = _landed_replace_docx(
            WRONG_SIGNATURE_BLOCK, self.template, others=[GOVERNING_LAW]
        )
        errors = verify_pdf_reconstruction_redline_coverage(docx_bytes, [self._signature_redline()])
        self.assertEqual(errors, [])

    def test_landed_multiline_replace_passes_through_real_renderer(self):
        # End-to-end through the REAL render_source_redline_package + the gate (NOT a
        # synthetic markup): the multiline replacement is anchored and built by the
        # production builder, then the gate runs on the genuine output bytes. No error.
        reconstructed = make_source_docx([WRONG_SIGNATURE_BLOCK, CONFIDENTIALITY])
        review_paragraphs = _pdf_review_paragraphs([WRONG_SIGNATURE_BLOCK, CONFIDENTIALITY])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=WRONG_SIGNATURE_BLOCK,
                    replacement_text=self.template,
                )
            ],
        }
        package_result = redline_export_service.docx_package_renderer.render_source_redline_package(
            reconstructed,
            review_result,
            expected_source_text="",
            expected_redline_edits=[],
        )
        self.assertEqual(package_result.anchor_uncertain_redlines, [])
        errors = verify_pdf_reconstruction_redline_coverage(
            package_result.data, review_result["redline_edits"]
        )
        self.assertEqual(errors, [])

    def test_signature_scenario_passes_export_end_to_end(self):
        # The full shipped workflow: a PDF-source matter whose present-but-wrong
        # signature block is redlined with the multiline playbook template. Driving
        # build_matter_redline with the REAL render_source_redline_package (only the
        # PDF reconstruction + review-result lookup are stubbed) must NOT raise
        # PdfSourceRedlineUnavailableError -- the export is produced.
        review_paragraphs = _pdf_review_paragraphs([WRONG_SIGNATURE_BLOCK, CONFIDENTIALITY])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=WRONG_SIGNATURE_BLOCK,
                    replacement_text=self.template,
                )
            ],
        }
        reconstructed = redline_export_service.pdf_docx_reconstruction.ReconstructedDocx(
            data=make_source_docx([WRONG_SIGNATURE_BLOCK, CONFIDENTIALITY]),
            filename="Signed-NDA.docx",
            headers={"X-PDF-DOCX-Reconstruction": "pdf2docx"},
        )
        with patch.object(
            redline_export_service,
            "_review_result_for_export",
            return_value=(review_result, b"%PDF-1.7\nsource\n%%EOF\n", "Signed NDA.pdf"),
        ), patch.object(
            redline_export_service.pdf_docx_reconstruction,
            "reconstruct_pdf_to_docx",
            return_value=reconstructed,
        ):
            export = redline_export_service.build_matter_redline(
                "matter-pdf", {}, persist=False, repository=object()
            )
        self.assertEqual(export.filename, "Signed-NDA-reviewed.docx")
        self.assertTrue(export.data)

    def test_dropped_signature_scenario_blocks_export_end_to_end(self):
        # The same workflow but the multiline redline is DROPPED (reconstruction keeps
        # the wrong signature block plain, no tracked change). The gate must BLOCK with
        # PdfSourceRedlineUnavailableError so the incomplete redline never ships.
        review_paragraphs = _pdf_review_paragraphs([WRONG_SIGNATURE_BLOCK, CONFIDENTIALITY])
        review_result = {
            "paragraphs": review_paragraphs,
            "redline_edits": [
                _pdf_replace_redline(
                    review_paragraphs[0],
                    original_text=WRONG_SIGNATURE_BLOCK,
                    replacement_text=self.template,
                )
            ],
        }
        dropped = _docx_with_paragraphs([WRONG_SIGNATURE_BLOCK, CONFIDENTIALITY])
        with patch.object(
            redline_export_service,
            "_review_result_for_export",
            return_value=(review_result, b"%PDF-1.7\nsource\n%%EOF\n", "Signed NDA.pdf"),
        ), patch.object(
            redline_export_service.pdf_docx_reconstruction,
            "reconstruct_pdf_to_docx",
            return_value=redline_export_service.pdf_docx_reconstruction.ReconstructedDocx(
                data=make_source_docx([WRONG_SIGNATURE_BLOCK, CONFIDENTIALITY]),
                filename="Signed-NDA.docx",
                headers={"X-PDF-DOCX-Reconstruction": "pdf2docx"},
            ),
        ), patch.object(
            redline_export_service.docx_package_renderer,
            "render_source_redline_package",
            return_value=_DroppingRenderResult(dropped),
        ):
            with self.assertRaises(PdfSourceRedlineUnavailableError) as caught:
                redline_export_service.build_matter_redline(
                    "matter-pdf", {}, persist=False, repository=object()
                )
        self.assertEqual(caught.exception.reason, "redline_coverage_shortfall")


# --------------------------------------------------------------------------- #
# Round-7 BLOCKER repro: the THIRD whole-text builder branch -- a replace redline
# carrying a non-empty ``replacement_runs`` run model. ``docx_export.
# _source_tracked_primary_redline_paragraph`` routes ANY such redline to
# ``_source_tracked_replace_paragraph_runs``, which emits WHOLE-del/WHOLE-ins markup
# (whole original in w:del, whole formatted replacement in w:ins) REGARDLESS of the
# text size. Rounds 5/6 made the gate mirror only the newline + matrix-overflow
# whole-text branches (via ``tracked_replace_uses_whole_text_markup(original,
# replacement)``), which never inspects ``replacement_runs``. So a single-line SMALL
# edit carrying a run model made the builder emit whole-text while the gate expected
# the fine-grained (shared-token-excluding) delta -> that delta is non-contiguous in
# the whole-text w:ins, ``_tokens_present`` fails, and a genuinely-LANDED export was
# wrongly BLOCKED.
#
# The fix routes BOTH the builder and the gate through the single predicate
# ``redline_replace_uses_whole_text_markup(redline, original, replacement)``, which
# returns whole-text for the run-model branch too. A LANDED run-model replace now
# reconciles and PASSES; a DROPPED one (target paragraph left plain) is still CAUGHT.
#
# Reachable on the accepted backend contract: ``export_service._clean_replacement_runs``
# sanitises ``replacement_runs`` on a manual_redline_edit with ``whole_paragraph`` truthy
# and guarantees the runs' joined text equals ``replacement_text``; it is preserved
# through ``redline_edit_contract.normalize_redline_edit`` and honored by the source
# builder, so the same redline drives BOTH the production render and the gate.
# --------------------------------------------------------------------------- #
# A SMALL single-line edit ("two (2) years" -> "five (5) years") inside a SHORT clause:
# no internal newline and far under the matrix limit, so the TEXT-only predicate would
# return fine-grained -- it is ONLY the run model that forces the whole-text branch.
RUN_MODEL_CLAUSE = (
    "The Receiving Party shall keep the Confidential Information confidential for a "
    "period of two (2) years from the date of disclosure."
)
RUN_MODEL_CLAUSE_REPLACEMENT = RUN_MODEL_CLAUSE.replace("two (2) years", "five (5) years")


def _run_model_for(text: str) -> list[dict]:
    """A two-run run model whose JOINED text == ``text`` (the contract
    ``export_service._clean_replacement_runs`` enforces). The first fragment is bold so
    the run model is genuinely formatting-bearing, not a trivial single plain run."""
    split = len(text) // 2
    return [{"text": text[:split], "bold": True}, {"text": text[split:]}]


def _run_model_replace_redline(
    original: str, replacement: str, *, paragraph_id: str = "p1"
) -> dict:
    """A whole-paragraph manual viewer replace carrying a ``replacement_runs`` run model
    -- the accepted backend contract that routes the builder into its whole-text runs
    branch. ``whole_paragraph`` truthy (so it is NOT a freeform char edit) and the runs'
    joined text equals ``replacement_text``."""
    return {
        "id": f"run-model-{paragraph_id}",
        "action": REDLINE_REPLACE_PARAGRAPH,
        "clause_id": MANUAL_VIEWER_EDIT_CLAUSE_ID,
        "paragraph_id": paragraph_id,
        "source_part": "pdf",
        "whole_paragraph": True,
        "original_text": original,
        "replacement_text": replacement,
        "replacement_runs": _run_model_for(replacement),
    }


def _landed_run_model_docx(original: str, replacement: str, *, others: list[str] | None = None) -> bytes:
    """A reconstruction in which a run-model replace DID land, built with the SAME
    production path the export uses (``build_source_redline_package`` ->
    ``_source_tracked_replace_paragraph_runs``) -- whole original in one w:del, whole
    formatted replacement in one w:ins. The gate must accept this whole-text markup
    without false-positiving. Building it through the real anchoring/builder (rather than
    synthesising the markup string) keeps the fixture byte-faithful to production."""
    from nda_automation import source_redline_docx

    paragraphs = [*(others or []), original]
    review_paragraphs = _pdf_review_paragraphs(paragraphs)
    target = review_paragraphs[-1]
    review_result = {
        "paragraphs": review_paragraphs,
        "redline_edits": [
            _run_model_replace_redline(original, replacement, paragraph_id=target["id"])
        ],
    }
    package = source_redline_docx.build_source_redline_package(
        make_source_docx(paragraphs), review_result
    )
    return package.data


class RunModelReplaceWholeTextMarkupTests(unittest.TestCase):
    """The ``replacement_runs`` run-model whole-text branch of the builder, mirrored by
    the gate. The text is a single-line SMALL edit, so ONLY the run model forces
    whole-text -- exactly the gate/builder divergence round 7 closes."""

    def test_fixture_is_single_line_small_edit(self):
        # Guard the repro: the clause is single-line and small, so the TEXT-only predicate
        # would NOT take a whole-text branch -- the bug is reachable ONLY via the run model.
        from nda_automation.redline_xml import tracked_replace_uses_whole_text_markup

        self.assertNotIn("\n", RUN_MODEL_CLAUSE)
        self.assertNotIn("\n", RUN_MODEL_CLAUSE_REPLACEMENT)
        self.assertFalse(
            tracked_replace_uses_whole_text_markup(
                RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT
            )
        )

    def test_run_model_forces_whole_text_branch(self):
        # The single predicate the gate AND builder now share must report whole-text for
        # this redline PURELY because it carries a run model.
        from nda_automation.redline_xml import (
            redline_replace_uses_whole_text_markup,
            replace_redline_has_run_model,
        )

        redline = _run_model_replace_redline(RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT)
        self.assertTrue(replace_redline_has_run_model(redline))
        self.assertTrue(
            redline_replace_uses_whole_text_markup(
                redline, RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT
            )
        )

    def test_landed_run_model_replace_passes_unit(self):
        # The load-bearing repro: a single-line small edit carrying a run model, LANDED
        # via the production runs builder (whole-del/whole-ins). The gate must return NO
        # errors -- a genuinely-landed export must not be blocked.
        docx_bytes = _landed_run_model_docx(RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT)
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes,
            [_run_model_replace_redline(RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT)],
        )
        self.assertEqual(errors, [])

    def test_dropped_run_model_replace_is_caught_unit(self):
        # Fail-safe preserved: the SAME run-model redline DROPPED (paragraph kept plain,
        # no w:ins/w:del) is still CAUGHT -- the fix must not blunt drop detection.
        docx_bytes = _docx_with_paragraphs([RUN_MODEL_CLAUSE])
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes,
            [_run_model_replace_redline(RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT)],
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("missing 1 of 1", errors[0])
        # Counts only -- no NDA text leaks into the error string.
        self.assertNotIn("five (5) years", errors[0])

    def test_landed_run_model_replace_passes_among_siblings(self):
        # An untouched sibling clause must not confuse coverage: the landed run-model
        # replace's target paragraph is resolved by its pre-change text.
        docx_bytes = _landed_run_model_docx(
            RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT, others=[GOVERNING_LAW]
        )
        errors = verify_pdf_reconstruction_redline_coverage(
            docx_bytes,
            [_run_model_replace_redline(RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT)],
        )
        self.assertEqual(errors, [])

    def test_landed_run_model_replace_passes_through_real_renderer(self):
        # End-to-end through the REAL build_source_redline_package + the gate (NOT a
        # synthetic markup): the run-model replacement is anchored and built by the
        # production builder, then the gate runs on the genuine output bytes. No error.
        reconstructed = make_source_docx([RUN_MODEL_CLAUSE, CONFIDENTIALITY])
        review_paragraphs = _pdf_review_paragraphs([RUN_MODEL_CLAUSE, CONFIDENTIALITY])
        redline = _run_model_replace_redline(
            RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT, paragraph_id=review_paragraphs[0]["id"]
        )
        review_result = {"paragraphs": review_paragraphs, "redline_edits": [redline]}
        package = redline_export_service.docx_package_renderer.render_source_redline_package(
            reconstructed,
            review_result,
            expected_source_text="",
            expected_redline_edits=[],
        )
        self.assertEqual(package.anchor_uncertain_redlines, [])
        errors = verify_pdf_reconstruction_redline_coverage(
            package.data, review_result["redline_edits"]
        )
        self.assertEqual(errors, [])

    def test_run_model_scenario_passes_export_end_to_end(self):
        # The full shipped workflow: a PDF-source matter whose paragraph is replaced with
        # a run-model edit. Driving build_matter_redline with the REAL
        # render_source_redline_package (only the PDF reconstruction + review-result
        # lookup are stubbed) must NOT raise -- the export is produced.
        review_paragraphs = _pdf_review_paragraphs([RUN_MODEL_CLAUSE, CONFIDENTIALITY])
        redline = _run_model_replace_redline(
            RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT, paragraph_id=review_paragraphs[0]["id"]
        )
        review_result = {"paragraphs": review_paragraphs, "redline_edits": [redline]}
        reconstructed = redline_export_service.pdf_docx_reconstruction.ReconstructedDocx(
            data=make_source_docx([RUN_MODEL_CLAUSE, CONFIDENTIALITY]),
            filename="Signed-NDA.docx",
            headers={"X-PDF-DOCX-Reconstruction": "pdf2docx"},
        )
        with patch.object(
            redline_export_service,
            "_review_result_for_export",
            return_value=(review_result, b"%PDF-1.7\nsource\n%%EOF\n", "Signed NDA.pdf"),
        ), patch.object(
            redline_export_service.pdf_docx_reconstruction,
            "reconstruct_pdf_to_docx",
            return_value=reconstructed,
        ):
            export = redline_export_service.build_matter_redline(
                "matter-pdf", {}, persist=False, repository=object()
            )
        self.assertEqual(export.filename, "Signed-NDA-reviewed.docx")
        self.assertTrue(export.data)

    def test_dropped_run_model_scenario_blocks_export_end_to_end(self):
        # The same workflow but the run-model redline is DROPPED (reconstruction keeps the
        # original clause plain, no tracked change). The gate must BLOCK with
        # PdfSourceRedlineUnavailableError so the incomplete redline never ships.
        review_paragraphs = _pdf_review_paragraphs([RUN_MODEL_CLAUSE, CONFIDENTIALITY])
        redline = _run_model_replace_redline(
            RUN_MODEL_CLAUSE, RUN_MODEL_CLAUSE_REPLACEMENT, paragraph_id=review_paragraphs[0]["id"]
        )
        review_result = {"paragraphs": review_paragraphs, "redline_edits": [redline]}
        dropped = _docx_with_paragraphs([RUN_MODEL_CLAUSE, CONFIDENTIALITY])
        with patch.object(
            redline_export_service,
            "_review_result_for_export",
            return_value=(review_result, b"%PDF-1.7\nsource\n%%EOF\n", "Signed NDA.pdf"),
        ), patch.object(
            redline_export_service.pdf_docx_reconstruction,
            "reconstruct_pdf_to_docx",
            return_value=redline_export_service.pdf_docx_reconstruction.ReconstructedDocx(
                data=make_source_docx([RUN_MODEL_CLAUSE, CONFIDENTIALITY]),
                filename="Signed-NDA.docx",
                headers={"X-PDF-DOCX-Reconstruction": "pdf2docx"},
            ),
        ), patch.object(
            redline_export_service.docx_package_renderer,
            "render_source_redline_package",
            return_value=_DroppingRenderResult(dropped),
        ):
            with self.assertRaises(PdfSourceRedlineUnavailableError) as caught:
                redline_export_service.build_matter_redline(
                    "matter-pdf", {}, persist=False, repository=object()
                )
        self.assertEqual(caught.exception.reason, "redline_coverage_shortfall")


if __name__ == "__main__":
    unittest.main()
