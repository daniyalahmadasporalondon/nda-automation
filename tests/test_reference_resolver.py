import unittest

from nda_automation.checker import review_nda
from nda_automation.contract_structure import build_contract_structure
from nda_automation.matter_view import review_result_with_structure
from nda_automation.reference_resolver import resolve_document_references
from nda_automation.review_document import split_document_paragraphs


class ReferenceResolverTests(unittest.TestCase):
    def test_resolves_explicit_clause_article_and_section_references(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "MUTUAL NON-DISCLOSURE AGREEMENT",
            "Clause 1: Definitions",
            "Confidential Information means non-public information.",
            "Clause 2 - Confidentiality",
            "Each party shall protect Confidential Information.",
            "Article II Term",
            "The obligations in clauses 1 and 2 survive. Section II.A also applies.",
            "Section II.A Permitted Disclosures",
            "Disclosures are limited to representatives.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        self.assertEqual(resolver["version"], 1)
        self.assertEqual(resolver["stats"], {
            "reference_count": 2,
            "resolved_reference_count": 2,
            "partial_reference_count": 0,
            "unresolved_reference_count": 0,
            "target_section_count": 3,
        })
        clause_reference, section_reference = resolver["references"]
        self.assertEqual(clause_reference["reference_text"], "clauses 1 and 2")
        self.assertEqual(clause_reference["kind"], "clause")
        self.assertEqual(clause_reference["numbers"], ["1", "2"])
        self.assertEqual(clause_reference["resolved_section_ids"], ["section-2", "section-3"])
        self.assertEqual(clause_reference["source_section_id"], "section-4")
        self.assertEqual([target["label"] for target in clause_reference["targets"]], ["Clause 1", "Clause 2"])
        self.assertEqual(section_reference["reference_text"], "Section II.A")
        self.assertEqual(section_reference["resolved_section_ids"], ["section-5"])
        self.assertEqual(section_reference["items"][0]["matched_alias"], "section:ii.a")

    def test_resolves_numbered_sections_when_kind_alias_is_absent(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "10. General",
            "General terms.",
            "10.1 Return of Materials",
            "Return terms.",
            "10.1A Certificate of Destruction",
            "The obligations in sections 10.1 and 10.1A survive.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        reference = resolver["references"][0]
        self.assertEqual(reference["numbers"], ["10.1", "10.1A"])
        self.assertEqual(reference["resolved_section_ids"], ["section-2", "section-3"])
        self.assertEqual([item["matched_alias"] for item in reference["items"]], ["number:10.1", "number:10.1a"])

    def test_expands_numeric_reference_ranges(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "Clause 1 Intro",
            "Introductory text.",
            "Clause 2 Confidentiality",
            "Confidentiality text.",
            "Clause 3 Return",
            "Return text.",
            "Clause 4 Non-Circumvention",
            "Non-circumvention text.",
            "Clause 5 Governing Law",
            "Governing law text.",
            "Clause 6 Survival",
            "Clauses 2 to 5 survive. Sections 2-5 also apply.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        clause_reference, section_reference = resolver["references"]
        self.assertEqual(clause_reference["reference_text"], "Clauses 2 to 5")
        self.assertEqual(clause_reference["numbers"], ["2", "3", "4", "5"])
        self.assertEqual(clause_reference["resolved_section_ids"], ["section-2", "section-3", "section-4", "section-5"])
        self.assertEqual(section_reference["reference_text"], "Sections 2-5")
        self.assertEqual(section_reference["numbers"], ["2", "3", "4", "5"])
        self.assertEqual(section_reference["resolved_section_ids"], ["section-2", "section-3", "section-4", "section-5"])

    def test_resolves_parenthetical_and_outline_references(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "1. General",
            "General terms.",
            "1(a) Confidentiality",
            "Confidentiality terms.",
            "10. Boilerplate",
            "Boilerplate terms.",
            "Section 10(b) Data Processing",
            "Data processing terms.",
            "A. Definitions",
            "Definition terms.",
            "Section 10(b) and Section A apply. Clause 1(a) also applies.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        self.assertEqual(
            [reference["reference_text"] for reference in resolver["references"]],
            ["Section 10(b)", "Section A", "Clause 1(a)"],
        )
        self.assertEqual(
            [reference["status"] for reference in resolver["references"]],
            ["resolved", "resolved", "resolved"],
        )
        self.assertEqual(
            [[target["label"] for target in reference["targets"]] for reference in resolver["references"]],
            [["Section 10(b)"], ["A"], ["1(a)"]],
        )
        self.assertEqual(
            [resolver["references"][index]["items"][0]["matched_alias"] for index in range(3)],
            ["section:10(b)", "number:a", "number:1(a)"],
        )

    def test_marks_partial_and_unresolved_references(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "Clause 1: Definitions",
            "Definitions text.",
            "Clause 2 - Confidentiality",
            "Clauses 1, 2 and 99 survive. Article IX also survives.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        self.assertEqual([reference["status"] for reference in resolver["references"]], ["partial", "unresolved"])
        self.assertEqual(resolver["references"][0]["resolved_section_ids"], ["section-1", "section-2"])
        self.assertEqual(resolver["references"][0]["unresolved_numbers"], ["99"])
        self.assertEqual(resolver["references"][1]["unresolved_numbers"], ["IX"])
        self.assertEqual(resolver["stats"]["partial_reference_count"], 1)
        self.assertEqual(resolver["stats"]["unresolved_reference_count"], 1)

    def test_does_not_read_common_prose_as_single_letter_reference(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "10. General",
            "This section introduces general terms.",
            "Clause 1: Definitions",
            "Definitions in Section 10 remain subject to Clause 1.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        self.assertEqual([reference["reference_text"] for reference in resolver["references"]], ["Section 10", "Clause 1"])

    def test_skips_section_heading_self_references(self):
        paragraphs = split_document_paragraphs("\n\n".join([
            "Clause 1: Definitions",
            "Definitions text.",
            "Clause 2 - Confidentiality",
            "Clause 1 survives this Agreement.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        self.assertEqual(len(resolver["references"]), 1)
        self.assertEqual(resolver["references"][0]["paragraph_id"], "p4")
        self.assertEqual(resolver["references"][0]["reference_text"], "Clause 1")

    def test_schedule_reference_does_not_alias_onto_section_number(self):
        # "Schedule 2" must not borrow the in-body Section 2 just because they
        # share the number. Schedules/annexes/appendices are a separate namespace;
        # bridging them is the latent governing-law false-clear.
        paragraphs = split_document_paragraphs("\n\n".join([
            "Section 1 Definitions",
            "Definitions text.",
            "Section 2 Confidentiality",
            "Confidentiality text.",
            "The governing law is set out in Schedule 2.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        schedule_reference = resolver["references"][-1]
        self.assertEqual(schedule_reference["kind"], "schedule")
        self.assertEqual(schedule_reference["numbers"], ["2"])
        # No Schedule 2 exists, so the reference is unresolved -- it must NOT fall
        # back onto Section 2 via the kind-agnostic number alias.
        self.assertEqual(schedule_reference["resolved_section_ids"], [])
        self.assertEqual(schedule_reference["status"], "unresolved")
        self.assertEqual(schedule_reference["items"][0]["matched_alias"], None)
        self.assertEqual(schedule_reference["items"][0]["alias_keys"], ["schedule:2"])

    def test_section_reference_does_not_alias_onto_schedule_number(self):
        # The reverse collision: a "Section 2" reference must not resolve to an
        # attachment "Schedule 2" target via the numeric fallback.
        paragraphs = split_document_paragraphs("\n\n".join([
            "Section 1 Definitions",
            "Definitions text.",
            "Schedule 2 Data Processing",
            "Data processing terms.",
            "The obligations in Section 2 survive termination.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        section_reference = resolver["references"][-1]
        self.assertEqual(section_reference["kind"], "section")
        self.assertEqual(section_reference["numbers"], ["2"])
        self.assertEqual(section_reference["resolved_section_ids"], [])
        self.assertEqual(section_reference["status"], "unresolved")

    def test_schedule_reference_resolves_to_explicit_schedule_target(self):
        # The guard only blocks the cross-namespace fallback; an explicit Schedule
        # target still resolves normally via its schedule:N alias.
        paragraphs = split_document_paragraphs("\n\n".join([
            "Section 1 Definitions",
            "Definitions text.",
            "Schedule 2 Governing Law",
            "Governing law terms.",
            "The governing law is set out in Schedule 2.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        schedule_reference = resolver["references"][-1]
        self.assertEqual(schedule_reference["kind"], "schedule")
        self.assertEqual(schedule_reference["status"], "resolved")
        self.assertEqual(schedule_reference["items"][0]["matched_alias"], "schedule:2")

    def test_duplicate_section_number_is_ambiguous_not_resolved_to_first(self):
        # An appended Exhibit/Order Form restarts numbering, so "Section 2" exists twice.
        # A "governed by ... Section 2" reference must NOT silently bind to the first
        # occurrence with full confidence -- the alias key is ambiguous and the reference
        # must be left unresolved so downstream consumers force human review rather than
        # trusting the wrong (first) target. This is the duplicate-section false-clear.
        paragraphs = split_document_paragraphs("\n\n".join([
            "Section 1 Definitions",
            "Definitions text.",
            "Section 2 Governing Law",
            "This Agreement is governed by the laws of England and Wales.",
            "ORDER FORM",
            "Section 1 Pricing",
            "Pricing terms.",
            "Section 2 Local Terms",
            "These local terms are governed by the laws of the State of Texas.",
            "The parties agree this Agreement is governed by Section 2.",
        ]))
        structure = build_contract_structure(paragraphs)

        # The colliding keys are recorded as ambiguous and never bound in the lookup.
        reference_index = structure["reference_index"]
        self.assertIn("section:2", reference_index["ambiguous_alias_keys"])
        self.assertIn("number:2", reference_index["ambiguous_alias_keys"])
        self.assertNotIn("section:2", reference_index["alias_to_section_id"])

        resolver = resolve_document_references(paragraphs, structure)

        section_2_reference = resolver["references"][-1]
        self.assertEqual(section_2_reference["reference_text"], "Section 2")
        self.assertEqual(section_2_reference["numbers"], ["2"])
        # Not silently resolved to the first Section 2.
        self.assertEqual(section_2_reference["resolved_section_ids"], [])
        self.assertEqual(section_2_reference["status"], "unresolved")
        self.assertIsNone(section_2_reference["items"][0]["section_id"])
        self.assertIsNone(section_2_reference["items"][0]["matched_alias"])
        self.assertTrue(section_2_reference["items"][0]["ambiguous"])

    def test_unique_section_number_still_resolves_when_a_different_number_is_duplicated(self):
        # The ambiguity guard is per-key: a duplicated "Section 2" must not poison the
        # resolution of an unambiguous "Section 1" elsewhere in the same document.
        paragraphs = split_document_paragraphs("\n\n".join([
            "Section 1 Definitions",
            "Definitions text.",
            "Section 2 Confidentiality",
            "Confidentiality text.",
            "ORDER FORM",
            "Section 2 Local Terms",
            "Local terms apply.",
            "The obligations in Section 1 survive. Section 2 also applies.",
        ]))
        structure = build_contract_structure(paragraphs)

        resolver = resolve_document_references(paragraphs, structure)

        section_1_reference, section_2_reference = resolver["references"][-2:]
        self.assertEqual(section_1_reference["reference_text"], "Section 1")
        self.assertEqual(section_1_reference["status"], "resolved")
        self.assertEqual(section_1_reference["resolved_section_ids"], ["section-1"])
        # The duplicated Section 2 stays ambiguous/unresolved.
        self.assertEqual(section_2_reference["reference_text"], "Section 2")
        self.assertEqual(section_2_reference["status"], "unresolved")
        self.assertEqual(section_2_reference["resolved_section_ids"], [])

    def test_review_result_and_legacy_enrichment_include_reference_resolver(self):
        text = "\n\n".join([
            "Clause 1: Definitions",
            "Definitions text.",
            "Clause 2 - Confidentiality",
            "Clause 1 survives this Agreement.",
        ])

        result = review_nda(text)
        enriched = review_result_with_structure({
            "paragraphs": split_document_paragraphs(text),
        })

        self.assertIn("reference_resolver", result)
        self.assertEqual(result["reference_resolver"]["references"][0]["resolved_section_ids"], ["section-1"])
        self.assertIn("reference_resolver", enriched)
        self.assertEqual(enriched["reference_resolver"]["references"][0]["resolved_section_ids"], ["section-1"])


if __name__ == "__main__":
    unittest.main()
