from io import BytesIO
import unittest
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from nda_automation.checker import review_nda
from nda_automation.docx_export import _diff_text_operations, _tracked_replace_paragraph, build_review_report_docx

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def docx_xml_roots(docx_bytes):
    with ZipFile(BytesIO(docx_bytes)) as archive:
        assert archive.testzip() is None
        settings_xml = archive.read("word/settings.xml").decode("utf-8")
        document_xml = archive.read("word/document.xml").decode("utf-8")
    return ET.fromstring(settings_xml), ET.fromstring(document_xml), document_xml


def tracked_deleted_text(document_root):
    return [
        "".join(node.text or "" for node in deletion.findall(".//w:delText", W_NS))
        for deletion in document_root.findall(".//w:del", W_NS)
    ]


def tracked_inserted_text(document_root):
    return [
        "".join(node.text or "" for node in insertion.findall(".//w:t", W_NS))
        for insertion in document_root.findall(".//w:ins", W_NS)
    ]


def revision_text_for_state(node, accepted):
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "del":
        return "".join(item.text or "" for item in node.findall(".//w:delText", W_NS)) if not accepted else ""
    if tag == "ins":
        return "".join(item.text or "" for item in node.findall(".//w:t", W_NS)) if accepted else ""
    if tag == "t":
        return node.text or ""
    if tag == "br":
        return "\n"
    return "".join(revision_text_for_state(child, accepted) for child in list(node))


class DocxExportTests(unittest.TestCase):
    def test_inline_diff_operations_cover_empty_inputs_and_lcs(self):
        self.assertEqual(_diff_text_operations("", "Alpha, beta."), [
            ("insert", "Alpha"),
            ("insert", ","),
            ("insert", "beta"),
            ("insert", "."),
        ])
        self.assertEqual(_diff_text_operations("Alpha, beta.", ""), [
            ("delete", "Alpha"),
            ("delete", ","),
            ("delete", "beta"),
            ("delete", "."),
        ])
        self.assertEqual(_diff_text_operations("Pay fees, taxes.", "Pay charges, taxes."), [
            ("same", "Pay"),
            ("delete", "fees"),
            ("insert", "charges"),
            ("same", ","),
            ("same", "taxes"),
            ("same", "."),
        ])

    def test_inline_diff_operations_large_input_uses_bounded_fallback(self):
        original = " ".join(f"old{index}" for index in range(201))
        replacement = " ".join(f"new{index}" for index in range(200))

        operations = _diff_text_operations(original, replacement)

        self.assertEqual(len(operations), 401)
        self.assertEqual([operation for operation, _token in operations[:201]], ["delete"] * 201)
        self.assertEqual([operation for operation, _token in operations[201:]], ["insert"] * 200)
        self.assertEqual(operations[0], ("delete", "old0"))
        self.assertEqual(operations[201], ("insert", "new0"))

    def test_tracked_replace_paragraph_preserves_punctuation_spacing(self):
        original = "This Agreement (California) applies."
        replacement = "This Agreement (England and Wales) applies."

        paragraph_xml, next_revision_id = _tracked_replace_paragraph(original, replacement, 7)

        root = ET.fromstring(f'<root xmlns:w="{W_NS["w"]}">{paragraph_xml}</root>')
        paragraph = root.find(".//w:p", W_NS)
        self.assertEqual(revision_text_for_state(paragraph, accepted=False), original)
        self.assertEqual(revision_text_for_state(paragraph, accepted=True), replacement)
        self.assertEqual(next_revision_id, 9)

    def test_review_report_docx_opens_with_track_changes_enabled(self):
        result = review_nda("This Agreement shall be governed by the laws of California.")

        docx_bytes = build_review_report_docx(result, title="California NDA")

        settings_root, document_root, document_xml = docx_xml_roots(docx_bytes)

        self.assertIsNotNone(settings_root.find(".//w:trackRevisions", W_NS))
        self.assertIsNotNone(settings_root.find(".//w:revisionView", W_NS))
        self.assertIn("NDA Redline", document_xml)
        self.assertIn("The Redlined NDA section contains native Word tracked changes.", document_xml)
        self.assertIn("Governing Law - CHECK", document_xml)
        self.assertIn("Template options", document_xml)
        self.assertIn("This Agreement shall be governed by the laws of the DIFC.", document_xml)
        deletions = document_root.findall(".//w:del", W_NS)
        insertions = document_root.findall(".//w:ins", W_NS)
        deleted_text = tracked_deleted_text(document_root)
        inserted_text = tracked_inserted_text(document_root)
        self.assertGreaterEqual(len(deletions), 1)
        self.assertGreaterEqual(len(insertions), 1)
        self.assertTrue(any("California" in text for text in deleted_text))
        self.assertFalse(any("This Agreement shall be governed by the laws of California." in text for text in deleted_text))
        self.assertTrue(any("England and Wales" in text for text in inserted_text))

    def test_review_report_docx_marks_replace_paragraph_redlines_inline(self):
        result = review_nda("The confidentiality obligations survive for seven years.")

        _settings_root, document_root, _document_xml = docx_xml_roots(build_review_report_docx(result))

        deleted_text = tracked_deleted_text(document_root)
        inserted_text = tracked_inserted_text(document_root)
        self.assertTrue(any("seven" in text for text in deleted_text))
        self.assertFalse(any("The confidentiality obligations survive for seven years." in text for text in deleted_text))
        self.assertTrue(any("a fixed period of up to five" in text for text in inserted_text))

    def test_review_report_docx_marks_delete_paragraph_redlines(self):
        result = review_nda("The Recipient must not circumvent the Company or deal directly with introduced parties.")

        _settings_root, document_root, _document_xml = docx_xml_roots(build_review_report_docx(result))

        deleted_text = tracked_deleted_text(document_root)
        self.assertTrue(
            any(
                "The Recipient must not circumvent the Company or deal directly with introduced parties." in text
                for text in deleted_text
            )
        )

    def test_review_report_docx_marks_insert_after_paragraph_redlines(self):
        result = review_nda("The parties will discuss a possible transaction.")

        _settings_root, document_root, _document_xml = docx_xml_roots(build_review_report_docx(result))

        inserted_text = tracked_inserted_text(document_root)
        self.assertTrue(any("This Agreement shall be governed by the laws of England and Wales." in text for text in inserted_text))

    def test_review_report_docx_splits_multi_paragraph_insertions(self):
        result = review_nda(
            "This Agreement shall be governed by the laws of the DIFC.\n\n"
            "The confidentiality obligations survive for three (3) years."
        )

        _settings_root, document_root, _document_xml = docx_xml_roots(build_review_report_docx(result))

        inserted_text = tracked_inserted_text(document_root)
        self.assertTrue(any("For [Party 1 legal name]" in text for text in inserted_text))
        self.assertTrue(any("For [Party 2 legal name]" in text for text in inserted_text))
        self.assertGreaterEqual(len([text for text in inserted_text if text.startswith("For [Party")]), 2)


if __name__ == "__main__":
    unittest.main()
