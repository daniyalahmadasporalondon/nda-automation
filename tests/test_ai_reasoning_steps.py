"""Tests for the additive, display-only ``reasoning_steps`` chain-of-thought.

The reasoning steps are surfaced for the Reasoning trail UI. They must be parsed
fail-open and must NEVER alter or block the decision parse, so every test asserts
the verdict is identical with and without (or with malformed) reasoning_steps.
"""

import unittest

from nda_automation.ai_assessment_contract import (
    AI_ASSESSMENT_CONTRACT_VERSION,
    AI_ASSESSMENT_MAX_REASONING_STEPS,
    AI_CLAUSE_ASSESSMENT_SCHEMA,
    validate_ai_clause_assessments,
)
from nda_automation.ai_first_review import build_ai_first_review_result
from nda_automation.redline_actions import REDLINE_REPLACE_PARAGRAPH
from nda_automation.review_document import split_document_paragraphs


SOURCE_TEXT = "\n\n".join([
    "Each party may disclose Confidential Information to the other party under this Agreement.",
    "This Agreement shall be governed by the laws of California.",
])


def _paragraphs():
    return split_document_paragraphs(SOURCE_TEXT)


def _valid_clause_ids():
    from nda_automation.checker import load_playbook

    return [clause["id"] for clause in load_playbook()["clauses"]]


def _valid_assessment(**overrides):
    assessment = {
        "clause_id": "governing_law",
        "decision": "fail",
        "issue_type": "present_but_wrong",
        "rationale": (
            "The clause selects California law, which is outside the approved governing-law options for this "
            "playbook. A reviewer should treat it as non-compliant even though the clause is otherwise clear."
        ),
        "evidence": [{
            "quote": "laws of california",
            "relevance": "Shows the governing-law jurisdiction.",
        }],
        "proposed_redline": {
            "action": REDLINE_REPLACE_PARAGRAPH,
            "paragraph_id": "p2",
            "text": "This Agreement shall be governed by the laws of England and Wales.",
            "jurisdiction": "England and Wales",
        },
        "confidence": 0.94,
        "blocks_send": False,
    }
    assessment.update(overrides)
    return assessment


def _verdict_fields(parsed):
    """The decision-relevant fields that must never move because of reasoning_steps."""
    return {
        "decision": parsed["decision"],
        "issue_type": parsed["issue_type"],
        "evidence": parsed["evidence"],
        "confidence": parsed["confidence"],
        "proposed_redline": parsed["proposed_redline"],
        "blocks_send": parsed["blocks_send"],
    }


def _parse(assessment):
    return validate_ai_clause_assessments(
        [assessment],
        valid_clause_ids=_valid_clause_ids(),
        paragraphs=_paragraphs(),
    )["governing_law"]


REASONING_STEPS = [
    {"step": "locate", "finding": "The governing-law clause is paragraph 2."},
    {"step": "read", "finding": "It selects the laws of California with no carve-out."},
    {"step": "apply", "finding": "California is not on the approved-options list."},
    {"step": "cite", "finding": "Quote: 'laws of California'."},
    {"step": "decide", "finding": "Outside approved options, so fail."},
]


class ReasoningStepsSchemaTests(unittest.TestCase):
    def test_contract_version_bumped_for_reasoning_steps(self):
        self.assertGreaterEqual(AI_ASSESSMENT_CONTRACT_VERSION, 2)

    def test_schema_declares_reasoning_steps_before_decision(self):
        props = AI_CLAUSE_ASSESSMENT_SCHEMA["properties"]
        self.assertIn("reasoning_steps", props)
        # reasoning_steps is optional (display only), never required.
        self.assertNotIn("reasoning_steps", AI_CLAUSE_ASSESSMENT_SCHEMA["required"])
        keys = list(props.keys())
        self.assertLess(
            keys.index("reasoning_steps"),
            keys.index("decision"),
            "reasoning_steps must precede decision (reason first, then decide)",
        )
        item = props["reasoning_steps"]["items"]
        self.assertEqual(set(item["required"]), {"step", "finding"})
        self.assertFalse(item["additionalProperties"])


