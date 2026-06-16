import copy
import importlib.util
import unittest
from unittest.mock import patch

from nda_automation import table_extraction
from nda_automation.pdf_text import extract_pdf_document
from nda_automation.table_extraction import (
    TABLE_AUGMENTATION_ENABLED_ENV,
    RecoveredTable,
    TableExtractionResult,
    _table_is_substantive,
    augment_quality_with_tables,
    camelot_stream_backend,
    extract_tables,
    pdfplumber_backend,
    select_table_pages,
    table_augmentation_enabled,
)

PYMUPDF_AVAILABLE = importlib.util.find_spec("fitz") is not None
requires_pymupdf = unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF is not installed")
CAMELOT_AVAILABLE = importlib.util.find_spec("camelot") is not None
requires_camelot = unittest.skipUnless(CAMELOT_AVAILABLE, "camelot is not installed")
PDFPLUMBER_AVAILABLE = importlib.util.find_spec("pdfplumber") is not None
requires_pdfplumber = unittest.skipUnless(PDFPLUMBER_AVAILABLE, "pdfplumber is not installed")


def _enable_flag():
    return patch.dict("os.environ", {TABLE_AUGMENTATION_ENABLED_ENV: "true"})


def _disable_flag():
    return patch.dict("os.environ", {TABLE_AUGMENTATION_ENABLED_ENV: ""}, clear=False)


def make_signature_page_pdf():
    """A single-page PDF whose text carries a 'Signature' / 'Name' / 'Title'
    marker, so the page-selector gate selects it. Built with PyMuPDF (text only;
    no table detection here). The bake-off target shape: a borderless 2-column
    signature block."""

    import fitz

    document = fitz.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 100), "Confidentiality. The Recipient shall keep all information secret.", fontsize=11)
    page.insert_text((72, 220), "Signed for and on behalf of the Discloser    Signed for the Recipient", fontsize=11)
    page.insert_text((72, 250), "Signature:                                   Signature:", fontsize=11)
    page.insert_text((72, 280), "Name:                                        Name:", fontsize=11)
    page.insert_text((72, 310), "Title:                                       Title:", fontsize=11)
    data = document.tobytes()
    document.close()
    return data


def make_prose_only_pdf():
    """A page of dense prose with NO signature/term markers — the page-selector
    gate must reject it so a table extractor never shatters it."""

    import fitz

    document = fitz.open()
    page = document.new_page(width=612, height=792)
    y = 100
    for line in [
        "This Agreement is made between the Discloser and the Recipient.",
        "Confidential Information means any information disclosed by one party.",
        "The Recipient shall protect Confidential Information from disclosure.",
        "Governing law: the laws of England and Wales.",
    ]:
        page.insert_text((72, y), line, fontsize=11)
        y += 24
    data = document.tobytes()
    document.close()
    return data


def make_two_page_mixed_pdf():
    """Page 1 = prose definitions (no marker); page 2 = a notice/term block with
    'Notice to' and 'Initial Term' markers. Only page 2 should be selected."""

    import fitz

    document = fitz.open()
    page1 = document.new_page(width=612, height=792)
    page1.insert_text((72, 100), "Definitions. Confidential Information means trade secrets.", fontsize=11)
    page1.insert_text((72, 130), "The parties agree to the following terms and conditions.", fontsize=11)
    page2 = document.new_page(width=612, height=792)
    page2.insert_text((72, 100), "Initial Term. This Agreement runs for three years.", fontsize=11)
    page2.insert_text((72, 140), "Notice to the Discloser shall be sent to the address below.", fontsize=11)
    data = document.tobytes()
    document.close()
    return data


class FlagTests(unittest.TestCase):
    def test_flag_default_off(self):
        with _disable_flag():
            self.assertFalse(table_augmentation_enabled())

    def test_flag_truthy_values_enable(self):
        for value in ("1", "true", "TRUE", "Yes", "on", " on "):
            with patch.dict("os.environ", {TABLE_AUGMENTATION_ENABLED_ENV: value}):
                self.assertTrue(table_augmentation_enabled(), value)

    def test_flag_falsy_values_stay_off(self):
        for value in ("", "0", "false", "no", "off", "maybe"):
            with patch.dict("os.environ", {TABLE_AUGMENTATION_ENABLED_ENV: value}):
                self.assertFalse(table_augmentation_enabled(), value)


