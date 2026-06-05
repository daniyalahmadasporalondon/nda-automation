"""Clause-as-data schema (Dynamic Clause Types, task #9).

A clause TYPE can be defined entirely as data with engine="dynamic": detection
cues, pass/review/fail criteria, approved/fallback positions + redline wording,
and clause-specific instructions. validate-draft validates such definitions, and
the six native clauses keep working unchanged.
"""

from __future__ import annotations

import unittest
from copy import deepcopy

from nda_automation.checker import PlaybookTemplateError, load_playbook, validate_playbook
from nda_automation.playbook_rules import clause_engine, is_dynamic_clause
from nda_automation.routes.playbook import collect_playbook_validation_errors


def make_dynamic_clause(**overrides) -> dict:
    """A valid, fully data-defined dynamic clause the code has never seen."""
    clause = {
        "id": "data_protection",
        "engine": "dynamic",
        "name": "Data Protection",
        "type": "required",
        "requirement": "The NDA must require compliance with applicable data protection law for personal data.",
        "preferred_position": "The receiving party processes personal data only as needed and per applicable data protection law.",
        "check_trigger": "The NDA covers personal data but omits a data-protection compliance obligation.",
        "search_terms": ["personal data", "data protection", "GDPR", "data processing"],
        "semantic_signals": ["process personal data", "comply with data protection law"],
        "rules": {
            "version": 1,
            "clause_type": "required",
            "acceptable_position": "The NDA requires data-protection-law compliance for any personal data.",
            "pass_conditions": [
                {
                    "id": "dp_present",
                    "decision": "pass",
                    "issue_type": "none",
                    "description": "A data-protection compliance obligation is present.",
                    "redline_action": "no_change",
                }
            ],
            "fail_conditions": [
                {
                    "id": "dp_missing",
                    "decision": "fail",
                    "issue_type": "missing",
                    "description": "No data-protection compliance obligation is present.",
                    "redline_action": "insert_after_paragraph",
                },
                {
                    "id": "dp_wrong",
                    "decision": "fail",
                    "issue_type": "present_but_wrong",
                    "description": "The data-protection obligation is present but inadequate.",
                    "redline_action": "replace_paragraph",
                },
            ],
            "review_triggers": [
                {
                    "id": "dp_unclear",
                    "decision": "review",
                    "issue_type": "unclear",
                    "description": "The data-protection obligation is ambiguous.",
                }
            ],
            "evidence_requirements": {
                "quote_required": True,
                "minimum_evidence_for_pass": 1,
                "minimum_evidence_for_fail": 0,
                "guidance": "Cite the data-protection obligation.",
            },
            "redline_guidance": {"default_action": "replace_paragraph", "template_field": "fallback"},
        },
        "fallback": {
            "redline_action": "replace_paragraph",
            "wording": "Each party shall comply with applicable data protection laws in processing any personal data disclosed under this Agreement.",
            "approved_positions": ["Compliance with applicable data protection law for personal data."],
        },
        "instructions": ["Treat any personal-data processing obligation as satisfying this clause."],
    }
    clause.update(overrides)
    return clause


def playbook_with_dynamic_clause(clause: dict | None = None) -> dict:
    playbook = deepcopy(load_playbook())
    playbook["clauses"].append(clause if clause is not None else make_dynamic_clause())
    return playbook


class DynamicClauseSchemaTests(unittest.TestCase):
    def test_clause_engine_defaults_to_native(self):
        native = next(clause for clause in load_playbook()["clauses"])
        self.assertEqual(clause_engine(native), "native")
        self.assertFalse(is_dynamic_clause(native))
        self.assertTrue(is_dynamic_clause(make_dynamic_clause()))

    def test_valid_dynamic_clause_passes_validation(self):
        playbook = playbook_with_dynamic_clause()

        self.assertEqual(collect_playbook_validation_errors(playbook), [])
        # Accepted alongside the six native clauses.
        validate_playbook(playbook)
        self.assertEqual(len(playbook["clauses"]), 7)

    def test_native_clauses_still_validate_unchanged(self):
        # The shipped six-clause playbook is unaffected by the schema extension.
        self.assertEqual(collect_playbook_validation_errors(load_playbook()), [])
        validate_playbook(load_playbook())

    def test_dynamic_clause_requires_fallback_wording(self):
        clause = make_dynamic_clause()
        del clause["fallback"]
        errors = collect_playbook_validation_errors(playbook_with_dynamic_clause(clause))

        self.assertTrue(any(error["field"] == "fallback" or "fallback" in error["message"] for error in errors), errors)
        fallback_error = next(error for error in errors if "fallback" in error["message"])
        self.assertEqual(fallback_error["clause"], "data_protection")

    def test_dynamic_clause_rejects_unsupported_fallback_redline_action(self):
        clause = make_dynamic_clause()
        clause["fallback"]["redline_action"] = "frobnicate"
        errors = collect_playbook_validation_errors(playbook_with_dynamic_clause(clause))

        self.assertTrue(any("fallback.redline_action" in error["message"] for error in errors), errors)

    def test_dynamic_clause_cannot_reuse_a_native_clause_id(self):
        clause = make_dynamic_clause(id="governing_law")
        errors = collect_playbook_validation_errors(playbook_with_dynamic_clause(clause))

        self.assertTrue(
            any("native clause id" in error["message"] or "shadow a native check" in error["message"] for error in errors),
            errors,
        )

    def test_unknown_native_clause_is_rejected_with_dynamic_hint(self):
        # A clause with no engine marker defaults to native; an unknown native id
        # has no Python check, so it's rejected with a hint to mark it dynamic.
        clause = make_dynamic_clause(id="freedom_of_information")
        del clause["engine"]
        with self.assertRaises(PlaybookTemplateError) as raised:
            validate_playbook(playbook_with_dynamic_clause(clause))
        self.assertIn("freedom_of_information", str(raised.exception))
        self.assertIn("dynamic", str(raised.exception))

    def test_dynamic_clause_rejects_unknown_engine_value(self):
        clause = make_dynamic_clause(engine="magic")
        errors = collect_playbook_validation_errors(playbook_with_dynamic_clause(clause))

        self.assertTrue(any("engine must be one of" in error["message"] for error in errors), errors)

    def test_dynamic_clause_instructions_accept_text_or_list(self):
        as_text = make_dynamic_clause(instructions="Single instruction line.")
        self.assertEqual(collect_playbook_validation_errors(playbook_with_dynamic_clause(as_text)), [])

        bad = make_dynamic_clause(instructions=[""])
        errors = collect_playbook_validation_errors(playbook_with_dynamic_clause(bad))
        self.assertTrue(any("instructions" in error["message"] for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
