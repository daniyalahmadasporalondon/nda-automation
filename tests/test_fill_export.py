"""Tests for inbound-NDA fills in the DOCX export pipeline.

Covers the two fill modes (clean -> plain text, no tracked markup; tracked ->
Word tracked insertion), clean-then-tracked ordering, fills coexisting with
existing redlines, and the absent/empty-fills backward-compatibility contract.
"""

import base64
from io import BytesIO
import unittest
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from nda_automation import fill_export, redline_export_service
from nda_automation.docx_export import build_source_redline_docx
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH

from tests.test_docx_export import (
    assert_docx_package_healthy,
    make_source_docx,
    tracked_deleted_text,
    tracked_inserted_text,
)

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _document_root(docx_bytes):
    with ZipFile(BytesIO(docx_bytes)) as archive:
        assert archive.testzip() is None
        document_xml = archive.read("word/document.xml").decode("utf-8")
    return ET.fromstring(document_xml), document_xml


def _paragraph_texts(document_root):
    texts = []
    for paragraph in document_root.iter(f"{{{W_NS['w']}}}p"):
        texts.append("".join(node.text or "" for node in paragraph.iter(f"{{{W_NS['w']}}}t")))
    return texts


def _review_result(paragraphs):
    """A minimal review_result with the paragraph model the export pipeline reads."""
    review_paragraphs = []
    for source_index, text in enumerate(paragraphs, start=1):
        review_paragraphs.append({
            "id": f"p{source_index}",
            "index": source_index,
            "source_index": source_index,
            "text": text,
        })
    return {
        "paragraphs": review_paragraphs,
        "extracted_text": "\n\n".join(paragraphs),
        "redline_edits": [],
    }


class CleanFillsValidationTests(unittest.TestCase):
    def test_clean_fills_drops_malformed_records(self):
        fills = [
            {"paragraph_id": "p1", "find": "____", "value": "Acme", "mode": "clean"},
            {"paragraph_id": "p2", "find": "____", "value": "X", "mode": "tracked", "field": "ignored"},
            "not-a-dict",
            {"paragraph_id": "", "find": "____", "value": "X", "mode": "clean"},  # empty paragraph_id
            {"paragraph_id": "p3", "find": "", "value": "X", "mode": "clean"},  # empty find
            {"paragraph_id": "p4", "find": "____", "value": 5, "mode": "clean"},  # non-string value
            {"paragraph_id": "p5", "find": "____", "value": "X", "mode": "bogus"},  # bad mode
            {"paragraph_id": "p6", "find": "____", "value": "X"},  # missing mode
            {"paragraph_id": 7, "find": "____", "value": "X", "mode": "clean"},  # non-string id
        ]

        cleaned = fill_export.clean_fills(fills)

        self.assertEqual(
            cleaned,
            [
                {"paragraph_id": "p1", "find": "____", "value": "Acme", "mode": "clean"},
                {"paragraph_id": "p2", "find": "____", "value": "X", "mode": "tracked"},
            ],
        )

    def test_clean_fills_allows_empty_value(self):
        cleaned = fill_export.clean_fills(
            [{"paragraph_id": "p1", "find": "____", "value": "", "mode": "clean"}]
        )
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["value"], "")

    def test_clean_fills_non_list_is_empty(self):
        self.assertEqual(fill_export.clean_fills(None), [])
        self.assertEqual(fill_export.clean_fills({"paragraph_id": "p1"}), [])


