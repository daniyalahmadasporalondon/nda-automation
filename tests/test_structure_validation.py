import unittest

from nda_automation.contract_structure import build_contract_structure
from nda_automation.structure_validation import (
    VERDICT_FALSE_POSITIVE,
    should_validate_structure,
    validate_structure,
)


def _alias_keys_for(structure, section_id):
    """Alias keys in reference_index.alias_to_section_id that point at section_id."""
    alias_to_section_id = structure["reference_index"]["alias_to_section_id"]
    return {key for key, value in alias_to_section_id.items() if value == section_id}


def _section_by_id(structure, section_id):
    return next(section for section in structure["sections"] if section["id"] == section_id)


class StubValidator:
    """Key-free injectable validator.

    Flags any candidate whose heading (normalised) matches one of ``flag_headings``
    as false_positive; everything else is genuine. Records the candidates it was
    given so the prompt-shape contract can be asserted.
    """

    def __init__(self, flag_headings):
        self.flag_headings = {heading.strip().lower() for heading in flag_headings}
        self.calls = []

    def __call__(self, candidates):
        self.calls.append(candidates)
        verdicts = []
        for candidate in candidates:
            heading = str(candidate.get("heading") or "").strip().lower()
            verdict = (
                VERDICT_FALSE_POSITIVE
                if heading in self.flag_headings
                else "genuine"
            )
            verdicts.append({
                "id": candidate["id"],
                "verdict": verdict,
                "reason": "stub",
            })
        return verdicts


def _mnda_paragraphs():
    """An MNDA-like DOCX parse with style-misuse false positives.

    "AND" (connective), the promoted definition sentence, "COMPANY NAME" and
    "IN WITNESS WHEREOF" (signature-block phrases) all inherited heading style and
    become false sections. The numbered clauses and the Annexure are genuine.
    """
    return [
        {"id": "p1", "index": 0, "text": "MUTUAL NON-DISCLOSURE AGREEMENT", "source_kind": "paragraph"},
        {"id": "p2", "index": 1, "text": "AND", "heading_level": 1, "source_kind": "paragraph"},
        {
            "id": "p3",
            "index": 2,
            "text": "Confidential Information means all non-public information disclosed by a party.",
            "heading_level": 1,
            "source_kind": "paragraph",
        },
        {
            "id": "p4",
            "index": 3,
            "text": "1. Definitions",
            "numbering": {"label": "1.", "format": "decimal", "level": 0},
            "structure_number": "1",
            "source_kind": "paragraph",
        },
        {"id": "p5", "index": 4, "text": "The terms used here have the meanings given.", "source_kind": "paragraph"},
        {
            "id": "p6",
            "index": 5,
            "text": "2. Confidentiality",
            "numbering": {"label": "2.", "format": "decimal", "level": 0},
            "structure_number": "2",
            "source_kind": "paragraph",
        },
        {"id": "p7", "index": 6, "text": "The Receiving Party shall not disclose Confidential Information.", "source_kind": "paragraph"},
        {"id": "p8", "index": 7, "text": "COMPANY NAME", "heading_level": 1, "source_kind": "paragraph"},
        {"id": "p9", "index": 8, "text": "IN WITNESS WHEREOF", "heading_level": 1, "source_kind": "paragraph"},
        {
            "id": "p10",
            "index": 9,
            "text": "Annexure A",
            "numbering": {"label": "A", "format": "upperLetter", "level": 0},
            "source_kind": "paragraph",
        },
        {"id": "p11", "index": 10, "text": "Permitted disclosures are listed in this annexure.", "source_kind": "paragraph"},
    ]


