from io import BytesIO
import json
import posixpath
from pathlib import Path
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile
import xml.etree.ElementTree as ET

from nda_automation.checker import review_nda
from nda_automation.docx_export import (
    A4_PAGE_HEIGHT_TWIPS,
    A4_PAGE_WIDTH_TWIPS,
    DocxExportError,
    _tracked_replace_paragraph,
    build_review_report_docx,
    build_source_redline_docx,
    validate_docx_open_health,
)
from nda_automation.inline_diff import diff_text_operations
from nda_automation import docx_text
from nda_automation.docx_text import extract_docx_paragraphs
from nda_automation.redline_actions import (
    REDLINE_DELETE_PARAGRAPH,
    REDLINE_INSERT_AFTER_PARAGRAPH,
    REDLINE_REPLACE_PARAGRAPH,
)
from tests.docx_redline_contract import (
    assert_docx_redline_contract,
    inspect_docx_redline_contract,
    paragraph_property_revisions,
)

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
REL_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
STYLE_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
SETTINGS_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
OFFICE_DOCUMENT_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
DOCUMENT_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
RELATIONSHIPS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
SETTINGS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
STYLES_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
INLINE_DIFF_VECTORS_PATH = Path(__file__).parent / "fixtures" / "inline_diff_vectors.json"
# Generated from inline_diff_vectors.source.json; use generate_inline_diff_vectors.mjs.
SOURCE_EXPORT_REPORT_LEAKAGE_PHRASES = [
    "NDA Redline",
    "Review Notes",
    "Clause Findings",
    "Proposed Redline",
    "Overall status:",
    "Requirements passed:",
    "Requirements failed:",
    "Checked at:",
    "The Redlined NDA section contains native Word tracked changes.",
    "source paragraph",
]


def docx_xml_roots(docx_bytes):
    with ZipFile(BytesIO(docx_bytes)) as archive:
        assert archive.testzip() is None
        settings_xml = archive.read("word/settings.xml").decode("utf-8")
        document_xml = archive.read("word/document.xml").decode("utf-8")
    return ET.fromstring(settings_xml), ET.fromstring(document_xml), document_xml


def assert_source_export_has_no_report_leakage(testcase, docx_bytes, extra_forbidden=()):
    with ZipFile(BytesIO(docx_bytes)) as archive:
        testcase.assertIsNone(archive.testzip())
        document_xml = archive.read("word/document.xml").decode("utf-8")
    for phrase in [*SOURCE_EXPORT_REPORT_LEAKAGE_PHRASES, *extra_forbidden]:
        testcase.assertNotIn(phrase, document_xml)


def docx_document_relationship_targets(docx_bytes):
    with ZipFile(BytesIO(docx_bytes)) as archive:
        relationships_xml = archive.read("word/_rels/document.xml.rels").decode("utf-8")
    relationships_root = ET.fromstring(relationships_xml)
    return {
        relationship.attrib["Type"]: relationship.attrib["Target"]
        for relationship in relationships_root.findall(".//rel:Relationship", REL_NS)
    }


def docx_content_type_overrides(docx_bytes):
    content_type_ns = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}
    with ZipFile(BytesIO(docx_bytes)) as archive:
        content_types_xml = archive.read("[Content_Types].xml").decode("utf-8")
    content_types_root = ET.fromstring(content_types_xml)
    return {
        override.attrib["PartName"]: override.attrib["ContentType"]
        for override in content_types_root.findall(".//ct:Override", content_type_ns)
    }


def docx_content_types(docx_bytes):
    content_type_ns = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}
    with ZipFile(BytesIO(docx_bytes)) as archive:
        content_types_xml = archive.read("[Content_Types].xml").decode("utf-8")
    content_types_root = ET.fromstring(content_types_xml)
    defaults = {
        default.attrib["Extension"]: default.attrib["ContentType"]
        for default in content_types_root.findall(".//ct:Default", content_type_ns)
    }
    overrides = {
        override.attrib["PartName"]: override.attrib["ContentType"]
        for override in content_types_root.findall(".//ct:Override", content_type_ns)
    }
    return defaults, overrides


def relationship_targets(archive, relationship_part):
    relationships_root = ET.fromstring(archive.read(relationship_part).decode("utf-8"))
    return [
        relationship.attrib
        for relationship in relationships_root.findall(".//rel:Relationship", REL_NS)
    ]


def resolve_relationship_target(relationship_part, target):
    if target.startswith("/"):
        return target.removeprefix("/")
    if relationship_part == "_rels/.rels":
        base_dir = ""
    else:
        rels_dir = posixpath.dirname(relationship_part)
        base_dir = posixpath.dirname(rels_dir)
    return posixpath.normpath(posixpath.join(base_dir, target))


