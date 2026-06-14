import unittest

from nda_automation.checker import review_nda
from nda_automation.contract_structure import build_contract_structure
from nda_automation.matter_view import review_result_with_structure
from nda_automation.reference_resolver import resolve_document_references
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

        self.assertEqual(structure["version"], 2)
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

    def test_detects_parenthetical_and_outline_heading_identifiers(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "1. General",
            "General text.",
            "1(a) Confidentiality",
            "Confidential text.",
            "1(a)(i) Permitted Recipients",
            "Recipient text.",
            "10. Boilerplate",
            "Boilerplate text.",
            "Section 10(b) Data Processing",
            "Data terms.",
            "A. Definitions",
            "Definition text.",
            "(B) Return of Materials",
            "Return text.",
            "IV. Term",
            "Term text.",
            "Section 10(b) and Section A apply. Clause 1(a) also applies.",
        ]))

        structure = build_contract_structure(paragraphs)
        sections = structure["sections"]
        sections_by_label = {section["label"]: section for section in sections}

        self.assertEqual(
            [section["label"] for section in sections],
            ["Preamble", "1", "1(a)", "1(a)(i)", "10", "Section 10(b)", "A", "(B)", "IV"],
        )
        self.assertEqual(sections_by_label["1(a)"]["parent_id"], sections_by_label["1"]["id"])
        self.assertEqual(sections_by_label["1(a)(i)"]["parent_id"], sections_by_label["1(a)"]["id"])
        self.assertEqual(sections_by_label["Section 10(b)"]["parent_id"], sections_by_label["10"]["id"])
        self.assertEqual(sections_by_label["1(a)"]["level"], 2)
        self.assertEqual(sections_by_label["1(a)(i)"]["level"], 3)
        self.assertEqual(sections_by_label["Section 10(b)"]["level"], 2)
        self.assertEqual(sections_by_label["IV"]["paragraph_ids"], ["p16", "p17", "p18"])
        self.assertIn(
            {"key": "number:1(a)", "section_id": sections_by_label["1(a)"]["id"], "label": "1(a)"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "number:1(a)(i)", "section_id": sections_by_label["1(a)(i)"]["id"], "label": "1(a)(i)"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "section:10(b)", "section_id": sections_by_label["Section 10(b)"]["id"], "label": "Section 10(b)"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "number:a", "section_id": sections_by_label["A"]["id"], "label": "A"},
            structure["aliases"],
        )
        self.assertIn(
            {"key": "number:(b)", "section_id": sections_by_label["(B)"]["id"], "label": "(B)"},
            structure["aliases"],
        )

    def test_does_not_treat_reference_sentences_as_explicit_headings(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "Clause 1: Definitions",
            "Definitions text.",
            "Clause 2 - Confidentiality",
            "Clause 1 survives this Agreement.",
            "Section 10(b) and Section A apply to the parties.",
        ]))

        structure = build_contract_structure(paragraphs)

        self.assertEqual([section["label"] for section in structure["sections"]], ["Clause 1", "Clause 2"])
        self.assertEqual(structure["sections"][1]["paragraph_ids"], ["p3", "p4", "p5"])

    def test_does_not_treat_outline_marked_prose_as_headings(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "A party shall keep Confidential Information confidential.",
            "(a) the Receiving Party shall protect Confidential Information.",
            "I agree that this paragraph is not a heading.",
        ]))

        structure = build_contract_structure(paragraphs)

        self.assertEqual([section["label"] for section in structure["sections"]], ["Preamble"])
        self.assertEqual(structure["sections"][0]["paragraph_ids"], ["p1", "p2", "p3"])

    def test_captures_run_in_numbered_clause_without_colon(self):
        # A flat-DOCX / PDF-reconstructed clause: a real clause number followed by a
        # long colon-less run-in sentence. The old heading gate (<=120 chars OR ':' in
        # first 90) silently DROPPED this; it must now be captured as a clause with the
        # correct paragraph boundaries.
        run_in = (
            "5. The Receiving Party shall not disclose Confidential Information to any "
            "third party without the prior written consent of the Disclosing Party and "
            "shall use it solely for the Purpose of evaluating the proposed transaction."
        )
        paragraphs = split_document_paragraphs("\n\n".join([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            run_in,
            "This obligation survives termination of the Agreement.",
            "6. Term",
            "This Agreement remains in force for three years.",
        ]))

        structure = build_contract_structure(paragraphs)
        sections = structure["sections"]
        sections_by_label = {section["label"]: section for section in sections}

        self.assertEqual([section["label"] for section in sections], ["Preamble", "5", "6"])
        clause_5 = sections_by_label["5"]
        self.assertEqual(clause_5["kind"], "numbered")
        self.assertEqual(clause_5["level"], 1)
        # Boundaries: the run-in clause owns its own paragraph plus the trailing body
        # sentence, ending exactly where clause 6 begins.
        self.assertEqual(clause_5["paragraph_ids"], ["p2", "p3"])
        self.assertEqual(clause_5["start_index"], 2)
        self.assertEqual(clause_5["end_index"], 3)
        self.assertEqual(sections_by_label["6"]["paragraph_ids"], ["p4", "p5"])
        self.assertEqual(structure["stats"]["unmapped_paragraph_count"], 0)
        self.assertIn(
            {"key": "number:5", "section_id": clause_5["id"], "label": "5"},
            structure["aliases"],
        )

    def test_does_not_promote_numbered_body_prose_run_in(self):
        # Body prose that merely begins with a number must NOT be promoted to a clause.
        # "5 years ..." separates the number from the next word with whitespace only
        # (not a deliberate "5." marker) and continues a sentence (lowercase), so the
        # run-in rescue must reject it.
        paragraphs = split_document_paragraphs("\n\n".join([
            "Clause 1: Definitions",
            "Definitions text.",
            "5 years from the date of disclosure the obligations shall cease and the "
            "Receiving Party is released from all duties of confidentiality forever.",
        ]))

        structure = build_contract_structure(paragraphs)

        self.assertEqual([section["label"] for section in structure["sections"]], ["Clause 1"])
        self.assertEqual(structure["sections"][0]["paragraph_ids"], ["p1", "p2", "p3"])

    def test_does_not_over_promote_lone_parenthetical_bullet(self):
        # A stray sub-list bullet "(ii) Confidentiality" in otherwise unstructured prose
        # must not become a level-1 section: promoting it let one bullet swallow ~28% of
        # a real document. With no prior numbered/explicit outline context it is rejected,
        # so the body stays in the preamble rather than being captured under the bullet.
        paragraphs = split_document_paragraphs("\n\n".join([
            "This Agreement is made between the parties on the date last signed below.",
            "The parties wish to exchange confidential information for the Purpose.",
            "(ii) Confidentiality",
            "The Receiving Party shall keep all Confidential Information secret.",
            "The obligations of confidentiality survive termination for five years.",
        ]))

        structure = build_contract_structure(paragraphs)

        self.assertEqual([section["label"] for section in structure["sections"]], ["Preamble"])
        self.assertEqual(
            structure["sections"][0]["paragraph_ids"],
            ["p1", "p2", "p3", "p4", "p5"],
        )

    def test_outline_bullet_promoted_only_with_prior_structure(self):
        # The bare-outline guard is context-aware: the SAME "(ii) Confidentiality" bullet
        # IS legitimate structure once genuine numbered clauses already precede it, so it
        # must still be captured there (no false negative for real outlines).
        paragraphs = split_document_paragraphs("\n\n".join([
            "1. Definitions",
            "Confidential Information means non-public information.",
            "(ii) Confidentiality",
            "The Receiving Party shall keep all Confidential Information secret.",
        ]))

        structure = build_contract_structure(paragraphs)
        labels = [section["label"] for section in structure["sections"]]

        self.assertEqual(labels, ["1", "(ii)"])
        sections_by_label = {section["label"]: section for section in structure["sections"]}
        self.assertEqual(sections_by_label["(ii)"]["paragraph_ids"], ["p3", "p4"])

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

        self.assertEqual(reference_index["version"], 2)
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

    def test_uses_docx_numbering_and_heading_metadata_as_structure(self):
        paragraphs = [
            {
                "id": "p1",
                "index": 1,
                "text": "Definitions",
                "start": 0,
                "end": 11,
                "source_index": 1,
                "source_kind": "paragraph",
                "style_id": "Heading1",
                "style_name": "heading 1",
                "heading_level": 1,
            },
            {
                "id": "p2",
                "index": 2,
                "text": "Confidentiality Obligations",
                "start": 13,
                "end": 40,
                "source_index": 2,
                "source_kind": "paragraph",
                "numbering": {"num_id": "42", "level": 0, "format": "decimal", "label": "1.", "value": 1},
                "structure_label": "1.",
                "structure_number": "1",
            },
            {
                "id": "p3",
                "index": 3,
                "text": "Permitted Disclosures",
                "start": 42,
                "end": 63,
                "source_index": 3,
                "source_kind": "paragraph",
                "numbering": {"num_id": "42", "level": 1, "format": "decimal", "label": "1.1", "value": 1},
                "structure_label": "1.1",
                "structure_number": "1.1",
            },
            {
                "id": "p4",
                "index": 4,
                "text": "Signature Block",
                "start": 65,
                "end": 80,
                "source_index": 4,
                "source_kind": "table_cell",
                "heading_level": 2,
                "table": {"table_index": 1, "row_index": 1, "cell_index": 1},
            },
            {
                "id": "p5",
                "index": 5,
                "text": "Clauses 1 and 1.1 survive for three years.",
                "start": 82,
                "end": 124,
                "source_index": 5,
                "source_kind": "paragraph",
            },
        ]

        structure = build_contract_structure(paragraphs)
        references = resolve_document_references(paragraphs, structure)
        sections_by_label = {section["label"]: section for section in structure["sections"]}

        self.assertEqual([section["label"] for section in structure["sections"]], ["Definitions", "1", "1.1", "Signature Block"])
        self.assertEqual(sections_by_label["Definitions"]["source"]["style_id"], "Heading1")
        self.assertEqual(sections_by_label["1"]["source"]["numbering"]["label"], "1.")
        self.assertEqual(sections_by_label["1.1"]["parent_id"], sections_by_label["1"]["id"])
        self.assertEqual(sections_by_label["Signature Block"]["source"]["table"], {"table_index": 1, "row_index": 1, "cell_index": 1})
        self.assertEqual(structure["stats"]["docx_numbered_paragraph_count"], 2)
        self.assertEqual(structure["stats"]["docx_heading_paragraph_count"], 2)
        self.assertEqual(structure["stats"]["table_paragraph_count"], 1)
        self.assertEqual(references["references"][0]["resolved_section_ids"], [sections_by_label["1"]["id"], sections_by_label["1.1"]["id"]])

    def test_review_result_includes_contract_structure(self):
        result = review_nda("\n\n".join([
            "1. Confidentiality",
            "Each party shall keep the other party's Confidential Information confidential.",
        ]))

        self.assertIn("contract_structure", result)
        self.assertEqual(result["contract_structure"]["version"], 2)
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
