"""End-to-end AI-review proof for a USER-AUTHORED dynamic clause.

Companion to test_playbook_add_clause.py (which proves the publish gate). Here we
prove the OTHER half of the Add-Clause contract: once an admin authors a dynamic
clause and it is in the active playbook, the AI-first review engine actually
ASSESSES it on a real document, surfaces a per-clause verdict, AND the clause's
authored rule rides in the model-facing ``binding_policy`` block.

We cross the real pipeline (``assess_nda_with_ai``) using the in-repo deterministic
stub reviewer -- the same key-free seam the engine ships for AI-first integration
tests -- so no live API key or secret is needed. The stub mirrors a real reviewer:
it fails any dynamic prohibited clause when triggering language is present and
passes otherwise.
"""
from __future__ import annotations

import unittest
from copy import deepcopy

from nda_automation.ai_assessor import (
    InMemoryAssessmentReviewer,
    assess_nda_with_ai,
    stub_ai_assessment_response,
)
from nda_automation.checker import load_playbook


def _authored_clause() -> dict:
    """A user-authored dynamic clause (mirrors the FE Add-Clause scaffold).

    Its search terms / language match a 'deal exclusively' restraint so the stub
    reviewer (which mirrors a real reviewer) recognises and fails it.
    """

    return {
        "id": "exclusive_dealing",
        "engine": "dynamic",
        "name": "Exclusive Dealing",
        "type": "prohibited",
        "requirement": "The NDA must not require either party to deal exclusively with the other.",
        "preferred_position": "No exclusive-dealing obligation is imposed.",
        "check_trigger": "An exclusive-dealing or sole-source obligation appears.",
        "acceptable_language": "No exclusive-dealing restriction is present.",
        "search_terms": ["exclusive dealing", "deal exclusively"],
        "semantic_signals": [],
        "fallback": {"redline_action": "delete_paragraph"},
        "rules": {
            "version": 1,
            "clause_type": "prohibited",
            "acceptable_position": "No exclusive-dealing restriction is present.",
            "pass_conditions": [
                {
                    "id": "absent",
                    "decision": "pass",
                    "issue_type": "none",
                    "description": "No exclusive-dealing obligation appears.",
                    "redline_action": "no_change",
                }
            ],
            "fail_conditions": [
                {
                    "id": "present",
                    "decision": "fail",
                    "issue_type": "present_but_wrong",
                    "description": "An exclusive-dealing obligation appears in operative form.",
                    "redline_action": "delete_paragraph",
                }
            ],
            "review_triggers": [
                {
                    "id": "ambiguous",
                    "decision": "review",
                    "issue_type": "unclear",
                    "description": "Exclusivity language is ambiguous enough for human review.",
                    "redline_action": "no_change",
                }
            ],
            "evidence_requirements": {
                "quote_required": True,
                "minimum_evidence_for_pass": 0,
                "minimum_evidence_for_fail": 1,
                "guidance": "Cite the exact exclusive-dealing obligation.",
            },
            "redline_guidance": {
                "default_action": "delete_paragraph",
                "drafting_note": "Remove the exclusive-dealing obligation.",
            },
        },
    }


# A short NDA whose body contains a prohibited exclusive-dealing restraint the
# authored clause exists to catch, plus benign confidentiality boilerplate.
TRIGGERING_DOCUMENT = """MUTUAL NON-DISCLOSURE AGREEMENT

This Agreement is entered into between Aspora and the Counterparty.

1. Confidential Information. Each party may disclose confidential information to the other for the Purpose.

2. Exclusivity. During the term, the Counterparty shall deal exclusively with Aspora and shall not engage any competing provider.

3. Governing Law. This Agreement is governed by the laws of England and Wales.
"""


class AddClauseAiReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.playbook = deepcopy(load_playbook())
        self.playbook["clauses"].append(_authored_clause())

    def test_authored_clause_is_assessed_and_fails_on_a_triggering_document(self) -> None:
        reviewer = InMemoryAssessmentReviewer(response=stub_ai_assessment_response)
        result = assess_nda_with_ai(
            TRIGGERING_DOCUMENT,
            reviewer=reviewer,
            playbook=self.playbook,
            checked_at="2026-06-20T00:00:00+00:00",
        )

        # (1) The AI engine produced a verdict for the AUTHORED clause.
        clause = next(
            (c for c in result["clauses"] if c["id"] == "exclusive_dealing"),
            None,
        )
        self.assertIsNotNone(clause, "authored clause was never assessed")
        self.assertEqual(clause["decision"], "fail")
        self.assertEqual(clause["decision_source"], "ai")

        # (2) The reviewer was actually called with a packet that carried BOTH the
        # authored clause in the model-facing clause list AND its rule in the
        # authoritative binding_policy block.
        self.assertEqual(len(reviewer.packets), 1)
        packet = reviewer.packets[0]
        packet_clause_ids = {
            str(c.get("clause_id") or "")
            for c in packet["playbook"]["clauses"]
        }
        self.assertIn("exclusive_dealing", packet_clause_ids)
        binding_policy = str(packet["playbook"].get("binding_policy") or "")
        self.assertIn("ADDITIONAL AUTHORED CLAUSE RULES", binding_policy)
        self.assertIn("Exclusive Dealing", binding_policy)
        self.assertIn("exclusive_dealing", binding_policy)

    def test_authored_clause_passes_when_its_trigger_is_absent(self) -> None:
        clean_document = """MUTUAL NON-DISCLOSURE AGREEMENT

1. Confidential Information. Each party may disclose confidential information for the Purpose.

2. Governing Law. This Agreement is governed by the laws of England and Wales.
"""
        reviewer = InMemoryAssessmentReviewer(response=stub_ai_assessment_response)
        result = assess_nda_with_ai(
            clean_document,
            reviewer=reviewer,
            playbook=self.playbook,
            checked_at="2026-06-20T00:00:00+00:00",
        )
        clause = next(
            (c for c in result["clauses"] if c["id"] == "exclusive_dealing"),
            None,
        )
        self.assertIsNotNone(clause)
        self.assertEqual(clause["decision"], "pass")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
