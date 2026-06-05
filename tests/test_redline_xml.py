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


class InlineSpacingTests(unittest.TestCase):
    """The diff-driven inline spacing must not pad quotes/brackets with spurious
    spaces. Regression for the baseline DOCX-export red where
    '"Confidential Information"' exported as '" Confidential Information "' and
    failed docx_health's content-coverage check."""

    def _rendered_insert_text(self, text):
        # A pure insertion exercises the token-join spacing on every token.
        paragraph_xml, _ = redline_xml._tracked_replace_paragraph("", text, 1)
        paragraph = _parse(paragraph_xml)
        return _text(paragraph, "t")

    def test_replace_does_not_pad_quotes_with_spurious_spaces(self):
        self.assertEqual(
            self._rendered_insert_text('"Confidential Information" means data.'),
            '"Confidential Information" means data.',
        )

    def test_replace_preserves_spacing_around_a_mid_sentence_quote(self):
        # The opening quote still gets its real leading space; the closing quote
        # still hugs the quoted word -- inter-word quoting is not over-corrected.
        self.assertEqual(
            self._rendered_insert_text('she said "hello" to me'),
            'she said "hello" to me',
        )

    def test_replace_does_not_pad_curly_quotes(self):
        self.assertEqual(
            self._rendered_insert_text("the term “Discloser” applies"),
            "the term “Discloser” applies",
        )

    def test_existing_bracket_currency_and_punctuation_spacing_unchanged(self):
        for text in (
            "governed by laws (England and Wales) here",
            "the cost is $5 today",
            "word, next; more: stuff.",
            "Each party's Confidential Information",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._rendered_insert_text(text), text)

    def test_needs_inline_space_hugs_opening_and_closing_quotes(self):
        # Closing quote: no space before it.
        self.assertFalse(redline_xml._needs_inline_space("Information", '"'))
        # Opening quote: no space after it.
        self.assertFalse(redline_xml._needs_inline_space('"', "Confidential"))
        # Two separate words still get a space.
        self.assertTrue(redline_xml._needs_inline_space("Confidential", "Information"))


if __name__ == "__main__":
    unittest.main()
