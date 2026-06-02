import unittest

from nda_automation.concept_classifier import classify_document_concepts
from nda_automation.contract_structure import build_contract_structure
from nda_automation.review_document import split_document_paragraphs


class ConceptClassifierTests(unittest.TestCase):
    def test_classifies_paragraphs_and_sections(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "Article 2 Confidentiality",
            "The Receiving Party shall protect Confidential Information and not disclose it.",
            "Article 3 Use",
            "Confidential Information may be used solely for the Purpose.",
            "Article 4 Term",
            "The confidentiality obligations survive for three years.",
        ]))
        structure = build_contract_structure(paragraphs)

        classifier = classify_document_concepts(paragraphs, structure)
        sections_by_label = {
            section["label"]: section
            for section in classifier["sections"]
        }

        self.assertEqual(classifier["version"], 1)
        self.assertIn("confidentiality_obligation", classifier["concepts_by_paragraph_id"]["p2"])
        self.assertIn("use_restriction", sections_by_label["Article 3"]["concepts"])
        self.assertIn("term_or_survival", sections_by_label["Article 4"]["concepts"])
        self.assertGreaterEqual(classifier["stats"]["classified_section_count"], 3)


if __name__ == "__main__":
    unittest.main()
