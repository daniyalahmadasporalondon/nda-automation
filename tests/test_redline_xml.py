import unittest

from nda_automation import redline_xml
from nda_automation.docx_xml import _w_tag, _word_paragraph_from_xml


def _parse(paragraph_xml):
    return _word_paragraph_from_xml(paragraph_xml)


def _text(paragraph, tag):
    return "".join(node.text or "" for node in paragraph.iter(_w_tag(tag)))


class RevisionAttrsTests(unittest.TestCase):
    def test_revision_attrs_shape(self):
        attrs = redline_xml._revision_attrs(12)
        self.assertIn('w:id="12"', attrs)
        self.assertIn('w:author="nda-automation"', attrs)
        self.assertRegex(attrs, r'w:date="\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"')


class TrackedDeleteTests(unittest.TestCase):
    def test_delete_wraps_text_in_del_using_delText(self):
        paragraph = _parse(redline_xml._tracked_delete_paragraph("Remove me", 5))
        dels = paragraph.findall(f".//{_w_tag('del')}")
        self.assertEqual(len(dels), 1)
        self.assertEqual(dels[0].get(_w_tag("id")), "5")
        self.assertEqual(_text(paragraph, "delText"), "Remove me")
        # Deleted text must use w:delText, never w:t.
        self.assertEqual(paragraph.findall(f".//{_w_tag('t')}"), [])


class TrackedInsertTests(unittest.TestCase):
    def test_insert_wraps_text_in_ins_using_t(self):
        xml_list = redline_xml._tracked_insert_paragraphs("Add this", 9)
        self.assertEqual(len(xml_list), 1)
        paragraph = _parse(xml_list[0])
        ins = paragraph.findall(f".//{_w_tag('ins')}")
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0].get(_w_tag("id")), "9")
        self.assertEqual(_text(paragraph, "t"), "Add this")

    def test_insert_splits_on_blank_lines_into_multiple_paragraphs(self):
        xml_list = redline_xml._tracked_insert_paragraphs("First block\n\nSecond block", 1)
        self.assertEqual(len(xml_list), 2)


class TrackedReplaceTests(unittest.TestCase):
    def test_replace_emits_delete_and_insert_and_advances_revision(self):
        paragraph_xml, next_id = redline_xml._tracked_replace_paragraph("old wording", "new wording", 3)
        paragraph = _parse(paragraph_xml)

        self.assertTrue(paragraph.findall(f".//{_w_tag('del')}"), "expected a tracked deletion")
        self.assertTrue(paragraph.findall(f".//{_w_tag('ins')}"), "expected a tracked insertion")
        self.assertGreater(next_id, 3)

        inserted = "".join(
            node.text or ""
            for ins in paragraph.findall(f".//{_w_tag('ins')}")
            for node in ins.iter(_w_tag("t"))
        )
        deleted = "".join(
            node.text or ""
            for delete in paragraph.findall(f".//{_w_tag('del')}")
            for node in delete.iter(_w_tag("delText"))
        )
        self.assertIn("new", inserted)
        self.assertIn("old", deleted)


if __name__ == "__main__":
    unittest.main()
