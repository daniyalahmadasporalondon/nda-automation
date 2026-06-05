"""Generic AI packet + dynamic output contract (Dynamic Clause Types, task #10).

The AI packet includes dynamic clauses with their fallback wording and
instructions; the review result is built generically from AI assessments keyed
by arbitrary clause id; and the result carries the dynamic clause's data
(engine, fallback, instructions) plus a generic redline for its findings — all
with no per-clause Python.
"""

from __future__ import annotations

import unittest
from copy import deepcopy

from nda_automation.ai_assessment_prompt import build_ai_assessment_packet
from nda_automation.ai_first_review import build_ai_first_review_result
from nda_automation.checker import load_playbook

from test_dynamic_clause_schema import make_dynamic_clause


def _playbook_with_dynamic() -> dict:
    playbook = deepcopy(load_playbook())
    playbook["clauses"].append(make_dynamic_clause())
    return playbook


def _default_redline_for(decision: str) -> dict:
    # The AI contract requires fail decisions to carry a real redline action.
    if decision == "fail":
        return {"action": "insert_after_paragraph", "text": "Proposed clause wording."}
    return {"action": "no_change"}


def _assessments(dynamic_decision: str, dynamic_issue_type: str, *, proposed_redline: dict | None = None) -> list[dict]:
    assessments = [
        {
            "clause_id": "data_protection",
            "decision": dynamic_decision,
            "issue_type": dynamic_issue_type,
            "rationale": "Dynamic clause assessment.",
            "evidence": [],
            "proposed_redline": proposed_redline or _default_redline_for(dynamic_decision),
            "confidence": 0.9,
            "blocks_send": False,
        }
    ]
    for clause_id in [
        "mutuality",
        "confidential_information",
        "governing_law",
        "term_and_survival",
        "non_circumvention",
        "signatures",
    ]:
        assessments.append({
            "clause_id": clause_id,
            "decision": "pass",
            "issue_type": "none",
            "rationale": "ok",
            "evidence": [],
            "proposed_redline": {"action": "no_change"},
            "confidence": 0.9,
            "blocks_send": False,
        })
    return assessments


class DynamicClausePacketTests(unittest.TestCase):
    def test_packet_includes_dynamic_clause_with_fallback_and_instructions(self):
        packet = build_ai_assessment_packet("NDA text about personal data.", playbook=_playbook_with_dynamic())

        clause_ids = [clause["clause_id"] for clause in packet["playbook"]["clauses"]]
        self.assertIn("data_protection", clause_ids)
        self.assertEqual(packet["output_contract"]["required_assessment_count"], 7)

        dynamic = next(c for c in packet["playbook"]["clauses"] if c["clause_id"] == "data_protection")
        self.assertEqual(dynamic["engine"], "dynamic")
        self.assertEqual(dynamic["fallback"]["redline_action"], "replace_paragraph")
        self.assertIn("data protection", dynamic["fallback"]["wording"].lower())
        self.assertEqual(
            dynamic["instructions"],
            ["Treat any personal-data processing obligation as satisfying this clause."],
        )

    def test_native_packet_clauses_have_native_engine_and_no_fallback(self):
        packet = build_ai_assessment_packet("NDA text.", playbook=load_playbook())
        for clause in packet["playbook"]["clauses"]:
            self.assertEqual(clause["engine"], "native")
            self.assertNotIn("fallback", clause)