def _air_india_paragraphs():
    """An Air-India-like DOCX parse: 6 definition sentences promoted to top-level.

    Real clauses, real sub-clauses (a/b/c, i/ii) and a recital heading are genuine.
    """
    paragraphs = [
        {"id": "p1", "index": 0, "text": "NON-DISCLOSURE AGREEMENT", "source_kind": "paragraph"},
        {"id": "p2", "index": 1, "text": "RECITALS", "heading_level": 1, "source_kind": "paragraph"},
        {"id": "p3", "index": 2, "text": "The parties wish to explore a business relationship.", "source_kind": "paragraph"},
    ]
    # 6 definition sentences mis-promoted to top-level headings.
    definitions = [
        "Affiliate means any entity controlling a party.",
        "Confidential Information means non-public information of either party.",
        "Disclosing Party means the party that discloses information.",
        "Receiving Party means the party that receives information.",
        "Purpose means the evaluation of a potential transaction.",
        "Representatives means directors, officers and advisers.",
    ]
    next_index = 3
    for offset, text in enumerate(definitions):
        paragraphs.append({
            "id": f"d{offset + 1}",
            "index": next_index,
            "text": text,
            "heading_level": 1,
            "source_kind": "paragraph",
        })
        next_index += 1
    # Real clause 1 with real sub-clauses a/b/c and i/ii.
    paragraphs.extend([
        {
            "id": "c1",
            "index": next_index,
            "text": "1. Obligations",
            "numbering": {"label": "1.", "format": "decimal", "level": 0},
            "structure_number": "1",
            "source_kind": "paragraph",
        },
        {
            "id": "c1a",
            "index": next_index + 1,
            "text": "(a) keep the information confidential;",
            "numbering": {"label": "(a)", "format": "lowerLetter", "level": 1},
            "structure_number": "1.(a)",
            "source_kind": "paragraph",
        },
        {
            "id": "c1b",
            "index": next_index + 2,
            "text": "(b) use it solely for the Purpose;",
            "numbering": {"label": "(b)", "format": "lowerLetter", "level": 1},
            "structure_number": "1.(b)",
            "source_kind": "paragraph",
        },
        {
            "id": "c1c",
            "index": next_index + 3,
            "text": "(c) limit access to Representatives, who must:",
            "numbering": {"label": "(c)", "format": "lowerLetter", "level": 1},
            "structure_number": "1.(c)",
            "source_kind": "paragraph",
        },
        {
            "id": "c1ci",
            "index": next_index + 4,
            "text": "(i) be bound by confidentiality; and",
            "numbering": {"label": "(i)", "format": "lowerRoman", "level": 2},
            "structure_number": "1.(c)(i)",
            "source_kind": "paragraph",
        },
        {
            "id": "c1cii",
            "index": next_index + 5,
            "text": "(ii) be informed of these obligations.",
            "numbering": {"label": "(ii)", "format": "lowerRoman", "level": 2},
            "structure_number": "1.(c)(ii)",
            "source_kind": "paragraph",
        },
        {
            "id": "c2",
            "index": next_index + 6,
            "text": "2. Term",
            "numbering": {"label": "2.", "format": "decimal", "level": 0},
            "structure_number": "2",
            "source_kind": "paragraph",
        },
        {"id": "c2body", "index": next_index + 7, "text": "This Agreement survives for three years.", "source_kind": "paragraph"},
    ])
    return paragraphs


class ShouldValidateStructureTests(unittest.TestCase):
    def test_runs_for_docx_sourced_structure(self):
        paragraphs = _mnda_paragraphs()
        structure = build_contract_structure(paragraphs)
        self.assertTrue(should_validate_structure(structure, paragraphs))

    def test_skips_pdf_or_plain_text_without_layout_metadata(self):
        # No source_kind / numbering / heading_level metadata -> not a DOCX parse.
        paragraphs = [
            {"id": "p1", "index": 0, "text": "1. Definitions"},
            {"id": "p2", "index": 1, "text": "Confidential Information means non-public information."},
        ]
        structure = build_contract_structure(paragraphs)
        self.assertFalse(should_validate_structure(structure, paragraphs))

    def test_skips_when_no_real_sections(self):
        paragraphs = [
            {"id": "p1", "index": 0, "text": "Just a preamble paragraph.", "source_kind": "paragraph"},
        ]
        structure = build_contract_structure(paragraphs)
        self.assertFalse(should_validate_structure(structure, paragraphs))


