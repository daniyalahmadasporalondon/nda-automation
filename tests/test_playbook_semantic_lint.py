"""Tests for the Layer-2 AI semantic playbook lint.

Every test uses a STUB linter injected across the seam -- NO network, NO API key.
The stub returns canned violation dicts (or raises) so the contract is exercised
deterministically. The flag is forced on/off per test.
"""
from __future__ import annotations

import unittest
from unittest import mock

from nda_automation import playbook_authoring, playbook_semantic_lint
from nda_automation.checker import load_playbook
from nda_automation.playbook_semantic_lint import (
    CHECK_IDS,
    SEMANTIC_LINT_ENABLED_ENV,
    SemanticLintViolation,
    semantic_lint_enabled,
    semantic_lint_playbook,
)


def _playbook(*clauses: dict) -> dict:
    return {"name": "test", "version": "1", "clauses": list(clauses)}


def _clause(clause_id: str = "mutuality", **extra) -> dict:
    base = {
        "id": clause_id,
        "name": clause_id.title(),
        "type": "required",
        "requirement": "The agreement must be mutual.",
        "preferred_position": "Mutual obligations.",
        "check_trigger": "Always.",
        "redline_template": "The parties agree mutually.",
        "rules": {
            "pass_conditions": [{"id": "p1", "decision": "pass", "issue_type": "none", "description": "mutual"}],
            "fail_conditions": [{"id": "f1", "decision": "fail", "issue_type": "missing", "description": "one-way"}],
        },
    }
    base.update(extra)
    return base


class _StubLinter:
    """Injectable, key-free linter.

    Returns the canned ``violations`` for every clause whose id is in ``flag_ids``
    (defaulting to all clauses) and ``[]`` otherwise. Records the packets it saw so
    the packet-shape contract can be asserted.
    """

    def __init__(self, violations, flag_ids=None):
        self._violations = violations
        self._flag_ids = set(flag_ids) if flag_ids is not None else None
        self.packets = []

    def __call__(self, packet):
        self.packets.append(packet)
        if self._flag_ids is not None and packet.get("clause_id") not in self._flag_ids:
            return []
        return list(self._violations)


