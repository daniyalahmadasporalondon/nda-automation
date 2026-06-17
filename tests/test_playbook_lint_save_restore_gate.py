"""Save/restore-gate integration tests for the playbook consistency lint.

The publish path already enforces the Layer-1 consistency lint hard-gate
(see ``tests/test_playbook_lint_publish_gate.py``). These tests own the
parallel gate on the OTHER two write paths that reach the live rules:

* ``save_active_playbook``  (POST /api/playbook)
* ``restore_playbook_history_entry``  (POST /api/playbook/restore)

A validate-passing but lint-FAILING playbook must be rejected on save AND on
restore with the SAME error surface publish uses (``PlaybookAuthoringError``,
status 400, ``{"error": ...}`` enumerating violations), the on-disk active
playbook must be UNCHANGED on rejection (no-op), and a lint bug must fail open
(never wedge save/restore).

Like the publish-gate test, this does NOT depend on ``nda_automation.playbook_lint``
being importable: it injects a FAKE ``lint_playbook`` by monkeypatching
``nda_automation.playbook_authoring.lint_playbook`` (resolved at call time).
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


_VIOLATIONS = [
    _FakeViolation("mutuality", "term_consistency", "preferred term exceeds the maximum allowed term"),
    _FakeViolation("governing_law", "option_coverage", "preferred_law is not among approved_laws"),
]


class _LintGateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.playbook_path = Path(self._tmpdir.name) / "playbook.json"
        # The active playbook on disk must itself be valid AND lint-clean so the
        # write flow gets past the active-playbook bootstrap.
        self.active_playbook = deepcopy(load_playbook())
        self.playbook_path.write_text(json.dumps(self.active_playbook), encoding="utf-8")

    def _on_disk(self) -> dict:
        return json.loads(self.playbook_path.read_text(encoding="utf-8"))


class SaveActivePlaybookLintGateTests(_LintGateTestBase):
    def _save(self, candidate: dict) -> dict:
        return playbook_authoring.save_active_playbook(
            {"playbook": candidate, "actor": "legal-admin"},
            playbook_path=self.playbook_path,
        )

    def test_save_rejected_when_lint_reports_violations(self) -> None:
        candidate = deepcopy(self.active_playbook)

        def fake_lint(playbook):
            # Confirms the gate hands the *candidate* playbook to the lint.
            self.assertEqual(playbook, candidate)
            return _VIOLATIONS

        with patch.object(playbook_authoring, "lint_playbook", fake_lint):
            with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
                self._save(candidate)

        error = ctx.exception
        # Same surface as a publish lint failure: 400 + {"error": ...} enumerating violations.
        self.assertEqual(error.status, 400)
        self.assertIn("error", error.payload)
        message = error.payload["error"]
        self.assertIn("mutuality", message)
        self.assertIn("preferred term exceeds the maximum allowed term", message)
        self.assertIn("governing_law", message)
        self.assertIn("preferred_law is not among approved_laws", message)

        # Rejection must be a no-op: the active playbook on disk is unchanged.
        self.assertEqual(self._on_disk(), self.active_playbook)

    def test_save_succeeds_when_lint_is_clean(self) -> None:
        candidate = deepcopy(self.active_playbook)
        mutuality = next(clause for clause in candidate["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Clean save with the lint gate present."

        with patch.object(playbook_authoring, "lint_playbook", lambda playbook: []):
            response = self._save(candidate)

        self.assertEqual(response["playbook"], candidate)
        self.assertEqual(self._on_disk(), candidate)

    def test_save_blocked_when_lint_machinery_raises(self) -> None:
        # #38: when the lint MACHINERY itself errors, the save gate must FAIL CLOSED
        # (reject + surface the failure) rather than silently no-op the lint and
        # persist an unvalidated playbook.
        candidate = deepcopy(self.active_playbook)

        def exploding_lint(playbook):
            raise RuntimeError("lint blew up")

        with patch.object(playbook_authoring, "lint_playbook", exploding_lint):
            with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
                self._save(candidate)

        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("lint could not run", ctx.exception.payload["error"])
        # No-op: the active playbook on disk is unchanged.
        self.assertEqual(self._on_disk(), self.active_playbook)


class RestorePlaybookHistoryEntryLintGateTests(_LintGateTestBase):
    def _seed_history_entry(self, snapshot: dict) -> str:
        """Write ``snapshot`` into the live rules via a clean save and return its history id.

        Saving inserts a ``save`` history entry whose ``snapshot`` is the saved
        playbook, giving us a restorable entry without hand-building runtime state.
        """
        with patch.object(playbook_authoring, "lint_playbook", lambda playbook: []):
            response = playbook_authoring.save_active_playbook(
                {"playbook": snapshot, "actor": "legal-admin"},
                playbook_path=self.playbook_path,
            )
        history = response["history"]
        self.assertTrue(history, "save should record a history entry to restore")
        return str(history[0]["id"])

    def _restore(self, history_id: str) -> dict:
        return playbook_authoring.restore_playbook_history_entry(
            {"history_id": history_id, "actor": "legal-admin"},
            playbook_path=self.playbook_path,
        )

    def test_restore_rejected_when_lint_reports_violations(self) -> None:
        # Seed a (then-clean) snapshot into history, then move the live rules
        # forward so restore is a real change, and make the lint fail on restore.
        snapshot = deepcopy(self.active_playbook)
        mutuality = next(clause for clause in snapshot["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Historical snapshot to restore."
        history_id = self._seed_history_entry(snapshot)

        live_now = self._on_disk()  # what restore must NOT change on rejection

        with patch.object(playbook_authoring, "lint_playbook", lambda playbook: _VIOLATIONS):
            with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
                self._restore(history_id)

        error = ctx.exception
        self.assertEqual(error.status, 400)
        self.assertIn("error", error.payload)
        message = error.payload["error"]
        self.assertIn("mutuality", message)
        self.assertIn("preferred term exceeds the maximum allowed term", message)
        self.assertIn("governing_law", message)
        self.assertIn("preferred_law is not among approved_laws", message)

        # Rejection must be a no-op: the active playbook on disk is unchanged.
        self.assertEqual(self._on_disk(), live_now)

    def test_restore_succeeds_when_lint_is_clean(self) -> None:
        snapshot = deepcopy(self.active_playbook)
        mutuality = next(clause for clause in snapshot["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Historical snapshot to restore cleanly."
        history_id = self._seed_history_entry(snapshot)

        # Move the live rules to something else so restore is an observable change.
        other = deepcopy(self.active_playbook)
        other_mutuality = next(clause for clause in other["clauses"] if clause["id"] == "mutuality")
        other_mutuality["preferred_position"] = "Different live state before restore."
        with patch.object(playbook_authoring, "lint_playbook", lambda playbook: []):
            playbook_authoring.save_active_playbook(
                {"playbook": other, "actor": "legal-admin"},
                playbook_path=self.playbook_path,
            )

        with patch.object(playbook_authoring, "lint_playbook", lambda playbook: []):
            response = self._restore(history_id)

        self.assertEqual(response["playbook"], snapshot)
        self.assertEqual(self._on_disk(), snapshot)

    def test_restore_blocked_when_lint_machinery_raises(self) -> None:
        # #38 fail-closed: a lint-machinery error must BLOCK restore (not silently
        # no-op the gate), with the same error surface as publish/save.
        snapshot = deepcopy(self.active_playbook)
        mutuality = next(clause for clause in snapshot["clauses"] if clause["id"] == "mutuality")
        mutuality["preferred_position"] = "Historical snapshot, lint will explode on restore."
        history_id = self._seed_history_entry(snapshot)
        before = self._on_disk()

        def exploding_lint(playbook):
            raise RuntimeError("lint blew up")

        with patch.object(playbook_authoring, "lint_playbook", exploding_lint):
            with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
                self._restore(history_id)

        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("lint could not run", ctx.exception.payload["error"])
        # No-op: the live playbook is unchanged by the rejected restore.
        self.assertEqual(self._on_disk(), before)


if __name__ == "__main__":
    unittest.main()