class MndaDemotionTests(unittest.TestCase):
    def setUp(self):
        self.paragraphs = _mnda_paragraphs()
        self.structure = build_contract_structure(self.paragraphs)
        self.sections_by_heading = {
            section["heading"]: section["id"] for section in self.structure["sections"]
        }
        # Sanity: the deterministic parse really did promote the false positives.
        self.assertIn("AND", self.sections_by_heading)
        self.assertIn("COMPANY NAME", self.sections_by_heading)
        self.assertIn("IN WITNESS WHEREOF", self.sections_by_heading)

    def test_demotes_signature_connective_and_definition_false_positives(self):
        validator = StubValidator(flag_headings=[
            "AND",
            "COMPANY NAME",
            "IN WITNESS WHEREOF",
            "Confidential Information means all non-public information disclosed by a party.",
        ])
        result = validate_structure(self.structure, self.paragraphs, validator=validator)

        false_positive_headings = ["AND", "COMPANY NAME", "IN WITNESS WHEREOF"]
        for heading in false_positive_headings:
            section_id = self.sections_by_heading[heading]
            section = _section_by_id(result, section_id)
            self.assertEqual(section.get("validation"), VERDICT_FALSE_POSITIVE, heading)
            # Demoted: no alias keys point at it anymore.
            self.assertEqual(_alias_keys_for(result, section_id), set(), heading)

    def test_genuine_clauses_and_annexure_remain(self):
        validator = StubValidator(flag_headings=["AND", "COMPANY NAME", "IN WITNESS WHEREOF"])
        result = validate_structure(self.structure, self.paragraphs, validator=validator)

        for heading in ("Definitions", "Confidentiality", "Annexure A"):
            section_id = self.sections_by_heading[heading]
            section = _section_by_id(result, section_id)
            self.assertNotEqual(section.get("validation"), VERDICT_FALSE_POSITIVE, heading)
            self.assertNotEqual(_alias_keys_for(result, section_id), set(), heading)

        # The numbered clause's number alias survives.
        clause_one_id = self.sections_by_heading["Definitions"]
        self.assertIn("number:1", _alias_keys_for(result, clause_one_id))
        # The annexure stays navigable.
        navigable = result["structure_validation"]["navigable_sections"]
        self.assertIn(self.sections_by_heading["Annexure A"], navigable)
        self.assertNotIn(self.sections_by_heading["AND"], navigable)

    def test_paragraphs_untouched_for_demoted_section(self):
        validator = StubValidator(flag_headings=["AND", "COMPANY NAME", "IN WITNESS WHEREOF"])
        result = validate_structure(self.structure, self.paragraphs, validator=validator)

        for heading in ("AND", "COMPANY NAME", "IN WITNESS WHEREOF"):
            section_id = self.sections_by_heading[heading]
            original = _section_by_id(self.structure, section_id)
            demoted = _section_by_id(result, section_id)
            self.assertEqual(demoted["paragraph_ids"], original["paragraph_ids"], heading)
            self.assertEqual(demoted["start_paragraph_id"], original["start_paragraph_id"])
            self.assertEqual(demoted["end_paragraph_id"], original["end_paragraph_id"])

    def test_candidates_carry_heading_and_snippet_and_exclude_preamble(self):
        validator = StubValidator(flag_headings=[])
        validate_structure(self.structure, self.paragraphs, validator=validator)
        candidates = validator.calls[0]
        candidate_ids = {candidate["id"] for candidate in candidates}
        preamble_id = self.structure["sections"][0]["id"]
        self.assertNotIn(preamble_id, candidate_ids)
        and_candidate = next(c for c in candidates if c["heading"] == "AND")
        self.assertIn("id", and_candidate)
        self.assertIn("snippet", and_candidate)
        clause_candidate = next(c for c in candidates if c["heading"] == "Definitions")
        self.assertEqual(clause_candidate["number"], "1")


class AirIndiaDemotionTests(unittest.TestCase):
    def setUp(self):
        self.paragraphs = _air_india_paragraphs()
        self.structure = build_contract_structure(self.paragraphs)
        self.sections_by_heading = {
            section["heading"]: section["id"] for section in self.structure["sections"]
        }

    def test_demotes_six_definition_sentences(self):
        definition_headings = [
            "Affiliate means any entity controlling a party",
            "Confidential Information means non-public information of either party",
            "Disclosing Party means the party that discloses information",
            "Receiving Party means the party that receives information",
            "Purpose means the evaluation of a potential transaction",
            "Representatives means directors, officers and advisers",
        ]
        # Confirm all 6 were promoted to sections by the deterministic parse.
        for heading in definition_headings:
            self.assertIn(heading, self.sections_by_heading, heading)

        validator = StubValidator(flag_headings=definition_headings)
        result = validate_structure(self.structure, self.paragraphs, validator=validator)

        self.assertEqual(result["structure_validation"]["demoted_count"], 6)
        for heading in definition_headings:
            section_id = self.sections_by_heading[heading]
            section = _section_by_id(result, section_id)
            self.assertEqual(section.get("validation"), VERDICT_FALSE_POSITIVE, heading)
            self.assertEqual(_alias_keys_for(result, section_id), set(), heading)

    def test_real_clauses_subclauses_and_recital_stay_genuine(self):
        definition_headings = [
            "Affiliate means any entity controlling a party",
            "Confidential Information means non-public information of either party",
            "Disclosing Party means the party that discloses information",
            "Receiving Party means the party that receives information",
            "Purpose means the evaluation of a potential transaction",
            "Representatives means directors, officers and advisers",
        ]
        validator = StubValidator(flag_headings=definition_headings)
        result = validate_structure(self.structure, self.paragraphs, validator=validator)

        genuine_headings = ["RECITALS", "Obligations", "Term"]
        for heading in genuine_headings:
            section_id = self.sections_by_heading[heading]
            section = _section_by_id(result, section_id)
            self.assertNotEqual(section.get("validation"), VERDICT_FALSE_POSITIVE, heading)

        # Real sub-clauses (a/b/c) survive: every section with a multi-part
        # structure number keeps its number and is not demoted.
        subclause_numbers = {"1.(a)", "1.(b)", "1.(c)"}
        surviving_numbers = {
            section["number"]
            for section in result["sections"]
            if section.get("validation") != VERDICT_FALSE_POSITIVE
            and isinstance(section.get("number"), str)
        }
        self.assertTrue(subclause_numbers.issubset(surviving_numbers), surviving_numbers)
        # And the i/ii enumeration text is still present in the (c) sub-clause's
        # paragraphs (never deleted), even though the deterministic parser folded
        # it into the (c) section rather than promoting it.
        c_section = next(s for s in result["sections"] if s.get("number") == "1.(c)")
        self.assertIn("c1ci", c_section["paragraph_ids"])
        self.assertIn("c1cii", c_section["paragraph_ids"])


