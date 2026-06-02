import unittest

from nda_automation.docx_xml import UnsafeDocxXmlError, parse_docx_xml, reject_unsafe_docx_xml


class DocxXmlTests(unittest.TestCase):
    def test_rejects_utf16_bom_dtd_entity_declarations(self):
        data = unsafe_xml_part("UTF-16").encode("utf-16")

        with self.assertRaisesRegex(UnsafeDocxXmlError, "word/document.xml"):
            reject_unsafe_docx_xml(data, part_name="word/document.xml")

    def test_rejects_utf16le_dtd_entity_declarations_without_bom(self):
        data = unsafe_xml_part("UTF-16LE").encode("utf-16-le")

        with self.assertRaisesRegex(UnsafeDocxXmlError, "word/header1.xml"):
            reject_unsafe_docx_xml(data, part_name="word/header1.xml")

    def test_rejects_utf32be_dtd_entity_declarations_without_bom(self):
        data = unsafe_xml_part("UTF-32BE").encode("utf-32-be")

        with self.assertRaisesRegex(UnsafeDocxXmlError, "word/footer1.xml"):
            reject_unsafe_docx_xml(data, part_name="word/footer1.xml")

    def test_parses_safe_utf16_xml_parts(self):
        data = safe_xml_part("UTF-16").encode("utf-16")

        root = parse_docx_xml(data, part_name="word/document.xml")

        self.assertEqual(root.tag, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document")


def unsafe_xml_part(encoding):
    return f"""<?xml version="1.0" encoding="{encoding}"?>
<!DOCTYPE w:document [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body>
</w:document>"""


def safe_xml_part(encoding):
    return f"""<?xml version="1.0" encoding="{encoding}"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Safe body text.</w:t></w:r></w:p></w:body>
</w:document>"""


if __name__ == "__main__":
    unittest.main()