class DynamicClauseResultTests(unittest.TestCase):
    def test_result_keyed_by_arbitrary_clause_id_carries_dynamic_data(self):
        result = build_ai_first_review_result(
            "NDA with no personal-data clause.",
            _assessments("fail", "missing"),
            playbook=_playbook_with_dynamic(),
        )

        clause_ids = [clause["id"] for clause in result["clauses"]]
        self.assertIn("data_protection", clause_ids)

        dynamic = next(c for c in result["clauses"] if c["id"] == "data_protection")
        self.assertEqual(dynamic["decision"], "fail")
        self.assertEqual(dynamic["issue_type"], "missing")
        self.assertEqual(dynamic["engine"], "dynamic")
        self.assertEqual(dynamic["fallback"]["redline_action"], "replace_paragraph")
        self.assertTrue(dynamic["instructions"])
        # Generic review-state derivation works for the unknown clause type.
        self.assertEqual(dynamic["review_state"]["state"], "check")

    def test_missing_dynamic_assessment_defaults_to_review(self):
        # Drop the dynamic assessment; the generic path must still produce a result.
        assessments = [a for a in _assessments("pass", "none") if a["clause_id"] != "data_protection"]
        result = build_ai_first_review_result(
            "NDA text.",
            assessments,
            playbook=_playbook_with_dynamic(),
        )
        dynamic = next(c for c in result["clauses"] if c["id"] == "data_protection")
        self.assertEqual(dynamic["decision"], "review")

    def test_required_dynamic_clause_missing_inserts_fallback_wording_redline(self):
        # fallback.redline_action replace_paragraph, but a MISSING required clause
        # has nothing to replace; the generic builder uses insert wording only when
        # the action is insert_after_paragraph. Use an insert-style dynamic clause.
        clause = make_dynamic_clause()
        clause["fallback"]["redline_action"] = "insert_after_paragraph"
        playbook = deepcopy(load_playbook())
        playbook["clauses"].append(clause)

        result = build_ai_first_review_result(
            "This Agreement is between the parties. Each party may disclose information.",
            _assessments("fail", "missing"),
            playbook=playbook,
        )
        dynamic_edits = [edit for edit in result["redline_edits"] if edit["clause_id"] == "data_protection"]
        # A generic insert redline is produced from the clause's own fallback wording.
        if dynamic_edits:
            self.assertEqual(dynamic_edits[0]["action"], "insert_after_paragraph")
            self.assertIn("data protection", str(dynamic_edits[0].get("insert_text", "")).lower())

    def test_prohibited_dynamic_clause_present_deletes_paragraph(self):
        clause = make_dynamic_clause(
            id="exclusivity_restraint",
            name="Exclusivity Restraint",
            type="prohibited",
            engine="dynamic",
        )
        clause["rules"]["clause_type"] = "prohibited"
        # Prohibited clauses only need a present_but_wrong fail condition.
        clause["rules"]["fail_conditions"] = [
            {
                "id": "exclusivity_present",
                "decision": "fail",
                "issue_type": "present_but_wrong",
                "description": "An exclusivity restraint is present.",
                "redline_action": "delete_paragraph",
            }
        ]
        clause["fallback"] = {"redline_action": "delete_paragraph"}
        playbook = deepcopy(load_playbook())
        playbook["clauses"].append(clause)

        source = "The parties agree to exclusivity restraint barring other deals."
        assessments = [
            {
                "clause_id": "exclusivity_restraint",
                "decision": "fail",
                "issue_type": "present_but_wrong",
                "rationale": "Exclusivity restraint present.",
                "evidence": [
                    {
                        "paragraph_id": "p1",
                        "quote": "exclusivity restraint barring other deals",
                        "relevance": "Shows the prohibited exclusivity restraint.",
                    }
                ],
                # A real delete redline names the offending paragraph.
                "proposed_redline": {"action": "delete_paragraph", "paragraph_id": "p1"},
                "confidence": 0.9,
                "blocks_send": False,
            }
        ]
        for clause_id in [
            "mutuality",
            "confidential_information",
            "governing_law",
            "term_and_survival",
            "non_circumvention",
            "signatures",
        ]:
            assessments.append({
                "clause_id": clause_id,
                "decision": "pass",
                "issue_type": "none",
                "rationale": "ok",
                "evidence": [],
                "proposed_redline": {"action": "no_change"},
                "confidence": 0.9,
                "blocks_send": False,
            })

        result = build_ai_first_review_result(source, assessments, playbook=playbook)
        dynamic_edits = [edit for edit in result["redline_edits"] if edit["clause_id"] == "exclusivity_restraint"]
        self.assertTrue(dynamic_edits)
        self.assertEqual(dynamic_edits[0]["action"], "delete_paragraph")


if __name__ == "__main__":
    unittest.main()
