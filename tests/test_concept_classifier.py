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

    def test_classifies_mutuality_roles_and_confidentiality_exclusions(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "Each party acts as both a Disclosing Party and a Receiving Party.",
            "The Disclosing Party means a party that discloses Confidential Information.",
            "Confidential Information does not include information in the public domain.",
        ]))

        classifier = classify_document_concepts(paragraphs, build_contract_structure(paragraphs))

        self.assertIn("mutuality", classifier["concepts_by_paragraph_id"]["p1"])
        self.assertIn("party_role_definition", classifier["concepts_by_paragraph_id"]["p2"])
        self.assertIn("confidential_information_exclusion", classifier["concepts_by_paragraph_id"]["p3"])


class ConceptRecallAndPrecisionTests(unittest.TestCase):
    def test_non_circ_detects_keyword_free_restrictions(self):
        # BUGFIX (B2): restrictions phrased WITHOUT the trigger keywords must still
        # be tagged non_circumvention, so they are not dropped from reference scope
        # (absent = pass for a prohibited clause).
        from nda_automation.concept_classifier import _concepts_for_text

        for text in [
            "Neither party shall enter into any transaction with persons first made known to it through this engagement.",
            "The Recipient shall not pursue any business opportunity with a contact disclosed hereunder.",
            "All dealings shall be conducted exclusively through the Disclosing Party.",
            "The Recipient shall not solicit, hire, or employ any employee of the other party.",
            "The Recipient shall not use the Confidential Information as a substitute for transacting.",
        ]:
            concepts, _ = _concepts_for_text(text)
            self.assertIn("non_circumvention", concepts, text)

    def test_mutuality_not_tagged_on_boilerplate(self):
        # BUGFIX (B3): bare "mutual"/"mutually" boilerplate must not be tagged as a
        # mutuality-of-obligations signal; genuine mutual confidentiality still is.
        from nda_automation.concept_classifier import _concepts_for_text

        for text in [
            "This Agreement may be amended only by mutual written consent of the parties.",
            "The parties shall meet at a mutually convenient time.",
        ]:
            concepts, _ = _concepts_for_text(text)
            self.assertNotIn("mutuality", concepts, text)
        concepts, _ = _concepts_for_text(
            "This is a mutual NDA; each party may disclose Confidential Information to the other."
        )
        self.assertIn("mutuality", concepts)


if __name__ == "__main__":
    unittest.main()