class CleanFillTests(unittest.TestCase):
    def test_clean_fill_substitutes_plain_text_without_tracked_markup(self):
        source_docx = make_source_docx([
            "This Agreement is made between ____ and Aspora.",
            "The confidentiality obligations survive for three years.",
        ])
        review_result = _review_result([
            "This Agreement is made between ____ and Aspora.",
            "The confidentiality obligations survive for three years.",
        ])
        clean_fills = [{
            "paragraph_id": "p1",
            "find": "____",
            "value": "Aspora Technology Services Private Limited",
            "mode": "clean",
        }]

        redlined = build_source_redline_docx(source_docx, review_result, clean_fills=clean_fills)

        assert_docx_package_healthy(self, redlined)
        document_root, document_xml = _document_root(redlined)
        # The value is real text in the body, not inside a tracked insertion.
        self.assertIn("Aspora Technology Services Private Limited", document_xml)
        self.assertNotIn("____", document_xml)
        self.assertEqual(document_root.findall(".//w:ins", W_NS), [])
        self.assertEqual(document_root.findall(".//w:del", W_NS), [])
        self.assertIn(
            "This Agreement is made between Aspora Technology Services Private Limited and Aspora.",
            _paragraph_texts(document_root),
        )

    def test_clean_fill_substitutes_across_split_runs(self):
        # A blank split across two <w:t> runs (Word often does this) must still fill.
        body = (
            "<w:p><w:r><w:t>Party: __</w:t></w:r><w:r><w:t>__ Ltd</w:t></w:r></w:p>"
        )
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>"
        )
        with BytesIO() as output:
            with ZipFile(output, "w") as archive:
                archive.writestr(
                    "[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                    "</Types>",
                )
                archive.writestr(
                    "_rels/.rels",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                    "</Relationships>",
                )
                archive.writestr("word/document.xml", document_xml)
                archive.writestr(
                    "word/_rels/document.xml.rels",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
                )
            source_docx = output.getvalue()

        review_result = _review_result(["Party: ____ Ltd"])
        clean_fills = [{"paragraph_id": "p1", "find": "____", "value": "Acme", "mode": "clean"}]

        redlined = build_source_redline_docx(source_docx, review_result, clean_fills=clean_fills)

        document_root, document_xml_out = _document_root(redlined)
        self.assertNotIn("____", document_xml_out)
        self.assertIn("Party: Acme Ltd", _paragraph_texts(document_root))
        self.assertEqual(document_root.findall(".//w:ins", W_NS), [])

    def test_clean_fill_updates_extracted_text_for_coverage_gate(self):
        review_result = _review_result(["Name: ____", "Body paragraph."])
        from nda_automation.docx_xml import parse_docx_xml

        document_root = parse_docx_xml(
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            "<w:p><w:r><w:t>Name: ____</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Body paragraph.</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        applied = fill_export.apply_clean_fills_to_source_document(
            document_root,
            [{"paragraph_id": "p1", "find": "____", "value": "Jane Doe", "mode": "clean"}],
            review_result,
        )
        self.assertEqual(applied, 1)
        self.assertEqual(review_result["paragraphs"][0]["text"], "Name: Jane Doe")
        self.assertEqual(review_result["extracted_text"], "Name: Jane Doe\n\nBody paragraph.")


class TrackedFillTests(unittest.TestCase):
    def test_tracked_fill_renders_as_tracked_change(self):
        source_docx = make_source_docx([
            "The disclosing party is ____.",
            "Other paragraph.",
        ])
        review_result = _review_result([
            "The disclosing party is ____.",
            "Other paragraph.",
        ])
        fills = fill_export.clean_fills([
            {"paragraph_id": "p1", "find": "____", "value": "Acme Corp", "mode": "tracked"}
        ])
        _clean, tracked = fill_export.split_fills_by_mode(fills)
        fill_export.merge_fill_redlines(
            review_result,
            fill_export.synthesize_tracked_fill_redlines(tracked, review_result),
        )

        redlined = build_source_redline_docx(source_docx, review_result)

        assert_docx_package_healthy(self, redlined)
        document_root, _document_xml = _document_root(redlined)
        insertions = document_root.findall(".//w:ins", W_NS)
        self.assertGreaterEqual(len(insertions), 1)
        inserted = tracked_inserted_text(document_root)
        self.assertTrue(any("Acme Corp" in text for text in inserted))

    def test_synthesize_skips_unknown_paragraph_and_missing_find(self):
        review_result = _review_result(["The disclosing party is ____."])
        redlines = fill_export.synthesize_tracked_fill_redlines(
            [
                {"paragraph_id": "p99", "find": "____", "value": "X", "mode": "tracked"},
                {"paragraph_id": "p1", "find": "NOTHERE", "value": "X", "mode": "tracked"},
            ],
            review_result,
        )
        self.assertEqual(redlines, [])

    def test_synthesized_redline_shape(self):
        review_result = _review_result(["The disclosing party is ____."])
        redlines = fill_export.synthesize_tracked_fill_redlines(
            [{"paragraph_id": "p1", "find": "____", "value": "Acme", "mode": "tracked"}],
            review_result,
        )
        self.assertEqual(len(redlines), 1)
        redline = redlines[0]
        self.assertEqual(redline["action"], REDLINE_REPLACE_PARAGRAPH)
        self.assertEqual(redline["paragraph_id"], "p1")
        self.assertEqual(redline["original_text"], "The disclosing party is ____.")
        self.assertEqual(redline["replacement_text"], "The disclosing party is Acme.")
        self.assertEqual(redline["source_index"], 1)