def assert_docx_package_healthy(testcase, docx_bytes, require_styles=False):
    testcase.assertEqual(validate_docx_open_health(docx_bytes, require_styles=require_styles), [])
    required_parts = {
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
        "word/settings.xml",
    }
    if require_styles:
        required_parts.add("word/styles.xml")

    with ZipFile(BytesIO(docx_bytes)) as archive:
        testcase.assertIsNone(archive.testzip())
        names = set(archive.namelist())
        testcase.assertTrue(required_parts <= names, f"Missing DOCX parts: {sorted(required_parts - names)}")

        defaults, overrides = docx_content_types(docx_bytes)
        testcase.assertEqual(defaults.get("rels"), RELATIONSHIPS_CONTENT_TYPE)
        testcase.assertEqual(defaults.get("xml"), "application/xml")
        testcase.assertEqual(overrides.get("/word/document.xml"), DOCUMENT_CONTENT_TYPE)
        testcase.assertEqual(overrides.get("/word/settings.xml"), SETTINGS_CONTENT_TYPE)
        if "word/styles.xml" in names:
            testcase.assertEqual(overrides.get("/word/styles.xml"), STYLES_CONTENT_TYPE)

        package_relationships = relationship_targets(archive, "_rels/.rels")
        office_document_targets = [
            resolve_relationship_target("_rels/.rels", relationship["Target"])
            for relationship in package_relationships
            if relationship.get("Type") == OFFICE_DOCUMENT_RELATIONSHIP_TYPE
        ]
        testcase.assertEqual(office_document_targets, ["word/document.xml"])

        document_relationships = relationship_targets(archive, "word/_rels/document.xml.rels")
        relationship_targets_by_type = {
            relationship["Type"]: resolve_relationship_target("word/_rels/document.xml.rels", relationship["Target"])
            for relationship in document_relationships
            if relationship.get("TargetMode") != "External"
        }
        for target in relationship_targets_by_type.values():
            testcase.assertIn(target, names)
        testcase.assertEqual(relationship_targets_by_type.get(SETTINGS_RELATIONSHIP_TYPE), "word/settings.xml")
        if require_styles:
            testcase.assertEqual(relationship_targets_by_type.get(STYLE_RELATIONSHIP_TYPE), "word/styles.xml")
        elif STYLE_RELATIONSHIP_TYPE in relationship_targets_by_type:
            testcase.assertEqual(relationship_targets_by_type[STYLE_RELATIONSHIP_TYPE], "word/styles.xml")


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


def paragraph_mark_revisions(document_root, kind):
    return document_root.findall(f".//w:pPr/w:rPr/w:{kind}", W_NS)


def revision_text_for_state(node, accepted):
    tag = node.tag.rsplit("}", 1)[-1]
    if tag == "del":
        return "" if accepted else "".join(revision_text_for_state(child, accepted=False) for child in list(node))
    if tag == "ins":
        return "".join(revision_text_for_state(child, accepted=True) for child in list(node)) if accepted else ""
    if tag in {"t", "delText"}:
        return node.text or ""
    if tag == "br":
        return "\n"
    return "".join(revision_text_for_state(child, accepted) for child in list(node))


