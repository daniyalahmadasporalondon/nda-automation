from io import BytesIO
import json
import posixpath
from pathlib import Path
import unittest
from zipfile import ZIP_DEFLATED, ZipFile
import xml.etree.ElementTree as ET

from nda_automation.checker import review_nda
from nda_automation.docx_export import (
    _diff_text_operations,
    _tracked_replace_paragraph,
    build_review_report_docx,
    build_source_redline_docx,
)
from nda_automation.docx_text import extract_docx_paragraphs

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


def docx_xml_roots(docx_bytes):
    with ZipFile(BytesIO(docx_bytes)) as archive:
        assert archive.testzip() is None
        settings_xml = archive.read("word/settings.xml").decode("utf-8")
        document_xml = archive.read("word/document.xml").decode("utf-8")
    return ET.fromstring(settings_xml), ET.fromstring(document_xml), document_xml


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
        return "".join(item.text or "" for item in node.findall(".//w:delText", W_NS)) if not accepted else ""
    if tag == "ins":
        return "".join(item.text or "" for item in node.findall(".//w:t", W_NS)) if accepted else ""
    if tag == "t":
        return node.text or ""
    if tag == "br":
        return "\n"
    return "".join(revision_text_for_state(child, accepted) for child in list(node))


def make_source_docx(paragraphs):
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
            archive.writestr("_rels/.rels", package_rels_xml)
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/_rels/document.xml.rels", document_rels_xml)
            archive.writestr("customXml/item1.xml", "<custom>preserved</custom>")
        return output.getvalue()


def escape_xml(value):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def inline_diff_vectors():
    with INLINE_DIFF_VECTORS_PATH.open(encoding="utf-8") as fixture:
        return json.load(fixture)


def expand_inline_diff_vector(vector):
    if "originalTokenBlock" in vector:
        original = token_block_text(vector["originalTokenBlock"])
    else:
        original = vector["original"]
    if "replacementTokenBlock" in vector:
        replacement = token_block_text(vector["replacementTokenBlock"])
    else:
        replacement = vector["replacement"]

    operations = []
    for operation in vector.get("operations", []):
        operations.append((operation["type"], operation["token"]))
    for block in vector.get("operationBlocks", []):
        operations.extend(
            (block["type"], f'{block["prefix"]}{index}')
            for index in range(block["count"])
        )
    return original, replacement, operations


def token_block_text(block):
    return " ".join(f'{block["prefix"]}{index}' for index in range(block["count"]))


class DocxExportTests(unittest.TestCase):
    def test_inline_diff_operations_match_shared_vectors(self):
        for vector in inline_diff_vectors():
            with self.subTest(vector["name"]):
                original, replacement, expected_operations = expand_inline_diff_vector(vector)
                if "originalTokenBlock" in vector:
                    self.assertEqual(len(original.split()), vector["originalTokenBlock"]["count"])
                if "replacementTokenBlock" in vector:
                    self.assertEqual(len(replacement.split()), vector["replacementTokenBlock"]["count"])
                if "operationBlocks" in vector:
                    self.assertEqual(
                        len(expected_operations),
                        sum(block["count"] for block in vector["operationBlocks"]),
                    )
                self.assertEqual(_diff_text_operations(original, replacement), expected_operations)

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
        self.assertNotIn("NDA Redline", document_xml)
        self.assertNotIn("Review Notes", document_xml)
        self.assertIn("Intro paragraph.", document_xml)
        self.assertTrue(any("California" in text for text in deleted_text))
        self.assertFalse(any("This Agreement shall be governed by the laws of California." in text for text in deleted_text))
        self.assertTrue(any("England and Wales" in text for text in inserted_text))

    def test_source_docx_export_marks_paragraph_deletes_and_insertions(self):
        source_docx = make_source_docx([
            "The parties will discuss a possible transaction.",
            "The Recipient must not circumvent the Company or deal directly with introduced parties.",
        ])
        paragraphs = extract_docx_paragraphs(source_docx)
        result = review_nda("\n\n".join(str(paragraph["text"]) for paragraph in paragraphs), paragraphs=paragraphs)

        redlined_docx = build_source_redline_docx(source_docx, result)

        _settings_root, document_root, _document_xml = docx_xml_roots(redlined_docx)
        deleted_text = tracked_deleted_text(document_root)
        inserted_text = tracked_inserted_text(document_root)
        self.assertTrue(any("must not circumvent" in text for text in deleted_text))
        self.assertTrue(any("This Agreement shall be governed by the laws of England and Wales." in text for text in inserted_text))
        self.assertGreaterEqual(len(paragraph_mark_revisions(document_root, "del")), 1)
        self.assertGreaterEqual(len(paragraph_mark_revisions(document_root, "ins")), 1)

    def test_source_docx_export_matches_redline_actions_at_paragraph_level(self):
        source_docx = make_source_docx([
            "This Agreement shall be governed by the laws of California.",
            "Insert after this paragraph.",
            "The Recipient must not circumvent the Company.",
        ])
        review_result = {
            "redline_edits": [
                {
                    "action": "replace_paragraph",
                    "source_index": 1,
                    "original_text": "This Agreement shall be governed by the laws of California.",
                    "replacement_text": "This Agreement shall be governed by the laws of England and Wales.",
                },
                {
                    "action": "insert_after_paragraph",
                    "source_index": 2,
                    "insert_text": "New required clause.",
                },
                {
                    "action": "delete_paragraph",
                    "source_index": 3,
                    "original_text": "The Recipient must not circumvent the Company.",
                },
            ],
        }

        redlined_docx = build_source_redline_docx(source_docx, review_result)

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
        self.assertGreaterEqual(len(paragraph_mark_revisions(document_root, "del")), 1)

    def test_review_report_docx_marks_insert_after_paragraph_redlines(self):
        result = review_nda("The parties will discuss a possible transaction.")

        _settings_root, document_root, _document_xml = docx_xml_roots(build_review_report_docx(result))

        inserted_text = tracked_inserted_text(document_root)
        self.assertTrue(any("This Agreement shall be governed by the laws of England and Wales." in text for text in inserted_text))
        self.assertGreaterEqual(len(paragraph_mark_revisions(document_root, "ins")), 1)

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
        self.assertGreaterEqual(len(paragraph_mark_revisions(document_root, "ins")), 2)


if __name__ == "__main__":
    unittest.main()
