import contextlib
import io
import json
import os
import unittest
from unittest import mock

from nda_automation import structure_validation
from nda_automation.contract_structure import build_contract_structure
from nda_automation.structure_validation import (
    STRUCTURE_VALIDATION_ENABLED_ENV,
    VERDICT_FALSE_POSITIVE,
    OpenRouterStructureValidator,
    StructureValidationError,
    _parse_model_verdicts,
    reset_structure_validation_cache,
    should_validate_structure,
    structure_validation_enabled,
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


class StructureValidationTestCase(unittest.TestCase):
    """Base case: isolate the process-local verdict cache between tests.

    The verdict cache is keyed by document content, so two tests that validate the
    SAME paragraphs would otherwise share a cached verdict (the second would make no
    validator call). Clearing it per-test keeps each test independent; the dedicated
    caching tests assert the hit behaviour explicitly.
    """

    def setUp(self):
        super().setUp()
        reset_structure_validation_cache()
        self.addCleanup(reset_structure_validation_cache)


class ShouldValidateStructureTests(StructureValidationTestCase):
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


class MndaDemotionTests(StructureValidationTestCase):
    def setUp(self):
        super().setUp()
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


class AirIndiaDemotionTests(StructureValidationTestCase):
    def setUp(self):
        super().setUp()
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


class FallbackTests(StructureValidationTestCase):
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


class DemotionCorrectnessTests(StructureValidationTestCase):
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


class LenientVerdictParseTests(StructureValidationTestCase):
    """The real DeepSeek V4 Flash response is correct JSON but often WRAPPED.

    The previous strict ``json.loads`` of the whole content threw those away with
    "non-JSON text", silently demoting 0 sections (the pass was inert). These
    drive the parsing path directly with wrapped raw responses.
    """

    _ARRAY = [{"id": "section-2", "verdict": "false_positive", "reason": "sig field"}]

    def test_clean_array_parses(self):
        parsed = _parse_model_verdicts(json.dumps(self._ARRAY))
        self.assertEqual(parsed, self._ARRAY)

    def test_json_fence_wrapped_array_parses(self):
        raw = "```json\n" + json.dumps(self._ARRAY) + "\n```"
        self.assertEqual(_parse_model_verdicts(raw), self._ARRAY)

    def test_bare_fence_wrapped_array_parses(self):
        raw = "```\n" + json.dumps(self._ARRAY) + "\n```"
        self.assertEqual(_parse_model_verdicts(raw), self._ARRAY)

    def test_prose_preamble_then_fence_parses(self):
        raw = "Here is the analysis:\n```json\n" + json.dumps(self._ARRAY) + "\n```\n"
        self.assertEqual(_parse_model_verdicts(raw), self._ARRAY)

    def test_prose_preamble_without_fence_parses(self):
        raw = "Here is the analysis: " + json.dumps(self._ARRAY) + " (let me know if you need more)"
        self.assertEqual(_parse_model_verdicts(raw), self._ARRAY)

    def test_truly_unparseable_returns_none(self):
        self.assertIsNone(_parse_model_verdicts("I could not complete this request, sorry."))

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_model_verdicts("   "))