class AugmentOffTests(unittest.TestCase):
    """Flag OFF must be a strict no-op: the quality block is returned unchanged
    and the backend / page-gate is never even invoked."""

    def test_off_returns_same_object_unchanged(self):
        quality = {"page_count": 1, "visual_profile": {"status": "ready"}}
        before = copy.deepcopy(quality)
        with _disable_flag():
            result = augment_quality_with_tables(quality, b"%PDF-1.4 ignored")
        self.assertIs(result, quality)
        self.assertEqual(result, before)
        self.assertNotIn("recovered_tables", result.get("visual_profile", {}))

    def test_off_never_calls_backend(self):
        sentinel_called = {"hit": False}

        def boom(_pdf_bytes, _pages):
            sentinel_called["hit"] = True
            raise AssertionError("backend must not run when flag is OFF")

        with _disable_flag():
            augment_quality_with_tables({}, b"%PDF-1.4", backend=boom)
        self.assertFalse(sentinel_called["hit"])


class AugmentOnTests(unittest.TestCase):
    """Flag ON attaches tables ADDITIVELY without disturbing existing keys."""

    def _stub_backend(self, tables, pages=(1,)):
        def backend(_pdf_bytes, gated_pages):
            return TableExtractionResult(
                status="ready", backend="stub", tables=tables, pages_scanned=list(gated_pages)
            )

        return backend

    def test_on_attaches_recovered_tables_additively(self):
        table = RecoveredTable(
            page_number=1,
            bbox=None,
            row_count=3,
            col_count=2,
            cells=[["Signature:", "Signature:"], ["Name:", "Name:"], ["Title:", "Title:"]],
        )
        quality = {
            "page_count": 1,
            "extracted_paragraphs": 5,
            "visual_profile": {"status": "ready", "visual_features": ["drawings_or_borders"]},
        }
        # Force the page-gate to select page 1 so the stub backend is reached.
        with _enable_flag(), patch.object(table_extraction, "select_table_pages", return_value=[1]):
            result = augment_quality_with_tables(quality, b"%PDF-1.4", backend=self._stub_backend([table]))

        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["extracted_paragraphs"], 5)
        self.assertEqual(result["visual_profile"]["status"], "ready")
        self.assertEqual(result["visual_profile"]["visual_features"], ["drawings_or_borders"])
        recovered = result["visual_profile"]["recovered_tables"]
        self.assertEqual(recovered["status"], "ready")
        self.assertEqual(recovered["table_count"], 1)
        self.assertEqual(
            recovered["tables"][0]["cells"],
            [["Signature:", "Signature:"], ["Name:", "Name:"], ["Title:", "Title:"]],
        )

    def test_on_creates_minimal_visual_profile_when_absent(self):
        table = RecoveredTable(2, None, 2, 2, [["Address:", "Address:"], ["Email:", "Email:"]])
        quality = {"page_count": 2}  # no visual_profile present
        with _enable_flag(), patch.object(table_extraction, "select_table_pages", return_value=[2]):
            result = augment_quality_with_tables(quality, b"%PDF-1.4", backend=self._stub_backend([table]))
        self.assertIn("visual_profile", result)
        self.assertEqual(result["visual_profile"]["status"], "augmented")
        self.assertEqual(result["visual_profile"]["recovered_tables"]["table_count"], 1)

    def test_on_records_zero_tables_when_gate_selects_nothing(self):
        quality = {"visual_profile": {"status": "ready"}}
        with _enable_flag(), patch.object(table_extraction, "select_table_pages", return_value=[]):
            result = augment_quality_with_tables(quality, b"%PDF-1.4", backend=self._stub_backend([]))
        recovered = result["visual_profile"]["recovered_tables"]
        self.assertEqual(recovered["table_count"], 0)
        self.assertEqual(recovered["tables"], [])
        self.assertEqual(recovered["pages_scanned"], [])

    def test_on_degrades_when_backend_unavailable(self):
        def unavailable(_pdf_bytes, pages):
            return TableExtractionResult(
                status="unavailable", backend="stub", reason="camelot_not_installed", pages_scanned=list(pages)
            )

        quality = {"visual_profile": {"status": "ready"}}
        with _enable_flag(), patch.object(table_extraction, "select_table_pages", return_value=[1]):
            result = augment_quality_with_tables(quality, b"%PDF-1.4", backend=unavailable)
        recovered = result["visual_profile"]["recovered_tables"]
        self.assertEqual(recovered["status"], "unavailable")
        self.assertEqual(recovered["reason"], "camelot_not_installed")
        self.assertEqual(recovered["table_count"], 0)


