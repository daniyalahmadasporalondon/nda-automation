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

    def test_detects_hybrid_letter_suffix_identifiers(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "Clause 1: General",
            "General obligations.",
            "Clause 1A Supplemental Confidentiality",
            "Supplemental confidentiality obligations.",
            "10. Materials",
            "This section introduces materials handling.",
            "10.1 Return of Materials",
            "The Receiving Party must return materials on request.",
            "10.1A Certificate of Destruction",
            "The Receiving Party must certify destruction.",
            "Section 10b Data Processing",
            "Data processing terms.",
        ]))

        structure = build_contract_structure(paragraphs)
        sections = structure["sections"]
        sections_by_label = {section["label"]: section for section in sections}

        self.assertEqual(
            [section["label"] for section in sections],
            ["Clause 1", "Clause 1A", "10", "10.1", "10.1A", "Section 10b"],
        )
        self.assertEqual(sections_by_label["Clause 1A"]["parent_id"], sections_by_label["Clause 1"]["id"])
        self.assertEqual(sections_by_label["10.1"]["parent_id"], sections_by_label["10"]["id"])
        self.assertEqual(sections_by_label["10.1A"]["parent_id"], sections_by_label["10.1"]["id"])
        self.assertEqual(sections_by_label["Section 10b"]["parent_id"], sections_by_label["10"]["id"])
        self.assertEqual(sections_by_label["Clause 1A"]["level"], 2)
        self.assertEqual(sections_by_label["10.1A"]["level"], 3)
        self.assertIn(
            {"key": "clause:1a", "section_id": sections_by_label["Clause 1A"]["id"], "label": "Clause 1A"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "number:10.1a", "section_id": sections_by_label["10.1A"]["id"], "label": "10.1A"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "section:10b", "section_id": sections_by_label["Section 10b"]["id"], "label": "Section 10b"},
            structure["aliases"],
        )

    def test_detects_roman_numeral_identifiers(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "Article II Confidentiality",
            "The Receiving Party must protect Confidential Information.",
            "Section II.A Permitted Disclosures",
            "Disclosures are permitted only for representatives.",
            "Clause IV - Term",
            "The obligations survive for three years.",
            "Article IX Governing Law",
            "This Agreement is governed by English law.",
        ]))

        structure = build_contract_structure(paragraphs)
        sections = structure["sections"]
        sections_by_label = {section["label"]: section for section in sections}

        self.assertEqual(
            [section["label"] for section in sections],
            ["Article II", "Section II.A", "Clause IV", "Article IX"],
        )
        self.assertEqual(sections_by_label["Section II.A"]["parent_id"], sections_by_label["Article II"]["id"])
        self.assertEqual(sections_by_label["Article II"]["level"], 1)
        self.assertEqual(sections_by_label["Section II.A"]["level"], 2)
        self.assertIn(
            {"key": "article:ii", "section_id": sections_by_label["Article II"]["id"], "label": "Article II"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "section:ii.a", "section_id": sections_by_label["Section II.A"]["id"], "label": "Section II.A"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "clause:iv", "section_id": sections_by_label["Clause IV"]["id"], "label": "Clause IV"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "article:ix", "section_id": sections_by_label["Article IX"]["id"], "label": "Article IX"},
            structure["aliases"],
        )

    def test_exposes_resolver_ready_reference_index(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Clause 1: Definitions",
            "Confidential Information means non-public information.",
            "Article II Confidentiality",
            "Each party shall protect Confidential Information.",
            "Section II.A Permitted Disclosures",
            "Disclosures are limited to representatives.",
        ]))

        structure = build_contract_structure(paragraphs)
        reference_index = structure["reference_index"]
        section_by_label = {section["label"]: section for section in structure["sections"]}
        preamble = section_by_label["Preamble"]
        clause_1 = section_by_label["Clause 1"]
        article_ii = section_by_label["Article II"]
        section_ii_a = section_by_label["Section II.A"]

        self.assertEqual(reference_index["version"], 1)
        self.assertEqual(reference_index["section_ids"], [section["id"] for section in structure["sections"]])
        self.assertEqual(reference_index["alias_to_section_id"]["clause:1"], clause_1["id"])
        self.assertEqual(reference_index["alias_to_section_id"]["article:ii"], article_ii["id"])
        self.assertEqual(reference_index["alias_to_section_id"]["section:ii.a"], section_ii_a["id"])
        self.assertEqual(reference_index["paragraph_to_section_id"]["p3"], clause_1["id"])
        self.assertEqual(reference_index["paragraph_to_section_id"]["p7"], section_ii_a["id"])
        self.assertIsNone(preamble["number"])
        self.assertIsNone(preamble["parent_id"])
        self.assertIsNone(reference_index["sections_by_id"][preamble["id"]]["number"])
        self.assertIsNone(reference_index["sections_by_id"][preamble["id"]]["parent_id"])
        self.assertEqual(reference_index["sections_by_id"][section_ii_a["id"]], {
            "id": section_ii_a["id"],
            "kind": "section",
            "number": "II.A",
            "label": "Section II.A",
            "heading": "Permitted Disclosures",
            "level": 2,
            "paragraph_ids": ["p6", "p7"],
            "start_index": 6,
            "end_index": 7,
            "parent_id": article_ii["id"],
        })

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