class _FakeHTTPResponse(io.BytesIO):
    """Context-manager byte stream standing in for an ``http.client`` response."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _openrouter_payload_with_content(content: str) -> bytes:
    return json.dumps({
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }).encode("utf-8")


class OpenRouterWrappedResponseTests(StructureValidationTestCase):
    """End-to-end: the live validator's HTTP path + a WRAPPED model response.

    This is the regression guard the stub-only tests missed -- the StubValidator
    never exercises ``OpenRouterStructureValidator``'s response parsing, where the
    bug lived. We mock only the transport (``urlopen``); everything else is real.
    """

    def setUp(self):
        super().setUp()
        self.paragraphs = _mnda_paragraphs()
        self.structure = build_contract_structure(self.paragraphs)
        self.sections_by_heading = {
            section["heading"]: section["id"] for section in self.structure["sections"]
        }

    def _verdict_array_for(self, headings):
        flagged = {h.strip().lower() for h in headings}
        verdicts = []
        for section in self.structure["sections"]:
            if str(section.get("kind") or "") == "preamble":
                continue
            heading = str(section.get("heading") or "")
            verdicts.append({
                "id": section["id"],
                "verdict": (
                    VERDICT_FALSE_POSITIVE
                    if heading.strip().lower() in flagged
                    else "genuine"
                ),
                "reason": "test",
            })
        return verdicts

    def test_wrapped_response_parses_and_demotes(self):
        false_positives = ["AND", "COMPANY NAME", "IN WITNESS WHEREOF"]
        verdicts = self._verdict_array_for(false_positives)
        # The exact wrapping shape the live model emits: prose preamble + ```json fence.
        wrapped = "Here is the analysis:\n```json\n" + json.dumps(verdicts) + "\n```\n"

        validator = OpenRouterStructureValidator(api_key="test-key")
        call_count = {"n": 0}

        def fake_urlopen(request, *args, **kwargs):
            call_count["n"] += 1
            return _FakeHTTPResponse(_openrouter_payload_with_content(wrapped))

        with mock.patch.object(structure_validation.urllib.request, "urlopen", fake_urlopen):
            result = validate_structure(self.structure, self.paragraphs, validator=validator)

        # The wrapped response was parsed and false positives demoted (not inert).
        self.assertEqual(call_count["n"], 1)  # no retries spun
        self.assertEqual(result["structure_validation"]["demoted_count"], 3)
        for heading in false_positives:
            section_id = self.sections_by_heading[heading]
            section = _section_by_id(result, section_id)
            self.assertEqual(section.get("validation"), VERDICT_FALSE_POSITIVE, heading)
            self.assertEqual(_alias_keys_for(result, section_id), set(), heading)
        # Genuine sections survive.
        for heading in ("Definitions", "Confidentiality", "Annexure A"):
            section = _section_by_id(result, self.sections_by_heading[heading])
            self.assertNotEqual(section.get("validation"), VERDICT_FALSE_POSITIVE, heading)

    def test_unparseable_response_falls_back_unchanged_without_retry(self):
        garbage = "Sorry, I was unable to analyze the document."

        validator = OpenRouterStructureValidator(api_key="test-key")
        call_count = {"n": 0}

        def fake_urlopen(request, *args, **kwargs):
            call_count["n"] += 1
            return _FakeHTTPResponse(_openrouter_payload_with_content(garbage))

        with mock.patch.object(structure_validation.urllib.request, "urlopen", fake_urlopen):
            with contextlib.redirect_stderr(io.StringIO()):
                result = validate_structure(self.structure, self.paragraphs, validator=validator)

        # A single parse failure is terminal: one HTTP call, deterministic structure unchanged.
        self.assertEqual(call_count["n"], 1)
        self.assertEqual(result, self.structure)
        self.assertNotIn("structure_validation", result)

    def test_validator_call_raises_on_unparseable_for_direct_callers(self):
        # The OpenRouter validator itself raises (caught upstream by validate_structure).
        garbage = "no json here at all"
        validator = OpenRouterStructureValidator(api_key="test-key")

        def fake_urlopen(request, *args, **kwargs):
            return _FakeHTTPResponse(_openrouter_payload_with_content(garbage))

        with mock.patch.object(structure_validation.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(StructureValidationError):
                validator([{"id": "section-2", "heading": "AND"}])


class _CountingValidator:
    """Records how many times it is invoked so cache hits can be asserted."""

    def __init__(self, flag_headings=()):
        self._inner = StubValidator(flag_headings)
        self.call_count = 0

    def __call__(self, candidates):
        self.call_count += 1
        return self._inner(candidates)


class KillSwitchFlagTests(StructureValidationTestCase):
    """P1: NDA_STRUCTURE_VALIDATION_ENABLED gates BOTH engine call sites.

    The flag defaults OFF: with it unset/falsy the engines must NOT call
    ``validate_structure`` at all and the structure is the pure deterministic parse;
    with it on, validation runs.
    """

    def setUp(self):
        super().setUp()
        self._saved = os.environ.pop(STRUCTURE_VALIDATION_ENABLED_ENV, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._saved is None:
            os.environ.pop(STRUCTURE_VALIDATION_ENABLED_ENV, None)
        else:
            os.environ[STRUCTURE_VALIDATION_ENABLED_ENV] = self._saved

    def test_enabled_helper_default_off(self):
        self.assertFalse(structure_validation_enabled())

    def test_enabled_helper_accepts_truthy_values(self):
        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            os.environ[STRUCTURE_VALIDATION_ENABLED_ENV] = value
            self.assertTrue(structure_validation_enabled(), value)

    def test_enabled_helper_rejects_falsy_values(self):
        for value in ("", "0", "false", "no", "off", "disabled"):
            os.environ[STRUCTURE_VALIDATION_ENABLED_ENV] = value
            self.assertFalse(structure_validation_enabled(), value)

    def test_ai_first_call_site_skips_validation_when_flag_off(self):
        # Spy on validate_structure where the ai_first engine calls it. The flag is
        # OFF (popped in setUp) so the gate must short-circuit BEFORE the call --
        # asserted whether or not the rest of the pipeline succeeds.
        from nda_automation import ai_first_review

        with mock.patch.object(
            ai_first_review, "should_validate_structure", return_value=True
        ), mock.patch.object(ai_first_review, "validate_structure") as spy:
            with contextlib.suppress(Exception):
                ai_first_review.build_ai_first_review_result(
                    "", [], paragraphs=_mnda_paragraphs(),
                )
        spy.assert_not_called()

    def test_ai_first_call_site_runs_validation_when_flag_on(self):
        from nda_automation import ai_first_review

        os.environ[STRUCTURE_VALIDATION_ENABLED_ENV] = "1"
        with mock.patch.object(
            ai_first_review, "should_validate_structure", return_value=True
        ), mock.patch.object(
            ai_first_review, "validate_structure", side_effect=lambda s, p, **k: s
        ) as spy:
            with contextlib.suppress(Exception):
                ai_first_review.build_ai_first_review_result(
                    "", [], paragraphs=_mnda_paragraphs(),
                )
        spy.assert_called_once()

    def test_orchestrate_review_skips_validation_when_flag_off(self):
        from nda_automation.review_orchestration import ReviewCommand, orchestrate_review

        paragraphs = _mnda_paragraphs()
        validator = _CountingValidator(flag_headings=["AND", "COMPANY NAME"])
        command = ReviewCommand(
            text="",
            paragraphs=paragraphs,
            structure_validator=validator,
            verify=False,
            ai_enabled=True,
        )
        result = orchestrate_review(command)
        self.assertEqual(validator.call_count, 0)
        self.assertNotIn("structure_validation", result.get("contract_structure", {}))

    def test_orchestrate_review_runs_validation_when_flag_on(self):
        from nda_automation.review_orchestration import ReviewCommand, orchestrate_review

        os.environ[STRUCTURE_VALIDATION_ENABLED_ENV] = "1"
        paragraphs = _mnda_paragraphs()
        validator = _CountingValidator(flag_headings=["AND", "COMPANY NAME"])
        command = ReviewCommand(
            text="",
            paragraphs=paragraphs,
            structure_validator=validator,
            verify=False,
            ai_enabled=True,
        )
        result = orchestrate_review(command)
        self.assertEqual(validator.call_count, 1)
        self.assertIn("structure_validation", result.get("contract_structure", {}))


class VerdictCacheTests(StructureValidationTestCase):
    """P2: the verdict is cached by document content -- one validator call per doc."""

    def test_second_review_of_same_document_reuses_cache_no_validator_call(self):
        paragraphs = _mnda_paragraphs()
        structure = build_contract_structure(paragraphs)
        validator = _CountingValidator(flag_headings=["AND", "COMPANY NAME"])

        first = validate_structure(structure, paragraphs, validator=validator)
        # A second identical review reads the cache: NO further validator call.
        second_structure = build_contract_structure(paragraphs)
        second = validate_structure(second_structure, paragraphs, validator=validator)

        self.assertEqual(validator.call_count, 1)
        self.assertEqual(
            first["structure_validation"]["demoted_section_ids"],
            second["structure_validation"]["demoted_section_ids"],
        )
        self.assertEqual(second["structure_validation"]["demoted_count"], 2)

    def test_cache_hit_makes_no_http_call(self):
        false_positives = ["AND", "COMPANY NAME", "IN WITNESS WHEREOF"]
        paragraphs = _mnda_paragraphs()
        structure = build_contract_structure(paragraphs)
        sections_by_heading = {s["heading"]: s["id"] for s in structure["sections"]}

        flagged = {h.strip().lower() for h in false_positives}
        verdicts = [
            {
                "id": section["id"],
                "verdict": (
                    VERDICT_FALSE_POSITIVE
                    if str(section.get("heading") or "").strip().lower() in flagged
                    else "genuine"
                ),
                "reason": "test",
            }
            for section in structure["sections"]
            if str(section.get("kind") or "") != "preamble"
        ]
        wrapped = "```json\n" + json.dumps(verdicts) + "\n```"

        validator = OpenRouterStructureValidator(api_key="test-key")
        http_calls = {"n": 0}

        def fake_urlopen(request, *args, **kwargs):
            http_calls["n"] += 1
            return _FakeHTTPResponse(_openrouter_payload_with_content(wrapped))

        with mock.patch.object(structure_validation.urllib.request, "urlopen", fake_urlopen):
            first = validate_structure(structure, paragraphs, validator=validator)
            # Second review of identical content -> served from cache, no HTTP.
            second = validate_structure(
                build_contract_structure(paragraphs), paragraphs, validator=validator
            )

        self.assertEqual(http_calls["n"], 1)
        self.assertEqual(first["structure_validation"]["demoted_count"], 3)
        self.assertEqual(second["structure_validation"]["demoted_count"], 3)
        for heading in false_positives:
            section = _section_by_id(second, sections_by_heading[heading])
            self.assertEqual(section.get("validation"), VERDICT_FALSE_POSITIVE, heading)

    def test_cache_returns_fresh_copy_not_shared_mutable(self):
        # A cache hit must not return the same object as the first call, so a
        # consumer mutating one result cannot corrupt a later one.
        paragraphs = _mnda_paragraphs()
        validator = _CountingValidator(flag_headings=["AND"])
        first = validate_structure(build_contract_structure(paragraphs), paragraphs, validator=validator)
        second = validate_structure(build_contract_structure(paragraphs), paragraphs, validator=validator)
        self.assertIsNot(first, second)
        first["sections"][0]["mutated"] = True
        self.assertNotIn("mutated", second["sections"][0])

    def test_different_document_content_misses_cache(self):
        validator = _CountingValidator(flag_headings=["AND"])
        validate_structure(build_contract_structure(_mnda_paragraphs()), _mnda_paragraphs(), validator=validator)
        # A different document (different content hash) must NOT hit the cache.
        validate_structure(
            build_contract_structure(_air_india_paragraphs()),
            _air_india_paragraphs(),
            validator=validator,
        )
        self.assertEqual(validator.call_count, 2)

    def test_unparseable_response_is_not_cached(self):
        # A garbled verdict must not pin the document to the fallback forever:
        # a later review that parses cleanly should still get a verdict.
        paragraphs = _mnda_paragraphs()

        def garbled(_candidates):
            return "totally not json"

        first = validate_structure(build_contract_structure(paragraphs), paragraphs, validator=garbled)
        self.assertNotIn("structure_validation", first)  # fell back

        good = _CountingValidator(flag_headings=["AND"])
        second = validate_structure(build_contract_structure(paragraphs), paragraphs, validator=good)
        # The good validator WAS called (the failed run did not poison the cache).
        self.assertEqual(good.call_count, 1)
        self.assertEqual(second["structure_validation"]["demoted_count"], 1)


class FailSafeTests(StructureValidationTestCase):
    """P3: every failure path returns the deterministic structure, never raises."""

    def setUp(self):
        super().setUp()
        self.paragraphs = _mnda_paragraphs()
        self.structure = build_contract_structure(self.paragraphs)

    def _assert_unchanged(self, result):
        self.assertEqual(result, self.structure)
        self.assertNotIn("structure_validation", result)

    def test_validator_timeout_falls_back(self):
        def timeout(_candidates):
            raise TimeoutError("validator timed out")

        self._assert_unchanged(validate_structure(self.structure, self.paragraphs, validator=timeout))

    def test_validator_arbitrary_exception_falls_back(self):
        def boom(_candidates):
            raise ValueError("garbled internal state")

        self._assert_unchanged(validate_structure(self.structure, self.paragraphs, validator=boom))

    def test_validator_returns_garbage_type_falls_back(self):
        self._assert_unchanged(validate_structure(self.structure, self.paragraphs, validator=lambda _c: object()))

    def test_validator_returns_none_falls_back(self):
        self._assert_unchanged(validate_structure(self.structure, self.paragraphs, validator=lambda _c: None))

    def test_missing_api_key_with_flag_on_falls_back(self):
        # Flag on but no key + no injected validator -> default validator is None.
        saved_key = os.environ.pop("OPENROUTER_API_KEY", None)
        saved_flag = os.environ.get(STRUCTURE_VALIDATION_ENABLED_ENV)
        os.environ[STRUCTURE_VALIDATION_ENABLED_ENV] = "1"
        try:
            result = validate_structure(self.structure, self.paragraphs, validator=None)
        finally:
            if saved_key is not None:
                os.environ["OPENROUTER_API_KEY"] = saved_key
            if saved_flag is None:
                os.environ.pop(STRUCTURE_VALIDATION_ENABLED_ENV, None)
            else:
                os.environ[STRUCTURE_VALIDATION_ENABLED_ENV] = saved_flag
        self._assert_unchanged(result)

    def test_non_dict_structure_returns_input(self):
        self.assertEqual(validate_structure(None, self.paragraphs, validator=lambda _c: []), None)

    def test_empty_paragraphs_falls_back(self):
        validator = _CountingValidator(flag_headings=["AND"])
        # No paragraphs -> snippets empty but candidates still derive from sections;
        # this must not raise.
        result = validate_structure(self.structure, None, validator=validator)
        self.assertIsInstance(result, dict)


class RecitalKeptTests(StructureValidationTestCase):
    """P4: recitals / preamble narrative are KEPT as genuine, navigable structure."""

    def _recital_paragraphs(self):
        return [
            {"id": "p1", "index": 0, "text": "NON-DISCLOSURE AGREEMENT", "source_kind": "paragraph"},
            {"id": "p2", "index": 1, "text": "RECITALS", "heading_level": 1, "source_kind": "paragraph"},
            {
                "id": "rA",
                "index": 2,
                "text": "A. The parties wish to explore a potential business relationship.",
                "numbering": {"label": "A.", "format": "upperLetter", "level": 0},
                "structure_number": "A",
                "source_kind": "paragraph",
            },
            {
                "id": "rB",
                "index": 3,
                "text": "B. Each party may disclose confidential information to the other.",
                "numbering": {"label": "B.", "format": "upperLetter", "level": 0},
                "structure_number": "B",
                "source_kind": "paragraph",
            },
            {
                "id": "rC",
                "index": 4,
                "text": "C. The parties wish to record the terms of disclosure.",
                "numbering": {"label": "C.", "format": "upperLetter", "level": 0},
                "structure_number": "C",
                "source_kind": "paragraph",
            },
            {
                "id": "c1",
                "index": 5,
                "text": "1. Confidentiality",
                "numbering": {"label": "1.", "format": "decimal", "level": 0},
                "structure_number": "1",
                "source_kind": "paragraph",
            },
            {"id": "c1b", "index": 6, "text": "The Receiving Party shall keep it confidential.", "source_kind": "paragraph"},
        ]

    def test_system_prompt_instructs_keeping_recitals(self):
        prompt = structure_validation.SYSTEM_PROMPT.lower()
        # The prompt must explicitly keep recital / preamble narrative and lettered
        # recitals, and must NOT lump them with promoted definition sentences.
        self.assertIn("recital", prompt)
        self.assertIn("preamble narrative", prompt)
        self.assertIn("a./b./c.", prompt)
        self.assertIn("whereas", prompt)

    def test_lettered_recitals_kept_genuine_and_navigable(self):
        paragraphs = self._recital_paragraphs()
        structure = build_contract_structure(paragraphs)
        sections_by_heading = {s["heading"]: s["id"] for s in structure["sections"]}
        recital_headings = [
            "The parties wish to explore a potential business relationship",
            "Each party may disclose confidential information to the other",
            "The parties wish to record the terms of disclosure",
        ]
        # A correct validator (per the tweaked spec) keeps recitals genuine.
        validator = StubValidator(flag_headings=[])
        result = validate_structure(structure, paragraphs, validator=validator)

        navigable = result["structure_validation"]["navigable_sections"]
        for heading in recital_headings:
            section_id = sections_by_heading[heading]
            section = _section_by_id(result, section_id)
            self.assertNotEqual(section.get("validation"), VERDICT_FALSE_POSITIVE, heading)
            self.assertIn(section_id, navigable, heading)
            self.assertNotEqual(_alias_keys_for(result, section_id), set(), heading)


if __name__ == "__main__":
    unittest.main()