class SemanticLintEnabledFlagTests(unittest.TestCase):
    def test_default_off(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(SEMANTIC_LINT_ENABLED_ENV, None)
            self.assertFalse(semantic_lint_enabled())

    def test_truthy_values_enable(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on"):
            with mock.patch.dict("os.environ", {SEMANTIC_LINT_ENABLED_ENV: value}):
                self.assertTrue(semantic_lint_enabled(), value)

    def test_falsy_values_stay_off(self) -> None:
        for value in ("0", "false", "no", "off", ""):
            with mock.patch.dict("os.environ", {SEMANTIC_LINT_ENABLED_ENV: value}):
                self.assertFalse(semantic_lint_enabled(), value)


class SemanticLintPlaybookTests(unittest.TestCase):
    def test_stub_violation_is_surfaced(self) -> None:
        stub = _StubLinter([
            {"check_id": "prose_mandate_unenforced", "message": "rules do not enforce the prose mandate", "confidence": 0.9},
        ])
        violations = semantic_lint_playbook(_playbook(_clause("mutuality")), linter=stub)
        self.assertEqual(len(violations), 1)
        violation = violations[0]
        self.assertIsInstance(violation, SemanticLintViolation)
        self.assertEqual(violation.clause_id, "mutuality")
        self.assertEqual(violation.check_id, "prose_mandate_unenforced")
        self.assertEqual(violation.severity, "warning")
        self.assertAlmostEqual(violation.confidence, 0.9)
        self.assertIn("prose mandate", violation.message)

    def test_clean_playbook_has_no_violations(self) -> None:
        stub = _StubLinter([])  # model says every clause is clean
        violations = semantic_lint_playbook(_playbook(_clause("a"), _clause("b")), linter=stub)
        self.assertEqual(violations, [])
        # Still called once per clause.
        self.assertEqual(len(stub.packets), 2)

    def test_per_clause_packet_shape(self) -> None:
        stub = _StubLinter([])
        clause = _clause(
            "confidentiality",
            allowed_exclusions=["public information"],
            rules={
                "pass_conditions": [{"id": "p1", "decision": "pass", "issue_type": "none", "description": "ok"}],
                "fail_conditions": [{"id": "f1", "decision": "fail", "issue_type": "missing", "description": "bad"}],
                "acceptable_position": "Acceptable fallback.",
                "approved_options": ["opt-a", "opt-b"],
            },
        )
        semantic_lint_playbook(_playbook(clause), linter=stub)
        packet = stub.packets[0]
        self.assertEqual(packet["clause_id"], "confidentiality")
        self.assertEqual(packet["requirement"], "The agreement must be mutual.")
        self.assertEqual(packet["preferred_position"], "Mutual obligations.")
        self.assertEqual(packet["acceptable_position"], "Acceptable fallback.")
        self.assertEqual(packet["check_trigger"], "Always.")
        self.assertEqual(packet["redline_template"], "The parties agree mutually.")
        self.assertEqual(packet["approved_options"], ["opt-a", "opt-b"])
        self.assertEqual(packet["allowed_exclusions"], ["public information"])
        self.assertEqual(len(packet["rules"]["pass_conditions"]), 1)
        self.assertEqual(packet["rules"]["fail_conditions"][0]["description"], "bad")

    def test_fail_open_when_linter_raises(self) -> None:
        class _Exploding:
            def __call__(self, packet):
                raise RuntimeError("model exploded")

        violations = semantic_lint_playbook(_playbook(_clause("a")), linter=_Exploding())
        self.assertEqual(violations, [])

    def test_one_clause_error_does_not_sink_others(self) -> None:
        class _SometimesExplodes:
            def __call__(self, packet):
                if packet["clause_id"] == "boom":
                    raise RuntimeError("boom")
                return [{"check_id": "threshold_contradiction", "message": "3 vs 5 years", "confidence": 0.8}]

        violations = semantic_lint_playbook(
            _playbook(_clause("boom"), _clause("ok")), linter=_SometimesExplodes()
        )
        self.assertEqual([v.clause_id for v in violations], ["ok"])

    def test_unknown_check_id_falls_back(self) -> None:
        stub = _StubLinter([{"check_id": "made_up", "message": "weird", "confidence": 0.9}])
        violations = semantic_lint_playbook(_playbook(_clause("a")), linter=stub)
        self.assertEqual(violations[0].check_id, "semantic_inconsistency")

    def test_low_confidence_dropped(self) -> None:
        stub = _StubLinter([
            {"check_id": "threshold_contradiction", "message": "weak signal", "confidence": 0.1},
            {"check_id": "threshold_contradiction", "message": "strong signal", "confidence": 0.9},
        ])
        violations = semantic_lint_playbook(_playbook(_clause("a")), linter=stub)
        self.assertEqual([v.message for v in violations], ["strong signal"])

    def test_missing_message_skipped(self) -> None:
        stub = _StubLinter([
            {"check_id": "threshold_contradiction", "confidence": 0.9},  # no message
            {"check_id": "threshold_contradiction", "message": "real", "confidence": 0.9},
        ])
        violations = semantic_lint_playbook(_playbook(_clause("a")), linter=stub)
        self.assertEqual([v.message for v in violations], ["real"])

    def test_lenient_parse_of_fenced_string_output(self) -> None:
        # A linter that returns the raw model string (preamble + ```json fence).
        def fenced_linter(packet):
            return (
                "Here is the analysis:\n```json\n"
                '[{"check_id": "redline_contradicts_requirement", '
                '"message": "template fights the requirement", "confidence": 0.88}]\n```'
            )

        violations = semantic_lint_playbook(_playbook(_clause("a")), linter=fenced_linter)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].check_id, "redline_contradicts_requirement")
        self.assertAlmostEqual(violations[0].confidence, 0.88)

    def test_object_envelope_string_output(self) -> None:
        def envelope_linter(packet):
            return '{"violations": [{"check_id": "threshold_contradiction", "message": "x", "confidence": 0.7}]}'

        violations = semantic_lint_playbook(_playbook(_clause("a")), linter=envelope_linter)
        self.assertEqual(len(violations), 1)

    def test_none_output_is_clean(self) -> None:
        violations = semantic_lint_playbook(_playbook(_clause("a")), linter=lambda packet: None)
        self.assertEqual(violations, [])

    def test_no_linter_and_no_key_fails_open(self) -> None:
        # No injected linter; force the default resolver to find no API key.
        with mock.patch.object(playbook_semantic_lint, "_configured_api_key", return_value=""):
            violations = semantic_lint_playbook(_playbook(_clause("a")))
        self.assertEqual(violations, [])

    def test_non_mapping_playbook_is_clean(self) -> None:
        self.assertEqual(semantic_lint_playbook("nope", linter=_StubLinter([])), [])
        self.assertEqual(semantic_lint_playbook({"clauses": "nope"}, linter=_StubLinter([])), [])

    def test_real_playbook_does_not_crash_with_stub(self) -> None:
        # The canonical "does not crash on the live playbook" smoke test.
        stub = _StubLinter([])
        violations = semantic_lint_playbook(load_playbook(), linter=stub)
        self.assertEqual(violations, [])
        # One packet per clause; every packet carries the required keys.
        self.assertTrue(stub.packets)
        for packet in stub.packets:
            for key in ("clause_id", "requirement", "rules", "redline_template"):
                self.assertIn(key, packet)

    def test_check_ids_match_prompt(self) -> None:
        # The registry and the system prompt must enumerate the same check ids.
        for check_id in CHECK_IDS:
            self.assertIn(check_id, playbook_semantic_lint.SYSTEM_PROMPT)