class ExtractTablesInterfaceTests(unittest.TestCase):
    def test_extract_tables_never_raises_on_backend_exception(self):
        def explode(_pdf_bytes, _pages):
            raise RuntimeError("kaboom")

        result = extract_tables(b"%PDF-1.4", backend=explode, pages=[1])
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "backend_exception")
        self.assertEqual(result.tables, [])

    def test_extract_tables_uses_pluggable_backend(self):
        table = RecoveredTable(1, None, 2, 2, [["a", "b"], ["c", "d"]])

        def backend(_pdf_bytes, pages):
            return TableExtractionResult(status="ready", backend="other_tool", tables=[table], pages_scanned=list(pages))

        result = extract_tables(b"%PDF-1.4", backend=backend, pages=[1])
        self.assertEqual(result.backend, "other_tool")
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.pages_scanned, [1])

    def test_extract_tables_default_backend_is_camelot(self):
        captured = {}

        def fake_backend(pdf_bytes, pages):
            captured["bytes"] = pdf_bytes
            captured["pages"] = list(pages)
            return TableExtractionResult(status="ready", backend="camelot_stream", pages_scanned=list(pages))

        with patch.object(table_extraction, "camelot_stream_backend", fake_backend):
            result = extract_tables(b"%PDF-DATA", pages=[3])
        self.assertEqual(captured["bytes"], b"%PDF-DATA")
        self.assertEqual(captured["pages"], [3])
        self.assertEqual(result.backend, "camelot_stream")

    def test_extract_tables_skips_backend_when_no_page_selected(self):
        sentinel = {"hit": False}

        def backend(_pdf_bytes, _pages):
            sentinel["hit"] = True
            return TableExtractionResult(status="ready", backend="stub")

        # No page passed the gate -> backend never invoked, ready/empty result.
        result = extract_tables(b"%PDF-1.4", backend=backend, pages=[])
        self.assertFalse(sentinel["hit"])
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.tables, [])


class PageSelectorTests(unittest.TestCase):
    @requires_pymupdf
    def test_selects_signature_page(self):
        self.assertEqual(select_table_pages(make_signature_page_pdf()), [1])

    @requires_pymupdf
    def test_rejects_prose_only_page(self):
        self.assertEqual(select_table_pages(make_prose_only_pdf()), [])

    @requires_pymupdf
    def test_selects_only_the_marker_page_in_mixed_doc(self):
        self.assertEqual(select_table_pages(make_two_page_mixed_pdf()), [2])

    def test_fails_closed_on_unreadable_bytes(self):
        self.assertEqual(select_table_pages(b"not a pdf at all"), [])


class SubstantiveGuardTests(unittest.TestCase):
    def test_drops_single_column_table(self):
        table = RecoveredTable(1, None, 3, 1, [["x"], ["y"], ["z"]])
        self.assertFalse(_table_is_substantive(table))

    def test_drops_all_empty_grid(self):
        table = RecoveredTable(1, None, 2, 2, [["", ""], ["", ""]])
        self.assertFalse(_table_is_substantive(table))

    def test_drops_single_non_empty_cell(self):
        table = RecoveredTable(1, None, 2, 2, [["only", ""], ["", ""]])
        self.assertFalse(_table_is_substantive(table))

    def test_keeps_real_two_column_table(self):
        table = RecoveredTable(
            1, None, 3, 2, [["Signature:", "Signature:"], ["Name:", "Name:"], ["Title:", "Title:"]]
        )
        self.assertTrue(_table_is_substantive(table))


