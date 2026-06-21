import unittest
import xml.etree.ElementTree as ET

from nda_automation import redline_xml
from nda_automation.docx_xml import _w_tag, _word_paragraph_from_xml

W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


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


class RunVertAlignTests(unittest.TestCase):
    """The serializer applies a run-scope ``vertAlign`` op (Superscript/Subscript)
    as ``<w:vertAlign w:val="superscript|subscript">`` and clears it on unset."""

    PARAGRAPH_XML = (
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:r><w:t>x2 footnote</w:t></w:r></w:p>"
    )

    def test_superscript_op_emits_vert_align_superscript(self):
        source_p = ET.fromstring(self.PARAGRAPH_XML)
        # Superscript the "2" (offset 1..2 in "x2 footnote").
        rebuilt, next_rev = redline_xml._apply_tracked_run_format(
            source_p,
            [
                {
                    "scope": "run",
                    "property": "vertAlign",
                    "start": 1,
                    "end": 2,
                    "from": "",
                    "to": "superscript",
                }
            ],
            3,
        )
        self.assertEqual(next_rev, 4)  # one revision consumed
        # Text byte-identical, no ins/del — it's a pure formatting change.
        self.assertEqual(
            "".join(n.text or "" for n in rebuilt.findall(".//w:t", W_NS)), "x2 footnote"
        )
        self.assertEqual(rebuilt.findall(".//w:ins", W_NS), [])
        self.assertEqual(rebuilt.findall(".//w:del", W_NS), [])
        # Exactly the "2" run carries <w:vertAlign w:val="superscript"> + tracked change.
        super_runs = [
            "".join(n.text or "" for n in run.findall("w:t", W_NS))
            for run in rebuilt.findall("w:r", W_NS)
            if (run.find("w:rPr/w:vertAlign", W_NS) is not None
                and run.find("w:rPr/w:vertAlign", W_NS).get(_w_tag("val")) == "superscript")
        ]
        self.assertEqual(super_runs, ["2"])
        covered = next(
            run for run in rebuilt.findall("w:r", W_NS)
            if "".join(n.text or "" for n in run.findall("w:t", W_NS)) == "2"
        )
        self.assertIsNotNone(covered.find("w:rPr/w:rPrChange", W_NS))

    def test_subscript_op_emits_vert_align_subscript(self):
        source_p = ET.fromstring(self.PARAGRAPH_XML)
        rebuilt, _ = redline_xml._apply_tracked_run_format(
            source_p,
            [{"scope": "run", "property": "vertAlign", "start": 1, "end": 2, "from": "", "to": "subscript"}],
            1,
        )
        vert = rebuilt.find(".//w:vertAlign", W_NS)
        self.assertIsNotNone(vert)
        self.assertEqual(vert.get(_w_tag("val")), "subscript")

    def test_unknown_vert_align_value_clears_the_override(self):
        # A run that ALREADY carries vertAlign, with an op turning it OFF (to="" /
        # "baseline"), drops the <w:vertAlign> element so the run returns to baseline.
        source_p = ET.fromstring(
            '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>x2 footnote</w:t></w:r></w:p>'
        )
        rebuilt, _ = redline_xml._apply_tracked_run_format(
            source_p,
            [{"scope": "run", "property": "vertAlign", "start": 1, "end": 2, "from": "superscript", "to": ""}],
            1,
        )
        # The covered "2" run carries no current <w:vertAlign> (only inside rPrChange's
        # from-state record). Assert no live vertAlign sits outside an rPrChange.
        covered = next(
            run for run in rebuilt.findall("w:r", W_NS)
            if "".join(n.text or "" for n in run.findall("w:t", W_NS)) == "2"
        )
        rpr = covered.find("w:rPr", W_NS)
        # The CURRENT rPr's direct vertAlign children (excluding the rPrChange subtree).
        live_vert = [child for child in rpr if child.tag == _w_tag("vertAlign")]
        self.assertEqual(live_vert, [], "vertAlign should be cleared from the current rPr")

    def test_set_run_vert_align_helper_sorts_before_rpr_change(self):
        # _RPR_CHILD_ORDER places vertAlign before rPrChange, so _sort_rpr_children
        # keeps a vertAlign + rPrChange rPr schema-valid (vertAlign must precede it).
        rpr = ET.Element(_w_tag("rPr"))
        redline_xml._set_run_vert_align(rpr, "superscript")
        rpr.append(ET.Element(_w_tag("rPrChange")))
        redline_xml._sort_rpr_children(rpr)
        tags = [child.tag.split("}", 1)[-1] for child in rpr]
        self.assertLess(tags.index("vertAlign"), tags.index("rPrChange"))

    def test_formatted_run_carries_vert_align(self):
        # The replacement-run insert path (_formatted_run) emits vertAlign so a clean
        # export of an edited paragraph preserves Superscript/Subscript.
        xml = redline_xml._formatted_run({"text": "footnote", "vertAlign": "superscript"})
        run = ET.fromstring(
            '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"{xml}</w:p>"
        )
        vert = run.find(".//w:vertAlign", W_NS)
        self.assertIsNotNone(vert)
        self.assertEqual(vert.get(_w_tag("val")), "superscript")

    def test_export_sanitizer_canonicalises_vert_align_op_then_applies(self):
        # End-to-end FE->backend: the FE emits a run-scope vertAlign op; the export
        # sanitiser lowercases the property for its whitelist check but must emit the
        # canonical camelCase token the applier matches. Then the applier renders it.
        from nda_automation import export_service

        cleaned = export_service._clean_format_ops(
            [
                {
                    "scope": "run",
                    "property": "vertAlign",
                    "start": 1,
                    "end": 2,
                    "from": "",
                    "to": "superscript",
                }
            ],
            original_text="x2 footnote",
        )
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["property"], "vertAlign")  # canonical camelCase
        self.assertEqual(cleaned[0]["to"], "superscript")
        # The cleaned op drives the applier unchanged.
        source_p = ET.fromstring(self.PARAGRAPH_XML)
        rebuilt, _ = redline_xml._apply_tracked_run_format(source_p, cleaned, 1)
        vert = rebuilt.find(".//w:vertAlign", W_NS)
        self.assertIsNotNone(vert, "sanitised vertAlign op did not reach the applier")
        self.assertEqual(vert.get(_w_tag("val")), "superscript")

    def test_export_sanitizer_drops_invalid_vert_align_value(self):
        from nda_automation import export_service

        cleaned = export_service._clean_format_ops(
            [{"scope": "run", "property": "vertAlign", "start": 0, "end": 1, "to": "bogus"}],
            original_text="x2",
        )
        self.assertEqual(cleaned, [])


if __name__ == "__main__":
    unittest.main()
