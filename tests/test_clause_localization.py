import unittest

from nda_automation.checker import load_playbook
from nda_automation.clause_localization import build_clause_localization
from nda_automation.contract_structure import build_contract_structure
from nda_automation.review_document import split_document_paragraphs


def _structure(source: str):
    paragraphs = split_document_paragraphs(source)
    return build_contract_structure(paragraphs)


class ClauseLocalizationTests(unittest.TestCase):
    def test_matches_clause_to_heading_by_topic(self):
        structure = _structure(
            "\n\n".join([
                "1. Confidentiality. Each party shall keep Confidential Information secret.",
                "2. Governing Law. This Agreement shall be governed by the laws of California.",
                "3. Term. The obligations survive for five years.",
            ])
        )
        localization = build_clause_localization(load_playbook(), structure)

        self.assertIn("governing_law", localization)
        self.assertTrue(localization["governing_law"]["suggested_section_ids"])
        self.assertIn("term_and_survival", localization)

    def test_returns_empty_without_structure(self):
        self.assertEqual(build_clause_localization(load_playbook(), None), {})
        self.assertEqual(build_clause_localization(load_playbook(), {"sections": []}), {})

    def test_unmatched_clause_gets_no_hint(self):
        # A document with no recognizable headings yields no localization at all
        # (safe default: no hint rather than a wrong hint).
        structure = _structure("Some flat prose with no clause headings whatsoever.")
        localization = build_clause_localization(load_playbook(), structure)
        # Either empty, or only clauses whose cue genuinely appears -- never a hint for
        # a topic the document does not name.
        self.assertNotIn("governing_law", localization)

    def test_hint_only_suggests_ids_present_in_structure(self):
        structure = _structure(
            "\n\n".join([
                "1. Governing Law. Laws of California apply.",
            ])
        )
        localization = build_clause_localization(load_playbook(), structure)
        valid_ids = {str(section.get("id")) for section in structure["sections"]}
        for hint in localization.values():
            for section_id in hint["suggested_section_ids"]:
                self.assertIn(section_id, valid_ids)


if __name__ == "__main__":
    unittest.main()
