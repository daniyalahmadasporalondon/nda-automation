"""Publish-gate integration test for the playbook consistency lint.

This test owns the *gate*: it verifies that ``publish_playbook`` runs the lint and
rejects a self-contradictory playbook by surfacing the existing validation error
type/shape with the violations enumerated.

It deliberately does NOT depend on ``nda_automation.playbook_lint`` being present.
A parallel teammate builds that module; here we inject a FAKE ``lint_playbook`` by
monkeypatching ``nda_automation.playbook_authoring.lint_playbook`` (which the
integration resolves at call time). Final integrated verification against the real
lint is the integrator's job.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from nda_automation import playbook_authoring
from nda_automation.checker import load_playbook


@dataclass
class _FakeViolation:
    """Stand-in matching the agreed LintViolation surface (clause_id/check_id/message)."""

    clause_id: str
    check_id: str
    message: str


class PlaybookLintPublishGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.playbook_path = Path(self._tmpdir.name) / "playbook.json"
        # The active playbook on disk must itself be valid so the publish flow gets
        # past the active-playbook bootstrap and reaches the lint on the candidate.
        self.active_playbook = deepcopy(load_playbook())
        self.playbook_path.write_text(json.dumps(self.active_playbook), encoding="utf-8")

    def _publish(self, candidate: dict) -> dict:
        # Direct publish (supply a playbook object, no draft present).
        return playbook_authoring.publish_playbook(
            {"playbook": candidate, "actor": "legal-admin"},
            playbook_path=self.playbook_path,
        )

    def test_publish_rejected_when_lint_reports_violations(self) -> None:
        candidate = deepcopy(self.active_playbook)
        violations = [
            _FakeViolation("mutuality", "term_consistency", "preferred term exceeds the maximum allowed term"),
            _FakeViolation("governing_law", "option_coverage", "preferred_law is not among approved_laws"),
        ]

        def fake_lint(playbook):
            # Confirms the gate hands the *candidate* playbook to the lint.
            self.assertEqual(playbook, candidate)
            return violations

        with patch.object(playbook_authoring, "lint_playbook", fake_lint):
            with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
                self._publish(candidate)

        error = ctx.exception
        # Same surface as any other validate_playbook publish failure: 400 + {"error": ...}.
        self.assertEqual(error.status, 400)
        self.assertIn("error", error.payload)
        message = error.payload["error"]
        # Message enumerates every violation with clause id + message.
        self.assertIn("mutuality", message)
        self.assertIn("preferred term exceeds the maximum allowed term", message)
        self.assertIn("governing_law", message)
        self.assertIn("preferred_law is not among approved_laws", message)

        # Rejection must be a no-op: the active playbook on disk is unchanged.
        self.assertEqual(
            json.loads(self.playbook_path.read_text(encoding="utf-8")),
            self.active_playbook,
        )

    def test_publish_succeeds_when_lint_is_clean(self) -> None:
        candidate = deepcopy(self.active_playbook)

        with patch.object(playbook_authoring, "lint_playbook", lambda playbook: []):
            response = self._publish(candidate)

        self.assertEqual(response["playbook"], candidate)
        self.assertIsNone(response["draft"])
        self.assertEqual(
            json.loads(self.playbook_path.read_text(encoding="utf-8")),
            candidate,
        )

    def test_publish_unaffected_when_lint_module_absent(self) -> None:
        # When the lint module is not wired (lint_playbook is None), the gate is a
        # no-op and publishing a valid playbook still succeeds.
        candidate = deepcopy(self.active_playbook)
        with patch.object(playbook_authoring, "lint_playbook", None):
            response = self._publish(candidate)
        self.assertEqual(response["playbook"], candidate)

    def test_publish_succeeds_when_lint_raises(self) -> None:
        # A bug in the lint itself must NOT block publishing: the gate fails open
        # (logs + treats as no violations) rather than crashing the publish flow.
        candidate = deepcopy(self.active_playbook)

        def exploding_lint(playbook):
            raise RuntimeError("lint blew up")

        with patch.object(playbook_authoring, "lint_playbook", exploding_lint):
            response = self._publish(candidate)
        self.assertEqual(response["playbook"], candidate)
        self.assertEqual(
            json.loads(self.playbook_path.read_text(encoding="utf-8")),
            candidate,
        )

    def test_draft_validation_surfaces_lint_violations(self) -> None:
        # The draft-validation path reuses the same lint, so violations show up as
        # structured validation errors (early UI feedback before publish).
        candidate = deepcopy(self.active_playbook)
        violations = [_FakeViolation("mutuality", "term_consistency", "preferred term exceeds the maximum allowed term")]

        with patch.object(playbook_authoring, "lint_playbook", lambda playbook: violations):
            result = playbook_authoring.validate_playbook_draft({"playbook": candidate})

        self.assertFalse(result["valid"])
        located = [
            err
            for err in result["errors"]
            if err.get("clause") == "mutuality"
            and "preferred term exceeds the maximum allowed term" in err.get("message", "")
        ]
        self.assertTrue(located, result["errors"])


if __name__ == "__main__":
    unittest.main()
