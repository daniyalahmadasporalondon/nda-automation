"""End-to-end gate tests for a USER-AUTHORED dynamic clause (Add-Clause feature).

Feature 2 ("Add Clause" UI + decision-logic editor) lets an admin author a brand
new clause in the Playbook editor. A NATIVE clause cannot be user-added -- it needs
a code-registered checker in ``checks/registry.CLAUSE_CHECKS`` and ``validate_playbook``
rejects a non-dynamic clause that has no matching check. So "Add Clause" always
creates a DYNAMIC clause (``engine="dynamic"``), reviewed generically from its data
by the AI-first engine.

These tests pin the BACKEND contract the UI relies on:

* the exact clause shape the FE ``newDynamicClauseScaffold`` produces publishes
  cleanly (validate + rules + lint all pass) through the real publish gate;
* the publish gate REJECTS the authored clause when its decision conditions are
  contradictory or a required field is missing (the lint / rules validation fire);
* a published authored clause becomes a FIRST-CLASS binding rule (its rule appears
  in the authoritative ``binding_policy`` block) and reaches the model packet.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from nda_automation import playbook_authoring
from nda_automation.checker import load_playbook
from nda_automation.playbook_authoring import PlaybookAuthoringError
from nda_automation.playbook_lint import lint_playbook
from nda_automation.playbook_policy import build_playbook_policy_block
from nda_automation.playbook_rules import playbook_rules_for_ai


def _fe_scaffold_clause(clause_id: str = "custom_clause_7") -> dict:
    """Mirror static/js/playbook-view.js::newDynamicClauseScaffold (prohibited).

    Keeping this in lock-step with the FE scaffold means a future regression that
    makes the UI emit a clause the backend rejects fails HERE, not silently in a
    browser. The author then edits the prose; the defaults must be publishable.
    """

    return {
        "id": clause_id,
        "engine": "dynamic",
        "name": "Data Localization",
        "type": "prohibited",
        "requirement": "The NDA must not require data to be stored in a specific jurisdiction.",
        "preferred_position": "No data-localization obligation is imposed.",
        "check_trigger": "A data-localization or data-residency requirement appears.",
        "acceptable_language": "No data-localization requirement is present.",
        "search_terms": ["data localization", "data residency"],
        "semantic_signals": [],
        "fallback": {"redline_action": "delete_paragraph"},
        "rules": {
            "version": 1,
            "clause_type": "prohibited",
            "acceptable_position": "No data-localization requirement is present.",
            "pass_conditions": [
                {
                    "id": "clause_absent",
                    "decision": "pass",
                    "issue_type": "none",
                    "description": "No data-localization requirement appears.",
                    "redline_action": "no_change",
                }
            ],
            "fail_conditions": [
                {
                    "id": "clause_present",
                    "decision": "fail",
                    "issue_type": "present_but_wrong",
                    "description": "A data-localization requirement appears in operative form.",
                    "redline_action": "delete_paragraph",
                }
            ],
            "review_triggers": [
                {
                    "id": "clause_ambiguous",
                    "decision": "review",
                    "issue_type": "unclear",
                    "description": "Data-residency language is ambiguous enough for human review.",
                    "redline_action": "no_change",
                }
            ],
            "evidence_requirements": {
                "quote_required": True,
                "minimum_evidence_for_pass": 0,
                "minimum_evidence_for_fail": 1,
                "guidance": "Cite the exact data-localization requirement.",
            },
            "redline_guidance": {
                "default_action": "delete_paragraph",
                "drafting_note": "Remove the data-localization requirement.",
            },
        },
    }


class AddClausePublishGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.playbook_path = Path(self._tmpdir.name) / "playbook.json"
        self.active_playbook = deepcopy(load_playbook())
        self.playbook_path.write_text(json.dumps(self.active_playbook), encoding="utf-8")

    def _publish(self, candidate: dict) -> dict:
        return playbook_authoring.publish_playbook(
            {"playbook": candidate, "actor": "legal-admin"},
            playbook_path=self.playbook_path,
        )

    def _candidate_with(self, clause: dict) -> dict:
        candidate = deepcopy(self.active_playbook)
        candidate["clauses"].append(clause)
        return candidate

    # --- happy path: the FE scaffold publishes through the real gate -------------

    def test_fe_scaffold_is_lint_clean(self) -> None:
        candidate = self._candidate_with(_fe_scaffold_clause())
        self.assertEqual(lint_playbook(candidate), [])

    def test_fe_scaffold_publishes(self) -> None:
        candidate = self._candidate_with(_fe_scaffold_clause())
        result = self._publish(candidate)
        ids = {c["id"] for c in result["playbook"]["clauses"]}
        self.assertIn("custom_clause_7", ids)

    def test_published_authored_clause_is_first_class_binding_rule(self) -> None:
        candidate = self._candidate_with(_fe_scaffold_clause())
        block = build_playbook_policy_block(candidate)
        self.assertIn("ADDITIONAL AUTHORED CLAUSE RULES", block)
        self.assertIn("Data Localization", block)
        self.assertIn("custom_clause_7", block)
        self.assertIn("[PROHIBITED]", block)

    def test_published_authored_clause_reaches_model_packet(self) -> None:
        candidate = self._candidate_with(_fe_scaffold_clause())
        packet = playbook_rules_for_ai(candidate)
        ids = {c["clause_id"] for c in packet["clauses"]}
        self.assertIn("custom_clause_7", ids)

    # --- the gate REJECTS malformed authored input -------------------------------

    def test_publish_rejected_when_decision_space_is_contradictory(self) -> None:
        # A clause that can only-ever-pass (no fail conditions and no review
        # triggers) is a dead rule set the lint must reject.
        clause = _fe_scaffold_clause()
        clause["rules"]["fail_conditions"] = []
        clause["rules"]["review_triggers"] = []
        with self.assertRaises(PlaybookAuthoringError) as ctx:
            self._publish(self._candidate_with(clause))
        self.assertEqual(ctx.exception.status, 400)

    def test_publish_rejected_when_condition_decision_mismatches_its_list(self) -> None:
        # A "fail" decision sitting in pass_conditions is internally contradictory.
        clause = _fe_scaffold_clause()
        clause["rules"]["pass_conditions"][0]["decision"] = "fail"
        with self.assertRaises(PlaybookAuthoringError) as ctx:
            self._publish(self._candidate_with(clause))
        self.assertEqual(ctx.exception.status, 400)

    def test_publish_rejected_when_required_field_missing(self) -> None:
        # An authored clause with no requirement prose fails rules validation.
        clause = _fe_scaffold_clause()
        clause["requirement"] = ""
        with self.assertRaises(PlaybookAuthoringError) as ctx:
            self._publish(self._candidate_with(clause))
        self.assertEqual(ctx.exception.status, 400)

    def test_publish_rejected_when_clause_id_shadows_native_check(self) -> None:
        # A user cannot add a dynamic clause that re-uses a native (checker-backed)
        # id -- validate_playbook flags the dynamic clause shadowing a native check.
        clause = _fe_scaffold_clause(clause_id="mutuality")
        clause["name"] = "Fake Mutuality"
        with self.assertRaises(PlaybookAuthoringError) as ctx:
            self._publish(self._candidate_with(clause))
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