class SemanticLintAdvisoryIntegrationTests(unittest.TestCase):
    """Layer-2 violations surface as draft-validation WARNINGS, never errors/blocks."""

    def _validate(self, playbook):
        return playbook_authoring.validate_playbook_draft({"playbook": playbook})

    def test_warnings_surface_when_enabled(self) -> None:
        candidate = load_playbook()
        fake_violations = [
            SemanticLintViolation(
                clause_id="mutuality",
                check_id="prose_mandate_unenforced",
                message="the requirement mandates X but no rule enforces X",
                confidence=0.91,
            )
        ]
        with mock.patch.object(playbook_authoring, "semantic_lint_enabled", lambda: True), \
                mock.patch.object(playbook_authoring, "semantic_lint_playbook", lambda pb: fake_violations):
            result = self._validate(candidate)

        # Valid is driven ONLY by errors; the advisory warning never flips it.
        self.assertTrue(result["valid"], result.get("errors"))
        warnings = result["warnings"]
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning["severity"], "warning")
        self.assertEqual(warning["clause"], "mutuality")
        self.assertEqual(warning["check_id"], "prose_mandate_unenforced")
        self.assertAlmostEqual(warning["confidence"], 0.91)
        self.assertIn("mandates X", warning["message"])
        # The warning is NOT in the blocking errors list.
        self.assertNotIn(
            "the requirement mandates X but no rule enforces X",
            [err.get("message") for err in result["errors"]],
        )

    def test_flag_off_is_no_op(self) -> None:
        candidate = load_playbook()
        sentinel = mock.Mock(return_value=[
            SemanticLintViolation("mutuality", "threshold_contradiction", "should never run"),
        ])
        with mock.patch.object(playbook_authoring, "semantic_lint_enabled", lambda: False), \
                mock.patch.object(playbook_authoring, "semantic_lint_playbook", sentinel):
            result = self._validate(candidate)
        # Flag off -> the linter is never invoked and warnings is empty.
        sentinel.assert_not_called()
        self.assertEqual(result["warnings"], [])

    def test_fail_open_when_lint_raises(self) -> None:
        candidate = load_playbook()

        def exploding(pb):
            raise RuntimeError("lint blew up")

        with mock.patch.object(playbook_authoring, "semantic_lint_enabled", lambda: True), \
                mock.patch.object(playbook_authoring, "semantic_lint_playbook", exploding):
            result = self._validate(candidate)
        self.assertEqual(result["warnings"], [])
        self.assertTrue(result["valid"])

    def test_module_absent_is_no_op(self) -> None:
        candidate = load_playbook()
        with mock.patch.object(playbook_authoring, "semantic_lint_playbook", None), \
                mock.patch.object(playbook_authoring, "semantic_lint_enabled", None):
            result = self._validate(candidate)
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
