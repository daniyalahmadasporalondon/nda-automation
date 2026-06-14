import unittest

from nda_automation.heading_detection import (
    block_clause_number,
    continuation_is_heading,
    parse_leading_number,
)


class ParseLeadingNumberTests(unittest.TestCase):
    def test_parses_explicit_separator_marker(self):
        leading = parse_leading_number("5. Confidentiality.")
        self.assertIsNotNone(leading)
        self.assertEqual(leading.number, "5")
        self.assertTrue(leading.has_explicit_separator)

    def test_whitespace_only_marker_is_not_explicit(self):
        leading = parse_leading_number("5 years following the date of disclosure.")
        self.assertIsNotNone(leading)
        self.assertEqual(leading.number, "5")
        self.assertFalse(leading.has_explicit_separator)

    def test_no_leading_number(self):
        self.assertIsNone(parse_leading_number("Confidentiality Obligations"))

    def test_strips_trailing_dot_from_number(self):
        self.assertEqual(parse_leading_number("10.1 Return").number, "10.1")


class BlockClauseNumberTests(unittest.TestCase):
    def test_text_literal_wins_over_mismatched_autonumber(self):
        # Repro 4 seam: parent autonumber "1", text "2. Second." -> "2".
        self.assertEqual(block_clause_number("2. Second.", "1"), "2")

    def test_metadata_used_when_text_has_no_literal_prefix(self):
        # Dominant DOCX case: numbered paragraph whose run carries no literal number.
        self.assertEqual(block_clause_number("Confidentiality Obligations", "5"), "5")

    def test_whitespace_only_text_number_does_not_override_metadata(self):
        self.assertEqual(block_clause_number("5 years from disclosure", "3"), "3")

    def test_falls_back_to_literal_when_no_metadata(self):
        self.assertEqual(block_clause_number("5 CONFIDENTIALITY", ""), "5")

    def test_empty_when_nothing_present(self):
        self.assertEqual(block_clause_number("Confidentiality", ""), "")


class ContinuationIsHeadingTests(unittest.TestCase):
    def test_same_number_continuation_is_not_a_heading(self):
        self.assertFalse(continuation_is_heading("5 business days notice", "5"))

    def test_capitalized_same_number_continuation_is_not_a_heading(self):
        # The round-2 killer: capitalization of following words is irrelevant.
        self.assertFalse(continuation_is_heading("5 Business Days notice is required.", "5"))
        self.assertFalse(continuation_is_heading("4 Weeks notice shall be given.", "4"))

    def test_no_leading_number_continuation_is_not_a_heading(self):
        self.assertFalse(continuation_is_heading("which the parties shall observe.", "5"))

    def test_whitespace_marker_continuation_is_not_a_heading(self):
        self.assertFalse(continuation_is_heading("5 years following disclosure.", "5"))

    def test_distinct_explicit_numbered_continuation_is_a_heading(self):
        # Repro 4: "3. Third." under a parent clause "1" is a genuinely new clause.
        self.assertTrue(continuation_is_heading("3. Third.", "1"))


if __name__ == "__main__":
    unittest.main()
