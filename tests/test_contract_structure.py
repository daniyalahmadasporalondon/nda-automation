import unittest

from nda_automation.checker import review_nda
from nda_automation.contract_structure import build_contract_structure
from nda_automation.matter_view import review_result_with_structure
from nda_automation.review_document import split_document_paragraphs


class ContractStructureTests(unittest.TestCase):
    def test_builds_clause_and_article_map(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Clause 1: Definitions",
            "Confidential Information means non-public information.",
            "Clause 2 - Confidentiality",
            "The Receiving Party shall not disclose Confidential Information.",
            "Article 3 Term and Termination",
            "The obligations set out at clauses 1 and 2 survive for three years.",
        ]))

        structure = build_contract_structure(paragraphs)
        labels = [section["label"] for section in structure["sections"]]

        self.assertEqual(structure["version"], 1)
        self.assertEqual(labels, ["Preamble", "Clause 1", "Clause 2", "Article 3"])
        self.assertEqual(structure["sections"][1]["paragraph_ids"], ["p2", "p3"])
        self.assertEqual(structure["sections"][2]["paragraph_ids"], ["p4", "p5"])
        self.assertIn(
            {"key": "clause:1", "section_id": "section-2", "label": "Clause 1"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "article:3", "section_id": "section-4", "label": "Article 3"},
            structure["aliases"],
        )
        self.assertEqual(structure["stats"]["mapped_paragraph_count"], 7)
        self.assertEqual(structure["stats"]["unmapped_paragraph_count"], 0)

    def test_detects_uppercase_prefix_heading_with_same_paragraph_text(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "TERM AND TERMINATION: This Agreement is effective from the date hereof.",
            "The confidentiality obligations survive for three years.",
            "NO LICENSE: The Disclosing Party retains all intellectual property rights.",
        ]))

        structure = build_contract_structure(paragraphs)
        sections = structure["sections"]

        self.assertEqual([section["heading"] for section in sections], ["TERM AND TERMINATION", "NO LICENSE"])
        self.assertEqual(sections[0]["kind"], "heading")
        self.assertEqual(sections[0]["confidence"], "medium")
        self.assertEqual(sections[0]["paragraph_ids"], ["p1", "p2"])
        self.assertIn(
            {"key": "heading:term and termination", "section_id": "section-1", "label": "TERM AND TERMINATION"},
            structure["aliases"],
        )

    def test_detects_nested_numbered_sections_and_parent(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "10. General",
            "This section introduces general provisions.",
            "10.1 Return of Materials",
            "The Receiving Party must return materials on request.",
            "10.2 Confidentiality",
            "The Receiving Party must keep information confidential.",
        ]))

        structure = build_contract_structure(paragraphs)
        sections = structure["sections"]

        self.assertEqual([section["label"] for section in sections], ["10", "10.1", "10.2"])
        self.assertEqual(sections[1]["parent_id"], sections[0]["id"])
        self.assertEqual(sections[2]["parent_id"], sections[0]["id"])
        self.assertEqual(sections[1]["level"], 2)
        self.assertEqual(sections[1]["paragraph_ids"], ["p3", "p4"])

    def test_review_result_includes_contract_structure(self):
        result = review_nda("\n\n".join([
            "1. Confidentiality",
            "Each party shall keep the other party's Confidential Information confidential.",
        ]))

        self.assertIn("contract_structure", result)
        self.assertEqual(result["contract_structure"]["version"], 1)
        self.assertEqual(result["contract_structure"]["sections"][0]["label"], "1")

    def test_legacy_matter_review_result_gets_structure_backfill(self):
        legacy_result = {
            "paragraphs": split_document_paragraphs("\n\n".join([
                "MUTUAL NON-DISCLOSURE AGREEMENT",
                "Clause 1: Definitions",
                "Confidential Information means non-public information.",
            ])),
        }

        enriched = review_result_with_structure(legacy_result)

        self.assertNotIn("contract_structure", legacy_result)
        self.assertEqual(enriched["contract_structure"]["sections"][1]["label"], "Clause 1")
        self.assertEqual(enriched["contract_structure"]["sections"][1]["paragraph_ids"], ["p2", "p3"])

    def test_legacy_matter_review_result_can_backfill_from_text(self):
        legacy_result = {}

        enriched = review_result_with_structure(
            legacy_result,
            "Clause 1: Definitions\n\nConfidential Information means non-public information.",
        )

        self.assertIn("paragraphs", enriched)
        self.assertEqual(enriched["contract_structure"]["sections"][0]["label"], "Clause 1")


if __name__ == "__main__":
    unittest.main()