class OrderingAndCoexistenceTests(unittest.TestCase):
    def test_clean_then_tracked_ordering(self):
        # Clean fill on p1 (plain), tracked fill on p2 (tracked insertion).
        source_docx = make_source_docx([
            "Company: ____ is the first party.",
            "Signatory: ____ signs for Aspora.",
        ])
        review_result = _review_result([
            "Company: ____ is the first party.",
            "Signatory: ____ signs for Aspora.",
        ])
        fills = fill_export.clean_fills([
            {"paragraph_id": "p1", "find": "____", "value": "Acme Ltd", "mode": "clean"},
            {"paragraph_id": "p2", "find": "____", "value": "Jane Doe", "mode": "tracked"},
        ])
        clean_mode, tracked_mode = fill_export.split_fills_by_mode(fills)
        fill_export.merge_fill_redlines(
            review_result,
            fill_export.synthesize_tracked_fill_redlines(tracked_mode, review_result),
        )

        redlined = build_source_redline_docx(source_docx, review_result, clean_fills=clean_mode)

        assert_docx_package_healthy(self, redlined)
        document_root, document_xml = _document_root(redlined)
        # Clean fill: plain text on p1, NOT in any tracked element.
        self.assertIn("Company: Acme Ltd is the first party.", _paragraph_texts(document_root))
        inserted = tracked_inserted_text(document_root)
        self.assertFalse(any("Acme Ltd" in text for text in inserted))
        # Tracked fill: p2 value appears as a tracked insertion.
        self.assertTrue(any("Jane Doe" in text for text in inserted))
        self.assertNotIn("____", document_xml)

    def test_fills_coexist_with_existing_redline(self):
        source_docx = make_source_docx([
            "Company: ____.",
            "This Agreement shall be governed by the laws of California.",
        ])
        review_result = _review_result([
            "Company: ____.",
            "This Agreement shall be governed by the laws of California.",
        ])
        # A pre-existing server redline on p2.
        review_result["redline_edits"] = [{
            "id": "r1",
            "paragraph_id": "p2",
            "paragraph_index": 2,
            "source_index": 2,
            "action": REDLINE_REPLACE_PARAGRAPH,
            "original_text": "This Agreement shall be governed by the laws of California.",
            "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
        }]
        clean_fills = [{"paragraph_id": "p1", "find": "____", "value": "Acme", "mode": "clean"}]

        redlined = build_source_redline_docx(source_docx, review_result, clean_fills=clean_fills)

        assert_docx_package_healthy(self, redlined)
        document_root, document_xml = _document_root(redlined)
        # Clean fill applied to p1 as plain text.
        self.assertIn("Company: Acme.", _paragraph_texts(document_root))
        self.assertNotIn("____", document_xml)
        # Existing redline still produced tracked changes on p2.
        self.assertTrue(any("California" in text for text in tracked_deleted_text(document_root)))
        self.assertTrue(any("England and Wales" in text for text in tracked_inserted_text(document_root)))

    def test_tracked_fill_supersedes_server_redline_on_same_paragraph(self):
        review_result = _review_result(["Party: ____."])
        review_result["redline_edits"] = [{
            "id": "server-1",
            "paragraph_id": "p1",
            "action": REDLINE_REPLACE_PARAGRAPH,
            "original_text": "Party: ____.",
            "replacement_text": "Party: SERVER SUGGESTION.",
        }]
        fill_export.merge_fill_redlines(
            review_result,
            fill_export.synthesize_tracked_fill_redlines(
                [{"paragraph_id": "p1", "find": "____", "value": "Acme", "mode": "tracked"}],
                review_result,
            ),
        )
        ids = [r["id"] for r in review_result["redline_edits"]]
        self.assertEqual(len(review_result["redline_edits"]), 1)
        self.assertTrue(ids[0].startswith("fill-p1"))