class FallbackTests(unittest.TestCase):
    def test_validator_raises_returns_structure_unchanged(self):
        paragraphs = _mnda_paragraphs()
        structure = build_contract_structure(paragraphs)

        def boom(_candidates):
            raise RuntimeError("network down")

        result = validate_structure(structure, paragraphs, validator=boom)
        self.assertEqual(result, structure)
        self.assertNotIn("structure_validation", result)

    def test_no_validator_and_no_api_key_returns_structure_unchanged(self):
        # No validator injected and no OPENROUTER_API_KEY -> default validator is
        # None -> unchanged.
        import os

        paragraphs = _mnda_paragraphs()
        structure = build_contract_structure(paragraphs)
        saved = os.environ.pop("OPENROUTER_API_KEY", None)
        try:
            result = validate_structure(structure, paragraphs, validator=None)
        finally:
            if saved is not None:
                os.environ["OPENROUTER_API_KEY"] = saved
        self.assertEqual(result, structure)

    def test_unparseable_output_returns_structure_unchanged(self):
        paragraphs = _mnda_paragraphs()
        structure = build_contract_structure(paragraphs)
        result = validate_structure(structure, paragraphs, validator=lambda _c: 12345)
        self.assertEqual(result, structure)

    def test_none_output_returns_structure_unchanged(self):
        paragraphs = _mnda_paragraphs()
        structure = build_contract_structure(paragraphs)
        result = validate_structure(structure, paragraphs, validator=lambda _c: None)
        self.assertEqual(result, structure)


class DemotionCorrectnessTests(unittest.TestCase):
    def test_demoted_aliases_removed_paragraphs_intact_genuine_aliases_kept(self):
        paragraphs = _mnda_paragraphs()
        structure = build_contract_structure(paragraphs)
        sections_by_heading = {s["heading"]: s["id"] for s in structure["sections"]}

        demoted_heading = "COMPANY NAME"
        demoted_id = sections_by_heading[demoted_heading]
        genuine_id = sections_by_heading["Definitions"]

        original_demoted_aliases = _alias_keys_for(structure, demoted_id)
        original_genuine_aliases = _alias_keys_for(structure, genuine_id)
        original_paragraph_ids = _section_by_id(structure, demoted_id)["paragraph_ids"]
        self.assertNotEqual(original_demoted_aliases, set())
        self.assertNotEqual(original_genuine_aliases, set())

        validator = StubValidator(flag_headings=[demoted_heading])
        result = validate_structure(structure, paragraphs, validator=validator)

        # Demoted section: alias keys gone from reference_index...
        self.assertEqual(_alias_keys_for(result, demoted_id), set())
        for key in original_demoted_aliases:
            self.assertNotIn(key, result["reference_index"]["alias_to_section_id"])
        # ...but its paragraphs are untouched.
        self.assertEqual(_section_by_id(result, demoted_id)["paragraph_ids"], original_paragraph_ids)
        # The resolver-facing record is flagged too.
        record = result["reference_index"]["sections_by_id"][demoted_id]
        self.assertEqual(record.get("validation"), VERDICT_FALSE_POSITIVE)

        # Genuine section: aliases intact.
        self.assertEqual(_alias_keys_for(result, genuine_id), original_genuine_aliases)

    def test_original_structure_is_not_mutated(self):
        paragraphs = _mnda_paragraphs()
        structure = build_contract_structure(paragraphs)
        sections_by_heading = {s["heading"]: s["id"] for s in structure["sections"]}
        demoted_id = sections_by_heading["AND"]
        original_alias_count = len(structure["reference_index"]["alias_to_section_id"])

        validator = StubValidator(flag_headings=["AND"])
        validate_structure(structure, paragraphs, validator=validator)

        # Source structure untouched (validate_structure works on a copy).
        self.assertNotIn("validation", _section_by_id(structure, demoted_id))
        self.assertEqual(len(structure["reference_index"]["alias_to_section_id"]), original_alias_count)
        self.assertNotIn("structure_validation", structure)


if __name__ == "__main__":
    unittest.main()