class ReasoningStepsParseTests(unittest.TestCase):
    def test_reasoning_steps_parsed_when_present(self):
        parsed = _parse(_valid_assessment(reasoning_steps=list(REASONING_STEPS)))
        self.assertEqual(parsed["reasoning_steps"], REASONING_STEPS)

    def test_reasoning_steps_strip_unknown_keys_and_whitespace(self):
        parsed = _parse(_valid_assessment(reasoning_steps=[
            {"step": "  locate  ", "finding": "  found it  ", "extra": "ignored"},
        ]))
        self.assertEqual(parsed["reasoning_steps"], [{"step": "locate", "finding": "found it"}])

    def test_reasoning_steps_capped(self):
        many = [{"step": f"s{i}", "finding": f"f{i}"} for i in range(AI_ASSESSMENT_MAX_REASONING_STEPS + 5)]
        parsed = _parse(_valid_assessment(reasoning_steps=many))
        self.assertEqual(len(parsed["reasoning_steps"]), AI_ASSESSMENT_MAX_REASONING_STEPS)

    def test_decision_unchanged_when_reasoning_steps_absent(self):
        baseline = _verdict_fields(_parse(_valid_assessment()))
        self.assertNotIn("reasoning_steps", _parse(_valid_assessment()))
        with_steps = _verdict_fields(_parse(_valid_assessment(reasoning_steps=list(REASONING_STEPS))))
        self.assertEqual(baseline, with_steps)

    def test_malformed_reasoning_steps_are_dropped_without_changing_verdict(self):
        baseline = _verdict_fields(_parse(_valid_assessment()))
        for malformed in (
            None,
            [],
            "locate then decide",          # not a list
            {"step": "locate"},            # a dict, not a list
            [None, 7, "x"],                # elements not objects
            [{"finding": "no step label"}],  # missing step
            [{"step": "locate"}],          # missing finding
            [{"step": "", "finding": ""}],  # blank
        ):
            with self.subTest(malformed=malformed):
                parsed = _parse(_valid_assessment(reasoning_steps=malformed))
                self.assertEqual(_verdict_fields(parsed), baseline)
                # A wholly-malformed value drops the field entirely (fail-open).
                self.assertNotIn("reasoning_steps", parsed)

    def test_partially_malformed_list_keeps_only_valid_elements(self):
        parsed = _parse(_valid_assessment(reasoning_steps=[
            {"step": "locate", "finding": "good"},
            {"step": "", "finding": "blank step dropped"},
            "garbage",
            {"step": "decide", "finding": "kept"},
        ]))
        self.assertEqual(parsed["reasoning_steps"], [
            {"step": "locate", "finding": "good"},
            {"step": "decide", "finding": "kept"},
        ])


class ReasoningStepsWiringTests(unittest.TestCase):
    def _clause(self, result, clause_id="governing_law"):
        return next(c for c in result["clauses"] if c["id"] == clause_id)

    def test_reasoning_steps_surface_into_audit_trace_steps(self):
        result = build_ai_first_review_result(
            SOURCE_TEXT,
            [_valid_assessment(reasoning_steps=list(REASONING_STEPS))],
            verify=False,
        )
        clause = self._clause(result)
        steps = clause["audit_trace"]["steps"]
        names = [step["name"] for step in steps]
        self.assertEqual(names, [s["step"] for s in REASONING_STEPS])
        details = [step["details"] for step in steps]
        self.assertEqual(details, [s["finding"] for s in REASONING_STEPS])
        # The hardcoded plumbing steps must NOT appear when real reasoning is present.
        self.assertNotIn("AI assessment normalization", names)
        self.assertNotIn("Decision", names)

    def test_fallback_two_step_trace_when_reasoning_steps_absent(self):
        result = build_ai_first_review_result(
            SOURCE_TEXT,
            [_valid_assessment()],
            verify=False,
        )
        clause = self._clause(result)
        names = [step["name"] for step in clause["audit_trace"]["steps"]]
        self.assertEqual(names, ["AI assessment normalization", "Decision"])

    def test_wiring_reasoning_steps_do_not_change_decision(self):
        without = build_ai_first_review_result(SOURCE_TEXT, [_valid_assessment()], verify=False)
        with_steps = build_ai_first_review_result(
            SOURCE_TEXT,
            [_valid_assessment(reasoning_steps=list(REASONING_STEPS))],
            verify=False,
        )
        clause_without = self._clause(without)
        clause_with = self._clause(with_steps)
        for field in ("decision", "status", "issue_type", "blocks_send", "passes", "needs_review"):
            self.assertEqual(clause_without[field], clause_with[field], field)


if __name__ == "__main__":
    unittest.main()
