import builtins
import importlib.util
import resource
import unittest
from io import BytesIO
from unittest.mock import patch

from nda_automation import pdf_text
from nda_automation.pdf_text import (
    PDF_SUPPORT_NOT_INSTALLED_MESSAGE,
    GeoLine,
    PdfExtractionError,
    _dominant_font_size,
    _dominant_line_height,
    _split_pdf_paragraphs,
    extract_pdf_document,
    extract_pdf_paragraphs,
    extract_pdf_text,
)

PYPDF_AVAILABLE = importlib.util.find_spec("pypdf") is not None
if PYPDF_AVAILABLE:
    from pypdf import PdfWriter
else:
    PdfWriter = None
requires_pypdf = unittest.skipUnless(PYPDF_AVAILABLE, "pypdf is not installed")
PYMUPDF_AVAILABLE = importlib.util.find_spec("fitz") is not None
requires_pymupdf = unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF is not installed")


class PdfTextTests(unittest.TestCase):
    @requires_pypdf
    def test_extracts_text_from_pdf(self):
        data = make_pdf("This Agreement shall be governed by the laws of California.")

        extraction = extract_pdf_document(data)
        paragraphs = extraction.paragraphs

        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0]["source_part"], "pdf")
        self.assertEqual(paragraphs[0]["page_number"], 1)
        self.assertIn("California", paragraphs[0]["text"])
        self.assertEqual(extraction.quality["page_count"], 1)
        self.assertEqual(extraction.quality["pages_with_text"], 1)
        self.assertEqual(extraction.quality["pages_without_text"], 0)
        self.assertEqual(extraction.quality["extracted_paragraphs"], 1)
        self.assertIn("California", extract_pdf_text(data))

    @requires_pypdf
    def test_encrypted_pdf_is_reported_as_encrypted(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt(user_password="a-real-password")
        buffer = BytesIO()
        writer.write(buffer)

        with self.assertRaises(PdfExtractionError) as error:
            extract_pdf_document(buffer.getvalue())

        self.assertEqual(str(error.exception), pdf_text.ENCRYPTED_PDF_MESSAGE)

    @requires_pypdf
    def test_reconstructs_wrapped_clause_paragraphs(self):
        # Explicit per-line geometry (TRUE sub-pitch adjacency between the wrapped
        # body lines, a real >pitch paragraph gap between the two clauses).
        # ``make_pdf_lines`` is NOT used here because its T*/TL stream makes pypdf
        # report a 168pt drop per line, which is not representative of real PDFs and
        # now (geometry-gate first) reads as a paragraph boundary on every line.
        #
        # OPTION-B CONTRACT: a marker-led heading no longer absorbs a CAPITALIZED
        # body line — a capitalized sentence-start after a marker-led-open block is
        # treated as a new clause and SPLITS (the accepted never-merge fragmentation).
        # So "1. Definitions" splits from its capitalized body "Confidential
        # Information means..."; the body's OWN mid-sentence wrap (lines 2->3, prev
        # unfinished + lowercase next) still joins whole; the "2. Term" clause sits a
        # paragraph-gap below and splits from its capitalized body too. The cardinal
        # invariant holds: no two distinct clauses ever share a block.
        data = make_pdf_positioned([
            ("1. Definitions", 72, 720, 12),
            ("Confidential Information means non-public information", 72, 706, 12),  # 14pt wrap
            ("and business plans disclosed by either party.", 72, 692, 12),  # 14pt wrap (lowercase -> joins)
            ("2. Term", 72, 662, 12),  # 30pt paragraph gap
            ("The confidentiality obligations survive for five years.", 72, 648, 12),  # 14pt wrap
        ])

        paragraphs = extract_pdf_paragraphs(data)

        texts = [paragraph["text"] for paragraph in paragraphs]
        self.assertEqual(
            texts,
            [
                "1. Definitions",
                "Confidential Information means non-public information and business plans disclosed by either party.",
                "2. Term",
                "The confidentiality obligations survive for five years.",
            ],
        )
        # The body's own wrap stays whole, and never-merge holds across clauses.
        self.assertFalse(
            any("Definitions" in text and "Term" in text for text in texts)
        )

    @requires_pypdf
    def test_geometry_round_trip_keeps_separate_clauses_apart(self):
        # End-to-end through pypdf: two prose clauses with no text-visible boundary
        # (no terminal punctuation at the break, lowercase start) separated only by
        # a real vertical gap. The geometry path must keep them as two clauses.
        data = make_pdf_positioned([
            ("The receiving party shall keep all Confidential Information confidential and shall", 72, 720, 12),
            ("not use it for any purpose other than the permitted purpose", 72, 706, 12),
            ("this agreement is governed by the laws of england and applies", 72, 678, 12),
            ("to all disputes between the parties hereto", 72, 664, 12),
        ])

        paragraphs = extract_pdf_paragraphs(data)
        texts = [paragraph["text"] for paragraph in paragraphs]

        self.assertGreaterEqual(len(texts), 2)
        self.assertFalse(
            any("permitted purpose" in text and "england" in text for text in texts),
            f"separate clauses were merged: {texts}",
        )

    @requires_pypdf
    def test_geometry_round_trip_splits_single_line_clauses(self):
        # End-to-end through pypdf: a single-line-per-clause page (no wrapped line to
        # sample the body pitch) with varied 14/20/28/50pt gaps. Every one-line
        # clause must remain its own paragraph; none may merge.
        data = make_pdf_positioned([
            ("Confidential Information means non-public information disclosed by either party.", 72, 720, 12),
            ("This Agreement is governed by the laws of England and Wales.", 72, 706, 12),  # 14pt
            ("The term of this Agreement is five years from the Effective Date.", 72, 686, 12),  # 20pt
            ("Each party shall bear its own costs in connection with this Agreement.", 72, 658, 12),  # 28pt
            ("No party may assign this Agreement without prior written consent.", 72, 608, 12),  # 50pt
        ])

        texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

        self.assertEqual(len(texts), 5, texts)
        markers = ["non-public information", "England and Wales", "term of this", "bear its own", "may assign"]
        for text in texts:
            present = [marker for marker in markers if marker in text]
            self.assertLessEqual(len(present), 1, f"two single-line clauses merged: {text!r}")

    @requires_pypdf
    def test_geometry_round_trip_keeps_mid_sentence_wrap_whole(self):
        # End-to-end: a single clause that wraps MID-SENTENCE at every break (each
        # break is prev-UNFINISHED + next-lowercase) must not fragment — this is the
        # one sub-pitch JOIN never-merge still allows.
        data = make_pdf_positioned([
            ("The receiving party shall protect the Confidential Information using", 72, 720, 12),
            ("the same care it uses for its own information and shall apply", 72, 706, 12),
            ("no less than a reasonable standard of care at all times.", 72, 692, 12),
        ])

        paragraphs = extract_pdf_paragraphs(data)

        self.assertEqual(len(paragraphs), 1)
        self.assertIn("reasonable standard", paragraphs[0]["text"])

    @requires_pypdf
    def test_geometry_round_trip_splits_sentence_boundary_on_a_line_break(self):
        # End-to-end, ACCEPTED-BY-DESIGN: a single multi-sentence clause whose
        # internal sentence boundary lands exactly on a line break SPLITS into two
        # paragraphs. Because never-merge is absolute, a finished sentence followed by
        # a sentence-start never joins, even within one logical clause. This is the
        # chosen safe failure — the round-trip twin of the unit-level test.
        data = make_pdf_positioned([
            ("The receiving party shall protect the Confidential Information using", 72, 720, 12),
            ("the same care it uses for its own information.", 72, 706, 12),
            ("Such care shall be no less than a reasonable standard at all times.", 72, 692, 12),
        ])

        paragraphs = extract_pdf_paragraphs(data)
        texts = [paragraph["text"] for paragraph in paragraphs]

        self.assertEqual(len(paragraphs), 2)
        self.assertIn("its own information.", texts[0])
        self.assertTrue(texts[1].startswith("Such care"))
        self.assertIn("reasonable standard", texts[1])

    @requires_pypdf
    def test_preserves_standalone_clause_numbers(self):
        # Explicit per-line geometry: each bare clause number sits at TRUE sub-pitch
        # adjacency above its title, and the second clause sits a real >pitch
        # paragraph gap below the first.
        # (``make_pdf_lines`` would feed pypdf a 168pt-per-line drop, which the
        # geometry-first gate now reads as a paragraph boundary on every line.)
        #
        # ROUND-7 CONTRACT (never-merge-absolute): ALL THREE joins now require a
        # LOWERCASE continuation. A bare number above a CAPITALIZED title is a
        # sentence-start, so JOIN 1 no longer fires — the number FRAGMENTS from its
        # title into its own block (the accepted safe failure). And the capitalized
        # body line after each title is likewise a sentence-start, so it SPLITS too.
        # Two distinct clauses never share a block.
        data = make_pdf_positioned([
            ("1", 72, 720, 12),
            ("Definitions", 72, 706, 12),  # 14pt -> capitalized title -> SPLIT (number fragments)
            ("Confidential Information means non-public information.", 72, 692, 12),  # capitalized body -> SPLIT
            ("2", 72, 662, 12),  # 30pt paragraph gap
            ("Term", 72, 648, 12),  # 14pt -> capitalized title -> SPLIT (number fragments)
            ("The confidentiality obligations survive for five years.", 72, 634, 12),  # capitalized body -> SPLIT
        ])

        paragraphs = extract_pdf_paragraphs(data)

        texts = [paragraph["text"] for paragraph in paragraphs]
        self.assertEqual(
            texts,
            [
                "1",
                "Definitions",
                "Confidential Information means non-public information.",
                "2",
                "Term",
                "The confidentiality obligations survive for five years.",
            ],
        )
        # Never-merge: the two clauses' content never shares a block.
        self.assertFalse(any("Definitions" in text and "Term" in text for text in texts))

    @requires_pypdf
    def test_removes_repeated_pdf_headers_and_page_numbers(self):
        data = make_pdf_pages([
            [
                "Acme Legal",
                "1. Definitions",
                "Confidential Information means technical information.",
                "1",
            ],
            [
                "Acme Legal",
                "2. Term",
                "The obligations survive for three years.",
                "2",
            ],
        ])

        extraction = extract_pdf_document(data)
        extracted_text = "\n\n".join(paragraph["text"] for paragraph in extraction.paragraphs)

        self.assertNotIn("Acme Legal", extracted_text)
        self.assertNotIn("\n\n1\n\n", f"\n\n{extracted_text}\n\n")
        self.assertEqual(extraction.quality["repeated_margin_lines_removed"], 1)

    @requires_pypdf
    def test_preserves_repeated_substantive_pdf_titles(self):
        data = make_pdf_pages([
            [
                "Acme Mutual NDA",
                "1. Definitions",
                "Confidential Information means technical information.",
                "1",
            ],
            [
                "Acme Mutual NDA",
                "2. Term",
                "The obligations survive for three years.",
                "2",
            ],
        ])

        extraction = extract_pdf_document(data)
        extracted_text = "\n\n".join(paragraph["text"] for paragraph in extraction.paragraphs)

        self.assertIn("Acme Mutual NDA", extracted_text)
        self.assertEqual(extraction.quality["repeated_margin_lines_removed"], 0)

    @requires_pypdf
    def test_preserves_repeated_multiword_document_titles(self):
        data = make_pdf_pages([
            [
                "Moorwand Project Proposal Form",
                "1. Overview",
                "The proposal describes project scope and commercial assumptions.",
                "1",
            ],
            [
                "Moorwand Project Proposal Form",
                "2. Timeline",
                "The implementation timeline is subject to mutual agreement.",
                "2",
            ],
        ])

        extraction = extract_pdf_document(data)
        extracted_text = "\n\n".join(paragraph["text"] for paragraph in extraction.paragraphs)

        self.assertIn("Moorwand Project Proposal Form", extracted_text)
        self.assertEqual(extraction.quality["repeated_margin_lines_removed"], 0)

    @requires_pypdf
    def test_quality_report_warns_when_some_pages_have_no_text(self):
        data = make_pdf_pages([
            ["This Agreement shall be governed by the laws of California."],
            [],
        ])

        extraction = extract_pdf_document(data)

        self.assertEqual(extraction.quality["page_count"], 2)
        self.assertEqual(extraction.quality["pages_without_text"], 1)
        warning_types = {warning["type"] for warning in extraction.quality["warnings"]}
        self.assertIn("pdf_pages_without_text", warning_types)

    @requires_pypdf
    @requires_pymupdf
    def test_quality_report_flags_pdf_visual_features_that_text_extraction_drops(self):
        data = make_visual_pdf()

        extraction = extract_pdf_document(data)

        self.assertIn("Red heading", extraction.paragraphs[0]["text"])
        visual_profile = extraction.quality["visual_profile"]
        self.assertEqual(visual_profile["status"], "ready")
        self.assertTrue(visual_profile["requires_source_preview"])
        self.assertGreaterEqual(visual_profile["non_black_text_span_count"], 1)
        self.assertGreaterEqual(visual_profile["drawing_count"], 1)
        self.assertIn("colored_text", visual_profile["visual_features"])
        self.assertIn("drawings_or_borders", visual_profile["visual_features"])
        warning_types = {warning["type"] for warning in extraction.quality["warnings"]}
        self.assertIn("pdf_visual_fidelity_requires_source_preview", warning_types)

    @requires_pypdf
    @requires_pymupdf
    def test_visual_profile_detects_embedded_images_without_materializing_pixels(self):
        # The visual profile strips TEXT_PRESERVE_IMAGES from the fitz text-dict flags
        # so image pixel bytes are never materialized (the per-review peak-RSS hog).
        # Image *presence* must still be detected -- now via the lightweight
        # get_image_info() path -- so this guards that the memory trim did not blind
        # the profile to images.
        import fitz

        data = make_image_pdf()

        extraction = extract_pdf_document(data)
        visual_profile = extraction.quality["visual_profile"]

        self.assertEqual(visual_profile["status"], "ready")
        self.assertGreaterEqual(visual_profile["image_count"], 1)
        self.assertGreaterEqual(visual_profile["pages_with_images"], 1)
        self.assertIn("images", visual_profile["visual_features"])
        self.assertTrue(visual_profile["requires_source_preview"])
        # The text dict produced under the no-images flags must carry no image (type==1)
        # blocks, proving the pixel-bearing branch is genuinely suppressed.
        flags = pdf_text._fitz_visual_text_flags(fitz)
        self.assertIsNotNone(flags)
        document = fitz.open(stream=data, filetype="pdf")
        try:
            blocks = document[0].get_text("dict", flags=flags).get("blocks", [])
        finally:
            document.close()
        self.assertFalse(
            any(isinstance(block, dict) and block.get("type") == 1 for block in blocks),
            "image bytes were still materialized in the text dict",
        )

    @requires_pypdf
    def test_quality_report_requires_source_preview_when_visual_profiler_missing(self):
        real_import = builtins.__import__

        def import_without_fitz(name, *args, **kwargs):
            if name == "fitz":
                raise ModuleNotFoundError("No module named 'fitz'")
            return real_import(name, *args, **kwargs)

        data = make_pdf("This Agreement shall be governed by the laws of California.")

        with patch("builtins.__import__", side_effect=import_without_fitz):
            extraction = extract_pdf_document(data)

        visual_profile = extraction.quality["visual_profile"]
        self.assertEqual(visual_profile["status"], "unavailable")
        self.assertEqual(visual_profile["reason"], "pymupdf_not_installed")
        self.assertTrue(visual_profile["requires_source_preview"])
        warning_types = {warning["type"] for warning in extraction.quality["warnings"]}
        self.assertIn("pdf_visual_fidelity_requires_source_preview", warning_types)

    @requires_pypdf
    def test_rejects_pdf_with_too_many_pages(self):
        data = make_pdf_pages([
            ["This Agreement shall be governed by the laws of California."],
            ["The confidentiality obligations survive for two years."],
        ])

        with patch.object(pdf_text, "MAX_PDF_PAGES", 1):
            with self.assertRaisesRegex(PdfExtractionError, "exceeds the 1 page review limit"):
                extract_pdf_paragraphs(data)

    @requires_pypdf
    def test_rejects_pdf_with_too_much_extracted_text(self):
        data = make_pdf("This Agreement shall be governed by the laws of California.")

        with patch.object(pdf_text, "MAX_PDF_EXTRACTED_CHARACTERS", 20):
            with self.assertRaisesRegex(PdfExtractionError, "more than the 20 character extraction limit"):
                extract_pdf_paragraphs(data)

    @requires_pypdf
    def test_rejects_pdf_without_extractable_text(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with BytesIO() as output:
            writer.write(output)
            data = output.getvalue()

        with self.assertRaisesRegex(PdfExtractionError, "No readable text"):
            extract_pdf_paragraphs(data)

    @requires_pypdf
    def test_rejects_non_pdf_bytes(self):
        with self.assertRaisesRegex(PdfExtractionError, "not a valid PDF"):
            extract_pdf_paragraphs(b"not a pdf")

    @requires_pypdf
    def test_rejects_non_pdf_bytes_before_parsing_pdf(self):
        with patch("pypdf.PdfReader") as reader:
            with self.assertRaisesRegex(PdfExtractionError, "not a valid PDF"):
                extract_pdf_paragraphs(b"not a pdf")
        reader.assert_not_called()

    def test_reports_missing_pdf_support_separately_from_bad_pdf(self):
        real_import = builtins.__import__

        def import_without_pypdf(name, *args, **kwargs):
            if name == "pypdf":
                raise ModuleNotFoundError("No module named 'pypdf'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_pypdf):
            with self.assertRaisesRegex(PdfExtractionError, "PDF support is not installed") as context:
                extract_pdf_paragraphs(make_pdf("This is a valid PDF with extractable text."))

        self.assertEqual(str(context.exception), PDF_SUPPORT_NOT_INSTALLED_MESSAGE)

    def test_reports_missing_pdf_support_before_file_validation(self):
        real_import = builtins.__import__

        def import_without_pypdf(name, *args, **kwargs):
            if name == "pypdf":
                raise ModuleNotFoundError("No module named 'pypdf'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_pypdf):
            with self.assertRaisesRegex(PdfExtractionError, "PDF support is not installed") as context:
                extract_pdf_paragraphs(b"not a pdf")

        self.assertEqual(str(context.exception), PDF_SUPPORT_NOT_INSTALLED_MESSAGE)

    # --- Decompression-bomb / decoded-pixel-area guard (P1) ---

    @requires_pypdf
    @requires_pymupdf
    def test_rejects_image_decompression_bomb_before_any_decode(self):
        # The adversarial probe: a single 6000x6000 image in a ~108 KB file. It
        # passes the byte cap (10 MB), the page cap (1 page) and the char cap
        # (little text), but its image would decode to ~144 MB of pixels and drove
        # peak RSS to ~274 MB. The guard must reject it from the CHEAP
        # get_image_info() dimensions BEFORE pypdf's extract_text() decodes anything,
        # so the RSS spike never happens.
        data = make_image_bomb_pdf(6000, 6000)
        self.assertLess(len(data), 10 * 1024 * 1024, "bomb fixture must pass the 10 MB byte cap")

        # Assert NO image decode is reached: extract_text() is what triggers the
        # decode, so it must NOT be called once the pre-extraction guard fires.
        with patch.object(pdf_text, "_extract_geo_lines") as extract_spy:
            with self.assertRaisesRegex(PdfExtractionError, "decompression bomb"):
                extract_pdf_document(data)
        extract_spy.assert_not_called()

    @requires_pypdf
    @requires_pymupdf
    def test_image_bomb_rejected_without_rss_spike(self):
        # Behavioural proof the bomb is rejected PRE-decode: measure peak RSS growth
        # while extracting the 6000x6000 bomb. With the pre-decode guard the image
        # stream is never decoded, so the ~144 MB pixel buffer (which previously
        # spiked RSS to ~274 MB) is never allocated.
        data = make_image_bomb_pdf(6000, 6000)

        before_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        with self.assertRaisesRegex(PdfExtractionError, "decompression bomb"):
            extract_pdf_document(data)
        after_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS, kilobytes on Linux; normalize to bytes.
        import sys

        scale = 1 if sys.platform == "darwin" else 1024
        growth_bytes = max(0, after_kb - before_kb) * scale
        # The decoded buffer alone would be ~144 MB. Allow generous slack for
        # interpreter churn but stay far below the decode cost: 60 MB.
        self.assertLess(
            growth_bytes,
            60 * 1024 * 1024,
            f"peak RSS grew {growth_bytes} bytes — the image stream was likely decoded",
        )

    @requires_pypdf
    @requires_pymupdf
    def test_normal_image_pdf_passes_the_pixel_guard(self):
        # A normal embedded image (a tiny logo) is well under the 24 Mpix budget and
        # must extract successfully — the guard rejects bombs, not legitimate images.
        data = make_image_pdf()

        extraction = extract_pdf_document(data)

        self.assertTrue(extraction.paragraphs)
        self.assertIn("Confidential Information", extraction.paragraphs[0]["text"])
        self.assertGreaterEqual(extraction.quality["visual_profile"]["image_count"], 1)

    @requires_pypdf
    @requires_pymupdf
    def test_image_just_under_budget_passes_just_over_rejects(self):
        # Boundary check around MAX_PDF_IMAGE_PIXELS. A 4000x4000 image = 16 Mpix is
        # under the 24 Mpix budget and must pass; a 5000x5000 = 25 Mpix is over and
        # must be rejected. Both are tiny single-color files (well under the byte cap).
        under = make_image_bomb_pdf(4000, 4000)  # 16 Mpix < 24 Mpix
        self.assertEqual(extract_pdf_document(under).quality["visual_profile"]["image_count"], 1)

        over = make_image_bomb_pdf(5000, 5000)  # 25 Mpix > 24 Mpix
        with self.assertRaisesRegex(PdfExtractionError, "decompression bomb"):
            extract_pdf_document(over)

    @requires_pypdf
    @requires_pymupdf
    def test_pixel_guard_fails_open_when_pymupdf_missing(self):
        # The guard must FAIL OPEN: if PyMuPDF is unavailable it cannot probe image
        # dimensions, so it must NOT reject — a reviewable PDF is never blocked on the
        # guard's own infrastructure gap. (A normal small image PDF still extracts.)
        data = make_image_pdf()
        real_import = builtins.__import__

        def import_without_fitz(name, *args, **kwargs):
            if name == "fitz":
                raise ModuleNotFoundError("No module named 'fitz'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_fitz):
            extraction = extract_pdf_document(data)
        self.assertTrue(extraction.paragraphs)

    @requires_pypdf
    def test_extract_reasserts_byte_size_limit_internally(self):
        # Belt-and-suspenders (P3): extract_pdf_document re-asserts the byte ceiling
        # itself rather than trusting an upstream ensure_document_size. A blob over the
        # (here patched-tiny) cap is rejected before any PDF parsing.
        data = make_pdf("This Agreement shall be governed by the laws of California.")

        with patch.object(pdf_text, "MAX_PDF_DOCUMENT_BYTES", 50):
            with patch("pypdf.PdfReader") as reader:
                with self.assertRaisesRegex(PdfExtractionError, "exceeds the 50 byte review limit"):
                    extract_pdf_document(data)
            reader.assert_not_called()

    # --- Drawings-bomb cap in the visual profile (P2) ---

    @requires_pypdf
    @requires_pymupdf
    def test_visual_profile_caps_drawings_count(self):
        # A "drawings bomb": many vector paths on one page. With the per-page cap
        # patched low, the profile must STOP counting at the cap (rather than letting
        # an unbounded get_drawings() materialization dominate memory), still report
        # drawing PRESENCE, and flag drawings_count_capped.
        data = make_drawings_pdf(40)

        with patch.object(pdf_text, "MAX_PDF_DRAWINGS_PER_PAGE", 5), patch.object(
            pdf_text, "MAX_PDF_DRAWINGS_TOTAL", 5
        ):
            extraction = extract_pdf_document(data)

        visual_profile = extraction.quality["visual_profile"]
        self.assertEqual(visual_profile["status"], "ready")
        # The clamp holds the count at the cap, never the true (larger) number.
        self.assertLessEqual(visual_profile["drawing_count"], 5)
        self.assertTrue(visual_profile["drawings_count_capped"])
        # Presence is preserved even though the exact count was clamped.
        self.assertTrue(visual_profile["pages_with_drawings"] >= 1)
        self.assertIn("drawings_or_borders", visual_profile["visual_features"])

    @requires_pypdf
    @requires_pymupdf
    def test_visual_profile_does_not_flag_capped_under_normal_load(self):
        # A handful of borders is well under the cap: drawings_count_capped is False
        # and the true count is reported.
        data = make_drawings_pdf(3)

        extraction = extract_pdf_document(data)

        visual_profile = extraction.quality["visual_profile"]
        self.assertEqual(visual_profile["status"], "ready")
        self.assertFalse(visual_profile["drawings_count_capped"])
        self.assertGreaterEqual(visual_profile["drawing_count"], 1)


BODY_LINE_HEIGHT = 14.0
PARAGRAPH_GAP = 30.0


def geo(text, *, left_x=72.0, y=0.0, font_size=12.0):
    return GeoLine(text=text, left_x=left_x, y=y, font_size=font_size)


def stacked_clauses(clauses, *, left_x=72.0, font_size=12.0):
    """Lay clauses out top-to-bottom: ``BODY_LINE_HEIGHT`` between wrapped lines
    within a clause and ``PARAGRAPH_GAP`` between clauses, returning GeoLines."""

    lines = []
    y = 720.0
    for clause_index, clause in enumerate(clauses):
        if clause_index:
            y -= PARAGRAPH_GAP
        for line_index, text in enumerate(clause):
            if line_index:
                y -= BODY_LINE_HEIGHT
            lines.append(geo(text, left_x=left_x, y=y, font_size=font_size))
    return lines


class GeometrySplitterTests(unittest.TestCase):
    """APPROACH B: geometry-aware clause splitting.

    The cardinal invariant is NEVER MERGE two separate clauses. These tests drive
    ``_split_pdf_paragraphs`` directly with GeoLines so they run without pypdf.
    """

    def test_never_merges_two_separate_prose_clauses_with_vertical_gap(self):
        # Two UNNUMBERED, UN-HEADED prose clauses. The first ends near wrap width
        # (no terminal punctuation at the break); the second starts lowercase, so
        # text alone sees no boundary. The vertical gap keeps them apart.
        lines = stacked_clauses([
            [
                "The receiving party shall keep all Confidential Information confidential and shall",
                "not use it for any purpose other than the permitted purpose",
            ],
            [
                "this agreement is governed by the laws of england and applies",
                "to all disputes between the parties hereto",
            ],
        ])

        blocks = _split_pdf_paragraphs(lines)

        self.assertGreaterEqual(len(blocks), 2)
        self.assertTrue(any("permitted purpose" in block for block in blocks))
        self.assertTrue(any("england" in block for block in blocks))
        # Cardinal invariant: the two clauses never land in one block.
        self.assertFalse(
            any("permitted purpose" in block and "england" in block for block in blocks)
        )

    def test_four_plain_prose_clauses_do_not_collapse_by_merging(self):
        clauses = [
            [
                "The receiving party shall hold the Confidential Information in strict confidence and",
                "shall not disclose it to any third party whatsoever",
            ],
            [
                "the information shall be used solely for evaluating the proposed transaction between",
                "the parties and for no other purpose at all",
            ],
            [
                "this document represents the entire agreement between the parties and supersedes all",
                "prior understandings whether written or oral",
            ],
            [
                "any dispute arising out of this agreement shall be subject to the exclusive",
                "jurisdiction of the courts of england and wales",
            ],
        ]

        blocks = _split_pdf_paragraphs(stacked_clauses(clauses))

        # Merging is forbidden; further fragmenting is acceptable.
        self.assertGreaterEqual(len(blocks), 4)
        for marker in ("confidence", "evaluating", "entire agreement", "jurisdiction"):
            hits = [block for block in blocks if marker in block]
            self.assertEqual(len(hits), 1, f"marker {marker!r} should sit in exactly one block")
            # No block may carry two of the four distinct clause markers.
            other_markers = {"confidence", "evaluating", "entire agreement", "jurisdiction"} - {marker}
            self.assertFalse(any(other in hits[0] for other in other_markers))

    def test_wrapped_mid_sentence_clause_stays_one_block(self):
        # A genuine mid-sentence wrap (every break is prev-UNFINISHED + next-lowercase)
        # must stay ONE block: this is the one sub-pitch JOIN never-merge still allows,
        # because the pairing cannot be two clauses (a clause never starts lowercase,
        # and the prior clause never ends without terminal punctuation).
        lines = stacked_clauses([
            [
                "The receiving party shall protect the Confidential Information using",
                "the same care it uses for its own information and shall apply",
                "no less than a reasonable standard of care at all times.",
            ],
        ])

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(len(blocks), 1)
        self.assertIn("reasonable standard", blocks[0])

    def test_multi_sentence_clause_splits_at_a_line_break_sentence_boundary(self):
        # ACCEPTED-BY-DESIGN (the never-merge cost): a SINGLE clause whose internal
        # sentence boundary aligns exactly with a line break fragments into TWO
        # blocks. The first line ends a sentence ("...its own information.") and the
        # next line begins a fresh sentence ("Such care..."), one wrap apart. Because
        # never-merge is absolute, the splitter cannot tell this apart from two
        # genuinely-separate one-line clauses, so it SPLITS. We assert this is
        # intentional: a finished sentence followed by a sentence-start never joins.
        lines = stacked_clauses([
            [
                "The receiving party shall protect the Confidential Information using",
                "the same care it uses for its own information.",
                "Such care shall be no less than a reasonable standard at all times.",
            ],
        ])

        blocks = _split_pdf_paragraphs(lines)

        # The mid-sentence wrap (line 1 -> line 2) still joins; the sentence boundary
        # (line 2 -> line 3) splits. Two blocks, by design.
        self.assertEqual(len(blocks), 2)
        self.assertIn("its own information.", blocks[0])
        self.assertTrue(blocks[1].startswith("Such care"))
        self.assertIn("reasonable standard", blocks[1])

    def test_outlier_recital_line_does_not_fragment_following_clause(self):
        # A long recital line, then a real paragraph gap to a genuinely-wrapped
        # clause. The outlier must not distort the line-pitch estimate enough to
        # split the wrapped clause that follows.
        lines = [
            geo(
                "This Confidentiality Agreement is made on 1 January 2026 between Acme Corp of "
                "1 High Street London and Beta Ltd of 2 Market Square Leeds.",
                y=720.0,
            ),
            geo(
                "The receiving party shall keep all Confidential Information strictly confidential and",
                y=690.0,  # 30pt paragraph gap
            ),
            geo(
                "shall not disclose it to third parties without prior written consent.",
                y=676.0,  # 14pt wrap -> same clause
            ),
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(len(blocks), 2)
        self.assertIn("Acme Corp", blocks[0])
        self.assertIn("strictly confidential", blocks[1])
        self.assertIn("prior written consent", blocks[1])

    def test_numbered_clause_after_sentence_end_splits(self):
        # A numbered clause marker after a finished sentence opens a new clause. Under
        # the OPTION-B contract its CAPITALIZED body ("This Agreement remains...") is a
        # sentence-start, not a lowercase continuation, so the marker-led-open guard no
        # longer absorbs it — "2. Term" SPLITS from its body too (accepted never-merge
        # fragmentation). The "2. Term" marker stands alone as its own block, and the
        # first clause never merges with the numbered clause.
        lines = [
            geo("Confidential Information means all non-public information disclosed by either party.", y=720.0),
            geo("2. Term", y=706.0),  # numbered marker at a normal wrap gap -> splits
            geo("This Agreement remains in force for five years from the effective date.", y=692.0),
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(
            blocks,
            [
                "Confidential Information means all non-public information disclosed by either party.",
                "2. Term",
                "This Agreement remains in force for five years from the effective date.",
            ],
        )
        self.assertFalse(
            any("either party" in block and "remains in force" in block for block in blocks)
        )

    def test_heading_font_jump_splits_clause(self):
        lines = [
            geo("the foregoing obligations shall survive termination of this agreement", y=720.0, font_size=12.0),
            geo("Governing Law", y=690.0, font_size=16.0),  # larger font -> heading
            geo("This Agreement is governed by the laws of England and Wales.", y=674.0, font_size=12.0),
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertGreaterEqual(len(blocks), 2)
        self.assertFalse(
            any("survive termination" in block and "England and Wales" in block for block in blocks)
        )

    def test_standalone_clause_number_after_wrapped_line_splits(self):
        # Mirrors test_preserves_standalone_clause_numbers but at GeoLine level: a
        # bare clause number on its own line opens a new clause even at a normal
        # wrap gap, once the prior clause has completed a sentence.
        #
        # ROUND-7 CONTRACT (never-merge-absolute): ALL THREE joins now require a
        # LOWERCASE continuation. A bare number above a CAPITALIZED title is a
        # sentence-start, so JOIN 1 no longer fires — the number FRAGMENTS from its
        # title. The capitalized body sentence after each title likewise SPLITS. Every
        # line break here is capitalized-next, so each line lands in its own block, and
        # two distinct clauses never merge.
        lines = [
            geo("1", y=720.0),
            geo("Definitions", y=706.0),
            geo("Confidential Information means non-public information.", y=692.0),
            geo("2", y=678.0),
            geo("Term", y=664.0),
            geo("The confidentiality obligations survive for five years.", y=650.0),
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(
            blocks,
            [
                "1",
                "Definitions",
                "Confidential Information means non-public information.",
                "2",
                "Term",
                "The confidentiality obligations survive for five years.",
            ],
        )
        self.assertFalse(any("Definitions" in block and "Term" in block for block in blocks))

    def test_never_merge_holds_when_geometry_is_absent(self):
        # Pathological PDF: no geometry at all. We cannot tell a wrap from a
        # boundary, so we MUST fail safe by fragmenting rather than merging.
        lines = [
            GeoLine("The receiving party shall keep all information confidential and shall", None, None, None),
            GeoLine("not use it for any purpose other than the permitted purpose", None, None, None),
            GeoLine("this agreement is governed by the laws of england and applies", None, None, None),
            GeoLine("to all disputes between the parties hereto", None, None, None),
        ]

        blocks = _split_pdf_paragraphs(lines)

        # Never merge: the two distinct clauses must not share a block.
        self.assertFalse(
            any("permitted purpose" in block and "england" in block for block in blocks)
        )
        # Honest cost: with no geometry we fragment aggressively.
        self.assertGreaterEqual(len(blocks), 2)

    def test_geometry_path_never_merges_across_a_clause_corpus(self):
        # Proof-style sweep: every ordered pair of distinct clauses, separated by
        # a real paragraph gap, must yield at least two blocks (never one).
        import itertools

        corpus = [
            ["1. Definitions", "Confidential Information means all non-public information disclosed", "by either party."],
            ["The receiving party shall keep all Confidential Information confidential and shall", "not use it for any permitted purpose"],
            ["this agreement is governed by the laws of england and applies", "to all disputes between the parties hereto"],
            ["The parties agree that any breach of this agreement", "may cause irreparable harm to the disclosing party"],
            ["nothing in this agreement grants any licence under any intellectual property", "of the disclosing party to the receiving party"],
        ]
        for first, second in itertools.permutations(range(len(corpus)), 2):
            with self.subTest(first=first, second=second):
                blocks = _split_pdf_paragraphs(stacked_clauses([corpus[first], corpus[second]]))
                self.assertGreaterEqual(len(blocks), 2)

    # --- Round-2 regression: the SINGLE-LINE-CLAUSE page (the suite's blind spot).
    # Every existing test clause wraps to >=2 lines, so a wrapped line is always
    # available to sample the body pitch. On a page where EVERY clause is one line
    # (definitions list, one-line governing-law/term clauses) there is no wrapped
    # line, and the old pitch estimate took the CLAUSE gap itself as the wrap pitch
    # -> the boundary threshold became unreachable -> separate clauses MERGED. ---

    SINGLE_LINE_CLAUSES = [
        "Confidential Information means non-public information disclosed by either party.",
        "This Agreement is governed by the laws of England and Wales.",
        "The term of this Agreement is five years from the Effective Date.",
        "Each party shall bear its own costs in connection with this Agreement.",
        "No party may assign this Agreement without prior written consent.",
    ]

    def _single_line_page(self, gap, *, font_size=12.0):
        lines = []
        y = 720.0
        for index, text in enumerate(self.SINGLE_LINE_CLAUSES):
            if index:
                y -= gap
            lines.append(geo(text, y=y, font_size=font_size))
        return lines

    def test_single_line_clauses_never_merge_uniform_gap_sweep(self):
        # Uniform single-line page: EVERY clause gap is identical (the worst case,
        # where the old code took the clause gap as the wrap pitch and collapsed the
        # whole page into one block). Swept across 14pt..50pt -> every clause must
        # land in its own block; none may merge.
        markers = ["non-public information", "England and Wales", "term of this", "bear its own", "may assign"]
        for gap in (14.0, 16.0, 18.0, 20.0, 25.0, 28.0, 30.0, 40.0, 50.0):
            with self.subTest(gap=gap):
                blocks = _split_pdf_paragraphs(self._single_line_page(gap))
                self.assertEqual(
                    len(blocks),
                    len(self.SINGLE_LINE_CLAUSES),
                    f"single-line clauses must each split at gap {gap}: {blocks}",
                )
                for block in blocks:
                    present = [marker for marker in markers if marker in block]
                    self.assertLessEqual(
                        len(present), 1, f"two clauses merged at gap {gap}: {block!r}"
                    )

    def test_single_line_clauses_never_merge_varied_gap(self):
        # Varied single-line page: gaps 14/20/28/50 between five one-line clauses.
        # No wrapped line exists to calibrate the pitch, yet all five must split.
        gaps = [14.0, 20.0, 28.0, 50.0]
        lines = []
        y = 720.0
        for index, text in enumerate(self.SINGLE_LINE_CLAUSES):
            if index:
                y -= gaps[index - 1]
            lines.append(geo(text, y=y))

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(len(blocks), len(self.SINGLE_LINE_CLAUSES))
        markers = ["non-public information", "England and Wales", "term of this", "bear its own", "may assign"]
        for block in blocks:
            present = [marker for marker in markers if marker in block]
            self.assertLessEqual(len(present), 1, f"two clauses merged: {block!r}")

    def test_no_geometry_fallback_never_merges_two_clauses(self):
        # Geometry UNAVAILABLE (visitor never fired): GeoLines carry no coordinates.
        # The fallback must be split-biased (accept fragmentation, never merge): it
        # splits at every line break except a clause-number/title pairing, so two
        # genuinely-separate clauses can never share a block.
        clause_pairs = [
            (
                "The receiving party shall keep all Confidential Information confidential and shall",
                "not use it for any purpose other than the permitted purpose",
                "this agreement is governed by the laws of england and applies",
                "to all disputes between the parties hereto",
                ("permitted purpose", "england"),
            ),
            (
                "Confidential Information means non-public information disclosed by either party",
                "and includes all technical and commercial data",
                "the receiving party shall return all materials on demand",
                "without retaining any copies thereof",
                ("disclosed by either party", "return all materials"),
            ),
        ]
        for *texts, (left, right) in clause_pairs:
            with self.subTest(left=left):
                lines = [GeoLine(text, None, None, None) for text in texts]
                blocks = _split_pdf_paragraphs(lines)
                self.assertGreaterEqual(len(blocks), 2)
                self.assertFalse(
                    any(left in block and right in block for block in blocks),
                    f"no-geometry fallback merged two clauses: {blocks}",
                )

    def test_no_geometry_fallback_keeps_clause_number_title_pairing(self):
        # The one join the split-biased fallback still makes: a bare clause number
        # immediately followed by its title is a single marker, never split.
        lines = [
            GeoLine("1", None, None, None),
            GeoLine("Definitions", None, None, None),
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(blocks, ["1 Definitions"])

    def test_unpunctuated_clause_does_not_absorb_next_across_a_gap(self):
        # Adversarial never-merge case on a CALIBRATED wrapping page: the first
        # clause's last line is unfinished (no terminal punctuation) and the next
        # clause starts lowercase, separated by a gap LARGER than the proven wrap
        # pitch but smaller than 1.4x it (the no-man's-land). The larger gap is a
        # boundary, so the runaway clause must not swallow the next clause.
        lines = [
            geo("The first clause wraps across two lines and continues", y=720.0),
            geo("onto a second line without any terminal punctuation", y=706.0),  # 14pt proven wrap
            geo("second clause also has no period and starts lowercase here", y=689.0),  # 17pt no-man's-land gap
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertGreaterEqual(len(blocks), 2)
        self.assertFalse(
            any("first clause wraps" in block and "second clause also" in block for block in blocks),
            f"a runaway unpunctuated clause absorbed the next across a gap: {blocks}",
        )

    def test_wrap_pitch_is_font_anchored_not_taken_from_clause_gap(self):
        # The pitch must come from the FONT (~1.2 * font), not the smallest observed
        # gap. On a single-line page the only gaps are clause gaps; the estimate must
        # NOT collapse onto them (that was the merge bug).
        lines = self._single_line_page(30.0, font_size=12.0)

        pitch = _dominant_line_height(lines, 12.0)

        # font-anchored wrap pitch ~ 1.2 * 12 = 14.4, never the 30pt clause gap.
        self.assertAlmostEqual(pitch, 14.4, places=3)
        self.assertLess(pitch, 30.0)

    def test_wrap_pitch_refines_downward_when_real_wraps_exist(self):
        # When wrapped lines DO exist (gap below the font pitch), the smallest-gap
        # cluster may refine the pitch downward, but never above the font anchor.
        lines = [
            geo("first wrapped clause line one continues onto", y=720.0, font_size=12.0),
            geo("a second line at a tight 11pt wrap.", y=709.0, font_size=12.0),  # 11pt wrap
            geo("a separate clause sits a full paragraph below.", y=679.0, font_size=12.0),  # 30pt gap
        ]

        pitch = _dominant_line_height(lines, 12.0)

        self.assertLessEqual(pitch, 14.4)  # never above the font anchor
        self.assertAlmostEqual(pitch, 11.0, places=3)  # refined to the observed wrap

    def test_lowercase_next_clause_after_28pt_gap_still_splits(self):
        # Re-confirm the geometry win B got right: a 28pt vertical gap to a
        # lowercase-starting next line is a real clause boundary that text-only
        # approaches structurally cannot detect. It must still SPLIT.
        lines = [
            geo("The receiving party shall keep all Confidential Information confidential and shall", y=720.0),
            geo("not use it for any purpose other than the permitted purpose", y=706.0),  # wrap (14pt)
            geo("this agreement is governed by the laws of england and applies", y=678.0),  # 28pt gap, lowercase
            geo("to all disputes between the parties hereto", y=664.0),  # wrap (14pt)
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertGreaterEqual(len(blocks), 2)
        self.assertFalse(
            any("permitted purpose" in block and "england" in block for block in blocks)
        )

    # --- Round-3 BLOCKER: round-2 gated the geometry gap-split behind a PROVEN wrap.
    # On a single-line-per-clause page no wrap is proven, so the gap-split was off and
    # the decision fell to "previous ends a sentence?". Single-line clauses that do
    # NOT end in terminal punctuation then merged across the ENTIRE 12pt..50pt sweep,
    # ignoring the real vertical gap. The fix: the font-anchored pitch is a valid
    # split threshold ON ITS OWN — a gap >= pitch splits regardless of punctuation or
    # whether any wrap was ever proven on the page. ---

    UNPUNCTUATED_SINGLE_LINE_CLAUSES = [
        "Confidential Information means non-public information disclosed by either party",
        "Affiliate means any entity controlling or controlled by a party",
        "Permitted Purpose means evaluating the proposed transaction between the parties",
        "Representatives means directors officers employees and professional advisers",
    ]

    def _unpunctuated_single_line_page(self, gap, *, font_size=12.0):
        lines = []
        y = 720.0
        for index, text in enumerate(self.UNPUNCTUATED_SINGLE_LINE_CLAUSES):
            if index:
                y -= gap
            lines.append(geo(text, y=y, font_size=font_size))
        return lines

    def test_unpunctuated_single_line_clauses_never_merge_gap_sweep(self):
        # THE BLOCKER. Single-line definition-list entries with NO terminal
        # punctuation (no period, no colon). On every gap from 12pt to 50pt each
        # entry MUST land in its own block; none may merge, even though no wrap is
        # proven and no line ends a sentence.
        markers = ["Confidential Information", "Affiliate", "Permitted Purpose", "Representatives"]
        for gap in (12.0, 14.0, 16.0, 18.0, 20.0, 25.0, 28.0, 30.0, 40.0, 50.0):
            with self.subTest(gap=gap):
                blocks = _split_pdf_paragraphs(self._unpunctuated_single_line_page(gap))
                self.assertEqual(
                    len(blocks),
                    len(self.UNPUNCTUATED_SINGLE_LINE_CLAUSES),
                    f"unpunctuated single-line clauses must each split at gap {gap}: {blocks}",
                )
                for block in blocks:
                    present = [marker for marker in markers if marker in block]
                    self.assertLessEqual(
                        len(present), 1, f"two unpunctuated clauses merged at gap {gap}: {block!r}"
                    )

    def test_comma_terminated_single_line_clauses_never_merge_gap_sweep(self):
        # Companion to the blocker: definition-list entries terminated by a COMMA
        # (also non-terminal punctuation, so the previous line never "ends a
        # sentence"). The geometric gap must still split them at every gap.
        clauses = [
            "Confidential Information means non-public information disclosed by either party,",
            "Affiliate means any entity controlling or controlled by a party,",
            "Permitted Purpose means evaluating the proposed transaction,",
        ]
        markers = ["Confidential Information", "Affiliate", "Permitted Purpose"]
        for gap in (12.0, 14.0, 18.0, 28.0, 50.0):
            with self.subTest(gap=gap):
                lines = []
                y = 720.0
                for index, text in enumerate(clauses):
                    if index:
                        y -= gap
                    lines.append(geo(text, y=y))
                blocks = _split_pdf_paragraphs(lines)
                self.assertEqual(len(blocks), len(clauses), f"comma clauses merged at gap {gap}: {blocks}")
                for block in blocks:
                    present = [marker for marker in markers if marker in block]
                    self.assertLessEqual(len(present), 1, f"two comma clauses merged at gap {gap}: {block!r}")

    def test_unterminated_line_with_lowercase_next_and_real_gap_splits(self):
        # An unfinished previous line (no terminal punctuation) followed by a
        # LOWERCASE next line separated by a gap LARGER than the wrap pitch. The text
        # signals (unfinished + lowercase continuation) look like a wrap, but the
        # vertical gap proves a boundary -> SPLIT. A runaway unpunctuated clause must
        # not swallow the next clause across a real gap.
        for gap in (16.0, 20.0, 25.0, 30.0, 50.0):
            with self.subTest(gap=gap):
                lines = [
                    geo("the receiving party shall hold the information in strict confidence and", y=720.0),
                    geo("shall protect it from any unauthorised disclosure", y=706.0),  # 14pt proven wrap
                    geo("the parties further agree that all notices shall be in writing and", y=706.0 - gap),
                ]
                blocks = _split_pdf_paragraphs(lines)
                self.assertGreaterEqual(len(blocks), 2)
                self.assertFalse(
                    any("strict confidence" in block and "all notices" in block for block in blocks),
                    f"unterminated clause absorbed the next across a {gap}pt gap: {blocks}",
                )

    def test_single_line_with_punctuation_still_splits_gap_sweep(self):
        # KEEP: round-2 already split single-line clauses that end in a period. The
        # round-3 fix must not regress that — period-terminated one-line clauses still
        # each split across the full gap sweep.
        markers = ["non-public information", "England and Wales", "term of this", "bear its own", "may assign"]
        for gap in (12.0, 14.0, 18.0, 28.0, 50.0):
            with self.subTest(gap=gap):
                blocks = _split_pdf_paragraphs(self._single_line_page(gap))
                self.assertEqual(len(blocks), len(self.SINGLE_LINE_CLAUSES), f"at gap {gap}: {blocks}")
                for block in blocks:
                    present = [marker for marker in markers if marker in block]
                    self.assertLessEqual(len(present), 1, f"two clauses merged at gap {gap}: {block!r}")

    def test_round3_blocker_round_trips_through_pypdf(self):
        # End-to-end through the real pypdf pipeline: unpunctuated single-line
        # definition entries separated by varied gaps must each remain their own
        # paragraph. This exercises the geometry the visitor actually reports.
        data = make_pdf_positioned([
            ("Confidential Information means non-public information disclosed by either party", 72, 720, 12),
            ("Affiliate means any entity controlling or controlled by a party", 72, 700, 12),  # 20pt
            ("Permitted Purpose means evaluating the proposed transaction between the parties", 72, 672, 12),  # 28pt
            ("Representatives means directors officers employees and professional advisers", 72, 622, 12),  # 50pt
        ])

        texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

        markers = ["Confidential Information", "Affiliate", "Permitted Purpose", "Representatives"]
        for text in texts:
            present = [marker for marker in markers if marker in text]
            self.assertLessEqual(len(present), 1, f"two unpunctuated clauses merged: {text!r}")

    # --- Round-4 BLOCKER: the page-global wrap-signal LEAK. Round-3 fed a page-wide
    # "this page has at least one proven wrap" flag into the sub-pitch JOIN: when the
    # page contained ANY real mid-sentence wrap, two genuinely-separate finished
    # period-terminated single-line clauses sitting one wrap apart MERGED. The fix
    # removes the finished-sentence JOIN entirely, so a finished sentence followed by
    # a sentence-start always splits regardless of page wraps. ---

    def test_round4_leak_repro_finished_clauses_split_despite_page_wrap(self):
        # THE LEAK REPRO at the helper layer. One genuine mid-sentence wrap (a clause
        # whose first line is UNFINISHED and whose second line is a lowercase
        # continuation) PLUS two separate finished period-terminated single-line
        # clauses one wrap apart. Under round-3, the wrap elsewhere flipped
        # page_has_wraps on and the two finished clauses MERGED. They must SPLIT.
        lines = [
            # A real mid-sentence wrap (proves a wrap exists on the page).
            geo("The receiving party shall hold the Confidential Information in strict", y=720.0),
            geo("confidence and shall not disclose it to any third party.", y=706.0),  # 14pt wrap
            # Two finished single-line clauses, each one wrap (14pt) apart.
            geo("This Agreement is governed by the laws of England and Wales.", y=692.0),  # 14pt
            geo("The term of this Agreement is five years from the date.", y=678.0),  # 14pt
        ]

        blocks = _split_pdf_paragraphs(lines)

        # The two finished single-line clauses must never share a block.
        self.assertFalse(
            any("England and Wales" in block and "five years" in block for block in blocks),
            f"the round-3 leak merged two finished clauses one wrap apart: {blocks}",
        )
        self.assertTrue(any("England and Wales" in block for block in blocks))
        self.assertTrue(any("five years" in block for block in blocks))

    @requires_pypdf
    def test_round4_leak_repro_round_trips_through_pypdf(self):
        # THE LEAK REPRO end-to-end through the real pypdf pipeline. Same shape: a real
        # mid-sentence wrap plus two finished single-line clauses one ~14pt wrap apart.
        data = make_pdf_positioned([
            ("The receiving party shall hold the Confidential Information in strict", 72, 720, 12),
            ("confidence and shall not disclose it to any third party.", 72, 706, 12),  # 14pt wrap
            ("This Agreement is governed by the laws of England and Wales.", 72, 692, 12),  # 14pt
            ("The term of this Agreement is five years from the date.", 72, 678, 12),  # 14pt
        ])

        texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

        self.assertFalse(
            any("England and Wales" in text and "five years" in text for text in texts),
            f"the round-3 leak merged two finished clauses end-to-end: {texts}",
        )
        self.assertTrue(any("England and Wales" in text for text in texts))
        self.assertTrue(any("five years" in text for text in texts))

    def test_finished_then_sentence_start_one_wrap_apart_splits_without_any_page_wrap(self):
        # Even with NO mid-sentence wrap anywhere on the page, two finished
        # period-terminated single-line clauses exactly one wrap (14pt) apart must
        # split. (Round-3 already split this case because page_has_wraps was False; we
        # lock it in so the removal of the branch can never regress it.)
        lines = [
            geo("This Agreement is governed by the laws of England and Wales.", y=720.0),
            geo("The term of this Agreement is five years from the date.", y=706.0),  # 14pt wrap apart
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(len(blocks), 2)
        self.assertIn("England and Wales", blocks[0])
        self.assertIn("five years", blocks[1])

    # --- Round-5 BLOCKER: two TEXT-MARKER JOIN guards fired BEFORE the geometry gap
    # check, so they merged two separate clauses at ANY gap. The marker-led-open guard
    # let a clause opened by a number/heading whose last line was unfinished absorb the
    # NEXT line unconditionally — even a separate markerless clause far below. The
    # standalone-number-previous guard let a bare number join its next line at any gap.
    # The fix makes the GEOMETRY GAP CHECK the FIRST gate: when geometry is present, a
    # gap > pitch SPLITS unconditionally, before any join guard runs, so the join
    # guards can only ever fire at TRUE sub-pitch adjacency (gap <= pitch). ---

    def test_round5_marker_led_open_does_not_absorb_clause_across_gap(self):
        # THE ROUND-5 REPRO at the helper layer. "5. Confidentiality" opens a
        # marker-led block; its own body line sits one wrap (14pt) below and JOINS; a
        # SEPARATE governing-law clause sits a real >pitch (50pt) gap below and must
        # SPLIT — the marker-led-open guard must NOT absorb it.
        lines = [
            geo("5. Confidentiality", y=720.0),
            geo("the receiving party shall keep all Confidential Information strictly confidential", y=706.0),  # 14pt body
            geo("This Agreement is governed by the laws of England and Wales.", y=656.0),  # 50pt gap -> SPLIT
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(len(blocks), 2, blocks)
        self.assertFalse(
            any("Confidentiality" in block and "England and Wales" in block for block in blocks),
            f"the marker-led-open guard absorbed a separate clause across a 50pt gap: {blocks}",
        )
        # The marker-led block still absorbs its OWN adjacent body line.
        self.assertIn("strictly confidential", blocks[0])
        self.assertTrue(blocks[1].startswith("This Agreement"))

    @requires_pypdf
    def test_round5_marker_led_open_does_not_absorb_clause_across_gap_round_trips(self):
        # THE ROUND-5 REPRO end-to-end through the real pypdf pipeline.
        data = make_pdf_positioned([
            ("5. Confidentiality", 72, 720, 12),
            ("the receiving party shall keep all Confidential Information strictly confidential", 72, 706, 12),  # 14pt
            ("This Agreement is governed by the laws of England and Wales.", 72, 656, 12),  # 50pt gap
        ])

        texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

        self.assertEqual(len(texts), 2, texts)
        self.assertFalse(
            any("Confidentiality" in text and "England and Wales" in text for text in texts),
            f"marker-led-open absorbed a separate clause end-to-end: {texts}",
        )
        self.assertIn("strictly confidential", texts[0])
        self.assertTrue(texts[1].startswith("This Agreement"))

    def test_round5_standalone_number_previous_does_not_join_across_gap(self):
        # A bare standalone clause number as the previous line must NOT join its next
        # line across a real >pitch gap. At sub-pitch (its own title) it joins; across
        # a 50pt paragraph gap the next clause splits.
        lines = [
            geo("5.", y=720.0),
            geo("This Agreement is governed by the laws of England and Wales.", y=670.0),  # 50pt gap -> SPLIT
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(len(blocks), 2, blocks)
        self.assertEqual(blocks[0], "5.")
        self.assertTrue(blocks[1].startswith("This Agreement"))

    @requires_pypdf
    def test_round5_standalone_number_previous_does_not_join_across_gap_round_trips(self):
        # End-to-end twin: a bare clause number above a real >pitch gap splits from the
        # following clause.
        data = make_pdf_positioned([
            ("5.", 72, 720, 12),
            ("This Agreement is governed by the laws of England and Wales.", 72, 670, 12),  # 50pt gap
        ])

        texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

        self.assertEqual(len(texts), 2, texts)
        self.assertEqual(texts[0], "5.")
        self.assertTrue(texts[1].startswith("This Agreement"))

    def test_round7_standalone_number_fragments_from_capitalized_title(self):
        # ROUND-7 CONTRACT (never-merge-absolute): JOIN 1 now also requires a LOWERCASE
        # continuation. A bare clause number above a CAPITALIZED title ("Confidentiality")
        # is a sentence-start, so JOIN 1 no longer fires — the number SPLITS from its
        # title (the accepted safe failure). This is the price of making a lone number
        # incapable of bridging into a separate capitalized clause under an inflated pitch.
        lines = [
            geo("5.", y=720.0),
            geo("Confidentiality", y=706.0),  # 14pt, but CAPITALIZED -> SPLIT (number fragments)
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(blocks, ["5.", "Confidentiality"])

    def test_round7_standalone_number_still_joins_its_lowercase_body(self):
        # The kept JOIN 1: a bare clause number DOES still absorb a LOWERCASE
        # continuation directly beneath it at TRUE sub-pitch adjacency — that is an
        # unambiguous body wrap, not a fresh clause.
        lines = [
            geo("5.", y=720.0),
            geo("the receiving party shall keep all information confidential.", y=706.0),  # lowercase -> JOIN
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(blocks, ["5. the receiving party shall keep all information confidential."])

    def test_round5_marker_led_open_still_joins_its_own_adjacent_body(self):
        # Guard the kept behavior: a marker-led-open block (heading whose last line is
        # unfinished) still absorbs its OWN immediately-adjacent body line at TRUE
        # sub-pitch adjacency.
        lines = [
            geo("5. Confidentiality", y=720.0),
            geo("the receiving party shall keep all Confidential Information strictly confidential", y=706.0),  # 14pt
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(len(blocks), 1)
        self.assertIn("Confidentiality the receiving party", blocks[0])

    # --- Round-6 (Option B): the uniform-spacing pitch-inflation bypass. ---
    # Round 5 made the geometry GAP gate the first split, but it trusts the refined
    # _dominant_line_height as the wrap pitch. On a page with UNIFORM clause spacing
    # (every gap identical) and that spacing sitting just below the font anchor, the
    # refinement settles ON the clause spacing, inflating the pitch so a REAL paragraph
    # gap reads as sub-pitch ("adjacent"). The marker-led-open JOIN then fired across
    # it, absorbing a SEPARATE capitalized clause. Option B closes this with a
    # CONTINUATION GATE: a body-absorbing JOIN may only swallow a LOWERCASE, non-
    # sentence-start line; a capitalized sentence-start is always a NEW clause -> SPLIT.

    def _uniform_spacing_repro_lines(self):
        # GeoLines all font 30, gaps [30, 30, 30, 26, 30] (every gap below the font
        # anchor 1.2*30 = 36, so refinement inflates the pitch up to ~30 and every gap
        # reads as sub-pitch). The capitalized governing-law clause must NOT be absorbed
        # into the marker-led confidentiality block.
        texts = [
            "Clause one obligations apply to the receiving party in full",
            "Clause two obligations apply to the disclosing party in full",
            "Clause three obligations apply to both parties equally here",
            "5. Confidentiality Provisions Apply Strictly",
            "the receiving party shall keep all information strictly",
            "This Agreement is governed by the laws of England and Wales.",
        ]
        gaps = [30, 30, 30, 26, 30]
        y = 720.0
        lines = [geo(texts[0], y=y, font_size=30.0)]
        for index, gap in enumerate(gaps):
            y -= gap
            lines.append(geo(texts[index + 1], y=y, font_size=30.0))
        return lines

    def test_round6_uniform_spacing_pitch_inflation_does_not_merge_clauses(self):
        # THE ROUND-6 REPRO at the helper layer. Even though the inflated pitch makes
        # the 30pt gap before the governing-law clause read as sub-pitch, the marker-led
        # confidentiality block must NOT absorb that separate capitalized clause.
        lines = self._uniform_spacing_repro_lines()

        blocks = _split_pdf_paragraphs(lines)

        # Never-merge: the confidentiality clause and the governing-law clause split.
        self.assertFalse(
            any("Confidentiality" in block and "England and Wales" in block for block in blocks),
            f"uniform-spacing pitch inflation merged two clauses: {blocks}",
        )
        confidentiality = [block for block in blocks if "Confidentiality" in block]
        self.assertEqual(len(confidentiality), 1)
        # The marker-led block still absorbs its OWN lowercase body wrap...
        self.assertIn("the receiving party shall keep all information strictly", confidentiality[0])
        # ...but the capitalized governing-law clause is its own block.
        self.assertNotIn("England and Wales", confidentiality[0])
        self.assertTrue(any(block == "This Agreement is governed by the laws of England and Wales." for block in blocks))

    @requires_pypdf
    def test_round6_uniform_spacing_pitch_inflation_round_trips(self):
        # THE ROUND-6 REPRO end-to-end through the real pypdf pipeline.
        data = make_pdf_positioned([
            ("Clause one obligations apply to the receiving party in full", 72, 720, 30),
            ("Clause two obligations apply to the disclosing party in full", 72, 690, 30),
            ("Clause three obligations apply to both parties equally here", 72, 660, 30),
            ("5. Confidentiality Provisions Apply Strictly", 72, 630, 30),
            ("the receiving party shall keep all information strictly", 72, 604, 30),  # 26pt
            ("This Agreement is governed by the laws of England and Wales.", 72, 574, 30),  # 30pt gap
        ])

        texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

        self.assertFalse(
            any("Confidentiality" in text and "England and Wales" in text for text in texts),
            f"uniform-spacing pitch inflation merged two clauses end-to-end: {texts}",
        )
        confidentiality = [text for text in texts if "Confidentiality" in text]
        self.assertEqual(len(confidentiality), 1)
        self.assertIn("the receiving party shall keep all information strictly", confidentiality[0])
        self.assertNotIn("England and Wales", confidentiality[0])
        self.assertTrue(any("England and Wales" in text for text in texts))

    def test_round6_genuine_mid_sentence_wrap_still_joins_at_sub_pitch(self):
        # The kept JOIN: a genuine mid-sentence wrap (previous line UNFINISHED + next
        # line a LOWERCASE continuation) at sub-pitch must still stay ONE block. The
        # continuation gate permits exactly this — a lowercase non-sentence-start line.
        lines = [
            geo("The receiving party shall keep all Confidential Information strictly", y=720.0),
            geo("confidential and shall not disclose it to any third party at all.", y=706.0),  # 14pt, lowercase
        ]

        blocks = _split_pdf_paragraphs(lines)

        self.assertEqual(len(blocks), 1, blocks)
        self.assertIn("strictly confidential and shall not disclose", blocks[0])

    def test_round6_realistic_numbered_clauses_split_and_keep_their_wraps_whole(self):
        # Realistic numbered/heading clauses at NORMAL spacing: clause-to-clause gaps
        # split, and each clause's OWN mid-sentence wrap stays whole. Under Option B a
        # numbered heading also splits from a capitalized body sentence (the accepted
        # fragmentation), but two distinct clauses never merge, and lowercase wraps join.
        lines = stacked_clauses([
            [
                "1. Confidentiality",
                "the receiving party shall hold the Confidential Information in strict confidence",
                "and shall not disclose it to any third party whatsoever.",
            ],
            [
                "2. Governing Law",
                "this agreement is governed by the laws of England and Wales and the parties",
                "submit to the exclusive jurisdiction of the English courts.",
            ],
        ])

        blocks = _split_pdf_paragraphs(lines)

        # Never-merge across clauses.
        self.assertFalse(
            any("Confidentiality" in block and "Governing Law" in block for block in blocks)
        )
        self.assertFalse(
            any("strict confidence" in block and "exclusive jurisdiction" in block for block in blocks)
        )
        # Each clause body's own wrap (unfinished + lowercase next) stays whole.
        self.assertTrue(
            any("strict confidence and shall not disclose it to any third party whatsoever." in block for block in blocks),
            blocks,
        )
        self.assertTrue(
            any("England and Wales and the parties submit to the exclusive jurisdiction" in block for block in blocks),
            blocks,
        )

    def test_round6_no_geometry_fallback_never_merges_uniform_repro_shape(self):
        # The no-geometry fallback (visitor never fired) must never merge — confirm on
        # the round-6 repro's clause shape with coordinates stripped. The split-biased
        # fallback fragments every line break except a bare-number+title pairing.
        texts = [
            "5. Confidentiality Provisions Apply Strictly",
            "the receiving party shall keep all information strictly",
            "This Agreement is governed by the laws of England and Wales.",
        ]
        lines = [GeoLine(text, None, None, None) for text in texts]

        blocks = _split_pdf_paragraphs(lines)

        self.assertFalse(
            any("Confidentiality" in block and "England and Wales" in block for block in blocks),
            f"no-geometry fallback merged two clauses: {blocks}",
        )
        self.assertGreaterEqual(len(blocks), 2)

    def test_round6_wrap_pitch_cap_holds_under_uniform_clause_spacing(self):
        # DEFENSE-IN-DEPTH (part 2): the refined wrap pitch may never exceed the font
        # anchor (~1.2 * body_font). On the uniform-spacing repro the refinement settles
        # AT the clause spacing (~30) BUT is hard-capped at the anchor 1.2*30 = 36 — it
        # can never be reported ABOVE the anchor. (The continuation gate, not the cap,
        # is what makes the residual at-clause-spacing inflation un-exploitable.)
        lines = self._uniform_spacing_repro_lines()

        pitch = _dominant_line_height(lines, _dominant_font_size(lines))

        self.assertLessEqual(pitch, 36.0 + 1e-6)

    # --- Round-7 BLOCKER: JOIN 1 (standalone-number-previous absorb) was the ONE join
    # round-6 left WITHOUT the continuation gate. So a LONE clause number on its own
    # baseline immediately above a SEPARATE capitalized clause — the common dense
    # legal-PDF hanging-indent layout (uniform tight single-spacing, the number alone
    # on its baseline) — still merged under an inflated/sub-pitch pitch. Round-7 adds
    # the SAME next_is_continuation gate to JOIN 1, so ALL THREE joins now require a
    # LOWERCASE continuation and a capitalized separate clause is never absorbed. ---

    def test_round7_lone_number_does_not_absorb_capitalized_clause_helper(self):
        # THE ROUND-7 REPRO at the helper layer, across realistic body fonts 10/11/12.
        # A lone "5." sits one wrap (gap = font, just under the 1.2*font anchor, so the
        # geometry reads it as sub-pitch "adjacent") above a SEPARATE capitalized
        # governing-law clause. JOIN 1 must NOT absorb it — the two SPLIT.
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font  # < 1.2 * font -> sub-pitch (the hanging-indent tight pitch)
                lines = [
                    geo("5.", y=720.0, font_size=font),
                    geo(
                        "This Agreement is governed by the laws of England and Wales.",
                        y=720.0 - gap,
                        font_size=font,
                    ),
                ]

                blocks = _split_pdf_paragraphs(lines)

                self.assertEqual(len(blocks), 2, blocks)
                self.assertEqual(blocks[0], "5.")
                self.assertEqual(
                    blocks[1],
                    "This Agreement is governed by the laws of England and Wales.",
                )
                self.assertFalse(
                    any("5." in block and "England and Wales" in block for block in blocks),
                    f"JOIN 1 absorbed a separate capitalized clause under a lone number (font {font}): {blocks}",
                )

    @requires_pypdf
    def test_round7_lone_number_does_not_absorb_capitalized_clause_round_trips(self):
        # THE ROUND-7 REPRO end-to-end through the real pypdf pipeline, fonts 10/11/12.
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font  # < 1.2 * font -> sub-pitch hanging-indent layout
                data = make_pdf_positioned([
                    ("5.", 72, 720, font),
                    (
                        "This Agreement is governed by the laws of England and Wales.",
                        72,
                        720 - gap,
                        font,
                    ),
                ])

                texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

                self.assertEqual(len(texts), 2, texts)
                self.assertEqual(texts[0], "5.")
                self.assertEqual(
                    texts[1],
                    "This Agreement is governed by the laws of England and Wales.",
                )
                self.assertFalse(
                    any("5." in text and "England and Wales" in text for text in texts),
                    f"JOIN 1 absorbed a separate capitalized clause end-to-end (font {font}): {texts}",
                )

    def test_round7_lone_number_does_not_absorb_capitalized_title_inflated_pitch(self):
        # Hanging-indent variant where the next line is a capitalized TITLE rather than
        # a full sentence ("Confidentiality"), under an INFLATED pitch (uniform tight
        # spacing). The number fragments from its title — the accepted safe failure —
        # and certainly cannot bridge into a separate clause. Fonts 10/11/12.
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font
                lines = [
                    geo("5.", y=720.0, font_size=font),
                    geo("Confidentiality", y=720.0 - gap, font_size=font),  # capitalized title
                    geo(
                        "This Agreement is governed by the laws of England and Wales.",
                        y=720.0 - 2 * gap,
                        font_size=font,
                    ),
                ]

                blocks = _split_pdf_paragraphs(lines)

                # Never-merge: no single block bridges the title and the separate clause.
                self.assertFalse(
                    any("Confidentiality" in block and "England and Wales" in block for block in blocks),
                    f"a lone number's title absorbed a separate clause (font {font}): {blocks}",
                )
                # The number splits from its capitalized title (accepted safe failure),
                # and the governing-law clause stands entirely alone.
                self.assertEqual(blocks[0], "5.")
                self.assertIn(
                    "This Agreement is governed by the laws of England and Wales.",
                    blocks,
                )

    def test_round7_genuine_mid_sentence_wrap_after_lone_number_still_joins(self):
        # The kept JOIN: a lone number directly above a LOWERCASE continuation is a
        # real body wrap and still JOINS at sub-pitch. Fonts 10/11/12.
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font
                lines = [
                    geo("5.", y=720.0, font_size=font),
                    geo(
                        "the receiving party shall keep all information confidential.",
                        y=720.0 - gap,
                        font_size=font,
                    ),
                ]

                blocks = _split_pdf_paragraphs(lines)

                self.assertEqual(
                    blocks,
                    ["5. the receiving party shall keep all information confidential."],
                )

    # --- Round-8 BLOCKER: the round-7 continuation gate was a NEGATIVE enumeration of
    # clause-start characters (_looks_like_sentence_start: ^[A-Z0-9(] plus a HANDCODED
    # set of quotes). The enumeration was INCOMPLETE — it omitted the curly SINGLE quote
    # U+2018, bullets, dashes, currency/section symbols, [, #, etc. A clause led by an
    # omitted character (e.g. a defined-term clause opening with a smart single quote,
    # which Word emits and pypdf decodes from WinAnsi byte 0x91) was mis-read as a
    # lowercase-style continuation and ABSORBED -> merge. Round-8 INVERTS the gate to a
    # POSITIVE lowercase-only check (_is_lowercase_continuation): a continuation is ONLY
    # a line whose first non-whitespace char is a lowercase letter. Complete by
    # construction — no clause-start character to omit. ---

    def test_round8_smart_single_quote_clause_does_not_absorb_into_number_helper(self):
        # THE ROUND-8 REPRO at the helper layer. A lone "1." sits one wrap above a
        # defined-term clause that OPENS with a curly single quote U+2018 (smart quote).
        # Under round-7 the U+2018 lead was not in the enumeration, so the gate read it
        # as a continuation and JOIN 1 absorbed it. With the lowercase-only gate the
        # U+2018 lead is NOT lowercase -> SPLIT. Fonts 10/11/12.
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font  # < 1.2 * font -> sub-pitch hanging-indent adjacency
                clause = (
                    "‘Confidential Information’ means all non-public "
                    "information disclosed by either party."
                )
                lines = [
                    geo("1.", y=720.0, font_size=font),
                    geo(clause, y=720.0 - gap, font_size=font),
                ]

                blocks = _split_pdf_paragraphs(lines)

                self.assertEqual(len(blocks), 2, blocks)
                self.assertEqual(blocks[0], "1.")
                self.assertEqual(blocks[1], clause)
                self.assertFalse(
                    any("1." in block and "Confidential Information" in block for block in blocks),
                    f"smart-single-quote clause absorbed into a lone number (font {font}): {blocks}",
                )

    @requires_pypdf
    def test_round8_smart_single_quote_clause_splits_round_trips(self):
        # THE ROUND-8 REPRO end-to-end through the REAL pypdf pipeline, with the curly
        # single quotes produced from WinAnsi bytes 0x91 (U+2018) / 0x92 (U+2019) — the
        # exact bytes pypdf decodes from a Word-exported smart-quoted PDF. Fonts 10/11/12.
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font
                clause = (
                    "‘Confidential Information’ means all non-public "
                    "information disclosed by either party."
                )
                data = make_pdf_positioned_winansi([
                    ("1.", 72, 720, font),
                    (clause, 72, 720 - gap, font),
                ])

                texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

                # Confirm the smart quotes actually survived the WinAnsi round-trip
                # (the repro is only meaningful if U+2018/U+2019 reach the gate).
                self.assertTrue(
                    any(text.startswith("‘Confidential Information’") for text in texts),
                    f"smart single quotes did not decode through pypdf: {texts}",
                )
                self.assertEqual(len(texts), 2, texts)
                self.assertEqual(texts[0], "1.")
                self.assertEqual(texts[1], clause)
                self.assertFalse(
                    any("1." in text and "Confidential Information" in text for text in texts),
                    f"smart-single-quote clause merged end-to-end (font {font}): {texts}",
                )

    def test_round8_non_lowercase_leads_all_split_from_join_eligible_previous(self):
        # The completeness proof, exercised: EVERY non-lowercase lead — across the
        # categories the round-7 enumeration omitted AND the ones it covered — falls
        # through to SPLIT from a join-eligible previous line. The previous line is an
        # UNFINISHED lowercase sentence (so JOIN 3, the mid-sentence-wrap join, is armed
        # and would fire on a true continuation), placed one wrap (sub-pitch) above.
        font = 11.0
        gap = font
        non_lowercase_leads = {
            "curly-single-quote(U+2018)": "‘Confidential Information’ means data.",
            "curly-single-close(U+2019)": "’odd lead is still not lowercase.",
            "curly-double-quote(U+201C)": "“Confidential” means data.",
            "straight-double-quote": '"Confidential" means data.',
            "straight-single-quote": "'Confidential' means data.",
            "bullet": "• first enumerated obligation of the party.",
            "hyphen": "- first enumerated obligation of the party.",
            "en-dash": "– first enumerated obligation of the party.",
            "em-dash": "— first enumerated obligation of the party.",
            "middot": "· first enumerated obligation of the party.",
            "open-bracket": "[Reserved] obligations of the party.",
            "hash": "#1 obligation of the party.",
            "currency-dollar": "$5,000 liquidated damages shall apply.",
            "currency-pound": "£5,000 liquidated damages shall apply.",
            "section-symbol": "§ 12 of the Act shall apply.",
            "digit": "5 obligations of the party are listed below.",
            "capital": "Confidential Information means data.",
        }
        for label, second in non_lowercase_leads.items():
            with self.subTest(lead=label):
                lines = [
                    geo(
                        "the parties agree to the following terms and",
                        y=720.0,
                        font_size=font,
                    ),
                    geo(second, y=720.0 - gap, font_size=font),
                ]

                blocks = _split_pdf_paragraphs(lines)

                self.assertEqual(len(blocks), 2, f"{label} was absorbed: {blocks}")
                self.assertFalse(
                    any("the parties agree" in block and second in block for block in blocks),
                    f"{label} (non-lowercase lead) was wrongly absorbed as a continuation: {blocks}",
                )

    def test_round8_genuine_lowercase_mid_sentence_wrap_still_joins(self):
        # The one thing that MUST still join: a genuine mid-sentence wrap. Previous line
        # UNFINISHED (no terminal punctuation) and next line opens with a LOWERCASE letter
        # one wrap (sub-pitch) apart -> JOIN. Covers ASCII and a Unicode lowercase lead.
        font = 11.0
        gap = font
        lowercase_leads = [
            "and business plans disclosed by either party.",  # ASCII lowercase
            "égalité and the remaining obligations of the party.",  # Unicode lowercase (é)
        ]
        for second in lowercase_leads:
            with self.subTest(second=second):
                lines = [
                    geo(
                        "the receiving party shall keep all confidential information confidential",
                        y=720.0,
                        font_size=font,
                    ),
                    geo(second, y=720.0 - gap, font_size=font),
                ]

                blocks = _split_pdf_paragraphs(lines)

                self.assertEqual(
                    blocks,
                    [
                        "the receiving party shall keep all confidential information confidential "
                        + second
                    ],
                )

    def test_round8_lowercase_continuation_helper_is_complete_by_construction(self):
        # Direct unit test of the gate helper: ONLY a lowercase-letter lead (after
        # optional leading whitespace) is a continuation; EVERYTHING else is not.
        for continuation in (
            "and the rest of the sentence",
            "  leading whitespace then lowercase",
            "égalité unicode lowercase lead",  # é
            "ß scharfes s lead",  # ß (lowercase)
        ):
            self.assertTrue(
                pdf_text._is_lowercase_continuation(continuation),
                f"lowercase lead not recognized as continuation: {continuation!r}",
            )
        for non_continuation in (
            "Capitalized start",
            "5 digit start",
            "‘curly single quote",
            "’curly single close",
            "“curly double quote",
            '"straight double quote',
            "'straight single quote",
            "• bullet",
            "- hyphen",
            "– en dash",
            "— em dash",
            "· middot",
            "[bracket",
            "(paren",
            "#hash",
            "$dollar",
            "£ pound",
            "§ section",
            "É unicode UPPER lead",  # É (uppercase) — not a continuation
            "",
            "   ",
        ):
            self.assertFalse(
                pdf_text._is_lowercase_continuation(non_continuation),
                f"non-lowercase lead wrongly treated as continuation: {non_continuation!r}",
            )

    # --- Round-9 BLOCKER: the round-8 gate used ``str.islower()`` on the first char,
    # which is the Unicode LOWERCASE PROPERTY — True for non-letter MARKER glyphs that
    # lead auto-numbered list items: small Roman numerals U+2170+ (Nl), circled latin
    # small letters U+24D0+ (So), ordinal indicators U+00AA/U+00BA (Lo), modifier
    # letters (Lm), and the letterlike script-small-l U+2113 (Ll-but-symbol). A Word /
    # InDesign auto-numbered list whose item (iii) text-extracts as the single glyph
    # U+2172 was therefore read as a lowercase continuation and ABSORBED -> merge.
    # Round-9 narrows the gate to "begins a lowercase WORD": the first char must be a
    # lowercase LETTER (ASCII a-z or category Ll, EXCLUDING tagged letterlike symbols)
    # AND must not be a lone single-letter list enumerator (letter immediately followed
    # by a list separator). Marker glyphs and enumerators -> SPLIT; real words -> JOIN. ---

    def test_round9_marker_glyph_leads_are_not_continuations(self):
        # Each MARKER glyph that carries the Unicode lowercase property but is NOT a
        # word-starting letter must be a SPLIT (not a continuation). Small Roman
        # numeral, circled small letter, ordinal indicators, a modifier letter, and the
        # letterlike script small l.
        marker_leads = {
            "small-roman-iii(U+2172)": "ⅲ to its advisers",
            "circled-small-a(U+24D0)": "ⓐ Definitions",
            "feminine-ordinal(U+00AA)": "ª clause",
            "masculine-ordinal(U+00BA)": "º clause",
            "modifier-letter-h(U+02B0)": "ʰ modifier lead",
            "script-small-l(U+2113)": "ℓ lead text",
        }
        for label, lead in marker_leads.items():
            with self.subTest(lead=label):
                self.assertFalse(
                    pdf_text._is_lowercase_continuation(lead),
                    f"{label} marker glyph wrongly treated as a continuation: {lead!r}",
                )

    def test_round9_lone_letter_list_enumerators_are_not_continuations(self):
        # A lone single-letter list enumerator ("i.", "a)", "b:", and the bracketed
        # Roman "(iii)") begins with a lowercase letter but is a list MARKER, not a
        # word -> SPLIT. Item text trailing on the same line must not rescue it.
        for enumerator in (
            "i. The first obligation of the party.",
            "a) Something the party shall do.",
            "b: text describing the obligation.",
            "c] another obligation of the party.",
            "(iii) the third enumerated obligation.",
        ):
            with self.subTest(enumerator=enumerator):
                self.assertFalse(
                    pdf_text._is_lowercase_continuation(enumerator),
                    f"list enumerator wrongly treated as a continuation: {enumerator!r}",
                )

    def test_round9_lowercase_word_wraps_are_continuations(self):
        # A genuine mid-sentence wrap begins a real lowercase WORD -> continuation.
        # Covers ASCII words and a Unicode-accented lowercase word (é lead).
        for wrap in (
            "to its advisers",
            "and the parties agree",
            "shall remain confidential",
            "shall not be disclosed",
            "égalité and the remaining obligations.",  # é-acute lowercase word
        ):
            with self.subTest(wrap=wrap):
                self.assertTrue(
                    pdf_text._is_lowercase_continuation(wrap),
                    f"genuine lowercase-word wrap not treated as a continuation: {wrap!r}",
                )

    def test_round9_marker_glyph_carveout_item_splits_from_lone_number_helper(self):
        # End-to-end at the helper layer: a lone "1." sits one wrap (sub-pitch) above a
        # carve-out list item that text-extracts as the single small-Roman-numeral glyph
        # U+2172 (iii). Under round-8 the U+2172 lead had the lowercase property and was
        # absorbed by JOIN 1 -> merge. With the round-9 word-gate the marker glyph SPLITS.
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font  # sub-pitch hanging-indent adjacency
                item = "ⅲ the residuals carve-out applies to retained knowledge."
                lines = [
                    geo("1.", y=720.0, font_size=font),
                    geo(item, y=720.0 - gap, font_size=font),
                ]

                blocks = _split_pdf_paragraphs(lines)

                self.assertEqual(len(blocks), 2, blocks)
                self.assertEqual(blocks[0], "1.")
                self.assertEqual(blocks[1], item)
                self.assertFalse(
                    any("1." in block and "residuals carve-out" in block for block in blocks),
                    f"small-roman-numeral carve-out absorbed into a lone number (font {font}): {blocks}",
                )

    @requires_pypdf
    def test_round9_marker_glyph_carveout_item_splits_round_trips(self):
        # THE ROUND-9 REPRO end-to-end through the REAL pypdf pipeline: a small-Roman
        # carve-out list item (U+2172) one wrap below a lone "1." stays its own block.
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font
                item = "ⅲ the residuals carve-out applies to retained knowledge."
                data = make_pdf_positioned_type0([
                    ("1.", 72, 720, font),
                    (item, 72, 720 - gap, font),
                ])

                texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

                # Confirm the small-Roman glyph actually survived the pypdf round-trip.
                self.assertTrue(
                    any("ⅲ" in text for text in texts),
                    f"small Roman numeral U+2172 did not decode through pypdf: {texts}",
                )
                self.assertEqual(len(texts), 2, texts)
                self.assertEqual(texts[0], "1.")
                self.assertFalse(
                    any("1." in text and "residuals carve-out" in text for text in texts),
                    f"small-roman-numeral carve-out merged end-to-end (font {font}): {texts}",
                )

    # --- Round-10 BLOCKER: the round-9 enumerator exclusion only tested the SECOND char
    # (``stripped[1] in ".):]"``), so it caught a LONE single-letter enumerator ("i.",
    # "a)") but a MULTI-letter ASCII roman/alpha enumerator ("ii.", "iii.", "iv.",
    # "viii.", "ix.", "ab)") — whose second char is itself a LETTER — slipped through and
    # was read as a lowercase-word continuation. A carve-out sub-list (e.g. a "Permitted
    # Disclosures" list of items i. ii. iii. iv.) therefore MERGED into its lead-in line.
    # Plain ASCII i/v/x is the MOST COMMON legal sub-clause numbering, not esoteric.
    # Round-10 takes the leading MAXIMAL run of ASCII lowercase letters and excludes a
    # SHORT run (length 1..6) immediately followed by a list separator — single OR
    # multi-letter. A genuine wrap word is never a short run then a separator (it is
    # longer, or followed by whitespace), so it still JOINs. ---

    def test_round10_multi_letter_enumerators_are_not_continuations(self):
        # Each MULTI-letter ASCII roman/alpha enumerator is a list MARKER, not a word ->
        # SPLIT (False). These are exactly the leads the round-9 second-char test missed.
        for enumerator in (
            "ii. x",
            "iii. x",
            "iv. x",
            "viii. x",
            "ix. x",
            "ab) x",
        ):
            with self.subTest(enumerator=enumerator):
                self.assertFalse(
                    pdf_text._is_lowercase_continuation(enumerator),
                    f"multi-letter enumerator wrongly treated as a continuation: {enumerator!r}",
                )

    def test_round10_single_letter_enumerators_still_split(self):
        # The round-9 single-letter enumerators must STILL split under the run-based gate.
        for enumerator in ("i. x", "a) x", "b: x"):
            with self.subTest(enumerator=enumerator):
                self.assertFalse(
                    pdf_text._is_lowercase_continuation(enumerator),
                    f"single-letter enumerator wrongly treated as a continuation: {enumerator!r}",
                )

    def test_round10_genuine_lowercase_word_wraps_still_join(self):
        # Genuine mid-sentence wraps — a short word then a SPACE, and longer words — are
        # NOT short-run-then-separator, so they still JOIN (True). Includes an accented
        # Unicode lowercase word.
        for wrap in (
            "to its advisers",
            "information already in the public domain",
            "and the parties agree",
            "égalité and the remaining obligations of the party.",  # é-acute lowercase
        ):
            with self.subTest(wrap=wrap):
                self.assertTrue(
                    pdf_text._is_lowercase_continuation(wrap),
                    f"genuine lowercase-word wrap not treated as a continuation: {wrap!r}",
                )

    def test_round10_carveout_sublist_splits_into_five_blocks_helper(self):
        # THE ROUND-10 REPRO at the helper layer. An UNFINISHED lead-in line (no terminal
        # period) sits one wrap (sub-pitch) above a "Permitted Disclosures" carve-out
        # sub-list of ASCII roman items i. ii. iii. iv. Under round-9 the multi-letter
        # items (ii./iii./iv.) read as lowercase continuations and the sub-list MERGED
        # into the lead-in. With the round-10 run-based enumerator gate all FOUR items
        # split, yielding FIVE separate blocks. Fonts 10/11/12.
        lead_in = "The following information is excluded from Confidential Information"
        items = [
            "i. information already in the public domain;",
            "ii. information lawfully received from a third party;",
            "iii. information independently developed by the recipient;",
            "iv. information required to be disclosed by law.",
        ]
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font  # < 1.2 * font -> sub-pitch hanging-indent adjacency
                y = 720.0
                lines = [geo(lead_in, y=y, font_size=font)]
                for item in items:
                    y -= gap
                    lines.append(geo(item, y=y, font_size=font))

                blocks = _split_pdf_paragraphs(lines)

                self.assertEqual(len(blocks), 5, blocks)
                self.assertEqual(blocks[0], lead_in)
                self.assertEqual(blocks[1:], items)
                self.assertFalse(
                    any(
                        lead_in in block and block != lead_in
                        for block in blocks
                    ),
                    f"carve-out sub-list merged into its lead-in (font {font}): {blocks}",
                )

    @requires_pypdf
    def test_round10_carveout_sublist_splits_into_five_blocks_round_trips(self):
        # THE ROUND-10 REPRO end-to-end through the REAL pypdf pipeline: an unfinished
        # lead-in line above an i./ii./iii./iv. carve-out sub-list must extract as FIVE
        # separate blocks (none merged). Fonts 10/11/12.
        lead_in = "The following information is excluded from Confidential Information"
        items = [
            "i. information already in the public domain;",
            "ii. information lawfully received from a third party;",
            "iii. information independently developed by the recipient;",
            "iv. information required to be disclosed by law.",
        ]
        for font in (10.0, 11.0, 12.0):
            with self.subTest(font=font):
                gap = font
                placed = []
                y = 720
                placed.append((lead_in, 72, y, font))
                for item in items:
                    y -= gap
                    placed.append((item, 72, y, font))
                data = make_pdf_positioned(placed)

                texts = [paragraph["text"] for paragraph in extract_pdf_paragraphs(data)]

                self.assertEqual(len(texts), 5, texts)
                self.assertEqual(texts[0], lead_in)
                self.assertEqual(texts[1:], items)
                self.assertFalse(
                    any(lead_in in text and text != lead_in for text in texts),
                    f"carve-out sub-list merged end-to-end (font {font}): {texts}",
                )


def make_pdf(text):
    return make_pdf_pages([[text]])


def make_pdf_lines(lines):
    return make_pdf_pages([lines])


def make_pdf_positioned(lines_with_geometry):
    """Build a single-page PDF placing each line at an explicit (x, y) with a font
    size. ``lines_with_geometry`` is a list of ``(text, x, y, font_size)`` tuples.
    Used to exercise the geometry-aware splitter through the real pypdf pipeline.
    """

    object_count = 5
    operations = ["BT"]
    for text, x, y, font_size in lines_with_geometry:
        operations.append(f"/F1 {font_size} Tf 1 0 0 1 {x} {y} Tm ({_escape_pdf_text(text)}) Tj")
    operations.append("ET")
    stream = " ".join(operations) + "\n"
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        f"/Resources << /Font << /F1 {object_count} 0 R >> >> /Contents 4 0 R >> endobj\n",
        f"4 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n",
        f"{object_count} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    return _pdf_package(objects)


# WinAnsi (CP1252) byte for the punctuation that Word emits as smart quotes / dashes and
# that pypdf decodes back to these Unicode code points. Used to reproduce a real
# smart-quoted PDF through the byte layer (not by writing the Unicode char directly).
_WINANSI_BYTES = {
    "‘": 0x91,  # ‘ left single quote
    "’": 0x92,  # ’ right single quote
    "“": 0x93,  # “ left double quote
    "”": 0x94,  # ” right double quote
    "•": 0x95,  # • bullet
    "–": 0x96,  # – en dash
    "—": 0x97,  # — em dash
}


def _winansi_encode_pdf_text(text):
    """Encode ``text`` to PDF content-stream bytes using WinAnsiEncoding for the
    smart-punctuation code points in ``_WINANSI_BYTES`` (latin-1 for the rest), with
    PDF string escaping applied per byte. Returns a ``bytes`` object."""

    out = bytearray()
    for char in text:
        raw = bytes([_WINANSI_BYTES[char]]) if char in _WINANSI_BYTES else char.encode("latin-1")
        for byte in raw:
            single = bytes([byte])
            if single in (b"\\", b"(", b")"):
                out += b"\\"
            out += single
    return bytes(out)


def make_pdf_positioned_winansi(lines_with_geometry):
    """Like ``make_pdf_positioned`` but declares ``/Encoding /WinAnsiEncoding`` on the
    font and emits smart punctuation via its WinAnsi BYTES, so pypdf decodes byte 0x91
    back to U+2018 etc. This reproduces a Word-exported smart-quoted PDF at the byte
    layer for the round-8 continuation-gate repro."""

    object_count = 5
    stream = bytearray(b"BT")
    for text, x, y, font_size in lines_with_geometry:
        stream += f" /F1 {font_size} Tf 1 0 0 1 {x} {y} Tm (".encode("latin-1")
        stream += _winansi_encode_pdf_text(text)
        stream += b") Tj"
    stream += b" ET\n"
    stream = bytes(stream)
    page_object = (
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        f"/Resources << /Font << /F1 {object_count} 0 R >> >> /Contents 4 0 R >> endobj\n"
    ).encode("latin-1")
    content_object = (
        f"4 0 obj << /Length {len(stream)} >> stream\n".encode("latin-1")
        + stream
        + b"endstream endobj\n"
    )
    font_object = (
        f"{object_count} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        "/Encoding /WinAnsiEncoding >> endobj\n"
    ).encode("latin-1")
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        page_object,
        content_object,
        font_object,
    ]
    return _pdf_package_bytes(objects)


def make_pdf_positioned_type0(lines_with_geometry):
    """Like ``make_pdf_positioned`` but uses a Type0 / Identity-H composite font with a
    ToUnicode CMap, the way Word / InDesign embed glyphs OUTSIDE WinAnsi (e.g. the small
    Roman numeral U+2172 a text-extracted auto-numbered list item becomes). The content
    stream addresses each character by its 2-byte Unicode code point and the ToUnicode
    CMap maps that code back to the same Unicode char, so pypdf decodes U+2172 etc.
    faithfully. Used for the round-9 marker-glyph carve-out repro, where the glyph is
    outside latin-1/WinAnsi entirely."""

    object_count = 5
    chars = set()
    for text, *_ in lines_with_geometry:
        chars.update(text)

    stream = bytearray(b"BT")
    for text, x, y, font_size in lines_with_geometry:
        hexcodes = "".join(f"{ord(char):04X}" for char in text)
        stream += f" /F1 {font_size} Tf 1 0 0 1 {x} {y} Tm <{hexcodes}> Tj".encode("latin-1")
    stream += b" ET\n"
    stream = bytes(stream)

    bfchar = "".join(f"<{ord(char):04X}> <{ord(char):04X}>\n" for char in sorted(chars))
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(chars)} beginbfchar\n{bfchar}endbfchar\nendcmap\n"
        "CMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("latin-1")

    page_object = (
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        f"/Resources << /Font << /F1 {object_count} 0 R >> >> /Contents 4 0 R >> endobj\n"
    ).encode("latin-1")
    content_object = (
        f"4 0 obj << /Length {len(stream)} >> stream\n".encode("latin-1")
        + stream
        + b"endstream endobj\n"
    )
    type0_object = (
        f"{object_count} 0 obj << /Type /Font /Subtype /Type0 /BaseFont /Helvetica "
        "/Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 7 0 R >> endobj\n"
    ).encode("latin-1")
    cid_font_object = (
        "6 0 obj << /Type /Font /Subtype /CIDFontType2 /BaseFont /Helvetica "
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        "/FontDescriptor 8 0 R /CIDToGIDMap /Identity >> endobj\n"
    ).encode("latin-1")
    tounicode_object = (
        f"7 0 obj << /Length {len(cmap)} >> stream\n".encode("latin-1")
        + cmap
        + b"endstream endobj\n"
    )
    descriptor_object = (
        "8 0 obj << /Type /FontDescriptor /FontName /Helvetica /Flags 32 "
        "/FontBBox [0 0 1000 1000] /ItalicAngle 0 /Ascent 800 /Descent -200 "
        "/CapHeight 700 /StemV 80 >> endobj\n"
    ).encode("latin-1")
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        page_object,
        content_object,
        type0_object,
        cid_font_object,
        tounicode_object,
        descriptor_object,
    ]
    return _pdf_package_bytes(objects)


def make_pdf_pages(pages):
    object_count = 3 + len(pages) * 2
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(pages)))
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Kids [{kids}] /Count {len(pages)} >> endobj\n",
    ]
    for index, lines in enumerate(pages):
        page_object_number = 3 + index * 2
        content_object_number = page_object_number + 1
        objects.append(
            f"{page_object_number} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {object_count} 0 R >> >> /Contents {content_object_number} 0 R >> endobj\n"
        )
        stream = _pdf_text_stream(lines)
        objects.append(f"{content_object_number} 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n")
    objects.append(f"{object_count} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    return _pdf_package(objects)


def _pdf_text_stream(lines):
    if not lines:
        return ""
    operations = ["BT /F1 12 Tf 14 TL 72 720 Td"]
    for index, line in enumerate(lines):
        if index:
            operations.append("T*")
        operations.append(f"({_escape_pdf_text(line)}) Tj")
    operations.append("ET")
    return " ".join(operations) + "\n"


def _escape_pdf_text(text):
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped


def _pdf_package(objects):
    return _pdf_package_bytes([pdf_object.encode("latin-1") for pdf_object in objects])


def _pdf_package_bytes(objects):
    """Assemble a PDF from already-encoded ``bytes`` objects (used when an object
    carries raw, non-latin-1 content-stream bytes such as WinAnsi smart-quote bytes)."""

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
        output.write(f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1"))
        return output.getvalue()


def make_visual_pdf():
    object_count = 5
    stream = (
        "q 0.5 0 0 rg BT /F1 12 Tf 14 TL 72 720 Td (Red heading) Tj ET Q "
        "0 0 0 RG 72 660 220 42 re S "
        "BT /F1 12 Tf 14 TL 82 684 Td (Table-like cell text) Tj ET\n"
    )
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        f"/Resources << /Font << /F1 {object_count} 0 R >> >> /Contents 4 0 R >> endobj\n",
        f"4 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n",
        f"{object_count} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    return _pdf_package(objects)


def make_image_pdf():
    """A one-page PDF with body text plus a small embedded raster image.

    Built with PyMuPDF (requires fitz) so the embedded image is a real image
    XObject -- exercising the visual profile's image-detection path that, after
    the memory trim, runs through get_image_info() rather than the text dict.
    """
    import fitz

    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 720), "Confidential Information means all data.", fontsize=12)
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
    pixmap.set_rect(pixmap.irect, (255, 0, 0))
    page.insert_image(fitz.Rect(400, 700, 440, 740), pixmap=pixmap)
    data = document.tobytes()
    document.close()
    return data


def make_image_bomb_pdf(width, height):
    """A one-page PDF embedding a single ``width`` x ``height`` raster image.

    The image is a solid colour so it compresses to a tiny on-disk stream (a 6000x6000
    image lands at ~108 KB, matching the adversarial probe) — but its DECODED pixel
    area is ``width * height``. The body text keeps the page reviewable. Used to drive
    the pre-decode pixel-area guard with a file that passes the byte/page/char caps.
    """
    import fitz

    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 720), "Confidential Information means all data disclosed here.", fontsize=12)
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
    pixmap.set_rect(pixmap.irect, (200, 200, 200))
    page.insert_image(fitz.Rect(50, 50, 560, 742), pixmap=pixmap)
    # deflate+garbage-collect so the solid image compresses to a small on-disk stream.
    data = document.tobytes(deflate=True, garbage=4)
    document.close()
    return data


def make_drawings_pdf(path_count):
    """A one-page PDF with ``path_count`` stroked rectangles (vector paths).

    Each ``re S`` operator is one vector path that ``get_drawings()`` reports as a
    separate drawing dict — so this fixture drives the drawings-bomb cap. The body
    text keeps the page reviewable.
    """
    rects = " ".join(
        f"{72 + (index % 8) * 60} {680 - (index // 8) * 20} 40 12 re S" for index in range(path_count)
    )
    stream = (
        "q 0 0 0 RG "
        + rects
        + " Q BT /F1 12 Tf 14 TL 72 740 Td (Confidential Information means all data.) Tj ET\n"
    )
    object_count = 5
    objects = [
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        f"/Resources << /Font << /F1 {object_count} 0 R >> >> /Contents 4 0 R >> endobj\n",
        f"4 0 obj << /Length {len(stream.encode('latin-1'))} >> stream\n{stream}endstream endobj\n",
        f"{object_count} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    return _pdf_package(objects)