class CamelotBackendTests(unittest.TestCase):
    """The camelot backend must no-op gracefully when the optional dep is absent
    (this is the env we ship in by default) and run when it is present."""

    def test_no_op_when_camelot_not_installed(self):
        import builtins

        real_import = builtins.__import__

        def import_without_camelot(name, *args, **kwargs):
            if name == "camelot":
                raise ModuleNotFoundError("No module named 'camelot'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_camelot):
            result = camelot_stream_backend(b"%PDF-1.4", [1])
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "camelot_not_installed")
        self.assertEqual(result.pages_scanned, [1])

    def test_empty_pages_short_circuits(self):
        result = camelot_stream_backend(b"%PDF-1.4", [])
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.tables, [])

    def test_fails_open_when_extraction_slot_busy(self):
        # Hold the single extraction slot, then a second extraction must shed the
        # work (fail OPEN) rather than block or raise. We stub `camelot` present so
        # control reaches the semaphore acquire.
        import sys
        import types

        fake_camelot = types.ModuleType("camelot")
        fake_camelot.read_pdf = lambda *a, **k: []  # pragma: no cover - never reached
        table_extraction._TABLE_EXTRACTION_SEMAPHORE.acquire()
        try:
            with patch.dict(sys.modules, {"camelot": fake_camelot}):
                # Shorten the wait so the test is fast.
                with patch.object(table_extraction, "_EXTRACTION_QUEUE_WAIT_SECONDS", 0.05):
                    result = camelot_stream_backend(b"%PDF-1.4", [1])
        finally:
            table_extraction._TABLE_EXTRACTION_SEMAPHORE.release()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "extractor_busy")

    def test_semaphore_released_after_successful_extraction(self):
        # A normal extraction must release the slot. We stub camelot to return no
        # tables, then assert the slot is immediately re-acquirable.
        import sys
        import types

        fake_camelot = types.ModuleType("camelot")
        fake_camelot.read_pdf = lambda *a, **k: []
        with patch.dict(sys.modules, {"camelot": fake_camelot}):
            result = camelot_stream_backend(b"%PDF-1.4", [1])
        self.assertEqual(result.status, "ready")
        acquired = table_extraction._TABLE_EXTRACTION_SEMAPHORE.acquire(blocking=False)
        self.assertTrue(acquired, "semaphore slot was not released after extraction")
        if acquired:
            table_extraction._TABLE_EXTRACTION_SEMAPHORE.release()

    @requires_camelot
    def test_recovers_signature_columns_when_camelot_present(self):
        data = make_signature_page_pdf()
        result = camelot_stream_backend(data, [1])
        self.assertEqual(result.status, "ready")
        # camelot stream should recover at least one substantive table; the precise
        # cell geometry is camelot's, so we only assert structure here.
        for table in result.tables:
            self.assertGreaterEqual(table.col_count, 2)


class PdfplumberBackendTests(unittest.TestCase):
    def test_no_op_when_pdfplumber_not_installed(self):
        import builtins

        real_import = builtins.__import__

        def import_without_pdfplumber(name, *args, **kwargs):
            if name == "pdfplumber":
                raise ModuleNotFoundError("No module named 'pdfplumber'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_pdfplumber):
            result = pdfplumber_backend(b"%PDF-1.4", [1])
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "pdfplumber_not_installed")

    def test_pdfplumber_is_swappable_behind_interface(self):
        # The lighter backend has the same (bytes, pages) signature as the default,
        # so extract_tables can drive it unchanged.
        with patch.object(table_extraction, "select_table_pages", return_value=[1]):
            result = extract_tables(b"%PDF-1.4", backend=pdfplumber_backend)
        # Without the dep installed this degrades cleanly rather than raising.
        self.assertIn(result.status, {"ready", "unavailable"})


@requires_pymupdf
class EndToEndThroughExtractPdfDocumentTests(unittest.TestCase):
    """The prose path stays byte-identical with the flag OFF; with it ON the
    recovered-tables block rides along in the quality block while paragraphs are
    untouched (regardless of whether a heavy backend is installed)."""

    def test_off_produces_no_recovered_tables_block(self):
        data = make_signature_page_pdf()
        with _disable_flag():
            extraction = extract_pdf_document(data)
        visual_profile = extraction.quality.get("visual_profile", {})
        self.assertNotIn("recovered_tables", visual_profile)

    def test_on_attaches_block_without_changing_paragraphs(self):
        data = make_signature_page_pdf()
        with _disable_flag():
            off = extract_pdf_document(data)
        with _enable_flag():
            on = extract_pdf_document(data)

        # Prose paragraphs byte-identical with the flag flipped: zero change to the
        # never-merge prose path.
        self.assertEqual(
            [p["text"] for p in off.paragraphs],
            [p["text"] for p in on.paragraphs],
        )
        recovered = on.quality["visual_profile"]["recovered_tables"]
        # The block is always present when ON; status is ready (page selected) and
        # the backend either recovered tables or no-opped because camelot is absent.
        self.assertIn(recovered["status"], {"ready", "unavailable"})
        self.assertIn("table_count", recovered)
        self.assertEqual(recovered["pages_scanned"], [1])

    def test_on_prose_only_doc_selects_no_pages(self):
        data = make_prose_only_pdf()
        with _enable_flag():
            on = extract_pdf_document(data)
        recovered = on.quality["visual_profile"]["recovered_tables"]
        self.assertEqual(recovered["pages_scanned"], [])
        self.assertEqual(recovered["table_count"], 0)


if __name__ == "__main__":
    unittest.main()
