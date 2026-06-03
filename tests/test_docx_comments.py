import unittest

from nda_automation import docx_comments
from nda_automation.docx_xml import _w_tag, _word_paragraph_from_xml


def _paragraph(*run_texts):
    runs = "".join(f"<w:r><w:t>{text}</w:t></w:r>" for text in run_texts)
    return _word_paragraph_from_xml(f"<w:p>{runs}</w:p>")


def _text_in_comment_range(paragraph, comment_id):
    collecting = False
    parts = []
    for child in list(paragraph):
        if child.tag == _w_tag("commentRangeStart") and child.get(_w_tag("id")) == comment_id:
            collecting = True
            continue
        if child.tag == _w_tag("commentRangeEnd") and child.get(_w_tag("id")) == comment_id:
            collecting = False
            continue
        if collecting:
            run_text = docx_comments._direct_run_text(child)
            if run_text is not None:
                parts.append(run_text)
    return "".join(parts)


def _has(paragraph, tag, comment_id):
    return any(
        child.tag == _w_tag(tag) and child.get(_w_tag("id")) == comment_id
        for child in paragraph.iter()
    )


class CommentSelectionRangeTests(unittest.TestCase):
    def test_explicit_indices_with_matching_text(self):
        paragraph = _paragraph("Hello brave world")
        comment = {"selection_start": 6, "selection_end": 11, "selected_text": "brave"}
        self.assertEqual(docx_comments._comment_selection_range(paragraph, comment), (6, 11))

    def test_explicit_indices_without_selected_text(self):
        paragraph = _paragraph("Hello brave world")
        comment = {"selection_start": 0, "selection_end": 5, "selected_text": ""}
        self.assertEqual(docx_comments._comment_selection_range(paragraph, comment), (0, 5))

    def test_falls_back_to_search_when_indices_are_wrong(self):
        paragraph = _paragraph("Hello brave world")
        comment = {"selection_start": 999, "selection_end": 1000, "selected_text": "brave"}
        self.assertEqual(docx_comments._comment_selection_range(paragraph, comment), (6, 11))

    def test_returns_none_when_text_absent(self):
        paragraph = _paragraph("Hello brave world")
        comment = {"selection_start": -1, "selection_end": -1, "selected_text": "absent"}
        self.assertIsNone(docx_comments._comment_selection_range(paragraph, comment))

    def test_returns_none_for_empty_paragraph(self):
        self.assertIsNone(docx_comments._comment_selection_range(_paragraph(), {"selected_text": "x"}))


class CommentAnchorTextSelectionTests(unittest.TestCase):
    def test_anchors_selection_and_preserves_run_text(self):
        paragraph = _paragraph("Hello brave world")
        comment = {"selection_start": 6, "selection_end": 11, "selected_text": "brave"}

        applied = docx_comments._apply_comment_anchor_to_text_selection(paragraph, "7", comment)

        self.assertTrue(applied)
        self.assertTrue(_has(paragraph, "commentRangeStart", "7"))
        self.assertTrue(_has(paragraph, "commentRangeEnd", "7"))
        # The visible run text is unchanged...
        self.assertEqual(docx_comments._paragraph_direct_text(paragraph), "Hello brave world")
        # ...and the comment range wraps exactly the selected span.
        self.assertEqual(_text_in_comment_range(paragraph, "7"), "brave")

    def test_preserves_text_when_selection_spans_into_a_run(self):
        paragraph = _paragraph("Hello ", "brave ", "world")  # split across three runs
        comment = {"selection_start": 6, "selection_end": 11, "selected_text": "brave"}

        applied = docx_comments._apply_comment_anchor_to_text_selection(paragraph, "3", comment)

        self.assertTrue(applied)
        self.assertEqual(docx_comments._paragraph_direct_text(paragraph), "Hello brave world")
        self.assertEqual(_text_in_comment_range(paragraph, "3"), "brave")

    def test_fallback_anchoring_when_indices_wrong_but_text_present(self):
        paragraph = _paragraph("Hello brave world")
        comment = {"selection_start": 100, "selection_end": 200, "selected_text": "world"}

        applied = docx_comments._apply_comment_anchor_to_text_selection(paragraph, "9", comment)

        self.assertTrue(applied)
        self.assertEqual(docx_comments._paragraph_direct_text(paragraph), "Hello brave world")
        self.assertEqual(_text_in_comment_range(paragraph, "9"), "world")

    def test_returns_false_when_selected_text_absent(self):
        paragraph = _paragraph("Hello brave world")
        comment = {"selection_start": -1, "selection_end": -1, "selected_text": "absent"}

        applied = docx_comments._apply_comment_anchor_to_text_selection(paragraph, "1", comment)

        self.assertFalse(applied)
        self.assertFalse(_has(paragraph, "commentRangeStart", "1"))
        self.assertEqual(docx_comments._paragraph_direct_text(paragraph), "Hello brave world")


if __name__ == "__main__":
    unittest.main()