def make_source_docx(paragraphs, include_package_rels=True):
    body = "".join(
        f"<w:p><w:r><w:t>{escape_xml(paragraph)}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body}</w:body>
</w:document>"""
    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    package_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
    with BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types_xml)
            if include_package_rels:
                archive.writestr("_rels/.rels", package_rels_xml)
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", document_rels_xml)
            archive.writestr("customXml/item1.xml", "<custom>preserved</custom>")
        return output.getvalue()


def replace_docx_parts(docx_bytes, replacements):
    with ZipFile(BytesIO(docx_bytes), "r") as source_archive:
        with BytesIO() as output:
            with ZipFile(output, "w", ZIP_DEFLATED) as patched_archive:
                written = set()
                for item in source_archive.infolist():
                    data = replacements.get(item.filename, source_archive.read(item.filename))
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                    patched_archive.writestr(item, data)
                    written.add(item.filename)
                for name, data in replacements.items():
                    if name in written:
                        continue
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                    patched_archive.writestr(name, data)
            return output.getvalue()


def unsafe_document_xml():
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE w:document [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body>
</w:document>"""


def escape_xml(value):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def track_changes_contract_review_result():
    paragraphs = [
        {
            "id": "p1",
            "index": 1,
            "source_index": 1,
            "text": "This Agreement shall be governed by the laws of California.",
        },
        {
            "id": "p2",
            "index": 2,
            "source_index": 2,
            "text": "Insert after this paragraph.",
        },
        {
            "id": "p3",
            "index": 3,
            "source_index": 3,
            "text": "The Recipient must not circumvent the Company.",
        },
    ]
    redline_edits = [
        {
            "id": "r1",
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p1",
            "source_index": 1,
            "original_text": paragraphs[0]["text"],
            "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
        },
        {
            "id": "r2",
            "action": REDLINE_INSERT_AFTER_PARAGRAPH,
            "paragraph_id": "p2",
            "source_index": 2,
            "insert_text": "New required clause.",
        },
        {
            "id": "r3",
            "action": REDLINE_DELETE_PARAGRAPH,
            "paragraph_id": "p3",
            "source_index": 3,
            "original_text": paragraphs[2]["text"],
        },
    ]
    return {
        "overall_status": "does_not_meet_requirements",
        "requirements_passed": 0,
        "requirements_failed": 3,
        "checked_at": "2026-05-31T00:00:00+00:00",
        "paragraphs": paragraphs,
        "clauses": [],
        "redline_edits": redline_edits,
    }


def assert_track_changes_contract(testcase, docx_bytes, redline_edits):
    return assert_docx_redline_contract(testcase, docx_bytes, redline_edits)


def inline_diff_vectors():
    with INLINE_DIFF_VECTORS_PATH.open(encoding="utf-8") as fixture:
        return json.load(fixture)


def expand_inline_diff_vector(vector):
    original = vector["original"]
    replacement = vector["replacement"]
    operations = [(operation["type"], operation["token"]) for operation in vector["operations"]]
    return original, replacement, operations


class DocxExportTests(unittest.TestCase):
    def test_inline_diff_operations_match_shared_vectors(self):
        for vector in inline_diff_vectors():
            with self.subTest(vector["name"]):
                original, replacement, expected_operations = expand_inline_diff_vector(vector)
                self.assertEqual(diff_text_operations(original, replacement), expected_operations)

    def test_tracked_replace_paragraph_preserves_punctuation_spacing(self):
        original = "This Agreement (California) applies."
        replacement = "This Agreement (England and Wales) applies."

        paragraph_xml, next_revision_id = _tracked_replace_paragraph(original, replacement, 7)

        root = ET.fromstring(f'<root xmlns:w="{W_NS["w"]}">{paragraph_xml}</root>')
        paragraph = root.find(".//w:p", W_NS)
        self.assertEqual(revision_text_for_state(paragraph, accepted=False), original)
        self.assertEqual(revision_text_for_state(paragraph, accepted=True), replacement)
        self.assertEqual(next_revision_id, 9)

    def test_tracked_replace_paragraph_preserves_grouped_numbers_and_non_ascii_words(self):
        original = "Payment cap is 1,000 for café records."
        replacement = "Payment cap is 1,000 for café documents."

        paragraph_xml, next_revision_id = _tracked_replace_paragraph(original, replacement, 7)

        root = ET.fromstring(f'<root xmlns:w="{W_NS["w"]}">{paragraph_xml}</root>')
        paragraph = root.find(".//w:p", W_NS)
        self.assertEqual(revision_text_for_state(paragraph, accepted=False), original)
        self.assertEqual(revision_text_for_state(paragraph, accepted=True), replacement)
        self.assertEqual(next_revision_id, 9)

    def test_tracked_replace_paragraph_preserves_spaced_numeric_list_items(self):
        original = "Payment caps are 1, 2, 3, 400 for different classes."
        replacement = "Payment caps are 1, 2, 3, 400 for different categories."

        paragraph_xml, next_revision_id = _tracked_replace_paragraph(original, replacement, 7)

        root = ET.fromstring(f'<root xmlns:w="{W_NS["w"]}">{paragraph_xml}</root>')
        paragraph = root.find(".//w:p", W_NS)
        self.assertEqual(revision_text_for_state(paragraph, accepted=False), original)
        self.assertEqual(revision_text_for_state(paragraph, accepted=True), replacement)
        self.assertEqual(next_revision_id, 9)

    def test_tracked_replace_paragraph_preserves_newlines(self):
        original = "Line one\nLine two"
        replacement = "Line one\nLine three"

        paragraph_xml, next_revision_id = _tracked_replace_paragraph(original, replacement, 7)

        root = ET.fromstring(f'<root xmlns:w="{W_NS["w"]}">{paragraph_xml}</root>')
        paragraph = root.find(".//w:p", W_NS)
        self.assertEqual(revision_text_for_state(paragraph, accepted=False), original)
        self.assertEqual(revision_text_for_state(paragraph, accepted=True), replacement)
        self.assertEqual(next_revision_id, 9)

    def test_review_report_docx_opens_with_track_changes_enabled(self):
        result = review_nda("This Agreement shall be governed by the laws of California.")

        docx_bytes = build_review_report_docx(result, title="California NDA")

        assert_docx_package_healthy(self, docx_bytes, require_styles=True)
        settings_root, document_root, document_xml = docx_xml_roots(docx_bytes)
        relationship_targets = docx_document_relationship_targets(docx_bytes)

        self.assertEqual(relationship_targets[STYLE_RELATIONSHIP_TYPE], "styles.xml")
        self.assertEqual(relationship_targets[SETTINGS_RELATIONSHIP_TYPE], "settings.xml")
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

    def test_review_report_docx_strips_invalid_xml_characters(self):
        result = review_nda("This Agreement shall be governed by the laws of California.\x08\ud800")

        docx_bytes = build_review_report_docx(result, title="California\x08\udfffNDA")

        assert_docx_package_healthy(self, docx_bytes, require_styles=True)
        with ZipFile(BytesIO(docx_bytes)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
            core_xml = archive.read("docProps/core.xml").decode("utf-8")
        ET.fromstring(document_xml)
        ET.fromstring(core_xml)
        self.assertNotIn("\x08", document_xml)
        self.assertNotIn("\x08", core_xml)
        self.assertNotIn("\ud800", document_xml)
        self.assertNotIn("\udfff", core_xml)

    def test_source_docx_export_strips_invalid_xml_characters_from_redlines(self):
        source_docx = make_source_docx(["This Agreement shall be governed by the laws of California."])
        review_result = {
            "paragraphs": [
                {
                    "id": "p1",
                    "index": 1,
                    "source_index": 1,
                    "text": "This Agreement shall be governed by the laws of California.",
                }
            ],
            "redline_edits": [
                {
                    "id": "r1",
                    "paragraph_id": "p1",
                    "paragraph_index": 1,
                    "source_index": 1,
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "original_text": "This Agreement shall be governed by the laws of California.\ud800",
                    "replacement_text": "This Agreement shall be governed by the laws of England and Wales.\udfff",
                }
            ],
        }

        redlined_docx = build_source_redline_docx(source_docx, review_result)

        assert_docx_package_healthy(self, redlined_docx)
        with ZipFile(BytesIO(redlined_docx)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        ET.fromstring(document_xml)
        self.assertIn("England and Wales", document_xml)
        self.assertNotIn("\ud800", document_xml)
        self.assertNotIn("\udfff", document_xml)

    def test_review_report_docx_marks_replace_paragraph_redlines_inline(self):
        result = review_nda("The confidentiality obligations survive for seven years.")

        _settings_root, document_root, _document_xml = docx_xml_roots(build_review_report_docx(result))

        deleted_text = tracked_deleted_text(document_root)
        inserted_text = tracked_inserted_text(document_root)
        self.assertTrue(any("seven" in text for text in deleted_text))
        self.assertFalse(any("The confidentiality obligations survive for seven years." in text for text in deleted_text))
        self.assertTrue(any("a fixed period of up to five" in text for text in inserted_text))

    def test_source_docx_export_redlines_original_document_inline(self):
        source_docx = make_source_docx([
            "Intro paragraph.",
            "This Agreement shall be governed by the laws of California.",
            "The confidentiality obligations survive for three years.",
        ])
        paragraphs = extract_docx_paragraphs(source_docx)
        result = review_nda("\n\n".join(str(paragraph["text"]) for paragraph in paragraphs), paragraphs=paragraphs)

        redlined_docx = build_source_redline_docx(source_docx, result)

        assert_docx_package_healthy(self, redlined_docx)
        assert_source_export_has_no_report_leakage(self, redlined_docx)
        settings_root, document_root, document_xml = docx_xml_roots(redlined_docx)
        relationship_targets = docx_document_relationship_targets(redlined_docx)
        content_type_overrides = docx_content_type_overrides(redlined_docx)
        with ZipFile(BytesIO(redlined_docx)) as archive:
            self.assertEqual(archive.read("customXml/item1.xml").decode("utf-8"), "<custom>preserved</custom>")

        deleted_text = tracked_deleted_text(document_root)
        inserted_text = tracked_inserted_text(document_root)
        self.assertIsNotNone(settings_root.find(".//w:trackRevisions", W_NS))
        self.assertEqual(relationship_targets[SETTINGS_RELATIONSHIP_TYPE], "settings.xml")
        self.assertEqual(
            content_type_overrides["/word/settings.xml"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
        )
        self.assertIn("Intro paragraph.", document_xml)
        self.assertTrue(any("California" in text for text in deleted_text))
        self.assertFalse(any("This Agreement shall be governed by the laws of California." in text for text in deleted_text))
        self.assertTrue(any("England and Wales" in text for text in inserted_text))

    def test_source_docx_export_preserves_ignorable_namespace_prefixes(self):
        source_text = "This Agreement shall be governed by the laws of California."
        document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
  xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
  xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
  xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
  mc:Ignorable="w14 wp14">
  <w:body><w:p><w:r><w:t>{escape_xml(source_text)}</w:t></w:r></w:p></w:body>
</w:document>"""
        source_docx = replace_docx_parts(
            make_source_docx([source_text]),
            {"word/document.xml": document_xml},
        )
        result = review_nda(source_text)

        redlined_docx = build_source_redline_docx(source_docx, result)

        assert_docx_package_healthy(self, redlined_docx)
        with ZipFile(BytesIO(redlined_docx)) as archive:
            redlined_xml = archive.read("word/document.xml").decode("utf-8")
        ET.fromstring(redlined_xml)
        self.assertIn('mc:Ignorable="w14 wp14"', redlined_xml)
        self.assertIn('xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"', redlined_xml)
        self.assertIn('xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"', redlined_xml)
        self.assertIn('xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"', redlined_xml)
        self.assertNotIn("ns0:Ignorable", redlined_xml)
        self.assertNotIn("xmlns:ns", redlined_xml)

    def test_source_docx_export_preserves_grouped_numbers_and_non_ascii_words(self):
        original = "Payment cap is 1,000 for café records."
        replacement = "Payment cap is 1,000 for café documents."
        source_docx = make_source_docx([original])
        review_result = {
            "overall_status": "does_not_meet_requirements",
            "requirements_passed": 0,
            "requirements_failed": 1,
            "checked_at": "2026-06-01T00:00:00+00:00",
            "paragraphs": [{"id": "p1", "index": 1, "source_index": 1, "text": original}],
            "clauses": [],
            "redline_edits": [
                {
                    "id": "r1",
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p1",
                    "source_index": 1,
                    "original_text": original,
                    "replacement_text": replacement,
                }
            ],
        }

        redlined_docx = build_source_redline_docx(source_docx, review_result)

        assert_docx_package_healthy(self, redlined_docx)
        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        paragraph = document_root.find(".//w:body/w:p", W_NS)
        self.assertEqual(revision_text_for_state(paragraph, accepted=False), original)
        self.assertEqual(revision_text_for_state(paragraph, accepted=True), replacement)

    def test_source_docx_export_preserves_multiple_redlines_on_same_source_paragraph(self):
        original = "This Agreement is governed by California. Confidentiality survives for seven years."
        governing_law_replacement = "This Agreement is governed by the laws of England and Wales."
        term_replacement = "Confidentiality survives for a fixed period of up to five years."
        source_docx = make_source_docx([original])
        review_result = {
            "overall_status": "does_not_meet_requirements",
            "requirements_passed": 0,
            "requirements_failed": 2,
            "checked_at": "2026-06-01T00:00:00+00:00",
            "paragraphs": [{"id": "p1", "index": 1, "source_index": 1, "text": original}],
            "clauses": [],
            "redline_edits": [
                {
                    "id": "r1",
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p1",
                    "source_index": 1,
                    "original_text": original,
                    "replacement_text": governing_law_replacement,
                },
                {
                    "id": "r2",
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p1",
                    "source_index": 1,
                    "original_text": original,
                    "replacement_text": term_replacement,
                },
            ],
        }

        redlined_docx = build_source_redline_docx(source_docx, review_result)

        assert_docx_package_healthy(self, redlined_docx)
        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        paragraphs = document_root.findall(".//w:body/w:p", W_NS)
        states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in paragraphs
        ]
        self.assertIn((original, governing_law_replacement), states)
        self.assertIn((original, term_replacement), states)
        self.assertEqual(sum(1 for rejected, _accepted in states if rejected == original), 2)
        assert_track_changes_contract(self, redlined_docx, review_result["redline_edits"])

    def test_source_docx_export_rejects_suspicious_compression_ratio(self):
        source_docx = make_source_docx(["A" * 4096])

        with patch.object(docx_text, "MAX_DOCX_ENTRY_COMPRESSION_RATIO", 2):
            with self.assertRaises(DocxExportError):
                build_source_redline_docx(source_docx, {"paragraphs": [], "redline_edits": []})

    def test_source_docx_export_rejects_xml_dtd_entity_declarations(self):
        source_docx = replace_docx_parts(
            make_source_docx(["Safe body text."]),
            {"word/document.xml": unsafe_document_xml()},
        )

        with self.assertRaises(DocxExportError):
            build_source_redline_docx(source_docx, {"paragraphs": [], "redline_edits": []})

    def test_source_docx_export_prefers_text_anchors_over_stale_source_index(self):
        source_docx = make_source_docx([
            "Intro paragraph.",
            "Insert after this paragraph.",
            "This Agreement shall be governed by the laws of California.",
            "The Recipient must not circumvent the Company.",
        ])
        review_result = {
            "overall_status": "does_not_meet_requirements",
            "requirements_passed": 0,
            "requirements_failed": 3,
            "checked_at": "2026-05-31T00:00:00+00:00",
            "paragraphs": [
                {"id": "p1", "index": 1, "source_index": 1, "text": "Intro paragraph."},
                {"id": "p2", "index": 2, "source_index": 2, "text": "Insert after this paragraph."},
                {
                    "id": "p3",
                    "index": 3,
                    "source_index": 3,
                    "text": "This Agreement shall be governed by the laws of California.",
                },
                {"id": "p4", "index": 4, "source_index": 4, "text": "The Recipient must not circumvent the Company."},
            ],
            "clauses": [],
            "redline_edits": [
                {
                    "id": "r1",
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p3",
                    "source_index": 1,
                    "original_text": "This Agreement shall be governed by the laws of California.",
                    "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
                },
                {
                    "id": "r2",
                    "action": REDLINE_INSERT_AFTER_PARAGRAPH,
                    "paragraph_id": "p2",
                    "source_index": 1,
                    "anchor_text": "Insert after this paragraph.",
                    "insert_text": "New required clause.",
                },
                {
                    "id": "r3",
                    "action": REDLINE_DELETE_PARAGRAPH,
                    "paragraph_id": "p4",
                    "source_index": 1,
                    "original_text": "The Recipient must not circumvent the Company.",
                },
            ],
        }

        redlined_docx = build_source_redline_docx(source_docx, review_result)

        assert_docx_package_healthy(self, redlined_docx)
        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        paragraphs = document_root.findall(".//w:body/w:p", W_NS)
        states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in paragraphs
        ]
        self.assertIn(("Intro paragraph.", "Intro paragraph."), states)
        self.assertIn(("Insert after this paragraph.", "Insert after this paragraph."), states)
        self.assertIn(("", "New required clause."), states)
        self.assertIn(
            (
                "This Agreement shall be governed by the laws of California.",
                "This Agreement shall be governed by the laws of England and Wales.",
            ),
            states,
        )
        self.assertIn(("The Recipient must not circumvent the Company.", ""), states)

    def test_source_docx_export_skips_ambiguous_text_anchor_without_source_index(self):
        source_docx = make_source_docx([
            "Duplicate paragraph.",
            "Duplicate paragraph.",
        ])
        review_result = {
            "overall_status": "does_not_meet_requirements",
            "requirements_passed": 0,
            "requirements_failed": 1,
            "checked_at": "2026-05-31T00:00:00+00:00",
            "paragraphs": [
                {"id": "p1", "index": 1, "text": "Duplicate paragraph."},
            ],
            "clauses": [],
            "redline_edits": [
                {
                    "id": "r1",
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p1",
                    "original_text": "Duplicate paragraph.",
                    "replacement_text": "Changed paragraph.",
                },
            ],
        }

        with self.assertLogs("nda_automation.docx_export", level="WARNING") as logs:
            redlined_docx = build_source_redline_docx(source_docx, review_result)
        self.assertIn("unresolved or ambiguous anchor", "\n".join(logs.output))

        assert_docx_package_healthy(self, redlined_docx)
        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        paragraphs = document_root.findall(".//w:body/w:p", W_NS)
        states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in paragraphs
        ]
        self.assertEqual(states, [("Duplicate paragraph.", "Duplicate paragraph.")] * 2)

    def test_source_docx_export_skips_supplemental_part_redlines(self):
        source_docx = make_source_docx([
            "This Agreement shall be governed by the laws of California.",
        ])
        review_result = {
            "overall_status": "does_not_meet_requirements",
            "requirements_passed": 0,
            "requirements_failed": 1,
            "checked_at": "2026-05-31T00:00:00+00:00",
            "paragraphs": [
                {
                    "id": "p1",
                    "index": 1,
                    "source_part": "header1",
                    "text": "This Agreement shall be governed by the laws of California.",
                },
            ],
            "clauses": [],
            "redline_edits": [
                {
                    "id": "r1",
                    "action": REDLINE_REPLACE_PARAGRAPH,
                    "paragraph_id": "p1",
                    "source_part": "header1",
                    "original_text": "This Agreement shall be governed by the laws of California.",
                    "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
                },
            ],
        }

        with self.assertLogs("nda_automation.docx_export", level="WARNING") as logs:
            redlined_docx = build_source_redline_docx(source_docx, review_result)
        self.assertIn("unresolved or ambiguous anchor", "\n".join(logs.output))

        assert_docx_package_healthy(self, redlined_docx)
        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        paragraphs = document_root.findall(".//w:body/w:p", W_NS)
        self.assertEqual(
            [
                (
                    revision_text_for_state(paragraph, accepted=False),
                    revision_text_for_state(paragraph, accepted=True),
                )
                for paragraph in paragraphs
            ],
            [
                (
                    "This Agreement shall be governed by the laws of California.",
                    "This Agreement shall be governed by the laws of California.",
                ),
            ],
        )

    def test_source_docx_export_repairs_missing_package_relationships(self):
        source_docx = make_source_docx(
            ["This Agreement shall be governed by the laws of California."],
            include_package_rels=False,
        )
        paragraphs = extract_docx_paragraphs(source_docx)
        result = review_nda("\n\n".join(str(paragraph["text"]) for paragraph in paragraphs), paragraphs=paragraphs)

        redlined_docx = build_source_redline_docx(source_docx, result)

        assert_docx_package_healthy(self, redlined_docx)

    def test_source_docx_export_repairs_malformed_required_package_metadata(self):
        source_docx = make_source_docx([
            "This Agreement shall be governed by the laws of California.",
        ])
        source_docx = replace_docx_parts(source_docx, {
            "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/xml"/>
  <Default Extension="xml" ContentType="text/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/xml"/>
</Types>""",
            "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/missing-document.xml"/>
</Relationships>""",
            "word/_rels/document.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="missing-settings.xml"/>
</Relationships>""",
            "word/settings.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:trackRevisions w:val="0"/>
</w:settings>""",
        })
        paragraphs = extract_docx_paragraphs(source_docx)
        result = review_nda("\n\n".join(str(paragraph["text"]) for paragraph in paragraphs), paragraphs=paragraphs)

        redlined_docx = build_source_redline_docx(source_docx, result)

        assert_docx_package_healthy(self, redlined_docx)
        defaults, overrides = docx_content_types(redlined_docx)
        self.assertEqual(defaults["rels"], RELATIONSHIPS_CONTENT_TYPE)
        self.assertEqual(defaults["xml"], "application/xml")
        self.assertEqual(overrides["/word/document.xml"], DOCUMENT_CONTENT_TYPE)
        self.assertEqual(overrides["/word/settings.xml"], SETTINGS_CONTENT_TYPE)
        with ZipFile(BytesIO(redlined_docx)) as archive:
            package_relationships = relationship_targets(archive, "_rels/.rels")
        office_document_targets = [
            resolve_relationship_target("_rels/.rels", relationship["Target"])
            for relationship in package_relationships
            if relationship.get("Type") == OFFICE_DOCUMENT_RELATIONSHIP_TYPE
        ]
        self.assertEqual(office_document_targets, ["word/document.xml"])
        self.assertEqual(docx_document_relationship_targets(redlined_docx)[SETTINGS_RELATIONSHIP_TYPE], "settings.xml")

    def test_source_docx_export_adds_missing_section_properties(self):
        source_docx = make_source_docx([
            "This Agreement shall be governed by the laws of California.",
        ])
        paragraphs = extract_docx_paragraphs(source_docx)
        result = review_nda("\n\n".join(str(paragraph["text"]) for paragraph in paragraphs), paragraphs=paragraphs)

        redlined_docx = build_source_redline_docx(source_docx, result)

        assert_docx_package_healthy(self, redlined_docx)
        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        section = document_root.find(".//w:body/w:sectPr", W_NS)
        self.assertIsNotNone(section)
        page_size = section.find("w:pgSz", W_NS)
        self.assertIsNotNone(page_size)
        self.assertEqual(page_size.get(f"{{{W_NS['w']}}}w"), A4_PAGE_WIDTH_TWIPS)
        self.assertEqual(page_size.get(f"{{{W_NS['w']}}}h"), A4_PAGE_HEIGHT_TWIPS)

    def test_source_docx_export_marks_paragraph_deletes_and_insertions(self):
        source_docx = make_source_docx([
            "The parties will discuss a possible transaction.",
            "The Recipient must not circumvent the Company or deal directly with introduced parties.",
        ])
        paragraphs = extract_docx_paragraphs(source_docx)
        result = review_nda("\n\n".join(str(paragraph["text"]) for paragraph in paragraphs), paragraphs=paragraphs)

        redlined_docx = build_source_redline_docx(source_docx, result)

        assert_source_export_has_no_report_leakage(self, redlined_docx)
        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        deleted_text = tracked_deleted_text(document_root)
        inserted_text = tracked_inserted_text(document_root)
        self.assertTrue(any("must not circumvent" in text for text in deleted_text))
        self.assertTrue(any("This Agreement shall be governed by the laws of England and Wales." in text for text in inserted_text))
        self.assertEqual(paragraph_property_revisions(document_root, "del"), [])
        self.assertEqual(paragraph_property_revisions(document_root, "ins"), [])

    def test_source_docx_export_strips_source_paragraph_property_revisions(self):
        source_docx = make_source_docx([
            "The parties will discuss a possible transaction.",
            "The Recipient must not circumvent the Company or deal directly with introduced parties.",
        ])
        with ZipFile(BytesIO(source_docx)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        document_xml = document_xml.replace(
            "<w:p><w:r><w:t>The parties will discuss a possible transaction.</w:t></w:r></w:p>",
            '<w:p><w:pPr><w:rPr><w:ins w:id="97" w:author="source" w:date="2026-06-01T00:00:00Z" />'
            '<w:del w:id="98" w:author="source" w:date="2026-06-01T00:00:00Z" /></w:rPr></w:pPr>'
            "<w:r><w:t>The parties will discuss a possible transaction.</w:t></w:r></w:p>",
        )
        source_docx = replace_docx_parts(source_docx, {"word/document.xml": document_xml})
        paragraphs = extract_docx_paragraphs(source_docx)
        result = review_nda("\n\n".join(str(paragraph["text"]) for paragraph in paragraphs), paragraphs=paragraphs)

        redlined_docx = build_source_redline_docx(source_docx, result)

        assert_docx_package_healthy(self, redlined_docx)
        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        self.assertEqual(paragraph_property_revisions(document_root, "del"), [])
        self.assertEqual(paragraph_property_revisions(document_root, "ins"), [])
        self.assertTrue(any("must not circumvent" in text for text in tracked_deleted_text(document_root)))
        self.assertTrue(any("England and Wales" in text for text in tracked_inserted_text(document_root)))

    def test_source_docx_export_matches_redline_actions_at_paragraph_level(self):
        source_docx = make_source_docx([
            "This Agreement shall be governed by the laws of California.",
            "Insert after this paragraph.",
            "The Recipient must not circumvent the Company.",
        ])
        review_result = track_changes_contract_review_result()

        redlined_docx = build_source_redline_docx(source_docx, review_result)

        assert_source_export_has_no_report_leakage(self, redlined_docx)
        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        paragraphs = document_root.findall(".//w:body/w:p", W_NS)
        states = [
            (
                revision_text_for_state(paragraph, accepted=False),
                revision_text_for_state(paragraph, accepted=True),
            )
            for paragraph in paragraphs
        ]
        self.assertIn(
            (
                "This Agreement shall be governed by the laws of California.",
                "This Agreement shall be governed by the laws of England and Wales.",
            ),
            states,
        )
        self.assertIn(("Insert after this paragraph.", "Insert after this paragraph."), states)
        self.assertIn(("", "New required clause."), states)
        self.assertIn(("The Recipient must not circumvent the Company.", ""), states)
        assert_track_changes_contract(self, redlined_docx, review_result["redline_edits"])

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
        self.assertEqual(paragraph_property_revisions(document_root, "del"), [])

    def test_review_report_docx_marks_insert_after_paragraph_redlines(self):
        result = review_nda("The parties will discuss a possible transaction.")

        _settings_root, document_root, _document_xml = docx_xml_roots(build_review_report_docx(result))

        inserted_text = tracked_inserted_text(document_root)
        self.assertTrue(any("This Agreement shall be governed by the laws of England and Wales." in text for text in inserted_text))
        self.assertEqual(paragraph_property_revisions(document_root, "ins"), [])

    def test_docx_open_health_rejects_revision_markup_inside_paragraph_properties(self):
        result = review_nda("The parties will discuss a possible transaction.")
        docx_bytes = build_review_report_docx(result)
        with ZipFile(BytesIO(docx_bytes), "r") as source_archive:
            document_xml = source_archive.read("word/document.xml").decode("utf-8")
            document_xml = document_xml.replace(
                "<w:p><w:ins ",
                '<w:p><w:pPr><w:rPr><w:ins w:id="999" w:author="bad" w:date="2026-05-31T00:00:00Z" /></w:rPr></w:pPr><w:ins ',
                1,
            )
            with BytesIO() as output:
                with ZipFile(output, "w", ZIP_DEFLATED) as patched_archive:
                    for item in source_archive.infolist():
                        data = document_xml.encode("utf-8") if item.filename == "word/document.xml" else source_archive.read(item.filename)
                        patched_archive.writestr(item, data)
                patched_docx = output.getvalue()

        errors = validate_docx_open_health(patched_docx, require_styles=True)

        self.assertIn("document.xml contains insertion revision markup inside paragraph properties.", errors)

    def test_docx_open_health_rejects_xml_dtd_entity_declarations(self):
        docx_bytes = replace_docx_parts(
            build_review_report_docx(review_nda("The parties will discuss a possible transaction.")),
            {"word/document.xml": unsafe_document_xml()},
        )

        errors = validate_docx_open_health(docx_bytes, require_styles=True)

        self.assertTrue(
            any("unsupported XML DTD/entity declarations" in error for error in errors),
            errors,
        )

    def test_review_report_docx_preserves_track_changes_contract_by_redline_action(self):
        result = track_changes_contract_review_result()

        report_docx = build_review_report_docx(result)

        inspection = assert_track_changes_contract(self, report_docx, result["redline_edits"])
        self.assertEqual(inspection.summary["expected_replace"], 1)
        self.assertEqual(inspection.summary["expected_insert"], 1)
        self.assertEqual(inspection.summary["expected_delete"], 1)

    def test_redline_contract_inspector_rejects_accepted_only_exports(self):
        result = track_changes_contract_review_result()
        accepted_only_result = {**result, "redline_edits": []}

        report_docx = build_review_report_docx(accepted_only_result)
        inspection = inspect_docx_redline_contract(report_docx, result["redline_edits"])

        self.assertTrue(any("replace did not preserve" in error for error in inspection.errors))
        self.assertTrue(any("insert did not preserve" in error for error in inspection.errors))
        self.assertTrue(any("delete did not preserve" in error for error in inspection.errors))

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
        self.assertEqual(paragraph_mark_revisions(document_root, "ins"), [])


if __name__ == "__main__":
    unittest.main()