class BackwardCompatibilityTests(unittest.TestCase):
    def test_absent_fills_unchanged_output(self):
        source_docx = make_source_docx(["Plain paragraph one.", "Plain paragraph two."])
        review_result = _review_result(["Plain paragraph one.", "Plain paragraph two."])

        baseline = build_source_redline_docx(source_docx, review_result)
        with_none = build_source_redline_docx(source_docx, _review_result(
            ["Plain paragraph one.", "Plain paragraph two."]
        ), clean_fills=None)
        with_empty = build_source_redline_docx(source_docx, _review_result(
            ["Plain paragraph one.", "Plain paragraph two."]
        ), clean_fills=[])

        _root_baseline, xml_baseline = _document_root(baseline)
        _root_none, xml_none = _document_root(with_none)
        _root_empty, xml_empty = _document_root(with_empty)
        self.assertEqual(xml_baseline, xml_none)
        self.assertEqual(xml_baseline, xml_empty)
        # No tracked markup, no fill artifacts.
        self.assertNotIn("<w:ins", xml_baseline)
        self.assertNotIn("<w:del", xml_baseline)

    def test_apply_clean_fills_empty_is_noop(self):
        review_result = _review_result(["Unchanged."])
        from nda_automation.docx_xml import parse_docx_xml

        document_root = parse_docx_xml(
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Unchanged.</w:t></w:r></w:p></w:body></w:document>"
        )
        applied = fill_export.apply_clean_fills_to_source_document(document_root, [], review_result)
        self.assertEqual(applied, 0)
        self.assertEqual(review_result["extracted_text"], "Unchanged.")

    def test_clean_fill_on_unknown_paragraph_is_skipped(self):
        source_docx = make_source_docx(["Only paragraph."])
        review_result = _review_result(["Only paragraph."])
        clean_fills = [{"paragraph_id": "p99", "find": "X", "value": "Y", "mode": "clean"}]

        redlined = build_source_redline_docx(source_docx, review_result, clean_fills=clean_fills)

        assert_docx_package_healthy(self, redlined)
        _root, xml = _document_root(redlined)
        self.assertIn("Only paragraph.", xml)


class ServiceIntegrationTests(unittest.TestCase):
    """End-to-end through build_review_export: the request body's top-level
    'fills' key threads into the export and survives the health/coverage gates."""

    def _export(self, paragraphs, fills):
        source_docx = make_source_docx(paragraphs)
        payload = {
            "filename": "uploaded.docx",
            "content_base64": base64.b64encode(source_docx).decode("ascii"),
            "fills": fills,
        }
        return redline_export_service.build_review_export(payload, "")

    def test_clean_and_tracked_fills_thread_through_service(self):
        export = self._export(
            [
                "Company: ____ is the first party.",
                "Signatory: ____ signs for Aspora.",
            ],
            [
                {"paragraph_id": "p1", "find": "____", "value": "Acme Ltd", "field": "company", "mode": "clean"},
                {"paragraph_id": "p2", "find": "____", "value": "Jane Doe", "field": "signatory", "mode": "tracked"},
            ],
        )

        document_root, document_xml = _document_root(export.data)
        assert_docx_package_healthy(self, export.data)
        # Clean fill p1: plain text, not in a tracked insertion.
        self.assertIn("Company: Acme Ltd is the first party.", _paragraph_texts(document_root))
        inserted = tracked_inserted_text(document_root)
        self.assertFalse(any("Acme Ltd" in text for text in inserted))
        # Tracked fill p2: tracked insertion.
        self.assertTrue(any("Jane Doe" in text for text in inserted))
        self.assertNotIn("____", document_xml)

    def test_absent_and_empty_fills_match_no_fills_baseline(self):
        # review_nda may add its own server redlines; the contract is only that an
        # absent or empty 'fills' key yields the SAME export as omitting it entirely.
        source_docx = make_source_docx(["Plain one.", "Plain two."])
        encoded = base64.b64encode(source_docx).decode("ascii")

        def export_with(extra):
            payload = {"filename": "uploaded.docx", "content_base64": encoded, **extra}
            return _document_root(redline_export_service.build_review_export(payload, "").data)[1]

        baseline = export_with({})
        with_empty = export_with({"fills": []})
        with_garbage = export_with({"fills": "not-a-list"})
        # Tracked-change ids carry timestamps, so compare the structure with the
        # volatile revision attributes stripped.
        import re

        def normalise(xml):
            return re.sub(r'w:date="[^"]*"', 'w:date="T"', xml)

        self.assertEqual(normalise(baseline), normalise(with_empty))
        self.assertEqual(normalise(baseline), normalise(with_garbage))
        self.assertIn("Plain one.", baseline)


if __name__ == "__main__":
    unittest.main()
