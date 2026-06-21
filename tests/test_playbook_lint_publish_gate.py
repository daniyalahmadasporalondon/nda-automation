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

    def test_publish_rejected_when_lint_machinery_raises(self) -> None:
        # #38: when the lint MACHINERY itself blows up (not a clause violation, but
        # the lint engine erroring), the gate must FAIL CLOSED -- reject the publish
        # and surface the failure -- rather than silently treating it as a no-op and
        # letting an unvalidated playbook go live.
        candidate = deepcopy(self.active_playbook)

        def exploding_lint(playbook):
            raise RuntimeError("lint blew up")

        with patch.object(playbook_authoring, "lint_playbook", exploding_lint):
            with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
                self._publish(candidate)

        error = ctx.exception
        self.assertEqual(error.status, 400)
        self.assertIn("error", error.payload)
        self.assertIn("lint could not run", error.payload["error"])
        # Rejection is a no-op: the active playbook on disk is unchanged.
        self.assertEqual(
            json.loads(self.playbook_path.read_text(encoding="utf-8")),
            self.active_playbook,
        )

    def _candidate_with_option_id_collision(self) -> dict:
        """A real, otherwise-clean playbook with two colliding approved-option names.

        "Ontario, Canada" and "Ontario Canada" both derive the option id
        "ontario_canada", so the REAL consistency lint (not a fake) flags exactly one
        collision and nothing else. We attach the colliding set to a non-governing
        clause to avoid the governing_law-specific rules-schema validators (which are
        orthogonal to this lint) and isolate the collision.
        """
        candidate = deepcopy(self.active_playbook)
        for clause in candidate.get("clauses", []):
            if clause.get("id") == "mutuality":
                clause["rules"]["approved_options"] = [
                    {"id": "opt_a", "label": "Ontario, Canada", "value": "Ontario, Canada"},
                    {"id": "opt_b", "label": "Ontario Canada", "value": "Ontario Canada"},
                ]
                break
        else:  # pragma: no cover - defensive: the live playbook has mutuality
            self.fail("mutuality clause not found in the live playbook")
        return candidate

    def test_publish_rejected_on_real_option_id_collision(self) -> None:
        # End-to-end: a genuine option-id collision is caught by the REAL lint and
        # rejected through the same 400 / {"error"} publish gate as any other failure.
        from nda_automation import playbook_lint

        candidate = self._candidate_with_option_id_collision()
        # Sanity: the real lint reports exactly the collision and nothing else.
        violations = playbook_lint.lint_playbook(candidate)
        self.assertEqual(len(violations), 1, [v.message for v in violations])
        self.assertIn("derive the same option id", violations[0].message)

        with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
            self._publish(candidate)

        error = ctx.exception
        self.assertEqual(error.status, 400)
        self.assertIn("error", error.payload)
        message = error.payload["error"]
        self.assertIn("mutuality", message)
        self.assertIn("derive the same option id", message)
        self.assertIn("'Ontario, Canada'", message)
        self.assertIn("'Ontario Canada'", message)

        # Rejection is a no-op: the active playbook on disk is unchanged.
        self.assertEqual(
            json.loads(self.playbook_path.read_text(encoding="utf-8")),
            self.active_playbook,
        )

    def _candidate_with_explicit_id_collision(self) -> dict:
        """A real, otherwise-clean playbook with two options sharing one explicit id.

        Two options carry id 'opt_dup' but map to different values, so the
        generation join (resolved[id] = value) would silently drop one. Attached to
        the mutuality clause (same isolation rationale as the name-collision helper).
        """
        candidate = deepcopy(self.active_playbook)
        for clause in candidate.get("clauses", []):
            if clause.get("id") == "mutuality":
                clause["rules"]["approved_options"] = [
                    {"id": "opt_dup", "label": "India", "value": "India"},
                    {"id": "opt_dup", "label": "Delaware", "value": "Delaware"},
                ]
                break
        else:  # pragma: no cover - defensive: the live playbook has mutuality
            self.fail("mutuality clause not found in the live playbook")
        return candidate

    def test_publish_rejected_on_real_explicit_id_collision(self) -> None:
        # GAP 1 end-to-end: two options sharing the explicit join-key id map to
        # different values, caught by the REAL lint and rejected through the gate.
        from nda_automation import playbook_lint

        candidate = self._candidate_with_explicit_id_collision()
        violations = playbook_lint.lint_playbook(candidate)
        self.assertEqual(len(violations), 1, [v.message for v in violations])
        self.assertIn("share the explicit id", violations[0].message)

        with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
            self._publish(candidate)

        error = ctx.exception
        self.assertEqual(error.status, 400)
        self.assertIn("error", error.payload)
        message = error.payload["error"]
        self.assertIn("mutuality", message)
        self.assertIn("share the explicit id", message)
        self.assertIn("'opt_dup'", message)
        self.assertIn("'India'", message)
        self.assertIn("'Delaware'", message)

        # Rejection is a no-op: the active playbook on disk is unchanged.
        self.assertEqual(
            json.loads(self.playbook_path.read_text(encoding="utf-8")),
            self.active_playbook,
        )

    def test_publish_blocked_when_a_single_check_raises(self) -> None:
        # #38 per-check isolation: if ONE check throws, the gate must FAIL CLOSED
        # (block publish + surface the failing check) -- not silently no-op the
        # whole lint and let an unchecked playbook through. The other checks still
        # run, so a real violation in the same playbook is reported alongside.
        from nda_automation import playbook_lint

        candidate = self._candidate_with_option_id_collision()

        def exploding_check(_clause):
            raise RuntimeError("collision check blew up")

        patched_checks = dict(playbook_lint.CHECKS)
        patched_checks["option_id_collision"] = exploding_check
        with patch.object(playbook_lint, "CHECKS", patched_checks):
            with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
                self._publish(candidate)

        error = ctx.exception
        self.assertEqual(error.status, 400)
        self.assertIn("raised and could not validate", error.payload["error"])
        # Rejection is a no-op: the active playbook on disk is unchanged.
        self.assertEqual(
            json.loads(self.playbook_path.read_text(encoding="utf-8")),
            self.active_playbook,
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


    # ------------------------------------------------------------------
    # Content-hardening end-to-end: each fix rejects through the REAL lint
    # at the same 400 / {"error"} publish gate.
    # ------------------------------------------------------------------

    # NOTE: the per-law "non-court forum" publish rejection was REMOVED in the
    # per-entity governing-law/court restructure. Courts are authored per signing
    # entity now (not per playbook law), so the publish gate no longer screens a
    # per-option forum_jurisdiction; entity-court shape/reconciliation + the
    # generation gate cover correctness instead.

    def test_publish_rejected_on_contradictory_conditions(self) -> None:
        # PROOF (P2): the same state described as both pass and fail is rejected.
        candidate = deepcopy(self.active_playbook)
        mut = next(c for c in candidate["clauses"] if c["id"] == "mutuality")
        shared = mut["rules"]["pass_conditions"][0]["description"]
        mut["rules"]["fail_conditions"].append(
            {
                "id": "contradiction_probe",
                "decision": "fail",
                "issue_type": "present_but_wrong",
                "description": shared,
                "redline_action": "replace_paragraph",
            }
        )
        with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
            self._publish(candidate)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("contradict themselves", ctx.exception.payload["error"])

    def test_publish_rejected_on_zero_width_only_search_term(self) -> None:
        # PROOF (P2): an invisible-unicode-only search term cannot publish.
        candidate = deepcopy(self.active_playbook)
        mut = next(c for c in candidate["clauses"] if c["id"] == "mutuality")
        mut["search_terms"] = ["​‌‍﻿"]
        with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
            self._publish(candidate)
        self.assertEqual(ctx.exception.status, 400)

    def test_publish_rejected_on_unknown_condition_key(self) -> None:
        # PROOF (P4): an invented condition key (reason_code) is rejected.
        candidate = deepcopy(self.active_playbook)
        mut = next(c for c in candidate["clauses"] if c["id"] == "mutuality")
        mut["rules"]["fail_conditions"][0]["reason_code"] = "sneaky"
        with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
            self._publish(candidate)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("unsupported field", ctx.exception.payload["error"])

    def test_publish_rejected_on_oversized_field(self) -> None:
        # PROOF (P4): an oversized authored requirement is rejected.
        candidate = deepcopy(self.active_playbook)
        mut = next(c for c in candidate["clauses"] if c["id"] == "mutuality")
        mut["requirement"] = "A" * 5000
        with self.assertRaises(playbook_authoring.PlaybookAuthoringError) as ctx:
            self._publish(candidate)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("too long", ctx.exception.payload["error"])

    def test_alias_collision_is_advisory_not_blocking(self) -> None:
        # The law alias-collision lint is a WARNING: it must NOT block a publish (it
        # surfaces in draft validation), so a clean-but-colliding playbook publishes.
        candidate = deepcopy(self.active_playbook)
        gl = next(c for c in candidate["clauses"] if c["id"] == "governing_law")
        opts = gl["rules"]["approved_options"]
        opts[0].setdefault("aliases", []).append("shared_alias")
        opts[1].setdefault("aliases", []).append("shared_alias")
        # Publish succeeds (warning does not block)...
        response = self._publish(candidate)
        self.assertEqual(response["playbook"], candidate)
        # ...but the warning surfaces in draft validation.
        result = playbook_authoring.validate_playbook_draft({"playbook": candidate})
        self.assertTrue(
            any(
                err.get("message", "").find("share the recognition alias") != -1
                for err in result["errors"]
            ),
            result["errors"],
        )


if __name__ == "__main__":
    unittest.main()
